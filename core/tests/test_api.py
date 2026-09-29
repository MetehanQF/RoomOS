from app.catalog import LIGHTS, LIGHT_BY_ID, PHYSICAL_LIGHT_ENTITY_IDS
from app.ha_client import HomeAssistantUnavailable
from app.config import Settings
from app.main import app, get_ha, get_settings, require_control_enabled
from app.service import validate_kelvin


def test_health_has_no_secrets(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "token" not in str(body).lower()


def test_lights_use_logical_ids(client):
    response = client.get("/api/v1/lights")
    assert response.status_code == 200
    lights = response.json()
    # Derived from the active registry: the API must expose exactly the
    # registered logical IDs, and never a Home Assistant entity ID.
    assert {item["id"] for item in lights} == {light.id for light in LIGHTS}
    assert "entity_id" not in str(lights)


def test_invalid_and_arbitrary_entity_ids_are_rejected(client, fake_ha):
    for light_id in ("unknown", "light.living_room", "light.evil"):
        response = client.post(f"/api/v1/lights/{light_id}/power", json={"on": True})
        assert response.status_code == 404
    assert fake_ha.calls == []


def test_invalid_brightness_is_rejected_before_ha(client, fake_ha):
    for value in (0, 256, -1, "bright"):
        response = client.post(
            "/api/v1/lights/living_room/brightness", json={"brightness": value}
        )
        assert response.status_code == 422
    assert fake_ha.calls == []


def test_kelvin_respects_per_device_ranges(client, fake_ha):
    assert client.post(
        "/api/v1/lights/living_room/temperature", json={"kelvin": 7000}
    ).status_code == 422
    assert client.post(
        "/api/v1/lights/desk_ambient/temperature", json={"kelvin": 7000}
    ).status_code == 200
    assert fake_ha.calls[-1][2]["color_temp_kelvin"] == 7000


def test_invalid_color_is_rejected(client, fake_ha):
    invalid = (
        {"rgb": [256, 0, 0]},
        {"hue": 30},
        {"saturation": 50},
        {"rgb": [1, 2, 3], "hue": 30, "saturation": 50},
    )
    for payload in invalid:
        response = client.post("/api/v1/lights/living_room/color", json=payload)
        assert response.status_code == 422
    assert fake_ha.calls == []


def test_all_lights_expands_to_every_registered_entity(client, fake_ha):
    response = client.post("/api/v1/lights/all/power", json={"on": False})
    assert response.status_code == 200
    assert response.json() == {
        "accepted": True,
        "device_id": "all_room_lights",
        "succeeded": [light.id for light in LIGHTS],
        "failed": [],
    }
    entity_ids = [call[2]["entity_id"] for call in fake_ha.calls]
    assert tuple(entity_ids) == PHYSICAL_LIGHT_ENTITY_IDS
    assert len(entity_ids) == len(set(entity_ids)) == len(LIGHTS)
    # The Home Assistant group entity is deliberately never called.
    assert "light.all_lights" not in entity_ids


def test_all_room_lights_group_exposes_only_unique_logical_ids(client):
    response = client.get("/api/v1/groups/all_room_lights")
    assert response.status_code == 200
    member_ids = response.json()["member_ids"]
    assert member_ids == [light.id for light in LIGHTS]
    assert len(member_ids) == len(set(member_ids)) == len(LIGHTS)
    assert all(not member_id.startswith("light.") for member_id in member_ids)


def test_group_reports_unavailable_target_without_calling_it(client, fake_ha):
    target = LIGHTS[1]
    fake_ha.states[target.entity_id] = {
        "entity_id": target.entity_id,
        "state": "unavailable",
        "last_updated": fake_ha.states[LIGHTS[0].entity_id]["last_updated"],
        "attributes": {},
    }
    response = client.post("/api/v1/lights/all/power", json={"on": True})
    assert response.status_code == 207
    body = response.json()
    assert body["accepted"] is False
    assert body["succeeded"] == [light.id for light in LIGHTS if light.id != target.id]
    assert body["failed"] == [{"device_id": target.id, "reason": "unavailable"}]
    # An unavailable target is reported without ever being called.
    assert target.entity_id not in [call[2]["entity_id"] for call in fake_ha.calls]


def test_group_reports_service_failure_and_does_not_retry(client, fake_ha):
    original_call_service = fake_ha.call_service

    target = LIGHTS[1]

    async def fail_target_once(domain, service, payload):
        if payload["entity_id"] == target.entity_id:
            fake_ha.calls.append((domain, service, payload))
            raise HomeAssistantUnavailable("simulated")
        return await original_call_service(domain, service, payload)

    fake_ha.call_service = fail_target_once
    response = client.post("/api/v1/lights/all/power", json={"on": True})
    assert response.status_code == 207
    body = response.json()
    assert body["accepted"] is False
    assert body["succeeded"] == [light.id for light in LIGHTS if light.id != target.id]
    assert body["failed"] == [{"device_id": target.id, "reason": "service_failed"}]
    called_ids = [call[2]["entity_id"] for call in fake_ha.calls]
    assert called_ids == list(PHYSICAL_LIGHT_ENTITY_IDS)
    # Exactly one attempt: a failed target is never retried.
    assert called_ids.count(target.entity_id) == 1


def test_ha_unavailable_is_sanitized(client):
    class OfflineHA:
        async def get_states(self, _entity_ids):
            raise HomeAssistantUnavailable("Home Assistant is unavailable")

    app.dependency_overrides[get_ha] = lambda: OfflineHA()
    response = client.get("/api/v1/lights")
    assert response.status_code == 503
    assert response.json() == {"detail": "Home Assistant is unavailable"}


async def test_missing_allowed_entity_does_not_make_ha_connection_unavailable():
    from app.config import Settings
    from app.ha_client import HomeAssistantClient
    import httpx

    async def handler(_request):
        return httpx.Response(404, json={"message": "Entity not found"})

    ha = HomeAssistantClient(Settings(HA_TOKEN="not-a-real-token"))  # privacy-scan:allow test fixture
    await ha._http.aclose()
    ha._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await ha.get_state("sensor.missing") is None
    assert ha.connected is True
    await ha.close()


async def test_successful_allowed_entity_read_returns_state():
    from app.config import Settings
    from app.ha_client import HomeAssistantClient
    import httpx

    expected = {"entity_id": "light.desk_ambient", "state": "off", "attributes": {}}

    async def handler(_request):
        return httpx.Response(200, json=expected)

    ha = HomeAssistantClient(Settings(HA_TOKEN="not-a-real-token"))  # privacy-scan:allow test fixture
    await ha._http.aclose()
    ha._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await ha.get_state("light.desk_ambient") == expected
    assert ha.connected is True
    await ha.close()


def test_effect_must_exist_in_device_effect_list(client, fake_ha):
    response = client.post(
        "/api/v1/lights/living_room/effect", json={"effect": "Not Real"}
    )
    assert response.status_code == 422
    assert fake_ha.calls == []


def test_live_default_blocks_all_control_routes(client, fake_ha):
    app.dependency_overrides.pop(require_control_enabled, None)
    app.dependency_overrides.pop(get_settings, None)
    routes = (
        ("/api/v1/lights/living_room/power", {"on": True}),
        ("/api/v1/lights/living_room/brightness", {"brightness": 100}),
        ("/api/v1/lights/living_room/color", {"rgb": [1, 2, 3]}),
        ("/api/v1/lights/living_room/temperature", {"kelvin": 3000}),
        ("/api/v1/lights/living_room/effect", {"effect": "Party"}),
        ("/api/v1/lights/all/power", {"on": False}),
    )
    for path, body in routes:
        response = client.post(path, json=body)
        assert response.status_code == 403
        assert response.json() == {"detail": "RoomOS control is disabled"}
    assert fake_ha.calls == []


def test_canary_scope_allows_only_desk_ambient_power(client, fake_ha):
    app.dependency_overrides[get_settings] = lambda: Settings(
        ROOMOS_CONTROL_ENABLED=True,
        ROOMOS_CONTROL_POLICY="scoped",
        ROOMOS_CONTROL_DEVICE_IDS="desk_ambient",
        ROOMOS_CONTROL_ACTIONS="power",
    )
    rejected = (
        ("/api/v1/lights/living_room/power", {"on": True}),
        ("/api/v1/lights/bedroom/power", {"on": True}),
        ("/api/v1/lights/all/power", {"on": True}),
        ("/api/v1/lights/light.evil/power", {"on": True}),
        ("/api/v1/lights/desk_ambient/brightness", {"brightness": 100}),
        ("/api/v1/lights/desk_ambient/color", {"rgb": [1, 2, 3]}),
        ("/api/v1/lights/desk_ambient/temperature", {"kelvin": 3000}),
        ("/api/v1/lights/desk_ambient/effect", {"effect": "Off"}),
    )
    for path, body in rejected:
        expected = 404 if "light.evil" in path else 403
        assert client.post(path, json=body).status_code == expected
    assert fake_ha.calls == []

    allowed = client.post("/api/v1/lights/desk_ambient/power", json={"on": True})
    assert allowed.status_code == 200
    assert fake_ha.calls == [
        ("light", "turn_on", {"entity_id": "light.desk_ambient"})
    ]


def test_brightness_canary_scope_is_server_side_and_exact(client, fake_ha):
    app.dependency_overrides[get_settings] = lambda: Settings(
        ROOMOS_CONTROL_ENABLED=True,
        ROOMOS_CONTROL_POLICY="scoped",
        ROOMOS_CONTROL_DEVICE_IDS="desk_ambient",
        ROOMOS_CONTROL_ACTIONS="power,brightness",
    )
    rejected = (
        ("/api/v1/lights/living_room/brightness", {"brightness": 100}),
        ("/api/v1/lights/desk_ambient/color", {"rgb": [80, 0, 160]}),
        ("/api/v1/lights/desk_ambient/temperature", {"kelvin": 3000}),
        ("/api/v1/lights/desk_ambient/effect", {"effect": "Off"}),
        ("/api/v1/lights/all/power", {"on": True}),
    )
    for path, body in rejected:
        expected = 404 if "light.desk_ambient" in path or "arbitrary" in path else 403
        assert client.post(path, json=body).status_code == expected
    assert fake_ha.calls == []

    allowed = client.post(
        "/api/v1/lights/desk_ambient/brightness", json={"brightness": 112}
    )
    assert allowed.status_code == 200
    assert fake_ha.calls == [
        (
            "light",
            "turn_on",
            {"entity_id": "light.desk_ambient", "brightness": 112},
        )
    ]


def test_brightness_canary_range_validation_calls_no_service(client, fake_ha):
    app.dependency_overrides[get_settings] = lambda: Settings(
        ROOMOS_CONTROL_ENABLED=True,
        ROOMOS_CONTROL_POLICY="scoped",
        ROOMOS_CONTROL_DEVICE_IDS="desk_ambient",
        ROOMOS_CONTROL_ACTIONS="power,brightness",
    )
    for value in (0, 256, -1, "bright"):
        response = client.post(
            "/api/v1/lights/desk_ambient/brightness", json={"brightness": value}
        )
        assert response.status_code == 422
    assert fake_ha.calls == []


def test_kill_switch_overrides_brightness_scope(client, fake_ha):
    app.dependency_overrides.pop(require_control_enabled, None)
    app.dependency_overrides[get_settings] = lambda: Settings(
        ROOMOS_CONTROL_ENABLED=False,
        ROOMOS_CONTROL_POLICY="scoped",
        ROOMOS_CONTROL_DEVICE_IDS="desk_ambient",
        ROOMOS_CONTROL_ACTIONS="power,brightness",
    )
    response = client.post(
        "/api/v1/lights/desk_ambient/brightness", json={"brightness": 112}
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "RoomOS control is disabled"}
    assert fake_ha.calls == []


def test_temperature_canary_scope_is_server_side_and_exact(client, fake_ha):
    app.dependency_overrides[get_settings] = lambda: Settings(
        ROOMOS_CONTROL_ENABLED=True,
        ROOMOS_CONTROL_POLICY="scoped",
        ROOMOS_CONTROL_DEVICE_IDS="desk_ambient",
        ROOMOS_CONTROL_ACTIONS="power,temperature",
    )
    rejected = (
        ("/api/v1/lights/living_room/temperature", {"kelvin": 4000}),
        ("/api/v1/lights/desk_ambient/brightness", {"brightness": 112}),
        ("/api/v1/lights/desk_ambient/color", {"rgb": [80, 0, 160]}),
        ("/api/v1/lights/desk_ambient/effect", {"effect": "Off"}),
        ("/api/v1/lights/all/power", {"on": True}),
        ("/api/v1/lights/light.desk_ambient/temperature", {"kelvin": 4000}),
        ("/api/v1/lights/arbitrary/temperature", {"kelvin": 4000}),
    )
    for path, body in rejected:
        expected = 404 if "arbitrary" in path or "light.desk_ambient" in path else 403
        assert client.post(path, json=body).status_code == expected
    assert fake_ha.calls == []

    allowed = client.post(
        "/api/v1/lights/desk_ambient/temperature", json={"kelvin": 4500}
    )
    assert allowed.status_code == 200
    assert fake_ha.calls == [
        (
            "light",
            "turn_on",
            {"entity_id": "light.desk_ambient", "color_temp_kelvin": 4500},
        )
    ]


def test_temperature_canary_rejects_invalid_values_before_ha(client, fake_ha):
    app.dependency_overrides[get_settings] = lambda: Settings(
        ROOMOS_CONTROL_ENABLED=True,
        ROOMOS_CONTROL_POLICY="scoped",
        ROOMOS_CONTROL_DEVICE_IDS="desk_ambient",
        ROOMOS_CONTROL_ACTIONS="power,temperature",
    )
    for value in (2499, 9001, "warm"):
        response = client.post(
            "/api/v1/lights/desk_ambient/temperature", json={"kelvin": value}
        )
        assert response.status_code == 422
    assert fake_ha.calls == []


def test_standard_and_extended_temperature_ranges_are_independent(client, fake_ha):
    assert client.post(
        "/api/v1/lights/living_room/temperature", json={"kelvin": 6500}
    ).status_code == 200
    assert client.post(
        "/api/v1/lights/living_room/temperature", json={"kelvin": 6501}
    ).status_code == 422
    assert client.post(
        "/api/v1/lights/desk_ambient/temperature", json={"kelvin": 9000}
    ).status_code == 200
    assert client.post(
        "/api/v1/lights/desk_ambient/temperature", json={"kelvin": 9001}
    ).status_code == 422
    assert fake_ha.calls == [
        (
            "light",
            "turn_on",
            {"entity_id": "light.living_room", "color_temp_kelvin": 6500},
        ),
        (
            "light",
            "turn_on",
            {"entity_id": "light.desk_ambient", "color_temp_kelvin": 9000},
        ),
    ]


def test_kelvin_capability_boundaries_are_pure_validation(fake_ha):
    validate_kelvin(LIGHT_BY_ID["living_room"], 6500)
    for value in (6501, 9000):
        try:
            validate_kelvin(LIGHT_BY_ID["living_room"], value)
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 422
        else:
            raise AssertionError(f"standard unexpectedly accepted {value} K")

    validate_kelvin(LIGHT_BY_ID["desk_ambient"], 6501)
    validate_kelvin(LIGHT_BY_ID["desk_ambient"], 9000)
    try:
        validate_kelvin(LIGHT_BY_ID["desk_ambient"], 9001)
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 422
    else:
        raise AssertionError("extended unexpectedly accepted 9001 K")
    assert fake_ha.calls == []


def test_representative_light_maps_to_exact_entity(client, fake_ha):
    app.dependency_overrides[get_settings] = lambda: Settings(
        ROOMOS_CONTROL_ENABLED=True,
        ROOMOS_CONTROL_POLICY="scoped",
        ROOMOS_CONTROL_DEVICE_IDS="living_room",
        ROOMOS_CONTROL_ACTIONS="power",
    )
    response = client.post("/api/v1/lights/living_room/power", json={"on": True})
    assert response.status_code == 200
    assert fake_ha.calls == [("light", "turn_on", {"entity_id": "light.living_room"})]


def test_kill_switch_overrides_group_scope(client, fake_ha):
    app.dependency_overrides.pop(require_control_enabled, None)
    app.dependency_overrides[get_settings] = lambda: Settings(
        ROOMOS_CONTROL_ENABLED=False,
        ROOMOS_CONTROL_POLICY="scoped",
        ROOMOS_CONTROL_DEVICE_IDS="all_room_lights",
        ROOMOS_CONTROL_ACTIONS="power",
    )
    response = client.post("/api/v1/lights/all/power", json={"on": True})
    assert response.status_code == 403
    assert response.json() == {"detail": "RoomOS control is disabled"}
    assert fake_ha.calls == []


def test_kill_switch_overrides_temperature_scope(client, fake_ha):
    app.dependency_overrides.pop(require_control_enabled, None)
    app.dependency_overrides[get_settings] = lambda: Settings(
        ROOMOS_CONTROL_ENABLED=False,
        ROOMOS_CONTROL_POLICY="scoped",
        ROOMOS_CONTROL_DEVICE_IDS="desk_ambient",
        ROOMOS_CONTROL_ACTIONS="power,temperature",
    )
    response = client.post(
        "/api/v1/lights/desk_ambient/temperature", json={"kelvin": 4500}
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "RoomOS control is disabled"}
    assert fake_ha.calls == []


def test_color_canary_scope_is_server_side_and_exact(client, fake_ha):
    app.dependency_overrides[get_settings] = lambda: Settings(
        ROOMOS_CONTROL_ENABLED=True,
        ROOMOS_CONTROL_POLICY="scoped",
        ROOMOS_CONTROL_DEVICE_IDS="desk_ambient",
        ROOMOS_CONTROL_ACTIONS="power,color",
    )
    rejected = (
        ("/api/v1/lights/living_room/color", {"rgb": [128, 0, 255]}),
        ("/api/v1/lights/desk_ambient/brightness", {"brightness": 112}),
        ("/api/v1/lights/desk_ambient/temperature", {"kelvin": 4500}),
        ("/api/v1/lights/desk_ambient/effect", {"effect": "Off"}),
        ("/api/v1/lights/all/power", {"on": True}),
        ("/api/v1/lights/arbitrary/color", {"rgb": [128, 0, 255]}),
        ("/api/v1/lights/light.desk_ambient/color", {"rgb": [128, 0, 255]}),
    )
    for path, body in rejected:
        expected = 404 if "arbitrary" in path or "light.desk_ambient" in path else 403
        assert client.post(path, json=body).status_code == expected
    assert fake_ha.calls == []

    allowed = client.post(
        "/api/v1/lights/desk_ambient/color", json={"rgb": [128, 0, 255]}
    )
    assert allowed.status_code == 200
    assert fake_ha.calls == [
        (
            "light",
            "turn_on",
            {"entity_id": "light.desk_ambient", "rgb_color": [128, 0, 255]},
        )
    ]


def test_color_canary_rejects_invalid_rgb_before_ha(client, fake_ha):
    app.dependency_overrides[get_settings] = lambda: Settings(
        ROOMOS_CONTROL_ENABLED=True,
        ROOMOS_CONTROL_POLICY="scoped",
        ROOMOS_CONTROL_DEVICE_IDS="desk_ambient",
        ROOMOS_CONTROL_ACTIONS="power,color",
    )
    invalid = (
        {"rgb": [-1, 0, 0]},
        {"rgb": [0, 0, 256]},
        {"rgb": [128, 0]},
        {"rgb": [128, 0, "purple"]},
        {},
    )
    for payload in invalid:
        response = client.post("/api/v1/lights/desk_ambient/color", json=payload)
        assert response.status_code == 422
    assert fake_ha.calls == []


def test_kill_switch_overrides_color_scope(client, fake_ha):
    app.dependency_overrides.pop(require_control_enabled, None)
    app.dependency_overrides[get_settings] = lambda: Settings(
        ROOMOS_CONTROL_ENABLED=False,
        ROOMOS_CONTROL_POLICY="scoped",
        ROOMOS_CONTROL_DEVICE_IDS="desk_ambient",
        ROOMOS_CONTROL_ACTIONS="power,color",
    )
    response = client.post(
        "/api/v1/lights/desk_ambient/color", json={"rgb": [128, 0, 255]}
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "RoomOS control is disabled"}
    assert fake_ha.calls == []
