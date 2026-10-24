"""7D.1-S2: GET /api/v1/system must not feed an unbounded device.updated feedback loop.

Realistic synthetic HA states for Primary/Secondary; HA last_updated advances on every read like a
polled REST sensor even when values do not change. No network, no real HA.
"""
from datetime import UTC, datetime, timedelta

import pytest

from app.catalog import SECONDARY_ENTITIES, PRIMARY_ENTITIES
from app.main import app

VALUES = {
    "cpu": ("23.5", "%"), "ram": ("61.2", "%"), "disk": ("36.9", "%"),
    "temperature": ("48.5", "°C"), "uptime": ("6d 6h", None),
}


class SystemHA:
    def __init__(self, base):
        self.base = base
        self.reads = 0
        self.overrides: dict[str, str] = {}
        self.now = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)

    def _state(self, entity_id):
        node, _, suffix = entity_id.split(".", 1)[1].partition("_")
        if entity_id in self.overrides:
            raw = self.overrides[entity_id]
        elif entity_id.startswith("binary_sensor."):
            raw = "on" if suffix in {"online", "docker", "tailscale", "cloudflared", "smart", "portainer"} else "off"
        elif suffix in VALUES:
            raw = VALUES[suffix][0]
        else:
            raw = "ok"
        unit = VALUES.get(suffix, (None, None))[1]
        return {"entity_id": entity_id, "state": raw, "last_updated": self.now.isoformat(),
                "attributes": {"unit_of_measurement": unit} if unit else {}}

    async def get_states(self, entity_ids):
        if any(e in PRIMARY_ENTITIES for e in entity_ids):
            self.reads += 1
            self.now += timedelta(seconds=1)  # polled sensor: timestamp moves, value does not
        if entity_ids and all(e in PRIMARY_ENTITIES + SECONDARY_ENTITIES for e in entity_ids):
            return {e: self._state(e) for e in entity_ids}
        return await self.base.get_states(entity_ids)

    def __getattr__(self, name):
        return getattr(self.base, name)


@pytest.fixture
def system_client(client, fake_ha):
    from app.main import get_ha
    ha = SystemHA(fake_ha)
    app.dependency_overrides[get_ha] = lambda: ha
    published = []
    broker = app.state.broker
    original = broker.broadcast

    async def spy(event, *, required_scope=None):
        published.append((event.type, required_scope, event.data))
        await original(event, required_scope=required_scope)

    broker.broadcast = spy
    try:
        yield client, ha, published
    finally:
        broker.broadcast = original


def device_events(published, device_id=None):
    return [d["device"] for t, _, d in published if t == "device.updated" and (device_id is None or d["device"]["id"] == device_id)]


def test_identical_snapshots_do_not_feed_a_device_event_loop(system_client):
    client, ha, published = system_client
    assert client.get("/api/v1/system").status_code == 200
    first = len(device_events(published))
    assert first <= 7  # one initial view per catalog device
    for _ in range(10):  # the System page refetching after each event
        assert client.get("/api/v1/system").status_code == 200
    assert ha.reads == 11
    assert len(device_events(published)) == first, "identical data with only newer timestamps must not republish"


def test_real_value_and_connectivity_changes_are_published_once(system_client):
    client, ha, published = system_client
    client.get("/api/v1/system")
    before = len(device_events(published, "primary"))
    ha.overrides["sensor.primary_cpu"] = "71.0"
    client.get("/api/v1/system"); client.get("/api/v1/system")
    changed = device_events(published, "primary")[before:]
    assert len(changed) == 1
    assert next(s for s in changed[0]["sensors"] if s["id"] == "cpu_usage")["value"] == 71.0
    ha.overrides["binary_sensor.secondary_online"] = "off"
    client.get("/api/v1/system"); client.get("/api/v1/system")
    secondary = device_events(published, "secondary")
    assert secondary[-1]["connection"] == "offline" and sum(d["connection"] == "offline" for d in secondary) == 1


def test_rest_reads_still_show_current_receipt_times(system_client):
    client, ha, _ = system_client
    client.get("/api/v1/system")
    first = client.get("/api/v1/devices/primary").json()["last_seen_at"]
    client.get("/api/v1/system")
    second = client.get("/api/v1/devices/primary").json()["last_seen_at"]
    assert second >= first  # REST stays authoritative for timestamps; events carry meaningful changes


def test_ha_unavailable_publishes_nothing_and_recovery_publishes_once(system_client):
    from app.ha_client import HomeAssistantUnavailable
    client, ha, published = system_client
    client.get("/api/v1/system")
    baseline = len(device_events(published))
    original = ha.get_states

    async def down(entity_ids):
        raise HomeAssistantUnavailable("synthetic outage")

    ha.get_states = down
    for _ in range(5):
        assert client.get("/api/v1/system").status_code == 503
    assert len(device_events(published)) == baseline  # no data, no events, no server-side retry
    ha.get_states = original
    ha.overrides["sensor.primary_ram"] = "70.0"
    assert client.get("/api/v1/system").status_code == 200
    ram = next(s for s in device_events(published, "primary")[-1]["sensors"] if s["id"] == "ram_usage")
    assert ram["value"] == 70.0 and len(device_events(published)) == baseline + 1


@pytest.mark.asyncio
async def test_heartbeat_storm_and_out_of_order_reports():
    from datetime import UTC, datetime, timedelta
    from app.devices import DeviceRegistry, DeviceSpec, Reading, SensorSpec, SourceSpec
    spec = DeviceSpec("phone", "Phone", "android", "Android", "devices:read", (SourceSpec("mqtt", True),),
                      (SensorSpec("battery_percent", "number", "supported", "mqtt", "%"),
                       SensorSpec("wifi_connected", "boolean", "supported", "mqtt")))
    wall, mono, sent = [datetime(2026, 10, 14, 12, 0, tzinfo=UTC)], [0.0], []

    async def publish(event, scope):
        sent.append(event.data["device"])

    registry = DeviceRegistry({"system_monitor": 180, "home_assistant": 900, "mqtt": 300, "roomos_native": 120}, publish,
                              catalog=(spec,), bindings={("mqtt", "phone-1"): "phone"},
                              clock=lambda: wall[0], monotonic=lambda: mono[0])
    registry.ingest("mqtt", "phone-1", {"battery_percent": Reading(80, measured_at=wall[0]), "wifi_connected": Reading(True)}, connection="online")
    await registry.publish_changes()
    first = len(sent)
    for _ in range(100):  # 1 Hz heartbeat, identical values: no event storm, stays fresh
        wall[0] += timedelta(seconds=1); mono[0] += 1
        registry.ingest("mqtt", "phone-1", {"wifi_connected": Reading(True)}, connection="online")
        await registry.publish_changes()
    assert len(sent) == first
    detail = registry.detail("phone", frozenset({"devices:read"}))
    assert next(s for s in detail.sensors if s.id == "wifi_connected").freshness == "fresh"
    newer = wall[0]
    registry.ingest("mqtt", "phone-1", {"battery_percent": Reading(75, measured_at=newer)}, connection="online")
    registry.ingest("mqtt", "phone-1", {"battery_percent": Reading(90, measured_at=newer - timedelta(minutes=2))}, connection="online")
    assert registry.rejected["out_of_order"] == 1
    await registry.publish_changes()
    assert len(sent) == first + 1 and next(s for s in sent[-1]["sensors"] if s["id"] == "battery_percent")["value"] == 75
    # The battery measurement ages to stale at 300 s even while Wi-Fi heartbeats keep arriving.
    for _ in range(310):
        wall[0] += timedelta(seconds=1); mono[0] += 1
        registry.ingest("mqtt", "phone-1", {"wifi_connected": Reading(True)}, connection="online")
        await registry.publish_changes()
    battery = next(s for s in sent[-1]["sensors"] if s["id"] == "battery_percent")
    wifi = next(s for s in sent[-1]["sensors"] if s["id"] == "wifi_connected")
    assert (battery["freshness"], wifi["freshness"]) == ("stale", "fresh") and len(sent) == first + 2
