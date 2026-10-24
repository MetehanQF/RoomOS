"""7G push API: device-bound, least-privilege, CSRF/origin-protected, secret-free responses."""

import json
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.auth import CSRF_COOKIE, SESSION_COOKIE
from app.main import app
from app.push_providers import b64url, generate_vapid_private_key
from test_auth import auth_client as _auth_client, pair, pair_native  # noqa: F401

ORIGIN = "https://roomos.example.com"
ENDPOINT = "https://fcm.googleapis.com/fcm/send/fixture-endpoint-1"


@pytest.fixture
def push_client(monkeypatch, tmp_path, fake_ha):
    monkeypatch.setenv("ROOMOS_PUSH_MODE", "smart")
    generate_vapid_private_key(os.environ["ROOMOS_WEBPUSH_VAPID_PRIVATE_KEY_PATH"])
    for value in _auth_client.__wrapped__(fake_ha, monkeypatch, tmp_path):
        # Never let a test reach a real push service: replace providers with recorders.
        recorder = Recorder()
        app.state.push.engine.providers = {"webpush": recorder, "fcm": recorder}
        app.state.push.recorder = recorder
        yield value


class Recorder:
    kind = "webpush"

    def __init__(self):
        self.sent = []

    async def send(self, message, device, ttl):
        self.sent.append((message.device_id, message.transition, message.title, message.body))
        return "accepted", None


def keys():
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {"p256dh": b64url(public), "auth": b64url(os.urandom(16))}


def web_device(client, store, name):
    client.cookies.clear()
    device_id = pair(client, store, name).json()["device"]["id"]
    jar = {"session": client.cookies.get(SESSION_COOKIE), "csrf": client.cookies.get(CSRF_COOKIE)}
    return device_id, jar


def use(client, jar):
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, jar["session"], domain="roomos.example.com", path="/api")
    client.cookies.set(CSRF_COOKIE, jar["csrf"], domain="roomos.example.com", path="/")
    return {"Origin": ORIGIN, "X-CSRF-Token": jar["csrf"]}


def assert_secret_free(text, *secrets):
    for value in secrets:
        assert value not in text


def test_settings_are_readable_but_not_writable_without_notifications_self(push_client):
    client, _ha, store, _settings = push_client
    device_id, jar = web_device(client, store, "Second phone")
    headers = use(client, jar)
    view = client.get("/api/v1/push/settings")
    assert view.status_code == 200 and view.headers["cache-control"].startswith("no-store")
    body = view.json()
    assert body["device_id"] == device_id and body["can_manage"] is False and body["session_kind"] == "web"
    assert body["can_receive"] is False  # no activity:read -> never receives alert content
    assert body["capabilities"]["mode"] == "smart" and body["capabilities"]["webpush_available"] is True
    assert body["preferences"]["enabled"] is False  # opt-in
    assert client.put("/api/v1/push/settings", json={"enabled": True}, headers=headers).status_code == 403
    sub = {"endpoint": ENDPOINT, "keys": keys()}
    assert client.put("/api/v1/push/webpush/subscription", json=sub, headers=headers).status_code == 403


def test_manage_scope_with_csrf_updates_only_own_device_and_never_echoes_secrets(push_client):
    client, _ha, store, _settings = push_client
    a_id, a_jar = web_device(client, store, "Second phone")
    b_id, b_jar = web_device(client, store, "Wall tablet")
    store.change_scope(a_id, "notifications:self", add=True)
    headers = use(client, a_jar)
    assert client.put("/api/v1/push/settings", json={"enabled": True}, headers={"Origin": ORIGIN}).status_code == 403
    assert client.put("/api/v1/push/settings", json={"enabled": True},
                      headers={"Origin": "https://evil.example", "X-CSRF-Token": a_jar["csrf"]}).status_code == 403
    assert client.put("/api/v1/push/settings", json={"enabled": True, "device_id": b_id}, headers=headers).status_code == 422
    updated = client.put("/api/v1/push/settings", json={
        "enabled": True, "info": True, "recovery": "all", "lock_screen_details": True,
        "quiet_hours": {"enabled": True, "start": "23:30", "end": "07:00", "allow_critical": False},
    }, headers=headers)
    assert updated.status_code == 200 and updated.json()["preferences"]["quiet_hours"]["start"] == "23:30"
    k = keys()
    subscribed = client.put("/api/v1/push/webpush/subscription", json={"endpoint": ENDPOINT, "keys": k}, headers=headers)
    assert subscribed.status_code == 200
    assert subscribed.json()["channels"] == [{"kind": "webpush", "status": "active", "since": subscribed.json()["channels"][0]["since"],
                                              "last_accepted_at": None, "endpoint_host": "fcm.googleapis.com"}]
    assert_secret_free(subscribed.text, ENDPOINT, k["p256dh"], k["auth"], a_jar["session"])
    registry = json.loads(Path(os.environ["ROOMOS_PUSH_REGISTRY_PATH"]).read_text())
    assert os.stat(os.environ["ROOMOS_PUSH_REGISTRY_PATH"]).st_mode & 0o777 == 0o600
    assert set(registry["devices"]) == {a_id}  # the other device was never touched
    use(client, b_jar)
    other = client.get("/api/v1/push/settings").json()
    assert other["channels"] == [] and other["preferences"]["enabled"] is False


@pytest.mark.parametrize("endpoint", ["http://fcm.googleapis.com/x", "https://192.0.2.20:8123/api/x",
                                      "https://roomos.example.com/api/v1/x", "https://evil.example/push"])
def test_subscription_endpoint_must_be_a_known_push_service(push_client, endpoint):
    client, _ha, store, _settings = push_client
    device_id, jar = web_device(client, store, "Tab")
    store.change_scope(device_id, "notifications:self", add=True)
    response = client.put("/api/v1/push/webpush/subscription", json={"endpoint": endpoint, "keys": keys()}, headers=use(client, jar))
    assert response.status_code == 422


def test_native_session_cannot_use_web_subscription_and_web_cannot_use_native_settings(push_client):
    client, _ha, store, _settings = push_client
    native = pair_native(client, store).json()
    store.change_scope(native["device"]["id"], "notifications:self", add=True)
    bearer = {"Authorization": f"Bearer {native['session_token']}"}
    client.cookies.clear()
    assert client.put("/api/v1/native/push/settings", json={"enabled": True}, headers=bearer).status_code == 200
    assert client.put("/api/v1/native/push/settings", json={"enabled": True},
                      headers={**bearer, "Origin": "https://evil.example"}).status_code == 403
    web_id, jar = web_device(client, store, "Tab")
    store.change_scope(web_id, "notifications:self", add=True)
    use(client, jar)
    # A web cookie session is not an android session, even with the scope.
    assert client.put("/api/v1/native/push/settings", json={"enabled": True}).status_code == 403


def test_rotation_requires_same_origin_and_proof_of_current_endpoint(push_client):
    client, _ha, store, _settings = push_client
    device_id, jar = web_device(client, store, "Tab")
    store.change_scope(device_id, "notifications:self", add=True)
    headers = use(client, jar)
    client.put("/api/v1/push/webpush/subscription", json={"endpoint": ENDPOINT, "keys": keys()}, headers=headers)
    new = {"endpoint": ENDPOINT + "-rotated", "keys": keys()}
    assert client.post("/api/v1/push/webpush/rotate", json={"old_endpoint": ENDPOINT, "subscription": new}).status_code == 403
    assert client.post("/api/v1/push/webpush/rotate", json={"old_endpoint": ENDPOINT + "-guess", "subscription": new},
                       headers={"Origin": ORIGIN}).status_code == 409
    assert client.post("/api/v1/push/webpush/rotate", json={"old_endpoint": ENDPOINT, "subscription": new},
                       headers={"Origin": ORIGIN}).status_code == 204
    registry = json.loads(Path(os.environ["ROOMOS_PUSH_REGISTRY_PATH"]).read_text())
    assert registry["devices"][device_id]["webpush"]["endpoint"] == new["endpoint"]


def test_unsubscribe_needs_csrf_but_no_scope_and_receipts_are_device_bound(push_client):
    client, _ha, store, _settings = push_client
    device_id, jar = web_device(client, store, "Tab")
    store.change_scope(device_id, "notifications:self", add=True)
    headers = use(client, jar)
    client.put("/api/v1/push/webpush/subscription", json={"endpoint": ENDPOINT, "keys": keys()}, headers=headers)
    store.change_scope(device_id, "notifications:self", add=False)
    assert client.delete("/api/v1/push/webpush/subscription", headers={"Origin": ORIGIN}).status_code == 403
    assert client.delete("/api/v1/push/webpush/subscription", headers=headers).json()["channels"] == []
    receipt = {"message_id": "psh_" + "a" * 32, "event": "opened"}
    assert client.post("/api/v1/push/receipts", json=receipt).status_code == 403          # no Origin
    assert client.post("/api/v1/push/receipts", json=receipt, headers={"Origin": ORIGIN}).status_code == 404


def test_anonymous_requests_are_rejected(push_client):
    client, _ha, _store, _settings = push_client
    client.cookies.clear()
    assert client.get("/api/v1/push/settings").status_code == 401
    assert client.put("/api/v1/push/settings", json={"enabled": True}, headers={"Origin": ORIGIN}).status_code == 401
    assert client.get("/api/v1/native/push/settings").status_code == 401


def test_smart_mode_keeps_all_background_tasks_and_legacy_policy_in_shadow(push_client):
    client, _ha, _store, _settings = push_client
    names = {name for name, _state in app.state.background_tasks.snapshot()}
    assert len(names) == 9 and {"notification_dispatcher", "smart_automations"} <= names
    assert app.state.notification_policy.sink.__name__ == "shadow_intent_sink"


def test_legacy_mode_is_exact_7f_wiring(_auth_client):
    client, _ha, _store, _settings = _auth_client
    assert app.state.notification_policy.sink == app.state.notifications.handle_production_intent
    assert app.state.alerts._lifecycle_sinks == []
    assert app.state.push.engine.configuration_status == "disabled"


def test_alert_lifecycle_reaches_only_opted_in_authorized_device_end_to_end(push_client):
    from app.alerts import AlertObservation, AlertRule
    client, _ha, store, _settings = push_client
    a_id, a_jar = web_device(client, store, "Tab")
    b_id, b_jar = web_device(client, store, "Phone 2")
    for device_id, jar, enabled in ((a_id, a_jar, True), (b_id, b_jar, False)):
        store.change_scope(device_id, "notifications:self", add=True)
        store.change_scope(device_id, "activity:read", add=True)
        headers = use(client, jar)
        client.put("/api/v1/push/webpush/subscription", json={"endpoint": f"{ENDPOINT}-{device_id}", "keys": keys()}, headers=headers)
        client.put("/api/v1/push/settings", json={"enabled": enabled}, headers=headers)
    rule = AlertRule(key="resource.workstation.disk_usage", severity="warning", type="resource.high",
                     title="Workstation disk usage is high", message="disk usage is 87%; sustained high usage requires attention.",
                     source="workstation", failure_seconds=0, recovery_seconds=0)
    for _ in range(50):
        client.portal.call(app.state.alerts.evaluate_observations, [AlertObservation(state="failed", rule=rule)])
    client.portal.call(app.state.push.engine.dispatch_pending)
    assert app.state.push.recorder.sent == [(a_id, "started", "RoomOS — Uyarı", "Workstation: C: sürücüsü %87 dolu.")]
    use(client, a_jar)
    recent = client.get("/api/v1/push/settings").json()["recent"]
    assert [(r["transition"], r["status"]) for r in recent] == [("started", "accepted")]
    store.revoke_device(a_id)
    client.portal.call(app.state.alerts.evaluate_observations, [AlertObservation(state="healthy", rule=rule)])
    client.portal.call(app.state.push.engine.dispatch_pending)
    assert len(app.state.push.recorder.sent) == 1  # revoked device gets no recovery


@pytest.mark.asyncio
async def test_existing_7f_handset_fcm_registration_is_adopted_once_with_its_enabled_state(tmp_path):
    from datetime import UTC, datetime
    from app.config import Settings
    from app.fcm import DestinationFile, FcmTargetFile, FcmTargetMetadata, _write_private_json
    from app.notifications import NotificationDestination
    from app.push import PushEngine, PushPreferencesUpdate
    from app.push_runtime import adopt_legacy_fcm
    now = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)
    settings = Settings(ROOMOS_FCM_TARGETS_PATH=str(tmp_path / "t.json"), ROOMOS_NOTIFICATION_DESTINATIONS_PATH=str(tmp_path / "d.json"))
    _write_private_json(settings.fcm_targets_path, FcmTargetFile(targets={"android-abc": "TOKEN"}, registrations={
        "android-abc": FcmTargetMetadata(device_id="handset", registered_at=now, token_fingerprint="f" * 64)}))
    _write_private_json(settings.notification_destinations_path, DestinationFile(destinations=[NotificationDestination(
        id="fcm-abc", display_name="Phone notifications", enabled=True, activated_at=now,
        provider="fcm", provider_target_ref="android-abc")]))
    before = (Path(settings.fcm_targets_path).read_bytes(), Path(settings.notification_destinations_path).read_bytes())
    engine = PushEngine(str(tmp_path / "r.json"), str(tmp_path / "o.json"))
    assert await adopt_legacy_fcm(engine, settings) == 1
    device = engine.device("handset")
    assert device.display_name == "Phone" and device.preferences.enabled is True
    assert device.fcm.target_ref == "android-abc" and "TOKEN" not in Path(tmp_path / "r.json").read_text()
    await engine.update_preferences("handset", "Phone", PushPreferencesUpdate(enabled=False))
    assert await adopt_legacy_fcm(engine, settings) == 0 and engine.device("handset").preferences.enabled is False
    assert (Path(settings.fcm_targets_path).read_bytes(), Path(settings.notification_destinations_path).read_bytes()) == before
