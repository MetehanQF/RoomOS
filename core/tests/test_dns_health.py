import asyncio
from datetime import UTC, datetime

import dns.exception
import dns.flags
import dns.message
import dns.rcode
import dns.rrset
import pytest

from app.config import Settings
from app.dns_health import (
    DnsHealthMonitor,
    ResolverTarget,
    derive_cluster_status,
    functional_dns_probe,
    unknown_dns_cluster,
)
from app.models import DnsResolverStatus


def resolver(resolver_id: str, status: str) -> DnsResolverStatus:
    return DnsResolverStatus(
        id=resolver_id,
        role=resolver_id,
        status=status,
        last_checked=datetime.now(UTC),
    )


@pytest.mark.parametrize(
    ("primary", "secondary", "expected"),
    [
        ("healthy", "healthy", "healthy"),
        ("healthy", "offline", "degraded"),
        ("offline", "healthy", "degraded"),
        ("healthy", "unknown", "degraded"),
        ("offline", "offline", "offline"),
        ("degraded", "offline", "offline"),
        ("degraded", "degraded", "offline"),
        ("unknown", "offline", "unknown"),
        ("unknown", "degraded", "unknown"),
        ("unknown", "unknown", "unknown"),
    ],
)
def test_cluster_status_is_deterministic(primary, secondary, expected):
    assert derive_cluster_status(
        resolver("primary", primary), resolver("secondary", secondary)
    ) == expected


def test_startup_state_is_unknown():
    snapshot = unknown_dns_cluster()
    assert snapshot.status == "unknown"
    assert snapshot.primary.status == "unknown"
    assert snapshot.secondary.status == "unknown"
    assert snapshot.last_checked is None


@pytest.mark.asyncio
async def test_functional_probe_requires_expected_a_answer(monkeypatch):
    async def response_for(query, *_args, **_kwargs):
        response = dns.message.make_response(query)
        response.answer.append(dns.rrset.from_text("example.com.", 60, "IN", "A", "93.184.216.34"))
        return response

    monkeypatch.setattr("dns.asyncquery.udp", response_for)
    result = await functional_dns_probe(
        ResolverTarget("primary", "primary", "192.0.2.1"),
        query_name="example.com.",
        timeout=1,
    )
    assert result.status == "healthy"
    assert result.response_time_ms is not None


@pytest.mark.asyncio
async def test_valid_negative_response_is_degraded_not_offline(monkeypatch):
    async def negative(query, *_args, **_kwargs):
        response = dns.message.make_response(query)
        response.set_rcode(dns.rcode.NXDOMAIN)
        return response

    monkeypatch.setattr("dns.asyncquery.udp", negative)
    result = await functional_dns_probe(
        ResolverTarget("secondary", "secondary", "192.0.2.2"),
        query_name="example.com.",
        timeout=1,
    )
    assert result.status == "degraded"
    assert result.response_time_ms is not None


@pytest.mark.asyncio
async def test_timeout_is_offline(monkeypatch):
    async def timeout(*_args, **_kwargs):
        raise dns.exception.Timeout

    monkeypatch.setattr("dns.asyncquery.udp", timeout)
    result = await functional_dns_probe(
        ResolverTarget("primary", "primary", "192.0.2.1"),
        query_name="example.com.",
        timeout=1,
    )
    assert result.status == "offline"
    assert result.response_time_ms is None


@pytest.mark.asyncio
async def test_malformed_response_is_degraded(monkeypatch):
    async def malformed(*_args, **_kwargs):
        raise dns.exception.FormError

    monkeypatch.setattr("dns.asyncquery.udp", malformed)
    result = await functional_dns_probe(
        ResolverTarget("primary", "primary", "192.0.2.1"),
        query_name="example.com.",
        timeout=1,
    )
    assert result.status == "degraded"


@pytest.mark.asyncio
async def test_monitor_publishes_one_logical_event_without_addresses(tmp_path):
    events = []

    async def publish(event):
        events.append(event)

    async def probe(target):
        return resolver(target.id, "healthy")

    monitor = DnsHealthMonitor(Settings(ROOMOS_DNS_MONITOR_ENABLED=False), publish, probe)
    snapshot = await monitor.probe_now()
    assert snapshot.status == "healthy"
    assert len(events) == 1
    payload = events[0].model_dump(mode="json")
    assert payload["type"] == "system.service_updated"
    assert payload["data"] == {"service": "dns", "status": "healthy"}
    assert "192.168" not in str(payload)


@pytest.mark.asyncio
async def test_overlapping_probe_is_suppressed():
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def publish(_event):
        return None

    async def probe(target):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return resolver(target.id, "healthy")

    monitor = DnsHealthMonitor(Settings(), publish, probe)
    first = asyncio.create_task(monitor.probe_now())
    await entered.wait()
    second = await monitor.probe_now()
    assert second.status == "unknown"
    release.set()
    await first
    assert calls == 2


@pytest.mark.asyncio
async def test_probe_failure_is_isolated_per_resolver():
    async def publish(_event):
        return None

    async def probe(target):
        if target.id == "primary":
            raise RuntimeError("fixture failure")
        return resolver("secondary", "healthy")

    monitor = DnsHealthMonitor(Settings(), publish, probe)
    snapshot = await monitor.probe_now()
    assert snapshot.primary.status == "unknown"
    assert snapshot.secondary.status == "healthy"
    assert snapshot.status == "degraded"


@pytest.mark.asyncio
async def test_scheduler_transitions_from_unknown_and_stops_cleanly():
    events = []

    async def publish(event):
        events.append(event)

    async def probe(target):
        return resolver(target.id, "healthy")

    settings = Settings(
        ROOMOS_DNS_PROBE_STARTUP_DELAY_SECONDS=0,
        ROOMOS_DNS_PROBE_INTERVAL_SECONDS=30,
    )
    monitor = DnsHealthMonitor(settings, publish, probe)
    assert monitor.snapshot().status == "unknown"
    task = asyncio.create_task(monitor.run())
    for _ in range(20):
        if monitor.snapshot().status == "healthy":
            break
        await asyncio.sleep(0)
    assert monitor.snapshot().status == "healthy"
    monitor.stop()
    await asyncio.wait_for(task, timeout=1)
    assert len(events) == 1


@pytest.mark.asyncio
async def test_unexpected_cycle_error_does_not_end_the_monitor(monkeypatch, caplog):
    publishes = 0

    async def publish(_event):
        nonlocal publishes
        publishes += 1
        if publishes == 1:
            raise RuntimeError("publisher detail 192.0.2.20")

    probes = 0

    async def probe(target):
        nonlocal probes
        probes += 1
        if probes == 4:
            monitor.stop()
        return resolver(target.id, "healthy")

    monitor = DnsHealthMonitor(Settings(ROOMOS_DNS_PROBE_STARTUP_DELAY_SECONDS=0), publish, probe)
    real_wait_for = asyncio.wait_for

    async def fast_wait_for(awaitable, timeout):
        return await real_wait_for(awaitable, timeout=min(timeout, 0.01))

    monkeypatch.setattr(asyncio, "wait_for", fast_wait_for)
    with caplog.at_level("WARNING"):
        async with asyncio.timeout(2):
            await monitor.run()
    assert probes >= 4  # both resolvers were probed again in the cycle after the failure
    assert "RuntimeError" in caplog.text and "192.168" not in caplog.text


@pytest.mark.asyncio
async def test_7i_one_lost_udp_reply_is_retried_not_offline(monkeypatch):
    import app.dns_health as dh
    from app.models import DnsResolverStatus
    calls = []
    async def fake(target, *, query_name, timeout):
        calls.append(target.id)
        status = "offline" if len(calls) == 1 else "healthy"
        return DnsResolverStatus(id=target.id, role=target.role, status=status, response_time_ms=None if status == "offline" else 3.2)
    monkeypatch.setattr(dh, "_single_dns_probe", fake)
    target = dh.ResolverTarget("secondary", "secondary", "192.0.2.22")
    result = await dh.functional_dns_probe(target, query_name="example.com.", timeout=2)
    assert (result.status, result.attempts, len(calls)) == ("healthy", 2, 2)


@pytest.mark.asyncio
async def test_7i_two_consecutive_losses_are_offline(monkeypatch):
    import app.dns_health as dh
    from app.models import DnsResolverStatus
    async def fake(target, *, query_name, timeout):
        return DnsResolverStatus(id=target.id, role=target.role, status="offline")
    monkeypatch.setattr(dh, "_single_dns_probe", fake)
    result = await dh.functional_dns_probe(dh.ResolverTarget("primary", "primary", "x"), query_name="e.", timeout=2)
    assert (result.status, result.attempts) == ("offline", 2)


@pytest.mark.asyncio
async def test_7i_transport_stats_are_bounded_and_exposed():
    from app.dns_health import DnsHealthMonitor
    from app.models import DnsResolverStatus
    from app.config import Settings
    sequence = [(1, 3.0)] * 58 + [(2, 400.0)] + [(1, 5.0)] * 10
    state = {"i": 0}
    async def probe(target):
        attempts, latency = sequence[min(state["i"], len(sequence) - 1)]
        from datetime import UTC, datetime
        return DnsResolverStatus(id=target.id, role=target.role, status="healthy", attempts=attempts,
                                 response_time_ms=latency, last_checked=datetime.now(UTC))
    async def publish(_event): pass
    monitor = DnsHealthMonitor(Settings(), publish, probe=probe)
    for i in range(len(sequence)):
        state["i"] = i
        snap = await monitor.probe_now()
    assert len(monitor._history["secondary"]) == 60
    assert snap.secondary.lost_reply_rate_1h == round(1 / 60, 4)
    assert snap.secondary.p95_ms_1h is not None and snap.status == "healthy"
