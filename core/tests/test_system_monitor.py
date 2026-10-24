import asyncio

import pytest

from app.alerts import AlertEngine
from app.dns_health import DnsHealthMonitor
from app.models import RoomOSEvent
from app.system_monitor import SystemMonitor


class RecordingAlerts(AlertEngine):
    def __init__(self, path):
        super().__init__(path)
        self.system_evaluations = 0
        self.connection = []

    async def evaluate(self, system):
        self.system_evaluations += 1
        return await super().evaluate(system)

    async def evaluate_ha_connection(self, connected):
        self.connection.append(connected)
        return await super().evaluate_ha_connection(connected)


@pytest.mark.asyncio
async def test_monitor_reuses_canonical_system_model_and_publishes_logical_event(
    fake_ha, tmp_path
):
    events: list[RoomOSEvent] = []

    async def publish(event):
        events.append(event)

    from app.config import Settings

    settings = Settings(ROOMOS_DNS_MONITOR_ENABLED=False)
    dns = DnsHealthMonitor(settings, publish)
    alerts = RecordingAlerts(str(tmp_path / "alerts.json"))
    monitor = SystemMonitor(fake_ha, dns, alerts, publish, startup_delay_seconds=0)
    snapshot = await monitor.refresh()
    assert snapshot is not None
    assert monitor.snapshot() == snapshot
    assert alerts.system_evaluations == 1
    assert alerts.connection[-1] is True
    assert events[-1].type == "system.snapshot_updated"
    assert "entity_id" not in events[-1].model_dump_json()


@pytest.mark.asyncio
async def test_monitor_coalesces_overlapping_refreshes(fake_ha, tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0
    original = fake_ha.get_states

    async def blocked(entity_ids):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return await original(entity_ids)

    fake_ha.get_states = blocked

    async def publish(_event):
        pass

    from app.config import Settings

    settings = Settings(ROOMOS_DNS_MONITOR_ENABLED=False)
    dns = DnsHealthMonitor(settings, publish)
    alerts = AlertEngine(str(tmp_path / "alerts.json"))
    monitor = SystemMonitor(fake_ha, dns, alerts, publish)
    first = asyncio.create_task(monitor.refresh())
    await started.wait()
    second = await monitor.refresh()
    assert second is None
    release.set()
    await first
    # system_overview reads Primary, Secondary and Frigate once each; the overlap did not
    # start a second three-read cycle.
    assert calls == 3
