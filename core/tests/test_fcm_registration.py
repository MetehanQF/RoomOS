import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from test_auth import auth_client, pair, pair_native  # noqa: F401


TOKEN_ONE = "a" * 80 + ":" + "b" * 80
TOKEN_TWO = "c" * 80 + ":" + "d" * 80
REGISTER_PATH = "/api/v1/native/notifications/fcm/register"


def configure_private_paths(settings):
    root = Path(settings.auth_database_path).parent
    settings.fcm_targets_path = str(root / "fcm-targets.json")
    settings.notification_destinations_path = str(
        root / "notification-destinations.json"
    )
    return root


def test_native_fcm_registration_is_device_bound_private_and_rotation_safe(auth_client):
    client, fake_ha, store, settings = auth_client
    configure_private_paths(settings)
    paired = pair_native(client, store).json()
    token = paired["session_token"]
    device_id = paired["device"]["id"]
    headers = {"Authorization": f"Bearer {token}"}

    response = client.post(
        REGISTER_PATH,
        json={"registration_token": TOKEN_ONE},
        headers=headers,
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "fcm"
    assert payload["device_id"] == device_id
    assert payload["destination_enabled"] is False
    assert datetime.fromisoformat(payload["activated_at"]).tzinfo is not None
    assert TOKEN_ONE not in response.text
    assert response.headers["cache-control"].startswith("no-store")

    targets_path = Path(settings.fcm_targets_path)
    destinations_path = Path(settings.notification_destinations_path)
    assert os.stat(targets_path).st_mode & 0o777 == 0o600
    assert os.stat(destinations_path).st_mode & 0o777 == 0o600
    targets = json.loads(targets_path.read_text())
    destinations = json.loads(destinations_path.read_text())
    assert list(targets["targets"].values()) == [TOKEN_ONE]
    assert list(targets["registrations"].values())[0]["device_id"] == device_id
    assert len(destinations["destinations"]) == 1
    assert destinations["destinations"][0]["enabled"] is False
    assert set(destinations["destinations"][0]["severity_policy"]) == {
        "info",
        "warning",
        "critical",
    }
    first_activation = destinations["destinations"][0]["activated_at"]

    rotated = client.post(
        REGISTER_PATH,
        json={"registration_token": TOKEN_TWO},
        headers=headers,
    )
    assert rotated.status_code == 200
    targets = json.loads(targets_path.read_text())
    destinations = json.loads(destinations_path.read_text())
    assert list(targets["targets"].values()) == [TOKEN_TWO]
    assert TOKEN_ONE not in targets_path.read_text()
    assert len(targets["targets"]) == 1
    assert len(destinations["destinations"]) == 1
    assert destinations["destinations"][0]["activated_at"] == first_activation
    assert fake_ha.calls == []


def test_native_fcm_registration_rejects_unauthenticated_web_cross_device_and_malformed(
    auth_client,
):
    client, fake_ha, store, settings = auth_client
    configure_private_paths(settings)
    assert client.post(
        REGISTER_PATH, json={"registration_token": TOKEN_ONE}
    ).status_code == 401

    pair(client, store, "Web browser")
    assert client.post(
        REGISTER_PATH, json={"registration_token": TOKEN_ONE}
    ).status_code == 403
    client.cookies.clear()

    native = pair_native(client, store).json()
    token = native["session_token"]
    device_id = native["device"]["id"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.post(
        REGISTER_PATH,
        json={
            "registration_token": TOKEN_ONE,
            "device_id": "another-device",
        },
        headers=headers,
    ).status_code == 422
    assert client.post(
        REGISTER_PATH,
        json={"registration_token": "short token"},
        headers=headers,
    ).status_code == 422
    assert client.post(
        REGISTER_PATH,
        json={"registration_token": TOKEN_ONE},
        headers={**headers, "Origin": "https://evil.example"},
    ).status_code == 403

    assert store.revoke_sessions(device_id)
    assert client.post(
        REGISTER_PATH,
        json={"registration_token": TOKEN_ONE},
        headers=headers,
    ).status_code == 401
    assert not Path(settings.fcm_targets_path).exists()
    assert not Path(settings.notification_destinations_path).exists()
    assert fake_ha.calls == []


def test_native_fcm_registration_rejects_non_android_bearer_session(auth_client):
    client, _, store, settings = auth_client
    configure_private_paths(settings)
    paired = pair_native(client, store).json()
    token = paired["session_token"]
    session_id = token.split(".", 1)[0]
    with sqlite3.connect(settings.auth_database_path) as database:
        database.execute(
            "UPDATE sessions SET kind='web' WHERE id=?", (session_id,)
        )
    assert client.post(
        REGISTER_PATH,
        json={"registration_token": TOKEN_ONE},
        headers={"Authorization": f"Bearer {token}"},
    ).status_code == 403
    assert not Path(settings.fcm_targets_path).exists()
