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
    )
    for key, value in {
        "ROOMOS_AUTH_ENABLED": "true",
        "ROOMOS_AUTH_DATABASE_PATH": settings.auth_database_path,
        "ROOMOS_AUTH_SECRET_PATH": settings.auth_secret_path,
        "ROOMOS_AUTH_ALLOWED_ORIGIN": settings.auth_allowed_origin,
        "ROOMOS_AUTH_ALLOWED_HOST": settings.auth_allowed_host,
        "ROOMOS_CONTROL_ENABLED": "false",
        "ROOMOS_CONTROL_POLICY": "read_only",
    }.items():
        monkeypatch.setenv(key, str(value))
    get_settings.cache_clear()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_ha] = lambda: fake_ha
    with TestClient(app, base_url="https://roomos.example.com") as client:
        yield client, fake_ha, app.state.auth, settings
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def pair(client, store, name="Test Tablet"):
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


def test_unauthenticated_rest_and_websocket_are_rejected(auth_client):
    client, fake_ha, _, _ = auth_client
    assert client.get("/api/v1/lights").status_code == 401
    assert client.post("/api/v1/lights/living_room/power", json={"on": True}).status_code == 401
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
        "/api/v1/lights/living_room/power",
        json={"on": True},
        headers={"Origin": "https://evil.example"},
    ).status_code == 403
    assert client.post(
        "/api/v1/lights/living_room/power",
        json={"on": True},
        headers={"Origin": settings.auth_allowed_origin},
    ).status_code == 403
    assert client.post(
        "/api/v1/lights/living_room/power",
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
    assert client.get("/api/v1/lights").status_code == 403
    assert client.post(
        "/api/v1/lights/living_room/power", json={"on": True}, headers=csrf_headers(client)
    ).status_code == 403
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
        "/api/v1/lights/living_room/power", json={"on": True}, headers=csrf_headers(client)
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
