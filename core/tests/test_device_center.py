"""Phase 7D.4 Device Center API: list with sensors and paired RoomOS clients (synthetic data)."""
import json
import sqlite3
import time

from test_auth import auth_client  # noqa: F401 - shared isolated auth fixture
from test_devices import primary_readings


def test_list_carries_sensors_identical_to_detail(client):
    from app.main import app
    app.state.devices.ingest("system_monitor", "primary", primary_readings(), connection="online")
    listed = {d["id"]: d for d in client.get("/api/v1/devices").json()["devices"]}
    assert set(listed) == {"primary", "secondary", "workstation", "phone", "tablet", "phone_2", "roomos_panel_phone"}
    for device_id, device in listed.items():
        detail = client.get(f"/api/v1/devices/{device_id}").json()
        # Same shape as a device.updated event; only receipt-free fields are compared.
        assert device["sensors"] == detail["sensors"] and device["connection"] == detail["connection"]
    cpu = next(s for s in listed["primary"]["sensors"] if s["id"] == "cpu_usage")
    assert cpu["availability"] == "available" and cpu["measured_at"] is None
    unsupported = next(s for s in listed["primary"]["sensors"] if s["id"] == "battery_percent")
    assert (unsupported["support"], unsupported["value"]) == ("unsupported", None)


def _session(store, client, scopes, name, profile="web"):
    from app.auth import SESSION_COOKIE
    code, _ = store.create_pairing_window(profile=profile)
    kind = "android" if profile == "android-readonly" else "web"  # as the native pairing route does
    result = store.pair(code, name, name, profile=profile, scopes=scopes, session_kind=kind)
    client.cookies.set(SESSION_COOKIE, result.token)
    return result


def test_clients_require_devices_read_strictly(auth_client):
    client, ha, store, _ = auth_client
    assert client.get("/api/v1/clients").status_code == 401
    _session(store, client, ("lights:read",), "Lights only")
    assert client.get("/api/v1/clients").status_code == 403
    # system:read lists servers in /devices but is NOT enough for the client inventory.
    _session(store, client, ("system:read", "lights:read"), "System reader")
    assert client.get("/api/v1/devices").status_code == 200
    assert client.get("/api/v1/clients").status_code == 403
    _session(store, client, ("devices:read",), "Device reader")
    assert client.get("/api/v1/clients").status_code == 200
    assert client.get("/api/v1/clients?all=1").status_code == 422
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)("/api/v1/clients").status_code == 405
    assert not ha.calls


def test_clients_list_is_minimal_and_excludes_revoked(auth_client, tmp_path):
    client, _, store, _ = auth_client
    phone = _session(store, client, ("lights:read", "system:read"), "Phone")
    native = _session(store, client, ("lights:read", "system:read"), "Phone Native", profile="android-readonly")
    gone = _session(store, client, ("system:read",), "Old tablet")
    store.revoke_device(gone.context.device_id)
    disabled = _session(store, client, ("system:read",), "Disabled panel")
    store.disable_device(disabled.context.device_id)
    me = _session(store, client, ("devices:read", "system:read"), "Operator <b>&")

    body = client.get("/api/v1/clients").json()
    names = [c["name"] for c in body["clients"]]
    assert names[0] == "Operator <b>&" and body["clients"][0]["current"] is True  # verbatim; UI escapes
    assert set(names) == {"Operator <b>&", "Phone", "Phone Native"}
    by_name = {c["name"]: c for c in body["clients"]}
    assert by_name["Phone"]["app"] == "web" and by_name["Phone Native"]["app"] == "android"
    assert all(c["active_session"] for c in body["clients"]) and body["last_seen_resolution_seconds"] == 300
    for c in body["clients"]:
        assert set(c) == {"id", "name", "app", "paired_at", "last_seen_at", "active_session", "current"}
        assert len(c["id"]) == 16
    text = json.dumps(body)
    for secret in (phone.token, native.token, me.token, phone.context.device_id, me.context.session_id,
                   "scopes", "lights:write", "token", "csrf"):
        assert secret not in text


def test_clients_without_live_session_are_inactive_not_hidden(auth_client):
    client, _, store, settings = auth_client
    stale = _session(store, client, ("system:read",), "Expired tablet")
    with sqlite3.connect(settings.auth_database_path) as db:
        db.execute("UPDATE sessions SET absolute_expires_at=? WHERE device_id=?", (int(time.time()) - 1, stale.context.device_id))
    _session(store, client, ("devices:read",), "Reader")
    expired = next(c for c in client.get("/api/v1/clients").json()["clients"] if c["name"] == "Expired tablet")
    assert (expired["active_session"], expired["app"]) == (False, "unknown")


def test_client_ids_are_stable_and_not_raw_ids(auth_client):
    client, _, store, _ = auth_client
    reader = _session(store, client, ("devices:read",), "Reader")
    first = client.get("/api/v1/clients").json()["clients"][0]["id"]
    assert first == client.get("/api/v1/clients").json()["clients"][0]["id"]
    assert first not in reader.context.device_id.replace("-", "")
