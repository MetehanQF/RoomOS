"""Phase 7D.2: Workstation telemetry from HASS.Agent via Home Assistant (synthetic data only)."""
import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from app.catalog import SUBSCRIBED_ENTITIES
from app.config import Settings
from app.devices import DeviceRegistry
from app.events import HAEventBridge
from app.windows_telemetry import WindowsTelemetryAdapter, configured_catalog, parse_config, parse_state

T = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
THRESHOLDS = {"system_monitor": 180, "home_assistant": 900, "mqtt": 300, "roomos_native": 120}
ALL = frozenset({"system:read", "devices:read"})
ENTITIES = {
    "heartbeat": "sensor.workstation_heartbeat",
    "cpu_usage": "sensor.workstation_cpuload",
    "ram_usage": "sensor.workstation_memoryusage",
    "disk_usage": "sensor.workstation_ssd_c",
    "battery_percent": "sensor.workstation_battery_charge_remaining_percentage",
    "charging": "sensor.workstation_battery_charge_status",
    "ac_connected": "sensor.workstation_battery_powerline_status",
    "last_boot": "sensor.workstation_lastboot",
}


def state(value, entity="sensor.x", attributes=None):
    return {"entity_id": entity, "state": value, "attributes": attributes or {}, "last_updated": T.isoformat()}


def drive(name="C", used_pct=61, total=510669.0, used=311508.0, label="Windows", **extra):
    """HASS.Agent 2.2.1 Storage drive sensor as observed on Workstation: state = volume label."""
    attributes = {"Name": name, "Label": label, "FileSystem": "NTFS", "TotalSizeMB": total,
                  "AvailableSpaceMB": 6373.0, "UsedSpaceMB": used,
                  "AvailableSpacePercentage": 100 - used_pct if isinstance(used_pct, int) else None, "UsedSpacePercentage": used_pct} | extra
    return state(label, ENTITIES["disk_usage"], {k: v for k, v in attributes.items() if v is not ...})


def snapshot(**overrides):
    values = {"heartbeat": "41", "cpu_usage": "12.4", "ram_usage": "48.0", "disk_usage": drive(),
              "battery_percent": "87", "charging": "High, Charging", "ac_connected": "Online",
              "last_boot": "2026-10-14T06:12:00+00:00"} | overrides
    return {ENTITIES[k]: (v if isinstance(v, dict) else state(v, ENTITIES[k]) if v is not None else None)
            for k, v in values.items()}


class Clock:
    def __init__(self):
        self.wall, self.mono = T, 100.0

    def advance(self, seconds):
        self.wall += timedelta(seconds=seconds)
        self.mono += seconds


def setup(entities=ENTITIES, publish=None):
    config = parse_config("workstation", json.dumps(entities), 120, reserved_entities=SUBSCRIBED_ENTITIES)
    catalog, bindings = configured_catalog(config)
    clock = Clock()
    registry = DeviceRegistry(THRESHOLDS, publish, catalog=catalog, bindings=bindings,
                              clock=lambda: clock.wall, monotonic=lambda: clock.mono)
    adapter = WindowsTelemetryAdapter(registry, config, monotonic=lambda: clock.mono)
    return registry, adapter, clock


def workstation(registry):
    return registry.detail("workstation", ALL)


def sensor(detail, sensor_id):
    return next(s for s in detail.sensors if s.id == sensor_id)


def beat(adapter, previous, value):
    adapter.observe(ENTITIES["heartbeat"], state(str(value)), state(str(previous)))


# ----- configuration --------------------------------------------------------------------

def test_unconfigured_production_default_is_unchanged():
    assert parse_config(None, None, 120) is None
    catalog, bindings = configured_catalog(None)
    workstation_spec = next(d for d in catalog if d.id == "workstation")
    assert workstation_spec.sources[0].configured is False and ("home_assistant", "hass.agent-workstation") not in bindings
    registry = DeviceRegistry(THRESHOLDS, catalog=catalog, bindings=bindings)
    assert workstation(registry).status_reason == "source_not_configured"


@pytest.mark.parametrize("agent,entities", [
    ("workstation", json.dumps({"cpu_usage": "sensor.workstation_cpuload"})),          # heartbeat missing
    ("workstation", json.dumps({**ENTITIES, "made_up": "sensor.x"})),               # unknown sensor id
    ("workstation", json.dumps({**ENTITIES, "ram_usage": ENTITIES["cpu_usage"]})),  # one entity, two sensors
    ("workstation", json.dumps({**ENTITIES, "cpu_usage": "switch.workstation_shutdown"})),  # not a sensor
    ("workstation", json.dumps({**ENTITIES, "cpu_usage": "sensor.primary_cpu"})),       # owned by Primary source
    ("Workstation PC", json.dumps(ENTITIES)),                                       # not a device name
    ("workstation", "{not json"),
])
def test_invalid_onboarding_config_fails_closed(agent, entities):
    assert parse_config(agent, entities, 120, reserved_entities=SUBSCRIBED_ENTITIES) is None


@pytest.fixture
def invalid_workstation_env(monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("ROOMOS_DEVICES_WORKSTATION_AGENT_ID", "workstation")
    monkeypatch.setenv("ROOMOS_DEVICES_WORKSTATION_ENTITIES", json.dumps({"cpu_usage": "sensor.primary_cpu"}))
    get_settings.cache_clear()


@pytest.fixture
def valid_workstation_env(monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("ROOMOS_DEVICES_WORKSTATION_AGENT_ID", "workstation")
    monkeypatch.setenv("ROOMOS_DEVICES_WORKSTATION_ENTITIES", json.dumps(ENTITIES))
    get_settings.cache_clear()


def test_core_starts_with_invalid_config_and_workstation_stays_unconfigured(invalid_workstation_env, client):
    from app.main import app
    assert app.state.windows_telemetry is None
    assert client.get("/api/v1/devices/workstation").json()["status_reason"] == "source_not_configured"
    assert client.get("/api/v1/system/health").status_code == 200


def test_core_enables_adapter_with_valid_config(valid_workstation_env, client):
    from app.main import app
    assert app.state.windows_telemetry is not None
    body = client.get("/api/v1/devices/workstation").json()
    assert body["status_reason"] == "awaiting_first_report" and body["sources"][0]["configured"] is True


# ----- identity and mapping -------------------------------------------------------------

def test_single_stable_identity_across_reconnects_and_agent_restarts():
    registry, adapter, clock = setup()
    adapter.bootstrap(snapshot())
    adapter.reset()
    adapter.bootstrap(snapshot(cpu_usage="20"))
    beat(adapter, 41, 42)
    ids = [d.id for d in registry.list(ALL).devices]
    assert ids.count("workstation") == 1 and len(ids) == 7
    # A renamed HASS.Agent device is a different identity until re-verified: never merged.
    assert registry.ingest("home_assistant", "hass.agent-workstation2", {}) is None
    assert registry.rejected["unbound_identity"] == 1


def test_sensor_mapping_types_and_unmapped_sensors():
    registry, adapter, _ = setup()
    adapter.bootstrap(snapshot())
    beat(adapter, 41, 77)
    d = workstation(registry)
    values = {s.id: (s.value, s.unit, s.availability) for s in d.sensors}
    assert values["cpu_usage"] == (12.4, "%", "available")
    assert values["ram_usage"] == (48.0, "%", "available")
    assert values["disk_usage"] == (61.0, "%", "available")
    assert values["battery_percent"] == (87.0, "%", "available")
    assert values["charging"][0] is True and values["ac_connected"][0] is True  # separate measurements
    assert values["last_boot"][0] == "2026-10-14T06:12:00+00:00"
    for unmapped in ("cpu_temperature", "gpu_usage", "gpu_temperature", "network_connected"):
        s = sensor(d, unmapped)
        assert (s.support, s.availability, s.value, s.reason) == ("unsupported", "unsupported", None, "not_supported_by_device")
    assert "sensor." not in json.dumps(d.model_dump(mode="json"))  # no HA entity ids leak


@pytest.mark.parametrize("charge,ac,expected", [
    ("High, Charging", "Online", (True, True)),
    ("High", "Online", (False, True)),       # plugged in, full, not charging
    ("Low", "Offline", (False, False)),
    ("Charging", "Unknown", (True, None)),
])
def test_battery_percentage_charging_and_ac_are_independent(charge, ac, expected):
    assert parse_state("charging", state(charge)).value is expected[0]
    ac_reading = parse_state("ac_connected", state(ac))
    assert (ac_reading.value if ac_reading.state == "value" else None) is expected[1]


@pytest.mark.parametrize("raw", ["abc", "150", "-3", "nan", "inf", "25500"])
def test_invalid_numbers_are_rejected_not_zero_filled(raw):
    registry, adapter, _ = setup()
    adapter.bootstrap(snapshot(cpu_usage=raw, battery_percent=raw))
    beat(adapter, 41, 42)
    d = workstation(registry)
    for s in ("cpu_usage", "battery_percent"):
        assert (sensor(d, s).value, sensor(d, s).reason) == (None, "invalid_report")


def test_no_system_battery_is_unavailable_not_zero():
    assert parse_state("charging", state("NoSystemBattery")).state == "unavailable"


def test_disk_usage_reads_real_workstation_storage_payload():
    # Verbatim shape of sensor.workstation_ssd_c on 2026-10-14 (state is the label).
    real = drive(used_pct=99, total=510669.0, used=504296.0)
    assert real["state"] == "Windows"
    assert parse_state("disk_usage", real).value == 99.0
    assert parse_state("disk_usage", drive(name="c:")).value == 61.0  # letter only, case-free
    assert parse_state("disk_usage", drive(label="-")).value == 61.0  # unlabelled volume


@pytest.mark.parametrize("payload", [
    drive(name="D"),                                     # another drive is never C:
    drive(name=...),                                     # no drive identity
    drive(name=7),
    drive(used_pct=...),                                 # percentage missing
    drive(used_pct="abc"),
    drive(used_pct=True),
    drive(used_pct=float("nan")),
    drive(used_pct=150, used=510669.0 * 1.5),
    drive(used_pct=-1, used=-5106.0),
    drive(used_pct=-2147483648, total=0.0, used=0.0),     # HASS.Agent's unguarded x/0 cast
    drive(total=...),                                    # cannot check the denominator
    drive(total=-1.0),
    drive(used=600000.0),                                # used > total
    {**drive(), "attributes": "not-a-map"},
    drive(used_pct=10),                                  # disagrees with MB figures
    state("61.2", ENTITIES["disk_usage"]),               # numeric state without drive info
])
def test_disk_usage_rejects_wrong_drive_missing_or_inconsistent_values(payload):
    reading = parse_state("disk_usage", payload)
    assert reading.state == "value" and reading.value is None  # -> invalid_report, never 0


def test_disk_usage_tolerates_missing_used_mb_but_not_garbage():
    assert parse_state("disk_usage", drive(used=...)).value == 61.0
    assert parse_state("disk_usage", drive(used="x")).value is None


def test_disk_usage_attribute_only_change_updates_and_invalid_is_reported():
    registry, adapter, _ = setup()
    adapter.bootstrap(snapshot())
    beat(adapter, 41, 42)
    adapter.observe(ENTITIES["disk_usage"], drive(used_pct=62, used=316615.0), drive())
    assert sensor(workstation(registry), "disk_usage").value == 62.0
    adapter.observe(ENTITIES["disk_usage"], drive(name="D"), drive(used_pct=62, used=316615.0))
    disk = sensor(workstation(registry), "disk_usage")
    assert (disk.value, disk.reason) == (None, "invalid_report")


def test_ha_timestamps_are_never_trusted_as_measurement_time():
    registry, adapter, _ = setup()
    bad = snapshot()
    bad[ENTITIES["cpu_usage"]]["last_updated"] = "2099-01-01T00:00:00Z"
    adapter.bootstrap(bad)
    beat(adapter, 41, 42)
    cpu = sensor(workstation(registry), "cpu_usage")
    assert cpu.measured_at is None and cpu.freshness == "fresh" and cpu.value == 12.4


# ----- freshness and availability -----------------------------------------------------

def test_retained_replay_is_not_fresh_until_a_live_heartbeat():
    registry, adapter, clock = setup()
    adapter.bootstrap(snapshot())  # values may be retained replays from before an HA restart
    d = workstation(registry)
    assert d.connection == "unknown" and sensor(d, "cpu_usage").freshness == "stale"
    # Replay of the heartbeat itself (previous state unknown/unavailable) proves nothing.
    adapter.observe(ENTITIES["heartbeat"], state("41"), state("unavailable"))
    adapter.observe(ENTITIES["heartbeat"], state("41"), None)
    assert workstation(registry).connection == "unknown"
    beat(adapter, 41, 63)
    d = workstation(registry)
    assert d.connection == "online" and d.health == "healthy" and sensor(d, "battery_percent").freshness == "fresh"


def test_sleep_or_unreachable_becomes_unknown_and_stale_without_new_events():
    registry, adapter, clock = setup()
    adapter.bootstrap(snapshot()); beat(adapter, 41, 42)
    clock.advance(119); adapter.tick()
    assert workstation(registry).connection == "online"
    clock.advance(2); adapter.tick()
    d = workstation(registry)
    assert d.connection == "unknown"  # not "off": sleep/shutdown/network/agent are indistinguishable
    assert sensor(d, "cpu_usage").value == 12.4 and sensor(d, "cpu_usage").freshness == "stale"


def test_dummy_heartbeat_cadence_matches_hass_agent_publishing():
    # HASS.Agent 2.2.1: Dummy = Random.Next(0, 100) every interval, but PublishStateAsync skips
    # a value equal to the previous one. A 30 s Dummy therefore produces a new MQTT message
    # (and a real HA state change) every 30 s, with an occasional 60 s gap (p = 1/100).
    registry, adapter, clock = setup()
    adapter.bootstrap(snapshot()); beat(adapter, 41, 42)
    for previous, value in [(42, 7), (7, 93), (93, 0)]:
        clock.advance(30); beat(adapter, previous, value); adapter.tick()
        assert workstation(registry).connection == "online"
    clock.advance(60); beat(adapter, 0, 55); adapter.tick()  # one suppressed equal draw
    assert workstation(registry).connection == "online"
    # An identical value is not a heartbeat (HA reports no change; never counted as liveness).
    clock.advance(100); beat(adapter, 55, 55); clock.advance(21); adapter.tick()
    assert workstation(registry).connection == "unknown"


def test_agent_lwt_offline_keeps_last_values_stale():
    registry, adapter, clock = setup()
    adapter.bootstrap(snapshot()); beat(adapter, 41, 42)
    for key in ("cpu_usage", "battery_percent", "heartbeat"):  # HA marks every agent entity unavailable
        adapter.observe(ENTITIES[key], state("unavailable"), state("1"))
    d = workstation(registry)
    assert (d.connection, d.health) == ("offline", "offline")
    cpu, battery = sensor(d, "cpu_usage"), sensor(d, "battery_percent")
    assert (cpu.value, cpu.freshness, battery.value, battery.freshness) == (12.4, "stale", 87.0, "stale")
    beat(adapter, 42, 43)  # agent reconnects (availability online, heartbeat changes)
    assert workstation(registry).connection == "online"


def test_sensor_unavailable_while_agent_alive_is_a_real_sensor_problem():
    registry, adapter, _ = setup()
    adapter.bootstrap(snapshot()); beat(adapter, 41, 42)
    adapter.observe(ENTITIES["disk_usage"], state("unavailable"), drive())
    d = workstation(registry)
    assert d.connection == "online" and sensor(d, "disk_usage").availability == "unavailable"
    assert d.health == "degraded"


def test_ha_disconnect_marks_stale_and_reconnect_recovers():
    registry, adapter, clock = setup()
    adapter.bootstrap(snapshot()); beat(adapter, 41, 42)
    adapter.reset()  # HA WebSocket lost
    d = workstation(registry)
    assert d.connection == "unknown" and sensor(d, "ram_usage").freshness == "stale"
    adapter.observe(ENTITIES["heartbeat"], state("50"), state("42"))  # late frame after reset: ignored
    assert workstation(registry).connection == "unknown"
    adapter.bootstrap(snapshot(ram_usage="51.0"))
    assert workstation(registry).connection == "unknown"
    beat(adapter, 41, 9)
    d = workstation(registry)
    assert d.connection == "online" and sensor(d, "ram_usage").value == 51.0 and sensor(d, "ram_usage").freshness == "fresh"


# ----- events -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_heartbeats_and_repeats_do_not_publish_but_real_changes_do():
    sent = []

    async def publish(event, scope):
        sent.append((event.data["device"]["id"], scope, event.data["device"]))

    registry, adapter, clock = setup(publish=publish)
    adapter.bootstrap(snapshot()); beat(adapter, 41, 42)
    await registry.publish_changes()
    baseline = len(sent)
    for i in range(120):  # 1 h of 30 s heartbeats, identical values
        clock.advance(30)
        beat(adapter, i, i + 1)
        adapter.observe(ENTITIES["cpu_usage"], state("12.4"), state("12.4"))
        adapter.tick()
        await registry.publish_changes()
    assert len(sent) == baseline, "last_seen/heartbeat alone must not publish"
    adapter.observe(ENTITIES["cpu_usage"], state("55.0"), state("12.4"))
    await registry.publish_changes()
    assert len(sent) == baseline + 1 and sent[-1][1] == "devices:read"
    assert sensor_value(sent[-1][2], "cpu_usage") == 55.0
    clock.advance(130); adapter.tick(); await registry.publish_changes()
    assert len(sent) == baseline + 2 and sent[-1][2]["connection"] == "unknown"


def sensor_value(device, sensor_id):
    return next(s for s in device["sensors"] if s["id"] == sensor_id)["value"]


class RecordingBroker:
    def __init__(self):
        self.events = []

    async def broadcast(self, event, required_scope=None):
        self.events.append((event, required_scope))


class WorkstationHA:
    """Synthetic HA: subscription ack, bootstrap REST snapshot, then HASS.Agent-like events."""

    def __init__(self, events):
        self.events, self.reads = events, []

    async def get_states(self, entity_ids):
        self.reads.append(tuple(entity_ids))
        snap = snapshot()
        return {e: snap.get(e) for e in entity_ids}

    async def event_stream(self):
        yield {"type": "result", "success": True}
        for entity, old, new in self.events:
            yield {"type": "event", "event": {"data": {"entity_id": entity, "old_state": state(old, entity), "new_state": state(new, entity)}}}
        raise asyncio.CancelledError


@pytest.mark.asyncio
async def test_bridge_routes_workstation_to_registry_only_without_system_events():
    published = []

    async def publish(event, scope):
        published.append((event.type, scope, event.data["device"]["id"]))

    registry, adapter, _ = setup(publish=publish)
    broker = RecordingBroker()
    events = [(ENTITIES["heartbeat"], str(i), str(i + 1)) for i in range(200)]
    events += [(ENTITIES["cpu_usage"], "12.4", "70.0"), (ENTITIES["battery_percent"], "87", "86")]
    ha = WorkstationHA(events)

    class Monitor:
        notified = 0

        def notify_source_changed(self):
            Monitor.notified += 1

    bridge = HAEventBridge(Settings(HA_TOKEN="not-a-real-token"), ha, broker, system_monitor=Monitor(),  # privacy-scan:allow fake test credential
                           windows=adapter, devices=registry)
    with pytest.raises(asyncio.CancelledError):
        await bridge.run()
    assert ha.reads == [adapter.entity_ids]  # exactly one bootstrap read per (re)connect
    assert not [e for e, _ in broker.events if e.type.startswith("system.")]
    assert Monitor.notified == 1  # only the existing connect-time notify, none per Workstation event
    d = workstation(registry)
    assert d.connection == "online" and sensor(d, "cpu_usage").value == 70.0 and sensor(d, "battery_percent").value == 86.0
    workstation_events = [p for p in published if p == ("device.updated", "devices:read", "workstation")]
    # initial view, bootstrap (values not yet fresh), first live heartbeat (online), CPU, battery.
    # 20 heartbeats contribute nothing: the count does not grow with heartbeat traffic.
    assert len(workstation_events) == 5


@pytest.mark.asyncio
async def test_primary_and_secondary_sources_are_unaffected():
    from test_system_device_feedback import VALUES  # noqa: F401 - shared realistic values exist
    from test_devices import snapshot as system_snapshot
    registry, adapter, _ = setup()
    adapter.bootstrap(snapshot()); beat(adapter, 41, 42)
    registry.ingest_system_snapshot(system_snapshot())
    primary = registry.detail("primary", ALL)
    assert primary.connection == "online" and primary.health == "healthy"
    assert workstation(registry).connection == "online"


# ----- API authorization --------------------------------------------------------------------

from test_auth import auth_client  # noqa: E402,F401


def test_workstation_telemetry_requires_devices_read(auth_client):
    from app.auth import SESSION_COOKIE
    client, _, store, _ = auth_client
    assert client.get("/api/v1/devices/workstation").status_code == 401
    for scopes, expected in ((("system:read",), 404), (("lights:read",), 403), (("devices:read",), 200)):
        code, _ = store.create_pairing_window()
        client.cookies.set(SESSION_COOKIE, store.pair(code, "fixture " + scopes[0], "fixture", scopes=scopes).token)
        assert client.get("/api/v1/devices/workstation/telemetry").status_code == expected


def test_empty_compose_defaults_mean_not_configured():
    assert parse_config("", "", 120) is None


def test_quick_agent_reconnect_requires_new_heartbeat_even_within_freshness_window():
    registry, adapter, clock = setup()
    adapter.bootstrap(snapshot())
    beat(adapter, 41, 42)
    clock.advance(5)
    adapter.observe(ENTITIES['heartbeat'], state('unavailable'), state('42'))
    assert workstation(registry).connection == 'offline'
    clock.advance(5)
    adapter.observe(ENTITIES['heartbeat'], state('42'), state('unavailable'))
    assert workstation(registry).connection == 'unknown'
    assert sensor(workstation(registry), 'cpu_usage').freshness == 'stale'
    beat(adapter, 42, 42)
    assert workstation(registry).connection == 'unknown'
    beat(adapter, 42, 43)
    assert workstation(registry).connection == 'online'


# ----- 7D.4.1: HASS.Agent GpuTemperature, Network (per adapter) and LastActive -----

@pytest.mark.parametrize(("raw", "expected"), [("Up", True), ("Down", False), ("up", True), ("on", True), ("off", False)])
def test_network_adapter_operational_status(raw, expected):
    assert parse_state("network_connected", state(raw)).value is expected


@pytest.mark.parametrize("raw", ["Testing", "Dormant", "NotPresent", "LowerLayerDown", "1"])
def test_network_other_operational_status_is_not_a_connection_claim(raw):
    assert parse_state("network_connected", state(raw)).value is None  # invalid_report, never "connected"


@pytest.mark.parametrize("raw", ["0", "0.0", "0,0"])
def test_zero_temperature_is_hass_agent_unreadable_sentinel(raw):
    assert parse_state("gpu_temperature", state(raw)).value is None
    assert parse_state("cpu_temperature", state(raw)).value is None


def test_real_temperatures_still_pass():
    assert parse_state("gpu_temperature", state("54")).value == 54.0
    assert parse_state("gpu_temperature", state("131")).value is None


@pytest.mark.parametrize(("raw", "expected"), [
    ("2026-10-15T06:59:37+00:00", "2026-10-15T06:59:37Z"),
    ("2026-10-15T09:59:37+03:00", "2026-10-15T06:59:37Z"),
    ("2026-10-15T06:59:37Z", "2026-10-15T06:59:37Z"),
])
def test_last_active_is_normalized_utc(raw, expected):
    assert parse_state("last_active", state(raw)).value == expected


@pytest.mark.parametrize("raw", ["yesterday", "2026-10-15T06:59:37", "12"])
def test_last_active_rejects_unplaceable_values(raw):
    assert parse_state("last_active", state(raw)).value is None


def test_new_sensors_map_through_onboarding_and_unmapped_stay_unsupported():
    entities = ENTITIES | {"gpu_temperature": "sensor.workstation_gputemp", "network_connected": "sensor.workstation_network_wifi",
                           "last_active": "sensor.workstation_lastactive"}
    config = parse_config("workstation", json.dumps(entities), 120, reserved_entities=SUBSCRIBED_ENTITIES)
    assert config is not None
    catalog, _ = configured_catalog(config)
    workstation = next(d for d in catalog if d.id == "workstation")
    support = {s.id: s.support for s in workstation.sensors}
    assert support["last_active"] == "supported" and support["network_connected"] == "unverified"
    assert support["cpu_temperature"] == "unsupported"
    baseline, _ = configured_catalog(parse_config("workstation", json.dumps(ENTITIES), 120, reserved_entities=SUBSCRIBED_ENTITIES))
    assert {s.id: s.support for s in next(d for d in baseline if d.id == "workstation").sensors}["last_active"] == "unsupported"
