import json
import os
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.auth import CSRF_COOKIE, SESSION_COOKIE
from app.config import Settings, get_settings
from app.main import app, get_ha


@pytest.fixture
def auth_client(fake_ha, monkeypatch, tmp_path):
    settings = Settings(
        ROOMOS_AUTH_ENABLED=True,
        ROOMOS_AUTH_DATABASE_PATH=str(tmp_path / "auth.db"),
        ROOMOS_AUTH_SECRET_PATH=str(tmp_path / "auth.secret"),
        ROOMOS_AUTH_ALLOWED_ORIGIN="https://roomos.example.com",
        ROOMOS_AUTH_ALLOWED_HOST="roomos.example.com",
        ROOMOS_CONTROL_ENABLED=False,
        ROOMOS_CONTROL_POLICY="read_only",
        ROOMOS_AUTH_PAIRING_ATTEMPTS=5,
        ROOMOS_ROUTINES_PATH=str(tmp_path / "routines.json"),
        ROOMOS_AUTOMATIONS_PATH=str(tmp_path / "automations.json"),
        ROOMOS_AUTOMATION_HISTORY_PATH=str(tmp_path / "automation-history.jsonl"),
        ROOMOS_SCENES_CONFIG_PATH=str(tmp_path / "scenes.json"),
        ROOMOS_SCENE_STATE_PATH=str(tmp_path / "scene-state.json"),
        ROOMOS_ALERTS_PATH=str(tmp_path / "alerts.json"),
        ROOMOS_NOTIFICATIONS_PATH=str(tmp_path / "notifications.json"),
    )
    for key, value in {
        "ROOMOS_AUTH_ENABLED": "true",
        "ROOMOS_AUTH_DATABASE_PATH": settings.auth_database_path,
        "ROOMOS_AUTH_SECRET_PATH": settings.auth_secret_path,
        "ROOMOS_AUTH_ALLOWED_ORIGIN": settings.auth_allowed_origin,
        "ROOMOS_AUTH_ALLOWED_HOST": settings.auth_allowed_host,
        "ROOMOS_CONTROL_ENABLED": "false",
        "ROOMOS_CONTROL_POLICY": "read_only",
        "ROOMOS_ROUTINES_PATH": settings.routines_path,
        "ROOMOS_AUTOMATIONS_PATH": settings.automations_path,
        "ROOMOS_AUTOMATION_HISTORY_PATH": settings.automation_history_path,
        "ROOMOS_SCENES_CONFIG_PATH": settings.scenes_config_path,
        "ROOMOS_SCENE_STATE_PATH": settings.scene_state_path,
        "ROOMOS_ALERTS_PATH": settings.alerts_path,
        "ROOMOS_NOTIFICATIONS_PATH": settings.notifications_path,
    }.items():
        monkeypatch.setenv(key, str(value))
    get_settings.cache_clear()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_ha] = lambda: fake_ha
    with TestClient(app, base_url="https://roomos.example.com") as client:
        yield client, fake_ha, app.state.auth, settings
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def pair(client, store, name="Wall tablet"):
    code, _ = store.create_pairing_window()
    response = client.post(
        "/api/v1/auth/pair",
        json={"code": code, "friendly_name": name},
        headers={"Origin": "https://roomos.example.com"},
    )
    assert response.status_code == 200
    return response


def csrf_headers(client):
    return {
        "Origin": "https://roomos.example.com",
        "X-CSRF-Token": client.cookies.get(CSRF_COOKIE),
    }


def pair_native(client, store, name="Phone"):
    code, _ = store.create_pairing_window(profile="android-readonly")
    response = client.post(
        "/api/v1/auth/native/pair",
        json={"code": code, "friendly_name": name},
    )
    assert response.status_code == 200
    return response


def test_native_pairing_is_profile_bound_read_only_and_bearer_authenticated(auth_client):
    client, fake_ha, store, settings = auth_client
    response = pair_native(client, store)
    payload = response.json()
    token = payload["session_token"]
    expected_scopes = {
        "lights:read",
        "system:read",
        "camera:read",
        "activity:read",
        "routines:read",
    }
    assert set(payload["device"]["scopes"]) == expected_scopes
    assert "lights:write" not in payload["device"]["scopes"]
    assert "routines:write" not in payload["device"]["scopes"]
    assert response.headers["cache-control"].startswith("no-store")
    assert response.headers.get_list("set-cookie") == []
    with sqlite3.connect(settings.auth_database_path) as db:
        db.row_factory = sqlite3.Row
        session = db.execute("SELECT * FROM sessions").fetchone()
        device = db.execute("SELECT * FROM devices").fetchone()
    assert session["kind"] == "android"
    assert token not in session["token_digest"]
    assert set(json.loads(device["scopes"])) == set(payload["device"]["scopes"])

    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/v1/auth/status", headers=headers).status_code == 200
    assert client.get("/api/v1/lights", headers=headers).status_code == 200
    assert client.get("/api/v1/system", headers=headers).status_code == 200
    assert client.get("/api/v1/alerts", headers=headers).status_code == 200
    assert client.get("/api/v1/cameras", headers=headers).status_code == 200
    assert client.get("/api/v1/routines", headers=headers).status_code == 200
    assert client.post(
        "/api/v1/lights/room_main/power",
        json={"on": True},
        headers=headers,
    ).status_code == 403
    assert fake_ha.calls == []


def test_native_pairing_rejects_wrong_profile_origin_expiry_and_bruteforce(auth_client):
    client, _, store, settings = auth_client
    web_code, _ = store.create_pairing_window(profile="web")
    assert client.post(
        "/api/v1/auth/native/pair",
        json={"code": web_code, "friendly_name": "Phone"},
    ).status_code == 400

    native_code, _ = store.create_pairing_window(profile="android-readonly")
    assert client.post(
        "/api/v1/auth/pair",
        json={"code": native_code, "friendly_name": "Browser"},
        headers={"Origin": settings.auth_allowed_origin},
    ).status_code == 400
    assert client.post(
        "/api/v1/auth/native/pair",
        json={"code": native_code, "friendly_name": "Phone"},
        headers={"Origin": "https://evil.example"},
    ).status_code == 403

    expired, _ = store.create_pairing_window(profile="android-readonly")
    with sqlite3.connect(settings.auth_database_path) as db:
        db.execute("UPDATE pairing_windows SET expires_at=?", (int(time.time()) - 1,))
    assert client.post(
        "/api/v1/auth/native/pair",
        json={"code": expired, "friendly_name": "Phone"},
    ).status_code == 400

    valid, _ = store.create_pairing_window(profile="android-readonly")
    for _ in range(settings.auth_pairing_attempts - 2):
        assert client.post(
            "/api/v1/auth/native/pair",
            json={"code": "AAAA-BBBB-CCCC", "friendly_name": "Phone"},
        ).status_code == 400
    assert client.post(
        "/api/v1/auth/native/pair",
        json={"code": valid, "friendly_name": "Phone"},
    ).status_code == 429


def test_native_session_revocation_expiry_malformed_auth_and_websocket(auth_client):
    client, _, store, settings = auth_client
    payload = pair_native(client, store).json()
    token = payload["session_token"]
    device_id = payload["device"]["id"]
    headers = {"Authorization": f"Bearer {token}"}
    with client.websocket_connect(
        "/api/v1/native/events",
        headers={
            **headers,
            "Host": settings.auth_allowed_host,
            "X-Forwarded-Proto": "https",
        },
    ) as socket:
        assert socket.receive_json()["type"] == "roomos.connection"

    with pytest.raises(WebSocketDisconnect) as insecure_native:
        with client.websocket_connect(
            "/api/v1/native/events",
            headers={**headers, "Host": settings.auth_allowed_host},
        ):
            pass
    assert insecure_native.value.code == 4403

    with pytest.raises(WebSocketDisconnect) as web_origin_required:
        with client.websocket_connect(
            "/api/v1/events",
            headers={
                **headers,
                "Host": settings.auth_allowed_host,
                "X-Forwarded-Proto": "https",
            },
        ):
            pass
    assert web_origin_required.value.code == 4403
    assert client.get(
        "/api/v1/lights", headers={"Authorization": "Token malformed"}
    ).status_code == 401

    session_id = token.split(".", 1)[0]
    with sqlite3.connect(settings.auth_database_path) as db:
        db.execute(
            "UPDATE sessions SET sliding_expires_at=? WHERE id=?",
            (int(time.time()) - 1, session_id),
        )
    assert client.get("/api/v1/lights", headers=headers).status_code == 401

    payload = pair_native(client, store, "Phone replacement").json()
    token = payload["session_token"]
    device_id = payload["device"]["id"]
    assert store.revoke_sessions(device_id)
    assert client.get(
        "/api/v1/lights", headers={"Authorization": f"Bearer {token}"}
    ).status_code == 401


def test_unauthenticated_rest_and_websocket_are_rejected(auth_client):
    client, fake_ha, _, _ = auth_client
    assert client.get("/api/v1/lights").status_code == 401
    assert client.post("/api/v1/lights/room_main/power", json={"on": True}).status_code == 401
    with pytest.raises(WebSocketDisconnect) as rejected:
        with client.websocket_connect(
            "/api/v1/events", headers={"Host": "roomos.example.com", "Origin": "https://roomos.example.com", "X-Forwarded-Proto": "https"}
        ):
            pass
    assert rejected.value.code == 4401
    assert fake_ha.calls == []


def test_pairing_issues_secure_cookie_and_single_use_session(auth_client):
    client, _, store, settings = auth_client
    code, _ = store.create_pairing_window()
    response = client.post(
        "/api/v1/auth/pair",
        json={"code": code, "friendly_name": "Tablet"},
        headers={"Origin": "https://roomos.example.com"},
    )
    assert response.status_code == 200
    cookies = response.headers.get_list("set-cookie")
    session_cookie = next(item for item in cookies if item.startswith(f"{SESSION_COOKIE}="))
    assert "HttpOnly" in session_cookie
    assert "Secure" in session_cookie
    assert "SameSite=strict" in session_cookie
    assert "Path=/api" in session_cookie
    assert client.get("/api/v1/auth/status").json()["authenticated"] is True
    plaintext = client.cookies.get(SESSION_COOKIE)
    csrf = client.cookies.get(CSRF_COOKIE)
    with sqlite3.connect(settings.auth_database_path) as db:
        stored = db.execute("SELECT token_digest,csrf_digest FROM sessions").fetchone()
    assert plaintext not in stored
    assert csrf not in stored
    assert oct(os.stat(settings.auth_database_path).st_mode & 0o777) == "0o600"
    assert oct(os.stat(settings.auth_secret_path).st_mode & 0o777) == "0o600"
    reused = client.post(
        "/api/v1/auth/pair",
        json={"code": code, "friendly_name": "Replay"},
        headers={"Origin": "https://roomos.example.com"},
    )
    assert reused.status_code == 400


def test_malformed_expired_pairing_and_rate_limit(auth_client):
    client, _, store, settings = auth_client
    assert client.post(
        "/api/v1/auth/pair",
        json={"code": "bad!", "friendly_name": "Tablet"},
        headers={"Origin": settings.auth_allowed_origin},
    ).status_code == 422
    code, _ = store.create_pairing_window()
    with sqlite3.connect(settings.auth_database_path) as db:
        db.execute("UPDATE pairing_windows SET expires_at=?", (int(time.time()) - 1,))
    assert client.post(
        "/api/v1/auth/pair",
        json={"code": code, "friendly_name": "Tablet"},
        headers={"Origin": settings.auth_allowed_origin},
    ).status_code == 400
    for _ in range(settings.auth_pairing_attempts - 1):
        assert client.post(
            "/api/v1/auth/pair",
            json={"code": "AAAA-BBBB-CCCC", "friendly_name": "Tablet"},
            headers={"Origin": settings.auth_allowed_origin},
        ).status_code == 400
    assert client.post(
        "/api/v1/auth/pair",
        json={"code": "AAAA-BBBB-CCCC", "friendly_name": "Tablet"},
        headers={"Origin": settings.auth_allowed_origin},
    ).status_code == 429


def test_invalid_expired_revoked_and_disabled_sessions(auth_client):
    client, _, store, settings = auth_client
    client.cookies.set(SESSION_COOKIE, "invalid.secret", domain="roomos.example.com", path="/api")
    assert client.get("/api/v1/lights").status_code == 401
    client.cookies.clear()
    paired = pair(client, store)
    device_id = paired.json()["device"]["id"]
    session_id = client.cookies.get(SESSION_COOKIE).split(".", 1)[0]
    with sqlite3.connect(settings.auth_database_path) as db:
        db.execute("UPDATE sessions SET sliding_expires_at=? WHERE id=?", (int(time.time()) - 1, session_id))
    assert client.get("/api/v1/lights").status_code == 401

    client.cookies.clear()
    paired = pair(client, store, "Second tablet")
    device_id = paired.json()["device"]["id"]
    assert store.revoke_sessions(device_id)
    assert client.get("/api/v1/lights").status_code == 401

    client.cookies.clear()
    paired = pair(client, store, "Third tablet")
    device_id = paired.json()["device"]["id"]
    assert store.disable_device(device_id)
    assert client.get("/api/v1/lights").status_code == 401

    client.cookies.clear()
    paired = pair(client, store, "Fourth tablet")
    device_id = paired.json()["device"]["id"]
    assert store.revoke_device(device_id)
    assert client.get("/api/v1/lights").status_code == 401


def test_host_origin_csrf_and_cross_origin_websocket_enforced(auth_client):
    client, fake_ha, store, settings = auth_client
    pair(client, store)
    assert client.get("/api/v1/lights", headers={"Host": "evil.example"}).status_code == 403
    assert client.post(
        "/api/v1/lights/room_main/power",
        json={"on": True},
        headers={"Origin": "https://evil.example"},
    ).status_code == 403
    assert client.post(
        "/api/v1/lights/room_main/power",
        json={"on": True},
        headers={"Origin": settings.auth_allowed_origin},
    ).status_code == 403
    assert client.post(
        "/api/v1/lights/room_main/power",
        json={"on": True},
        headers={"Origin": settings.auth_allowed_origin, "X-CSRF-Token": "invalid"},
    ).status_code == 403
    with pytest.raises(WebSocketDisconnect) as rejected:
        with client.websocket_connect(
            "/api/v1/events", headers={"Host": "roomos.example.com", "Origin": "https://evil.example", "X-Forwarded-Proto": "https"}
        ):
            pass
    assert rejected.value.code == 4403
    assert fake_ha.calls == []


def test_valid_auth_scopes_policy_ids_and_kill_switch_are_independent(auth_client):
    client, fake_ha, store, settings = auth_client
    paired = pair(client, store)
    device_id = paired.json()["device"]["id"]
    with sqlite3.connect(settings.auth_database_path) as db:
        db.execute("UPDATE devices SET scopes=? WHERE id=?", (json.dumps(["system:read"]), device_id))
    assert client.get("/api/v1/system").status_code == 200
    assert client.get("/api/v1/system/version").status_code == 200
    assert client.get("/api/v1/lights").status_code == 403
    assert client.post(
        "/api/v1/lights/room_main/power", json={"on": True}, headers=csrf_headers(client)
    ).status_code == 403
    assert fake_ha.calls == []

    with sqlite3.connect(settings.auth_database_path) as db:
        db.execute("UPDATE devices SET scopes=? WHERE id=?", (json.dumps(["lights:read"]), device_id))
    assert client.get("/api/v1/system/version").status_code == 403
    assert fake_ha.calls == []

    with sqlite3.connect(settings.auth_database_path) as db:
        db.execute("UPDATE devices SET scopes=? WHERE id=?", (json.dumps(["lights:read", "lights:write", "system:read"]), device_id))
    production = settings.model_copy(update={"control_enabled": True, "control_policy": "production"})
    app.dependency_overrides[get_settings] = lambda: production
    assert client.get("/api/v1/lights").status_code == 200
    for action, body in (
        ("brightness", {"brightness": 100}),
        ("color", {"rgb": [1, 2, 3]}),
        ("temperature", {"kelvin": 4000}),
        ("effect", {"effect": "Off"}),
    ):
        assert client.post(
            f"/api/v1/lights/all_room_lights/{action}", json=body, headers=csrf_headers(client)
        ).status_code == 403
    assert client.post(
        "/api/v1/lights/light.living_room/power", json={"on": True}, headers=csrf_headers(client)
    ).status_code == 404
    assert client.post(
        "/api/v1/lights/arbitrary/power", json={"on": True}, headers=csrf_headers(client)
    ).status_code == 404
    assert fake_ha.calls == []

    app.dependency_overrides[get_settings] = lambda: settings
    assert client.post(
        "/api/v1/lights/room_main/power", json={"on": True}, headers=csrf_headers(client)
    ).status_code == 403
    assert fake_ha.calls == []


def test_authenticated_websocket_uses_same_session(auth_client):
    client, _, store, settings = auth_client
    pair(client, store)
    session = client.cookies.get(SESSION_COOKIE)
    with client.websocket_connect(
        "/api/v1/events", headers={"Host": "roomos.example.com", "Origin": settings.auth_allowed_origin, "X-Forwarded-Proto": "https", "Cookie": f"{SESSION_COOKIE}={session}"}
    ) as socket:
        event = socket.receive_json()
        assert event["type"] == "roomos.connection"


def test_scene_auth_scope_csrf_origin_host_and_revocation(auth_client):
    client, fake_ha, store, settings = auth_client
    assert client.get("/api/v1/scenes").status_code == 401
    assert client.post("/api/v1/scenes/movie/activate").status_code == 401

    paired = pair(client, store, "Scene test device")
    device_id = paired.json()["device"]["id"]
    assert client.get("/api/v1/scenes").status_code == 200
    assert client.post(
        "/api/v1/scenes/movie/activate",
        headers={"Origin": settings.auth_allowed_origin},
    ).status_code == 403
    assert client.post(
        "/api/v1/scenes/movie/activate",
        headers={"Origin": "https://evil.example", "X-CSRF-Token": client.cookies.get(CSRF_COOKIE)},
    ).status_code == 403
    assert client.get("/api/v1/scenes", headers={"Host": "evil.example"}).status_code == 403

    with sqlite3.connect(settings.auth_database_path) as db:
        db.execute("UPDATE devices SET scopes=? WHERE id=?", (json.dumps(["lights:read"]), device_id))
    read_only_catalog = client.get("/api/v1/scenes")
    assert read_only_catalog.status_code == 200
    assert all(not scene["activatable"] for scene in read_only_catalog.json()["scenes"])
    assert client.post(
        "/api/v1/scenes/movie/activate", headers=csrf_headers(client)
    ).status_code == 403
    assert fake_ha.calls == []

    assert store.revoke_sessions(device_id)
    assert client.get("/api/v1/scenes").status_code == 401


def test_routine_scopes_csrf_origin_host_and_no_silent_escalation(auth_client):
    client, fake_ha, store, settings = auth_client
    assert client.get("/api/v1/routines").status_code == 401
    paired = pair(client, store, "Routine test device")
    device_id = paired.json()["device"]["id"]
    assert paired.json()["device"]["scopes"] == ["lights:read", "lights:write", "system:read"]
    assert client.get("/api/v1/routines").status_code == 403
    with sqlite3.connect(settings.auth_database_path) as db:
        db.execute("UPDATE devices SET scopes=? WHERE id=?", (json.dumps(["routines:read"]), device_id))
    assert client.get("/api/v1/routines").status_code == 200
    payload = {"name": "Night schedule", "enabled": False, "trigger": {"type": "manual"}, "action": {"type": "activate_scene", "scene_id": "night"}}
    assert client.post("/api/v1/routines", json=payload, headers=csrf_headers(client)).status_code == 403
    with sqlite3.connect(settings.auth_database_path) as db:
        db.execute("UPDATE devices SET scopes=? WHERE id=?", (json.dumps(["routines:read", "routines:write"]), device_id))
    assert client.post("/api/v1/routines", json=payload).status_code == 403
    assert client.post("/api/v1/routines", json=payload, headers={"Origin": "https://evil.example", "X-CSRF-Token": client.cookies.get(CSRF_COOKIE)}).status_code == 403
    created = client.post("/api/v1/routines", json=payload, headers=csrf_headers(client))
    assert created.status_code == 201
    routine_id = created.json()["id"]
    assert client.post(f"/api/v1/routines/{routine_id}/run", headers=csrf_headers(client)).status_code == 403
    assert client.get("/api/v1/routines", headers={"Host": "evil.example"}).status_code == 403
    assert fake_ha.calls == []
    assert store.revoke_sessions(device_id)
    assert client.get("/api/v1/routines").status_code == 401


def test_automation_read_write_simulation_enable_and_revoke_boundaries(auth_client):
    client, fake_ha, store, settings = auth_client
    assert client.get("/api/v1/automations").status_code == 401
    paired = pair(client, store, "Automation test device")
    device_id = paired.json()["device"]["id"]
    assert client.get("/api/v1/automations").status_code == 403
    with sqlite3.connect(settings.auth_database_path) as db:
        db.execute("UPDATE devices SET scopes=? WHERE id=?", (json.dumps(["routines:read"]), device_id))
    assert client.get("/api/v1/automations").status_code == 200
    assert client.get("/api/v1/automations/history").status_code == 200
    payload = {
        "name": "Night schedule",
        "trigger": {"type": "time", "time": "23:30", "days": ["mon"]},
        "actions": [{"type": "scene", "scene_id": "all_off"}],
    }
    assert client.post("/api/v1/automations", json=payload, headers=csrf_headers(client)).status_code == 403
    with sqlite3.connect(settings.auth_database_path) as db:
        db.execute("UPDATE devices SET scopes=? WHERE id=?", (json.dumps(["routines:read", "routines:write"]), device_id))
    assert client.post("/api/v1/automations", json=payload).status_code == 403
    created = client.post("/api/v1/automations", json=payload, headers=csrf_headers(client))
    assert created.status_code == 201 and created.json()["enabled"] is False
    automation_id = created.json()["id"]
    assert client.post(f"/api/v1/automations/{automation_id}/simulate", json={}, headers=csrf_headers(client)).status_code == 200
    assert client.post(f"/api/v1/automations/{automation_id}/enabled", json={"enabled": True}, headers=csrf_headers(client)).status_code == 403
    app.state.automations.store.set_enabled(
        automation_id,
        True,
        now=app.state.automations.clock(),
    )
    disabled = client.post(
        f"/api/v1/automations/{automation_id}/enabled",
        json={"enabled": False},
        headers=csrf_headers(client),
    )
    assert disabled.status_code == 200 and disabled.json()["enabled"] is False
    assert fake_ha.calls == []
    assert store.revoke_sessions(device_id)
    assert client.get("/api/v1/automations").status_code == 401


def test_camera_read_scope_and_cross_origin_media_boundary(auth_client):
    client, fake_ha, store, settings = auth_client
    assert client.get("/api/v1/cameras").status_code == 401
    paired = pair(client, store, "Camera test device")
    device_id = paired.json()["device"]["id"]
    assert "camera:read" not in paired.json()["device"]["scopes"]
    assert client.get("/api/v1/cameras").status_code == 403

    with sqlite3.connect(settings.auth_database_path) as db:
        db.execute("UPDATE devices SET scopes=? WHERE id=?", (json.dumps(["camera:read"]), device_id))
    assert client.get("/api/v1/cameras").status_code == 200
    assert client.get(
        "/api/v1/cameras", headers={"Origin": "https://evil.example"}
    ).status_code == 403
    assert client.get(
        "/api/v1/cameras", headers={"Sec-Fetch-Site": "cross-site"}
    ).status_code == 403
    assert client.get("/api/v1/cameras/camera.example_camera/snapshot").status_code == 404
    assert fake_ha.calls == []

    assert store.revoke_sessions(device_id)
    assert client.get("/api/v1/cameras").status_code == 401


def test_activity_read_scope_is_explicit_and_read_only(auth_client):
    client, fake_ha, store, settings = auth_client
    assert client.get("/api/v1/alerts").status_code == 401
    assert client.get("/api/v1/activity").status_code == 401
    paired = pair(client, store, "Activity test device")
    device_id = paired.json()["device"]["id"]
    assert "activity:read" not in paired.json()["device"]["scopes"]
    assert client.get("/api/v1/alerts").status_code == 403
    assert client.get("/api/v1/activity").status_code == 403

    with sqlite3.connect(settings.auth_database_path) as db:
        db.execute(
            "UPDATE devices SET scopes=? WHERE id=?",
            (json.dumps(["activity:read"]), device_id),
        )
    alerts = client.get("/api/v1/alerts")
    activity = client.get("/api/v1/activity?limit=10&offset=0&category=all")
    assert alerts.status_code == 200
    assert activity.status_code == 200
    assert alerts.json()["active"] == []
    assert activity.json()["activity"] == []
    assert client.get("/api/v1/activity?limit=101").status_code == 422
    assert client.get("/api/v1/activity?category=raw_ha").status_code == 422
    assert client.get("/api/v1/alerts", headers={"Host": "evil.example"}).status_code == 403
    assert fake_ha.calls == []

    assert store.revoke_sessions(device_id)
    assert client.get("/api/v1/alerts").status_code == 401
