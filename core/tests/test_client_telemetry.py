"""Phase 7D.3: Android/PWA self-reported telemetry (synthetic data only)."""
import json
from datetime import UTC, datetime, timedelta

import pytest

from app.client_telemetry import (
    MAX_BODY_BYTES,
    ClientTelemetryIngest,
    TelemetryRejected,
    configured_catalog,
    parse_bindings,
    parse_value,
)
from app.devices import CATALOG, DeviceRegistry
from test_auth import auth_client  # noqa: F401 - shared isolated auth fixture

T = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
THRESHOLDS = {"system_monitor": 180, "home_assistant": 900, "mqtt": 300, "roomos_native": 120, "roomos_web": 120}
ALL = frozenset({"system:read", "devices:read"})
NATIVE_ID = "11111111-1111-4111-8111-111111111111"
TAB_ID = "22222222-2222-4222-8222-222222222222"
OTHER_ID = "33333333-3333-4333-8333-333333333333"
BINDINGS = {NATIVE_ID: {"device": "phone", "app": "android"}, TAB_ID: {"device": "tablet", "app": "web"}}
NATIVE_VALUES = {
    "battery_percent": 76, "charging": True, "power_source": "usb", "battery_temperature": 31.4,
    "battery_health": "good", "ram_usage": 61.2, "ram_total": 7.4, "disk_usage": 93.5, "storage_free": 8.1,
    "storage_total": 128.0, "network_type": "wifi", "network_connected": True, "thermal_status": "none",
    "android_version": "14", "device_model": "Pixel 7",
}


class Clock:
    def __init__(self):
        self.wall, self.mono = T, 100.0

    def advance(self, seconds):
        self.wall += timedelta(seconds=seconds)
        self.mono += seconds


def setup(bindings=BINDINGS, min_interval=15):
    parsed = parse_bindings(json.dumps(bindings))
    catalog, phone_bindings = configured_catalog(CATALOG, parsed)
    clock = Clock()
    registry = DeviceRegistry(THRESHOLDS, catalog=catalog, bindings=phone_bindings,
                              clock=lambda: clock.wall, monotonic=lambda: clock.mono)
    ingest = ClientTelemetryIngest(registry, parsed, min_interval_seconds=min_interval,
                                   clock=lambda: clock.wall, monotonic=lambda: clock.mono)
    return registry, ingest, clock


def report(clock, sequence=1, sensors=None, next_report=60, measured=None, **extra):
    body = {"schema_version": 1, "sequence": sequence, "measured_at": (measured or clock.wall).isoformat(),
            "next_report_seconds": next_report, "sensors": NATIVE_VALUES if sensors is None else sensors} | extra
    return json.dumps(body).encode()


def device(registry, device_id="phone"):
    return registry.detail(device_id, ALL)


def sensor(detail, sensor_id):
    return next(s for s in detail.sensors if s.id == sensor_id)


# ----- binding / onboarding ---------------------------------------------------------------

def test_unconfigured_default_keeps_phones_unconnected():
    assert parse_bindings(None) == {} and parse_bindings("") == {}
    registry = DeviceRegistry(THRESHOLDS)
    handset = device(registry)
    assert handset.status_reason == "source_not_configured" and handset.connection == "unknown"
    assert sensor(handset, "battery_percent").reason == "source_not_configured"
    assert sensor(handset, "cpu_usage").availability == "unsupported"


@pytest.mark.parametrize("raw", [
    "[]", "{}", "not json",
    json.dumps({"Phone": {"device": "phone", "app": "android"}}),                      # not an auth id
    json.dumps({NATIVE_ID: {"device": "primary", "app": "android"}}),                          # server
    json.dumps({NATIVE_ID: {"device": "workstation", "app": "android"}}),                    # windows
    json.dumps({NATIVE_ID: {"device": "roomos_panel_phone", "app": "web"}}),               # 7E.1 panel
    json.dumps({NATIVE_ID: {"device": "phone", "app": "ios"}}),
    json.dumps({NATIVE_ID: {"device": "phone", "app": "android", "extra": 1}}),
    json.dumps({NATIVE_ID: {"device": "phone", "app": "android"},                     # merge attempt
                OTHER_ID: {"device": "phone", "app": "web"}}),
])
def test_invalid_bindings_fail_closed(raw):
    assert parse_bindings(raw) == {}


def test_bound_devices_get_their_app_contract():
    registry, _, _ = setup()
    handset, tab = device(registry), device(registry, "tablet")
    assert [s.kind for s in handset.sources] == ["roomos_native"] and handset.sources[0].configured
    assert [s.kind for s in tab.sources] == ["roomos_web"] and tab.sources[0].configured
    assert handset.capabilities["ram_usage"] == "supported" and handset.capabilities["cpu_usage"] == "unsupported"
    # The PWA cannot read RAM, storage or temperatures; browser APIs are unverified.
    assert tab.capabilities["ram_usage"] == "unsupported" and tab.capabilities["disk_usage"] == "unsupported"
    assert tab.capabilities["battery_percent"] == "unverified"
    assert "planned_phase" not in handset.metadata and handset.metadata["reporter"] == "RoomOS Android app"
    phone2 = device(registry, "phone_2")
    assert phone2.status_reason == "source_not_configured"


# ----- accepted reports ---------------------------------------------------------------------

def test_native_report_populates_registry_with_device_and_receipt_times():
    registry, ingest, clock = setup()
    measured = clock.wall - timedelta(seconds=4)
    assert ingest.accept(NATIVE_ID, "android", report(clock, measured=measured)) == ("phone", 90)
    handset = device(registry)
    assert handset.connection == "online" and handset.health == "healthy"
    battery = sensor(handset, "battery_percent")
    assert (battery.value, battery.freshness, battery.availability) == (76.0, "fresh", "available")
    assert battery.measured_at == measured and battery.observed_at == clock.wall  # device vs RoomOS time
    assert sensor(handset, "network_type").value == "wifi" and sensor(handset, "charging").value is True
    assert sensor(handset, "cpu_usage").reason == "not_supported_by_device"
    assert handset.sources[0].stale_after_seconds == 90


def test_null_means_unknown_and_invalid_values_are_never_zero():
    registry, ingest, clock = setup()
    values = NATIVE_VALUES | {"battery_percent": 255, "battery_temperature": None, "ram_total": 0,
                              "network_type": "5g", "device_model": "<script>", "android_version": "fourteen",
                              "charging": "yes", "disk_usage": -1}
    ingest.accept(NATIVE_ID, "android", report(clock, sensors=values))
    handset = device(registry)
    for sensor_id in ("battery_percent", "ram_total", "network_type", "device_model", "android_version",
                      "charging", "disk_usage"):
        view = sensor(handset, sensor_id)
        assert (view.value, view.reason) == (None, "invalid_report"), sensor_id
    temperature = sensor(handset, "battery_temperature")
    assert (temperature.value, temperature.reason) == (None, "source_reported_unknown")


@pytest.mark.parametrize("value", [True, "76", [76], {"v": 76}])
def test_number_sensor_rejects_non_numbers(value):
    assert parse_value("battery_percent", "number", value).value is None


def test_values_stale_and_connection_unknown_after_the_announced_deadline():
    registry, ingest, clock = setup()
    ingest.accept(NATIVE_ID, "android", report(clock, next_report=60))
    clock.advance(89)
    assert device(registry).connection == "online"
    clock.advance(2)
    handset = device(registry)
    # Silence proves nothing: unknown (never offline); last values kept but stale.
    assert handset.connection == "unknown" and handset.health == "unknown"
    assert (sensor(handset, "battery_percent").value, sensor(handset, "battery_percent").freshness) == (76.0, "stale")


def test_background_cadence_extends_the_deadline_honestly():
    registry, ingest, clock = setup()
    # The app leaves the foreground: next report via WorkManager in ~15 min (Doze may delay it).
    ingest.accept(NATIVE_ID, "android", report(clock, next_report=900))
    clock.advance(1300)
    assert device(registry).connection == "online" and device(registry).sources[0].stale_after_seconds == 1350
    clock.advance(60)
    assert device(registry).connection == "unknown"


def test_old_measurement_counts_against_freshness():
    registry, ingest, clock = setup()
    ingest.accept(NATIVE_ID, "android", report(clock, next_report=60, measured=clock.wall - timedelta(seconds=80)))
    clock.advance(15)
    assert sensor(device(registry), "ram_usage").freshness == "stale"  # 80 s + 15 s >= 90 s


def test_reconnect_after_network_loss_resumes_with_new_measurements():
    registry, ingest, clock = setup()
    ingest.accept(NATIVE_ID, "android", report(clock, sequence=10))
    clock.advance(600)  # Wi-Fi lost: nothing arrives, nothing is backfilled
    assert device(registry).connection == "unknown"
    ingest.accept(NATIVE_ID, "android", report(clock, sequence=11, sensors=NATIVE_VALUES | {"battery_percent": 70}))
    handset = device(registry)
    assert handset.connection == "online" and sensor(handset, "battery_percent").value == 70.0


# ----- rejected reports -----------------------------------------------------------------------

def rejected(callable_, *args):
    with pytest.raises(TelemetryRejected) as error:
        callable_(*args)
    return error.value.status, error.value.reason


def test_identity_comes_from_the_session_binding_only():
    registry, ingest, clock = setup()
    assert rejected(ingest.accept, OTHER_ID, "web", report(clock)) == (403, "unbound_client")
    # A bound app through the other session kind (e.g. a stolen cookie for a native binding).
    assert rejected(ingest.accept, NATIVE_ID, "web", report(clock)) == (403, "session_kind_mismatch")
    assert rejected(ingest.accept, TAB_ID, "android", report(clock)) == (403, "session_kind_mismatch")
    # There is no field to name another device.
    assert rejected(ingest.accept, NATIVE_ID, "android", report(clock, device_id="workstation")) == (422, "invalid_payload")
    assert device(registry).connection == "unknown"


def test_pwa_may_only_report_browser_sensors():
    registry, ingest, clock = setup()
    assert rejected(ingest.accept, TAB_ID, "web", report(clock, sensors={"ram_usage": 50})) == (422, "unknown_sensor")
    assert ingest.accept(TAB_ID, "web", report(clock, sensors={"battery_percent": 64, "charging": False,
                                                               "network_type": "wifi", "network_connected": True}))
    tab = device(registry, "tablet")
    assert sensor(tab, "battery_percent").value == 64.0 and sensor(tab, "ram_usage").availability == "unsupported"


@pytest.mark.parametrize("change", [
    {"schema_version": 2}, {"sequence": 0}, {"sequence": "1"}, {"next_report_seconds": 5},
    {"next_report_seconds": 7200}, {"sensors": {f"s{i}": 1 for i in range(25)}}, {"unexpected": True},
])
def test_payload_schema_is_strict(change):
    _, ingest, clock = setup()
    assert rejected(ingest.accept, NATIVE_ID, "android", report(clock, **change)) == (422, "invalid_payload")


def test_size_limit():
    _, ingest, clock = setup()
    big = report(clock, padding="x" * MAX_BODY_BYTES)
    assert rejected(ingest.accept, NATIVE_ID, "android", big) == (413, "too_large")


@pytest.mark.parametrize("offset", [timedelta(minutes=11), timedelta(seconds=-61)])
def test_too_old_or_future_measurements_are_rejected(offset):
    _, ingest, clock = setup()
    body = report(clock, measured=clock.wall - offset)
    assert rejected(ingest.accept, NATIVE_ID, "android", body) == (422, "invalid_timestamp")


def test_naive_timestamp_is_rejected():
    _, ingest, clock = setup()
    body = json.loads(report(clock))
    body["measured_at"] = "2026-10-15T12:00:00"
    assert rejected(ingest.accept, NATIVE_ID, "android", json.dumps(body).encode()) == (422, "invalid_timestamp")


def test_replay_and_out_of_order_and_rate_limit():
    registry, ingest, clock = setup()
    first = report(clock, sequence=5)
    ingest.accept(NATIVE_ID, "android", first)
    clock.advance(20)
    assert rejected(ingest.accept, NATIVE_ID, "android", first) == (409, "replayed_or_out_of_order")
    older = report(clock, sequence=6, measured=clock.wall - timedelta(seconds=30))
    assert rejected(ingest.accept, NATIVE_ID, "android", older) == (409, "replayed_or_out_of_order")
    ingest.accept(NATIVE_ID, "android", report(clock, sequence=6))
    clock.advance(3)
    with pytest.raises(TelemetryRejected) as error:
        ingest.accept(NATIVE_ID, "android", report(clock, sequence=7))
    assert (error.value.status, error.value.retry_after) == (429, 12)
    assert sensor(device(registry), "battery_percent").value == 76.0


# ----- API ------------------------------------------------------------------------------------

def _pair(store, client, scopes, name, kind):
    from app.auth import SESSION_COOKIE
    profile = "android-readonly" if kind == "android" else "web"
    code, _ = store.create_pairing_window(profile=profile)
    result = store.pair(code, name, name, profile=profile, scopes=scopes, session_kind=kind)
    client.cookies.clear()
    if kind == "web":
        client.cookies.set(SESSION_COOKIE, result.token)
        client.cookies.set("roomos_csrf", result.csrf_token)
    return result


def bind(bindings, settings):
    from app.main import app
    from app.windows_telemetry import configured_catalog as windows_catalog

    parsed = parse_bindings(json.dumps(bindings))
    catalog, phone = configured_catalog(windows_catalog(None)[0], parsed)
    app.state.devices.stop()  # the lifespan awaits the original registry loop at shutdown
    app.state.devices = DeviceRegistry(settings.device_stale_thresholds(), app.state.devices.publish,
                                       catalog=catalog, bindings=phone)
    app.state.client_telemetry = ClientTelemetryIngest(app.state.devices, parsed)


def native_post(client, token, body, **headers):
    return client.post("/api/v1/native/devices/telemetry", content=body,
                       headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"} | headers)


def test_api_native_ingest_requires_scope_binding_and_native_session(auth_client):
    client, ha, store, settings = auth_client
    clock = Clock()
    clock.wall = datetime.now(UTC)
    reader = _pair(store, client, ("devices:read", "system:read", "lights:read"), "Phone Native", "android")
    writer = _pair(store, client, ("devices:read", "telemetry:write:self"), "Phone Native 2", "android")
    bind({reader.context.device_id: {"device": "phone", "app": "android"},
          writer.context.device_id: {"device": "tablet", "app": "android"}}, settings)
    assert client.post("/api/v1/native/devices/telemetry", content=report(clock)).status_code == 401
    # devices:read is read-only: never a write permission.
    assert native_post(client, reader.token, report(clock)).status_code == 403
    response = native_post(client, writer.token, report(clock))
    assert response.status_code == 202 and response.json()["device_id"] == "tablet"
    assert set(response.json()) == {"device_id", "stale_after_seconds", "received_at"}
    assert response.headers["cache-control"].startswith("no-store")
    tab = client.get("/api/v1/devices/tablet", headers={"Authorization": f"Bearer {writer.token}"}).json()
    assert tab["connection"] == "online"
    assert next(s for s in tab["sensors"] if s["id"] == "battery_percent")["value"] == 76.0
    handset = client.get("/api/v1/devices/phone", headers={"Authorization": f"Bearer {writer.token}"}).json()
    assert handset["connection"] == "unknown"  # nothing was written for the other phone
    later = report(clock, sequence=2, measured=clock.wall + timedelta(seconds=1))
    assert native_post(client, writer.token, later).status_code == 429
    assert native_post(client, writer.token, report(clock), Origin="https://evil.example").status_code == 403
    assert client.post("/api/v1/native/devices/telemetry?x=1", content=report(clock),
                       headers={"Authorization": f"Bearer {writer.token}"}).status_code == 422
    big = b"{" + b" " * (MAX_BODY_BYTES + 10) + b"}"
    assert native_post(client, writer.token, big).status_code == 413
    assert not ha.calls


def test_api_unbound_or_revoked_writer_is_rejected(auth_client):
    client, _, store, settings = auth_client
    clock = Clock()
    clock.wall = datetime.now(UTC)
    writer = _pair(store, client, ("telemetry:write:self",), "Unbound", "android")
    bind({}, settings)
    assert native_post(client, writer.token, report(clock)).status_code == 403
    bound = _pair(store, client, ("telemetry:write:self",), "Bound", "android")
    bind({bound.context.device_id: {"device": "phone", "app": "android"}}, settings)
    store.revoke_device(bound.context.device_id)
    assert native_post(client, bound.token, report(clock)).status_code == 401


def test_api_pwa_ingest_needs_csrf_and_web_session(auth_client):
    client, _, store, settings = auth_client
    clock = Clock()
    clock.wall = datetime.now(UTC)
    tab = _pair(store, client, ("devices:read", "telemetry:write:self"), "Tab", "web")
    bind({tab.context.device_id: {"device": "tablet", "app": "web"}}, settings)
    body = report(clock, sensors={"battery_percent": 64, "network_connected": True})
    origin = {"Origin": settings.auth_allowed_origin, "Content-Type": "application/json"}
    assert client.post("/api/v1/devices/self/telemetry", content=body, headers=origin).status_code == 403  # no CSRF
    response = client.post("/api/v1/devices/self/telemetry", content=body,
                           headers=origin | {"X-CSRF-Token": tab.csrf_token})
    assert response.status_code == 202 and response.json()["device_id"] == "tablet"
    # The PWA session cannot use the native route (session kind mismatch).
    assert client.post("/api/v1/native/devices/telemetry", content=report(clock, sequence=2),
                       headers={"Content-Type": "application/json"}).status_code == 403


def test_operator_scope_change_is_atomic_audited_and_keeps_sessions(auth_client, caplog):
    client, _, store, _ = auth_client
    phone = _pair(store, client, ("lights:read", "system:read"), "Phone Native", "android")
    with caplog.at_level("INFO", logger="roomos.security"):
        before, after = store.change_scope(phone.context.device_id, "telemetry:write:self", add=True)
    assert before == ["lights:read", "system:read"] and after == before + ["telemetry:write:self"]
    assert "device_scope_added scope=telemetry:write:self" in caplog.text and phone.token not in caplog.text
    assert store.change_scope(phone.context.device_id, "telemetry:write:self", add=True)[1] == after  # idempotent
    assert store.authenticate(phone.token).scopes == frozenset(after)  # same session, no re-pairing
    assert store.change_scope(phone.context.device_id, "telemetry:write:self", add=False)[1] == before
    with pytest.raises(ValueError):
        store.change_scope(phone.context.device_id, "telemetry:write:other", add=True)
    store.revoke_device(phone.context.device_id)
    assert store.change_scope(phone.context.device_id, "devices:read", add=True) is None
    assert store.change_scope("00000000-0000-0000-0000-000000000000", "devices:read", add=True) is None


def test_phone2_has_its_real_name_and_keeps_the_stable_id():
    registry, _, _ = setup({OTHER_ID: {"device": "phone_2", "app": "web"}})
    phone2 = device(registry, "phone_2")
    assert phone2.name == "Second phone" and phone2.id == "phone_2"
    assert [s.kind for s in phone2.sources] == ["roomos_web"] and phone2.sources[0].configured
