import json
from datetime import UTC, datetime, timedelta

import pytest

from app.devices import (
    BINDINGS, CATALOG, DeviceRegistry, DeviceSpec, Reading, SensorSpec, SourceSpec, validate_catalog,
)
from app.models import DnsClusterStatus, DnsResolverStatus, SystemMetric, SystemNode, SystemResponse

T = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
THRESHOLDS = {"system_monitor": 180, "home_assistant": 900, "mqtt": 300, "roomos_native": 120}
SYSTEM = frozenset({"system:read"})
PERSONAL = frozenset({"devices:read"})
ALL = SYSTEM | PERSONAL


class Clock:
    def __init__(self):
        self.wall, self.mono = T, 1000.0

    def advance(self, seconds, *, wall_only=False):
        self.wall += timedelta(seconds=seconds)
        if not wall_only:
            self.mono += seconds


def registry(catalog=CATALOG, bindings=BINDINGS, publish=None):
    clock = Clock()
    return DeviceRegistry(THRESHOLDS, publish, catalog=catalog, bindings=bindings,
                          clock=lambda: clock.wall, monotonic=lambda: clock.mono), clock


def sensor(detail, sensor_id):
    return next(s for s in detail.sensors if s.id == sensor_id)


def primary_readings(**overrides):
    values = {"cpu_usage": Reading(31.5, "%"), "ram_usage": Reading(40, "%"), "disk_usage": Reading(30, "%"),
              "cpu_temperature": Reading(52.1, "°C"), "uptime": Reading("3 days"), "docker_running": Reading(True)}
    return values | overrides


PHONE = DeviceSpec("phone", "Phone", "android", "Android", "devices:read",
                   (SourceSpec("home_assistant", True),),
                   (SensorSpec("battery_percent", "number", "supported", "home_assistant", "%"),
                    SensorSpec("wifi_connected", "boolean", "supported", "home_assistant"),
                    SensorSpec("cpu_usage", "number", "unsupported")))
PHONE_BINDINGS = {("home_assistant", "phone-proof-id"): "phone"}


def test_empty_registry_and_single_device():
    empty, _ = registry(catalog=(), bindings={})
    assert empty.list(ALL).devices == []
    single, _ = registry(catalog=(PHONE,), bindings=PHONE_BINDINGS)
    devices = single.list(ALL).devices
    assert [d.id for d in devices] == ["phone"] and devices[0].status_reason == "awaiting_first_report"


@pytest.mark.asyncio
async def test_change_listeners_receive_real_device_views_and_are_isolated():
    r, _ = registry()
    seen = []

    async def broken(_detail):
        raise RuntimeError("listener detail")

    async def capture(detail):
        seen.append((detail.id, detail.health))

    r.change_listeners.extend([broken, capture])
    r.ingest("system_monitor", "primary", primary_readings(), connection="online")
    await r.publish_changes()
    assert ("primary", "healthy") in seen


def test_multiple_devices_filtered_by_scope():
    r, _ = registry()
    assert [d.id for d in r.list(SYSTEM).devices] == ["primary", "secondary"]
    assert [d.id for d in r.list(PERSONAL).devices] == ["workstation", "phone", "tablet", "phone_2", "roomos_panel_phone"]
    assert len(r.list(ALL).devices) == 7 and r.list(frozenset({"lights:read"})).devices == []


def test_unsupported_and_unconfigured_sensors_are_not_zero_filled():
    r, _ = registry()
    handset = r.detail("phone", ALL)
    cpu = sensor(handset, "cpu_usage")
    assert (cpu.availability, cpu.value, cpu.reason, cpu.source) == ("unsupported", None, "not_supported_by_device", None)
    battery = sensor(handset, "battery_percent")
    assert (battery.availability, battery.value, battery.reason) == ("unknown", None, "source_not_configured")
    assert handset.capabilities["battery_temperature"] == "supported"  # BatteryManager EXTRA_TEMPERATURE (7D.3)
    assert (handset.connection, handset.health, handset.status_reason) == ("unknown", "unknown", "source_not_configured")


def test_unknown_connectivity_before_first_report_and_after_restart():
    r, _ = registry()
    primary = r.detail("primary", ALL)
    assert (primary.connection, primary.telemetry_freshness, primary.status_reason, primary.last_seen_at) == ("unknown", "unknown", "awaiting_first_report", None)
    r.ingest("system_monitor", "primary", primary_readings(), connection="online")
    restarted, _ = registry()  # no persistence: a new process never re-presents old values
    after = restarted.detail("primary", ALL)
    assert after.connection == "unknown" and all(s.value is None for s in after.sensors)


def test_fresh_then_stale_telemetry_and_reachability_expiry():
    r, clock = registry()
    r.ingest("system_monitor", "primary", primary_readings(), connection="online")
    fresh = r.detail("primary", ALL)
    assert (fresh.connection, fresh.health, fresh.telemetry_freshness) == ("online", "healthy", "fresh")
    assert sensor(fresh, "cpu_usage").value == 31.5 and sensor(fresh, "cpu_usage").measured_at is None
    assert fresh.last_seen_at == T and fresh.last_telemetry_at == T
    clock.advance(179)
    assert r.detail("primary", ALL).telemetry_freshness == "fresh"
    clock.advance(1)
    stale = r.detail("primary", ALL)
    cpu = sensor(stale, "cpu_usage")
    # The old value stays visible but is never presented as current; reachability becomes unknown.
    assert (cpu.value, cpu.freshness, stale.telemetry_freshness) == (31.5, "stale", "stale")
    assert (stale.connection, stale.health, stale.last_seen_at) == ("unknown", "unknown", T)


def test_offline_device_is_reported_offline_not_powered_off():
    r, _ = registry()
    r.ingest("system_monitor", "secondary", primary_readings(), connection="offline")
    secondary = r.detail("secondary", ALL)
    assert (secondary.connection, secondary.health) == ("offline", "offline")
    assert secondary.sources[0].connection == "offline" and secondary.sources[0].last_seen_at == T


def test_old_cpu_measurement_is_stale_on_arrival_and_source_stale_verdict_is_kept():
    r, _ = registry()
    old = Reading(30, "%", measured_at=T - timedelta(minutes=5))
    r.ingest("system_monitor", "primary", primary_readings(cpu_usage=old, ram_usage=Reading(40, "%", source_fresh=False)), connection="online")
    detail = r.detail("primary", ALL)
    cpu = sensor(detail, "cpu_usage")
    assert (cpu.value, cpu.freshness, cpu.measured_at, cpu.observed_at) == (30, "stale", T - timedelta(minutes=5), T)
    assert sensor(detail, "ram_usage").freshness == "stale"
    assert (detail.telemetry_freshness, detail.health) == ("stale", "degraded")


def test_battery_and_connectivity_age_independently():
    r, clock = registry(catalog=(PHONE,), bindings=PHONE_BINDINGS)
    r.ingest("home_assistant", "phone-proof-id", {"battery_percent": Reading(80)}, connection="online")
    clock.advance(800)
    r.ingest("home_assistant", "phone-proof-id", {"wifi_connected": Reading(True)}, connection="online")
    clock.advance(101)
    phone = r.detail("phone", ALL)
    battery, wifi = sensor(phone, "battery_percent"), sensor(phone, "wifi_connected")
    # A newer Wi-Fi report proves nothing about battery recency (or vice versa).
    assert (battery.freshness, wifi.freshness) == ("stale", "fresh")
    assert battery.observed_at == T and wifi.observed_at == T + timedelta(seconds=800)
    assert (phone.connection, phone.telemetry_freshness, phone.health) == ("online", "stale", "degraded")


@pytest.mark.parametrize("measured", [T.replace(tzinfo=None), T + timedelta(minutes=5), "not-a-timestamp", 123])
def test_invalid_timestamps_drop_the_value(measured):
    r, _ = registry()
    r.ingest("system_monitor", "primary", primary_readings(cpu_usage=Reading(10, "%", measured_at=measured)), connection="online")
    cpu = sensor(r.detail("primary", ALL), "cpu_usage")
    assert (cpu.value, cpu.availability, cpu.freshness, cpu.reason) == (None, "unknown", "unknown", "invalid_timestamp")


@pytest.mark.parametrize("bad", ["31%", float("nan"), float("inf"), True, None, {"x": 1}])
def test_corrupt_values_replace_previous_good_value(bad):
    r, clock = registry()
    r.ingest("system_monitor", "primary", primary_readings(), connection="online")
    clock.advance(5)
    r.ingest("system_monitor", "primary", primary_readings(cpu_usage=Reading(bad, "%")), connection="online")
    detail = r.detail("primary", ALL)
    cpu = sensor(detail, "cpu_usage")
    assert (cpu.value, cpu.availability, cpu.reason) == (None, "unknown", "invalid_report")
    assert detail.health == "degraded"
    assert sensor(detail, "docker_running").value is True  # other sensors unaffected


def test_source_reported_unknown_and_unavailable_are_distinct():
    r, _ = registry()
    r.ingest("system_monitor", "primary", primary_readings(cpu_temperature=Reading(state="unavailable"), uptime=Reading(state="unknown")))
    detail = r.detail("primary", ALL)
    assert (sensor(detail, "cpu_temperature").availability, sensor(detail, "cpu_temperature").reason) == ("unavailable", "source_unavailable")
    assert (sensor(detail, "uptime").availability, sensor(detail, "uptime").reason) == ("unknown", "source_reported_unknown")


def test_clock_discontinuity_makes_freshness_unknown():
    r, clock = registry()
    r.ingest("system_monitor", "primary", primary_readings(), connection="online")
    clock.advance(3600, wall_only=True)
    detail = r.detail("primary", ALL)
    assert sensor(detail, "cpu_usage").reason == "clock_discontinuity"
    assert (detail.connection, detail.telemetry_freshness) == ("unknown", "unknown")


def test_unknown_and_invisible_device_ids():
    r, _ = registry()
    for device_id in ("nope", "../primary", "PRIMARY", ""):
        assert r.detail(device_id, ALL) is None and r.telemetry(device_id, ALL) is None
    assert r.detail("phone", SYSTEM) is None  # invisible is indistinguishable from unknown


def test_identity_bindings_are_exact_and_never_merge_or_create():
    r, _ = registry()
    assert r.ingest("system_monitor", "Primary", primary_readings()) is None
    assert r.ingest("home_assistant", "primary", primary_readings()) is None
    assert r.ingest("mqtt", "phone", {"battery_percent": Reading(50)}) is None
    assert r.rejected["unbound_identity"] == 3 and len(r.list(ALL).devices) == 7
    assert r.ingest("system_monitor", "primary", {"battery_percent": Reading(50), "made_up": Reading(1)}) == "primary"
    assert r.rejected["foreign_sensor"] == 2
    dual = DeviceSpec("workstation", "Workstation", "windows", "Windows", "devices:read",
                      (SourceSpec("home_assistant", True), SourceSpec("mqtt", True)),
                      (SensorSpec("cpu_usage", "number", "supported", "home_assistant", "%"),
                       SensorSpec("battery_percent", "number", "supported", "mqtt", "%")))
    two, _ = registry(catalog=(dual,), bindings={("home_assistant", "hass-agent-1"): "workstation", ("mqtt", "workstation-topic"): "workstation"})
    two.ingest("home_assistant", "hass-agent-1", {"cpu_usage": Reading(20), "battery_percent": Reading(1)}, connection="online")
    two.ingest("mqtt", "workstation-topic", {"battery_percent": Reading(77)}, connection="unknown")
    workstation = two.detail("workstation", ALL)
    assert len(two.list(ALL).devices) == 1 and sensor(workstation, "battery_percent").value == 77
    assert workstation.connection == "online" and two.rejected["foreign_sensor"] == 1


def test_catalog_validation_rejects_ambiguous_definitions():
    validate_catalog(CATALOG, BINDINGS)
    with pytest.raises(ValueError):
        validate_catalog((PHONE, PHONE), {})
    with pytest.raises(ValueError):
        validate_catalog((PHONE,), {("mqtt", "x"): "phone"})
    with pytest.raises(ValueError):
        validate_catalog((PHONE,), {("home_assistant", "x"): "ghost"})


def node(node_id, online, **metrics):
    return SystemNode(id=node_id, name=node_id, online=online, metrics={
        f"{node_id}_{k}": SystemMetric(id=f"{node_id}_{k}", value=v, unit=u, available=f in {"fresh", "stale"},
                                       stale=f == "stale", freshness=f, last_updated=T - timedelta(hours=3))
        for k, (v, u, f) in metrics.items()})


def snapshot(primary_online=True):
    dns = DnsResolverStatus(id="primary", role="primary", status="healthy")
    return SystemResponse(
        primary=node("primary", primary_online, cpu=(12, "%", "fresh"), ram=(50, "%", "fresh"), disk=(30, "%", "fresh"),
                 temperature=(55.0, "°C", "fresh"), uptime=("5 days", None, "fresh"), docker=(True, None, "fresh")),
        secondary=node("secondary", None, cpu=(None, "%", "unknown"), temperature=(None, "°C", "unavailable"), disk=(20, "%", "stale")),
        services=[], dns_cluster=DnsClusterStatus(status="healthy", primary=dns,
                                                  secondary=dns.model_copy(update={"id": "secondary", "role": "secondary"})))


def test_system_monitor_adapter_maps_existing_snapshot_truthfully():
    r, _ = registry()
    r.ingest_system_snapshot(snapshot())
    primary, secondary = r.detail("primary", ALL), r.detail("secondary", ALL)
    assert (primary.connection, primary.health, sensor(primary, "cpu_temperature").value) == ("online", "healthy", 55.0)
    # HA last_updated (3 h old state change) is not treated as a measurement time.
    assert sensor(primary, "cpu_usage").measured_at is None and sensor(primary, "cpu_usage").freshness == "fresh"
    assert secondary.connection == "unknown"
    assert sensor(secondary, "cpu_usage").reason == "source_reported_unknown"
    assert sensor(secondary, "cpu_temperature").availability == "unavailable"
    assert sensor(secondary, "disk_usage").freshness == "stale"
    assert sensor(secondary, "ram_usage").reason == "awaiting_first_report"


@pytest.mark.asyncio
async def test_events_are_deduplicated_scoped_and_track_time_transitions():
    sent = []

    async def publish(event, scope):
        sent.append((event.data["device"]["id"], scope, event.data["device"]["telemetry_freshness"]))

    r, clock = registry(publish=publish)
    assert await r.publish_changes() == 7
    assert dict((d, s) for d, s, _ in sent) == {"primary": "system:read", "secondary": "system:read", "workstation": "devices:read",
                                                "phone": "devices:read", "tablet": "devices:read", "phone_2": "devices:read", "roomos_panel_phone": "devices:read"}
    assert await r.publish_changes() == 0
    await r.on_system_snapshot(snapshot())
    assert [d for d, _, _ in sent[7:]] == ["primary", "secondary"]
    await r.on_system_snapshot(snapshot())  # identical report: no UI churn
    assert len(sent) == 9
    clock.advance(200)
    # Only Primary changes (fresh -> stale); Secondary was already unknown/stale, so nothing is re-sent.
    assert await r.publish_changes() == 1 and sent[-1][:3] == ("primary", "system:read", "stale")


@pytest.mark.asyncio
async def test_failing_snapshot_listener_does_not_break_system_monitor():
    from app.system_monitor import SystemMonitor

    class Alerts:
        async def evaluate_ha_connection(self, _):
            pass

        async def evaluate(self, _):
            pass

    published = []

    async def publish(event):
        published.append(event.type)

    async def broken(_):
        raise RuntimeError("listener bug")

    received = []

    async def good(snap):
        received.append(snap)

    monitor = SystemMonitor(None, None, Alerts(), publish)
    monitor.snapshot_listeners += [broken, good]
    await monitor.accept(snapshot())
    await monitor.accept(snapshot())
    assert published == ["system.snapshot_updated"] and len(received) == 2


# ----- API ------------------------------------------------------------------------------

def test_api_list_detail_telemetry_and_unknown(client):
    devices = client.get("/api/v1/devices")
    assert devices.status_code == 200 and len(devices.json()["devices"]) == 7
    detail = client.get("/api/v1/devices/phone")
    assert detail.status_code == 200 and detail.json()["connection"] == "unknown"
    telemetry = client.get("/api/v1/devices/primary/telemetry").json()
    assert telemetry["device_id"] == "primary" and {s["id"] for s in telemetry["sensors"]} >= {"cpu_usage", "battery_percent"}
    for path in ("/api/v1/devices/nope", "/api/v1/devices/nope/telemetry", "/api/v1/devices/%2e%2e"):
        response = client.get(path)
        assert response.status_code == 404
    assert client.get("/api/v1/devices?limit=5").status_code == 422
    text = json.dumps(devices.json()) + json.dumps(detail.json())
    for leak in ("sensor.", "binary_sensor", "device_tracker", "latitude", "token", "device_tracker.phone"):
        assert leak not in text


def test_api_has_no_write_methods(client):
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)("/api/v1/devices/primary").status_code == 405


def test_registry_persists_nothing(client, tmp_path):
    from app.main import app
    before = {p.name for p in tmp_path.iterdir()}
    app.state.devices.ingest("system_monitor", "primary", primary_readings(), connection="online")
    client.get("/api/v1/devices/primary")
    assert {p.name for p in tmp_path.iterdir()} == before
    assert not any("device" in name for name in before)


from test_auth import auth_client  # noqa: E402,F401 - shared isolated auth fixture


def test_authentication_and_per_device_authorization(auth_client):
    from app.auth import SESSION_COOKIE
    client, ha, store, _ = auth_client
    assert client.get("/api/v1/devices").status_code == 401
    assert client.get("/api/v1/devices/primary").status_code == 401

    def session(scopes):
        code, _ = store.create_pairing_window()
        client.cookies.set(SESSION_COOKIE, store.pair(code, "fixture " + "+".join(scopes), "fixture", scopes=scopes).token)

    session(("lights:read",))
    assert client.get("/api/v1/devices").status_code == 403
    session(("system:read",))
    assert [d["id"] for d in client.get("/api/v1/devices").json()["devices"]] == ["primary", "secondary"]
    assert client.get("/api/v1/devices/phone").status_code == 404
    session(("devices:read",))
    ids = [d["id"] for d in client.get("/api/v1/devices").json()["devices"]]
    assert "primary" not in ids and "phone" in ids
    assert client.get("/api/v1/devices/primary/telemetry").status_code == 404
    assert not ha.calls


def test_new_scope_does_not_change_pairing_defaults():
    from app.auth import DEFAULT_DEVICE_SCOPES, NATIVE_READONLY_SCOPES
    assert "devices:read" not in DEFAULT_DEVICE_SCOPES and "devices:read" not in NATIVE_READONLY_SCOPES


def test_backup_tooling_unchanged_because_registry_has_no_state_file():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import roomos_state_backup as backup
    assert not any("device" in name for name in backup.STATE_SPECS)


@pytest.mark.asyncio
async def test_failed_publish_retries_without_losing_event():
    attempts = []

    async def publish(event, scope):
        attempts.append(event)
        if len(attempts) == 1:
            raise RuntimeError("transient")

    r, _ = registry(catalog=(PHONE,), bindings=PHONE_BINDINGS, publish=publish)
    assert await r.publish_changes() == 0
    assert await r.publish_changes() == 1
    assert await r.publish_changes() == 0


@pytest.mark.asyncio
async def test_device_events_are_filtered_by_existing_broker():
    from app.events import EventBroker

    class Socket:
        def __init__(self):
            self.received = []
        async def accept(self):
            pass
        async def send_json(self, data):
            self.received.append(data)

    broker = EventBroker()
    servers, personal, neither = Socket(), Socket(), Socket()
    await broker.connect(servers, scopes=SYSTEM)
    await broker.connect(personal, scopes=PERSONAL)
    await broker.connect(neither, scopes=frozenset({"lights:read"}))

    async def publish(event, scope):
        await broker.broadcast(event, required_scope=scope)

    r, _ = registry(publish=publish)
    await r.publish_changes()
    assert len(servers.received) == 2 and len(personal.received) == 5
    assert neither.received == []
