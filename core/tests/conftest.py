from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, get_ha, get_settings, require_control_enabled


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


@pytest.fixture
def fake_ha():
    return FakeHA()


@pytest.fixture
def client(fake_ha, monkeypatch, tmp_path):
    monkeypatch.setenv("ROOMOS_AUTH_ENABLED", "false")
    monkeypatch.setenv("ROOMOS_AUTH_DATABASE_PATH", str(tmp_path / "auth.db"))
    monkeypatch.setenv("ROOMOS_AUTH_SECRET_PATH", str(tmp_path / "auth.secret"))
    get_settings.cache_clear()
    app.dependency_overrides[get_ha] = lambda: fake_ha
    app.dependency_overrides[require_control_enabled] = lambda: None
    app.dependency_overrides[get_settings] = lambda: Settings(
        ROOMOS_CONTROL_ENABLED=True,
        ROOMOS_CONTROL_POLICY="production",
        ROOMOS_CONTROL_DEVICE_IDS="living_room,bedroom,kitchen,hallway,desk_ambient,all_room_lights",
        ROOMOS_CONTROL_ACTIONS="power,brightness,color,temperature,effect",
        ROOMOS_AUTH_ENABLED=False,
    )
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    get_settings.cache_clear()
