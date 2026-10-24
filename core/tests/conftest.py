from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, get_ha, get_settings, require_control_enabled


@pytest.fixture(autouse=True)
def isolated_push_state(monkeypatch, tmp_path):
    """7G: no test may read or write the host's real push/FCM state or VAPID key."""
    monkeypatch.setenv("ROOMOS_METRICS_HISTORY_PATH", str(tmp_path / "metrics-history.db"))
    for key, name in {
        "ROOMOS_PUSH_REGISTRY_PATH": "push-registry.json",
        "ROOMOS_PUSH_OUTBOX_PATH": "push-outbox.json",
        "ROOMOS_NOTIFICATION_DESTINATIONS_PATH": "notification-destinations.json",
        "ROOMOS_FCM_TARGETS_PATH": "fcm-targets.json",
        "ROOMOS_WEBPUSH_VAPID_PRIVATE_KEY_PATH": "vapid-private.pem",
        "ROOMOS_ALERT_POLICY_PATH": "alert-policy.json",
    }.items():
        monkeypatch.setenv(key, str(tmp_path / name))


class FakeHA:
    def __init__(self):
        self.calls = []
        now = datetime.now(UTC).isoformat()
        self.states = {
            "light.living_room": {
                "entity_id": "light.living_room",
                "state": "off",
                "last_updated": now,
                "attributes": {
                    "brightness": None,
                    "color_mode": None,
                    "rgb_color": None,
                    "hs_color": None,
                    "effect_list": ["Off", "Party"],
                    "min_color_temp_kelvin": 2500,
                    "max_color_temp_kelvin": 6500,
                },
            }
        }

    async def get_state(self, entity_id):
        return self.states.get(
            entity_id,
            {
                "entity_id": entity_id,
                "state": "off" if entity_id.startswith("light.") else "unknown",
                "last_updated": datetime.now(UTC).isoformat(),
                "attributes": {},
            },
        )

    async def get_states(self, entity_ids):
        return {entity_id: await self.get_state(entity_id) for entity_id in entity_ids}

    async def call_service(self, domain, service, payload):
        self.calls.append((domain, service, payload))
        return []

    async def get_history(self, entity_ids, start, end):
        self.history_requests = getattr(self, "history_requests", []) + [(list(entity_ids), start, end)]
        if getattr(self, "history_error", None) is not None:
            raise self.history_error
        return [
            [{"entity_id": entity_ids[0], "state": "42.5", "last_changed": start.isoformat()},
             {"state": "47.25", "last_changed": end.isoformat()}],
            [{"entity_id": entity_ids[1], "state": "on", "last_changed": start.isoformat()}],
        ]

    async def get_image(self, entity_id, *, max_bytes=5 * 1024 * 1024):
        return b"\xff\xd8\xff\xd9", "image/jpeg"


@pytest.fixture
def fake_ha():
    return FakeHA()


@pytest.fixture
def client(fake_ha, monkeypatch, tmp_path):
    monkeypatch.setenv("ROOMOS_AUTH_ENABLED", "false")
    monkeypatch.setenv("ROOMOS_AUTH_DATABASE_PATH", str(tmp_path / "auth.db"))
    monkeypatch.setenv("ROOMOS_AUTH_SECRET_PATH", str(tmp_path / "auth.secret"))
    monkeypatch.setenv("ROOMOS_ROUTINES_PATH", str(tmp_path / "routines.json"))
    monkeypatch.setenv("ROOMOS_AUTOMATIONS_PATH", str(tmp_path / "automations.json"))
    monkeypatch.setenv("ROOMOS_AUTOMATION_HISTORY_PATH", str(tmp_path / "automation-history.jsonl"))
    monkeypatch.setenv("ROOMOS_SCENES_CONFIG_PATH", str(tmp_path / "scenes.json"))
    monkeypatch.setenv("ROOMOS_SCENE_STATE_PATH", str(tmp_path / "scene-state.json"))
    monkeypatch.setenv("ROOMOS_ALERTS_PATH", str(tmp_path / "alerts.json"))
    monkeypatch.setenv("ROOMOS_NOTIFICATIONS_PATH", str(tmp_path / "notifications.json"))
    monkeypatch.setenv(
        "ROOMOS_NOTIFICATION_INCIDENTS_PATH", str(tmp_path / "notification-incidents.json")
    )
    get_settings.cache_clear()
    app.dependency_overrides[get_ha] = lambda: fake_ha
    app.dependency_overrides[require_control_enabled] = lambda: None
    app.dependency_overrides[get_settings] = lambda: Settings(
        ROOMOS_CONTROL_ENABLED=True,
        ROOMOS_CONTROL_POLICY="production",
        ROOMOS_CONTROL_DEVICE_IDS="room_main,room_2,room_3,room_4,desk_ambient,all_room_lights",
        ROOMOS_CONTROL_ACTIONS="power,brightness,color,temperature,effect",
        ROOMOS_AUTH_ENABLED=False,
        ROOMOS_ROUTINES_PATH=str(tmp_path / "routines.json"),
        ROOMOS_AUTOMATIONS_PATH=str(tmp_path / "automations.json"),
        ROOMOS_AUTOMATION_HISTORY_PATH=str(tmp_path / "automation-history.jsonl"),
        ROOMOS_SCENES_CONFIG_PATH=str(tmp_path / "scenes.json"),
        ROOMOS_SCENE_STATE_PATH=str(tmp_path / "scene-state.json"),
        ROOMOS_ALERTS_PATH=str(tmp_path / "alerts.json"),
        ROOMOS_NOTIFICATIONS_PATH=str(tmp_path / "notifications.json"),
        ROOMOS_NOTIFICATION_INCIDENTS_PATH=str(
            tmp_path / "notification-incidents.json"
        ),
    )
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def isolated_execution_store(monkeypatch, tmp_path):
    monkeypatch.setenv("ROOMOS_ROUTINE_EVALUATIONS_PATH", str(tmp_path / "routine-evaluations.json"))
    monkeypatch.setenv("ROOMOS_EXECUTIONS_PATH", str(tmp_path / "executions.json"))
