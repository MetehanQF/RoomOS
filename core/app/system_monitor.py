import asyncio
import logging
from collections.abc import Awaitable, Callable

from .alerts import AlertEngine
from .dns_health import DnsHealthMonitor
from .ha_client import HomeAssistantClient, HomeAssistantUnavailable
from .models import RoomOSEvent, SystemResponse
from .production_notifications import ProductionNotificationPolicy
from .service import system_overview

logger = logging.getLogger(__name__)
Publish = Callable[[RoomOSEvent], Awaitable[None]]


class SystemMonitor:
    """Single canonical system snapshot producer for API, alerts and WS clients."""

    def __init__(
        self,
        client: HomeAssistantClient,
        dns: DnsHealthMonitor,
        alerts: AlertEngine,
        publish: Publish,
        notification_policy: ProductionNotificationPolicy | None = None,
        *,
        interval_seconds: float = 60.0,
        startup_delay_seconds: float = 1.0,
    ) -> None:
        self.client = client
        self.dns = dns
        self.alerts = alerts
        self.publish = publish
        self.notification_policy = notification_policy
        self.interval_seconds = interval_seconds
        self.startup_delay_seconds = startup_delay_seconds
        self._snapshot: SystemResponse | None = None
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        # Read-only consumers of every accepted snapshot (changed or not), e.g. the Device
        # Registry. A failing listener never affects alerts, notifications or publishing.
        self.snapshot_listeners: list[Callable[[SystemResponse], Awaitable[None]]] = []

    def snapshot(self) -> SystemResponse | None:
        return self._snapshot.model_copy(deep=True) if self._snapshot is not None else None

    def notify_source_changed(self) -> None:
        self._wake.set()

    async def _accept_locked(self, snapshot: SystemResponse) -> None:
        changed = (
            self._snapshot is None
            or self._snapshot.model_dump(mode="json")
            != snapshot.model_dump(mode="json")
        )
        self._snapshot = snapshot.model_copy(deep=True)
        await self.alerts.evaluate_ha_connection(True)
        await self.alerts.evaluate(snapshot)
        if self.notification_policy is not None:
            await self.notification_policy.evaluate(snapshot)
        if changed:
            await self.publish(
                RoomOSEvent(
                    type="system.snapshot_updated",
                    data={"system": snapshot.model_dump(mode="json")},
                )
            )
        for listener in self.snapshot_listeners:
            try:
                await listener(snapshot)
            except Exception as exc:
                logger.warning("System snapshot listener failed error=%s", type(exc).__name__)

    async def accept(self, snapshot: SystemResponse) -> None:
        async with self._lock:
            await self._accept_locked(snapshot)

    async def refresh(self) -> SystemResponse | None:
        if self._lock.locked():
            return self.snapshot()
        async with self._lock:
            try:
                snapshot = await system_overview(self.client, self.dns.snapshot())
            except HomeAssistantUnavailable:
                await self.alerts.evaluate_ha_connection(False)
                return self.snapshot()
            await self._accept_locked(snapshot)
            return self.snapshot()

    async def run(self) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=self.startup_delay_seconds)
            return
        except TimeoutError:
            pass
        while not self._stop.is_set():
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Canonical system refresh failed error=%s", type(exc).__name__)
            self._wake.clear()
            try:
                wake_task = asyncio.create_task(self._wake.wait())
                stop_task = asyncio.create_task(self._stop.wait())
                done, pending = await asyncio.wait(
                    (wake_task, stop_task),
                    timeout=self.interval_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                if stop_task in done and stop_task.result():
                    return
                if wake_task in done:
                    await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                raise

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
