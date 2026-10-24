import collections
import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import dns.asyncquery
import dns.exception
import dns.flags
import dns.message
import dns.name
import dns.rcode
import dns.rdataclass
import dns.rdatatype

from .config import Settings
from .models import DnsClusterStatus, DnsResolverStatus, RoomOSEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolverTarget:
    id: str
    role: str
    address: str


Probe = Callable[[ResolverTarget], Awaitable[DnsResolverStatus]]
Publish = Callable[[RoomOSEvent], Awaitable[None]]


def unknown_dns_cluster() -> DnsClusterStatus:
    return DnsClusterStatus(
        status="unknown",
        primary=DnsResolverStatus(id="primary", role="primary", status="unknown"),
        secondary=DnsResolverStatus(id="secondary", role="secondary", status="unknown"),
    )


def derive_cluster_status(
    primary: DnsResolverStatus, secondary: DnsResolverStatus
) -> str:
    statuses = (primary.status, secondary.status)
    healthy = statuses.count("healthy")
    if healthy == 2:
        return "healthy"
    if healthy == 1:
        return "degraded"
    if "unknown" in statuses:
        return "unknown"
    return "offline"


async def functional_dns_probe(
    target: ResolverTarget,
    *,
    query_name: str,
    timeout: float,
    retries: int = 1,
) -> DnsResolverStatus:
    """One lost UDP reply over Wi-Fi is not an outage (observed: the resolver answered in well under
    a millisecond and the reply never arrived). A timeout is retried once before the resolver counts as offline."""
    result = await _single_dns_probe(target, query_name=query_name, timeout=timeout)
    attempts = 1
    while result.status == "offline" and attempts <= retries:
        attempts += 1
        result = await _single_dns_probe(target, query_name=query_name, timeout=timeout)
    return result.model_copy(update={"attempts": attempts})


async def _single_dns_probe(
    target: ResolverTarget,
    *,
    query_name: str,
    timeout: float,
) -> DnsResolverStatus:
    checked_at = datetime.now(UTC)
    started = time.monotonic()
    query = dns.message.make_query(query_name, dns.rdatatype.A, dns.rdataclass.IN)
    try:
        response = await dns.asyncquery.udp(
            query,
            target.address,
            timeout=timeout,
            ignore_unexpected=False,
        )
        latency = round((time.monotonic() - started) * 1000, 1)
        expected_name = dns.name.from_text(query_name)
        has_expected_answer = any(
            rrset.name == expected_name
            and rrset.rdclass == dns.rdataclass.IN
            and rrset.rdtype == dns.rdatatype.A
            and len(rrset) > 0
            for rrset in response.answer
        )
        status = (
            "healthy"
            if response.rcode() == dns.rcode.NOERROR
            and bool(response.flags & dns.flags.QR)
            and has_expected_answer
            else "degraded"
        )
        return DnsResolverStatus(
            id=target.id,
            role=target.role,
            status=status,
            last_checked=checked_at,
            response_time_ms=latency,
        )
    except (dns.exception.Timeout, TimeoutError, OSError):
        return DnsResolverStatus(
            id=target.id,
            role=target.role,
            status="offline",
            last_checked=checked_at,
        )
    except dns.exception.DNSException:
        latency = round((time.monotonic() - started) * 1000, 1)
        return DnsResolverStatus(
            id=target.id,
            role=target.role,
            status="degraded",
            last_checked=checked_at,
            response_time_ms=latency,
        )


class DnsHealthMonitor:
    def __init__(
        self,
        settings: Settings,
        publish: Publish,
        probe: Probe | None = None,
    ) -> None:
        self.settings = settings
        self.publish = publish
        self._snapshot = unknown_dns_cluster()
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        if probe is None:
            async def configured_probe(target: ResolverTarget) -> DnsResolverStatus:
                return await functional_dns_probe(
                    target,
                    query_name=settings.dns_probe_name,
                    timeout=settings.dns_probe_timeout_seconds,
                )
            self._probe = configured_probe
        else:
            self._probe = probe
        self.targets = (
            ResolverTarget("primary", "primary", settings.dns_primary_address),
            ResolverTarget("secondary", "secondary", settings.dns_secondary_address),
        )
        # Bounded in-memory transport history (~1 h at the 60 s cadence); never persisted.
        self._history: dict[str, collections.deque] = {
            target.id: collections.deque(maxlen=60) for target in self.targets
        }

    def _with_transport_stats(self, status: DnsResolverStatus) -> DnsResolverStatus:
        history = self._history[status.id]
        if status.status != "unknown":
            history.append((status.attempts or 1, status.response_time_ms))
        if not history:
            return status
        lost = sum(1 for attempts, _latency in history if attempts > 1)
        latencies = sorted(latency for _attempts, latency in history if latency is not None)
        p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else None
        return status.model_copy(update={"lost_reply_rate_1h": round(lost / len(history), 4), "p95_ms_1h": p95})

    def snapshot(self) -> DnsClusterStatus:
        return self._snapshot.model_copy(deep=True)

    async def _safe_probe(self, target: ResolverTarget) -> DnsResolverStatus:
        try:
            return await self._probe(target)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "DNS resolver probe failed unexpectedly resolver=%s error=%s",
                target.id,
                type(exc).__name__,
            )
            return DnsResolverStatus(
                id=target.id,
                role=target.role,
                status="unknown",
                last_checked=datetime.now(UTC),
            )

    async def probe_now(self) -> DnsClusterStatus:
        if self._lock.locked():
            return self.snapshot()
        async with self._lock:
            primary, secondary = await asyncio.gather(
                *(self._safe_probe(target) for target in self.targets)
            )
            primary, secondary = self._with_transport_stats(primary), self._with_transport_stats(secondary)
            checked = max(
                value for value in (primary.last_checked, secondary.last_checked) if value is not None
            )
            self._snapshot = DnsClusterStatus(
                status=derive_cluster_status(primary, secondary),
                primary=primary,
                secondary=secondary,
                last_checked=checked,
            )
            logger.info(
                "DNS cluster probe completed cluster=%s primary=%s/%s secondary=%s/%s",
                self._snapshot.status,
                primary.status,
                primary.attempts,
                secondary.status,
                secondary.attempts,
            )
            await self.publish(
                RoomOSEvent(
                    type="system.service_updated",
                    device_id="dns",
                    data={
                        "service": "dns",
                        "status": self._snapshot.status,
                    },
                )
            )
            return self.snapshot()

    async def run(self) -> None:
        logger.info("DNS cluster monitor started state=unknown")
        try:
            await asyncio.wait_for(
                self._stop.wait(), timeout=self.settings.dns_probe_startup_delay_seconds
            )
            return
        except TimeoutError:
            pass
        while not self._stop.is_set():
            try:
                await self.probe_now()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # One failed cycle must not end DNS monitoring for the process lifetime.
                logger.warning("DNS health cycle failed error=%s", type(exc).__name__)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.settings.dns_probe_interval_seconds
                )
            except TimeoutError:
                continue

    def stop(self) -> None:
        self._stop.set()
