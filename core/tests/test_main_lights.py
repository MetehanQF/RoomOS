"""Phase 7E.1: the four L530 room lights as one logical group (``main_room_lights``).

Membership mirrors the Home Assistant group light.all_lights (verified read-only against
production) but, like all_room_lights, commands expand to the physical member entities so
every member gets its own live check and its own result.
"""

import json
import sqlite3

from app.catalog import MAIN_LIGHT_IDS, MAIN_LIGHTS
from app.config import Settings
from app.ha_client import HomeAssistantUnavailable
from app.main import app, get_settings, require_control_enabled
from app.policy import writable_permissions
from test_auth import auth_client, csrf_headers, pair  # noqa: F401 - shared isolated auth fixture

MAIN_ENTITIES = ["light.living_room", "light.bedroom", "light.kitchen", "light.hallway"]


def test_membership_is_the_four_l530_room_lights():
    assert MAIN_LIGHT_IDS == ("room_main", "room_2", "room_3", "room_4")
    assert [light.entity_id for light in MAIN_LIGHTS] == MAIN_ENTITIES
    assert {light.model for light in MAIN_LIGHTS} == {"L530"}


def test_production_policy_allows_power_brightness_temperature_only():
    settings = Settings(ROOMOS_CONTROL_ENABLED=True, ROOMOS_CONTROL_POLICY="production")
    assert writable_permissions(settings)["main_room_lights"] == frozenset({"power", "brightness", "temperature"})


def test_power_expands_to_the_four_members_only(client, fake_ha):
    response = client.post("/api/v1/lights/main_room_lights/power", json={"on": True})
    assert response.status_code == 200
    assert response.json() == {"accepted": True, "device_id": "main_room_lights", "succeeded": list(MAIN_LIGHT_IDS), "failed": []}
    assert fake_ha.calls == [("light", "turn_on", {"entity_id": entity}) for entity in MAIN_ENTITIES]
    called = str(fake_ha.calls)
    assert "light.desk_ambient" not in called and "light.all_lights" not in called


def test_shared_brightness_reaches_every_member(client, fake_ha):
    response = client.post("/api/v1/lights/main_room_lights/brightness", json={"brightness": 128})
    assert response.status_code == 200
    assert fake_ha.calls == [("light", "turn_on", {"entity_id": entity, "brightness": 128}) for entity in MAIN_ENTITIES]


def test_shared_temperature_reaches_every_member(client, fake_ha):
    response = client.post("/api/v1/lights/main_room_lights/temperature", json={"kelvin": 3200})
    assert response.status_code == 200
    assert fake_ha.calls == [("light", "turn_on", {"entity_id": entity, "color_temp_kelvin": 3200}) for entity in MAIN_ENTITIES]


def test_invalid_values_are_rejected_before_home_assistant(client, fake_ha):
    for path, body in (
        ("brightness", {"brightness": 0}),
        ("brightness", {"brightness": 256}),
        ("temperature", {"kelvin": 2499}),
        # Desk Ambient accepts 9000 K; the group range is the members' common 2500–6500 K.
        ("temperature", {"kelvin": 9000}),
    ):
        assert client.post(f"/api/v1/lights/main_room_lights/{path}", json=body).status_code == 422
    assert fake_ha.calls == []


def test_unavailable_member_is_reported_and_not_called(client, fake_ha):
    fake_ha.states["light.kitchen"] = {"entity_id": "light.kitchen", "state": "unavailable", "last_updated": fake_ha.states["light.living_room"]["last_updated"], "attributes": {}}
    response = client.post("/api/v1/lights/main_room_lights/brightness", json={"brightness": 90})
    assert response.status_code == 207
    body = response.json()
    assert body["accepted"] is False
    assert body["succeeded"] == ["room_main", "room_2", "room_4"]
    assert body["failed"] == [{"device_id": "room_3", "reason": "unavailable"}]
    assert "light.kitchen" not in [call[2]["entity_id"] for call in fake_ha.calls]


def test_member_service_failure_is_partial_and_not_retried(client, fake_ha):
    original = fake_ha.call_service

    async def fail_room_2(domain, service, payload):
        if payload["entity_id"] == "light.bedroom":
            fake_ha.calls.append((domain, service, payload))
            raise HomeAssistantUnavailable("simulated")
        return await original(domain, service, payload)

    fake_ha.call_service = fail_room_2
    response = client.post("/api/v1/lights/main_room_lights/power", json={"on": False})
    assert response.status_code == 207
    assert response.json()["failed"] == [{"device_id": "room_2", "reason": "service_failed"}]
    called = [call[2]["entity_id"] for call in fake_ha.calls]
    assert called == MAIN_ENTITIES and called.count("light.bedroom") == 1


def test_unreadable_home_assistant_is_a_503_like_a_single_light(client, fake_ha):
    async def offline(_entity_ids):
        raise HomeAssistantUnavailable("Home Assistant is unavailable")

    fake_ha.get_states = offline
    response = client.post("/api/v1/lights/main_room_lights/power", json={"on": True})
    assert response.status_code == 503
    assert "light." not in response.text
    assert fake_ha.calls == []


def test_colour_and_effect_are_not_group_actions(client, fake_ha):
    assert client.post("/api/v1/lights/main_room_lights/color", json={"rgb": [1, 2, 3]}).status_code == 403
    assert client.post("/api/v1/lights/main_room_lights/effect", json={"effect": "Off"}).status_code == 403
    assert fake_ha.calls == []


def test_kill_switch_and_scoped_policy_block_the_group(client, fake_ha):
    app.dependency_overrides[get_settings] = lambda: Settings(
        ROOMOS_CONTROL_ENABLED=True, ROOMOS_CONTROL_POLICY="scoped",
        ROOMOS_CONTROL_DEVICE_IDS="room_main,room_2,room_3,room_4", ROOMOS_CONTROL_ACTIONS="power,brightness,temperature",
    )
    for path, body in (("power", {"on": True}), ("brightness", {"brightness": 10}), ("temperature", {"kelvin": 3000})):
        assert client.post(f"/api/v1/lights/main_room_lights/{path}", json=body).status_code == 403
    app.dependency_overrides.pop(require_control_enabled, None)
    app.dependency_overrides[get_settings] = lambda: Settings(ROOMOS_CONTROL_ENABLED=False, ROOMOS_CONTROL_POLICY="production")
    response = client.post("/api/v1/lights/main_room_lights/power", json={"on": True})
    assert response.status_code == 403 and response.json() == {"detail": "RoomOS control is disabled"}
    assert fake_ha.calls == []


def test_group_requires_lights_write_scope_and_csrf(auth_client):
    client, fake_ha, store, settings = auth_client
    paired = pair(client, store)
    device_id = paired.json()["device"]["id"]
    production = settings.model_copy(update={"control_enabled": True, "control_policy": "production"})
    app.dependency_overrides[get_settings] = lambda: production
    with sqlite3.connect(settings.auth_database_path) as db:
        db.execute("UPDATE devices SET scopes=? WHERE id=?", (json.dumps(["lights:read"]), device_id))
    assert client.post("/api/v1/lights/main_room_lights/power", json={"on": True}, headers=csrf_headers(client)).status_code == 403
    with sqlite3.connect(settings.auth_database_path) as db:
        db.execute("UPDATE devices SET scopes=? WHERE id=?", (json.dumps(["lights:read", "lights:write"]), device_id))
    assert client.post("/api/v1/lights/main_room_lights/power", json={"on": True},
                       headers={"Origin": settings.auth_allowed_origin, "X-CSRF-Token": "invalid"}).status_code == 403
    assert fake_ha.calls == []
    assert client.post("/api/v1/lights/main_room_lights/power", json={"on": True}, headers=csrf_headers(client)).status_code == 200
    assert [call[2]["entity_id"] for call in fake_ha.calls] == MAIN_ENTITIES
