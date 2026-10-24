"""Native alarm writes require an Android Bearer session, never a web cookie."""

from app.alerts import AlertObservation, AlertRule
from app.main import app
from test_auth import auth_client as _auth_client, pair_native  # noqa: F401
from test_push_api import ORIGIN, use, web_device


def _active_alert(client):
    rule = AlertRule(
        key="resource.workstation.disk_usage", severity="warning", type="resource.high",
        title="Workstation disk usage is high", message="disk usage is 87%",
        source="workstation", failure_seconds=0, recovery_seconds=0,
    )
    client.portal.call(app.state.alerts.evaluate_observations, [AlertObservation(state="failed", rule=rule)])
    return rule.key


def test_native_snooze_requires_android_bearer_and_scope(_auth_client):
    client, _ha, store, _settings = _auth_client
    alert_id = _active_alert(client)
    native = pair_native(client, store).json()
    device_id = native["device"]["id"]
    bearer = {"Authorization": f"Bearer {native['session_token']}"}
    path = f"/api/v1/native/notifications/{alert_id}/snooze"
    client.cookies.clear()
    assert client.post(path, json={"hours": 4}, headers=bearer).status_code == 403
    store.change_scope(device_id, "alerts:snooze", add=True)
    assert client.post(path, json={"hours": 4}, headers={**bearer, "Origin": "https://evil.example"}).status_code == 403
    assert client.post(path, json={"hours": 3}, headers=bearer).status_code == 422
    response = client.post(path, json={"hours": 4}, headers=bearer)
    assert response.status_code == 200 and response.json()["severity"] == "warning"
    assert client.post(path, json={"hours": 4}, headers=bearer).json()["until"] == response.json()["until"]
    assert app.state.alerts.alert_by_id(alert_id).status == "active"
    assert client.delete(path, headers=bearer).json()["cleared"] is True
    web_id, jar = web_device(client, store, "Phone Chrome")
    store.change_scope(web_id, "alerts:snooze", add=True)
    assert client.post(path, json={"hours": 1}, headers=use(client, jar)).status_code == 403
    assert client.post(path, json={"hours": 1}, headers={"Authorization": f"Bearer {jar['session']}"}).status_code == 403


def test_native_profile_targets_one_device_only(_auth_client):
    client, _ha, store, _settings = _auth_client
    native = pair_native(client, store).json()
    device_id = native["device"]["id"]
    bearer = {"Authorization": f"Bearer {native['session_token']}"}
    path = "/api/v1/native/alerts/profiles/workstation"
    client.cookies.clear()
    assert client.put(path, json={"profile": "gaming", "hours": 4}, headers=bearer).status_code == 403
    store.change_scope(device_id, "alerts:profile", add=True)
    assert client.put(path, json={"profile": "gaming", "hours": 4}, headers=bearer).status_code == 200
    first_until = client.get("/api/v1/alerts/profiles", headers=bearer).json()["devices"]["workstation"]["until"]
    assert client.put(path, json={"profile": "gaming", "hours": 4}, headers=bearer).json()["until"] == first_until
    profiles = client.get("/api/v1/alerts/profiles", headers=bearer).json()["devices"]
    assert profiles["workstation"]["profile"] == "gaming"
    assert profiles["primary"]["profile"] == profiles["secondary"]["profile"] == "normal"
    assert client.put("/api/v1/native/alerts/profiles/router", json={"profile": "gaming"}, headers=bearer).status_code == 422
    assert client.put(path, json={"profile": "normal"}, headers=bearer).status_code == 200
    assert client.get("/api/v1/alerts/profiles", headers=bearer).json()["devices"]["workstation"]["profile"] == "normal"


def test_native_alert_writes_are_bounded_per_device(_auth_client):
    client, _ha, store, _settings = _auth_client
    native = pair_native(client, store).json()
    store.change_scope(native["device"]["id"], "alerts:snooze", add=True)
    bearer = {"Authorization": f"Bearer {native['session_token']}"}
    client.cookies.clear()
    path = "/api/v1/native/notifications/nonexistent-alert/snooze"
    assert [client.post(path, json={"hours": 1}, headers=bearer).status_code for _ in range(20)] == [404] * 20
    assert client.post(path, json={"hours": 1}, headers=bearer).status_code == 429
