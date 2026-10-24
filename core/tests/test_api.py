from datetime import UTC, datetime, timedelta

from app.catalog import LIGHTS, LIGHT_BY_ID, PHYSICAL_LIGHT_ENTITY_IDS
from app.ha_client import HomeAssistantUnavailable
from app.config import Settings
from app.models import DnsClusterStatus, DnsResolverStatus
from app.main import app, get_ha, get_settings, require_control_enabled
from app.service import _metric, validate_kelvin


def test_health_has_no_secrets(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "token" not in str(body).lower()
    assert body["notifications"] == {
        "status": "disabled",
        "configured_providers": 0,
        "configured_destinations": 0,
        "pending_count": 0,
        "failed_count": 0,
        "external_attempts": 0,
        "external_deliveries": 0,
        "last_delivery": None,
    }


def test_system_version_exposes_only_bounded_release_identity(client):
    response = client.get("/api/v1/system/version")
    assert response.status_code == 200
    assert response.json() == {
        "release": "development",
        "revision": "unknown",
        "component": "core",
    }
    assert set(response.json()) == {"release", "revision", "component"}


def test_lights_use_logical_ids(client):
    response = client.get("/api/v1/lights")
    assert response.status_code == 200
    lights = response.json()
    assert {item["id"] for item in lights} == {
        "room_main",
        "room_2",
        "room_3",
        "room_4",
        "desk_ambient",
    }
    assert "entity_id" not in str(lights)


def test_system_services_expose_truthful_nextcloud_and_dns_status(client, fake_ha):
    now = datetime.now(UTC).isoformat()
    fake_ha.states.update(
        {
            "sensor.frigate_status": {
                "entity_id": "sensor.frigate_status",
                "state": "running",
                "last_updated": now,
                "attributes": {},
            },
            "sensor.primary_nextcloud": {
                "entity_id": "sensor.primary_nextcloud",
                "state": "OK",
                "last_updated": now,
                "attributes": {},
            },
        }
    )
    services = {
        service["id"]: service for service in client.get("/api/v1/system").json()["services"]
    }
    assert services["home_assistant"] == {
        "id": "home_assistant",
        "status": "online",
        "health": "healthy",
        "source": "RoomOS HA API",
        "stale": False,
        "freshness": "fresh",
    }
    assert services["frigate"]["status"] == "online"
    assert services["frigate"]["stale"] is False
    assert services["frigate"]["freshness"] == "fresh"
    assert services["nextcloud"]["status"] == "online"
    assert services["nextcloud"]["stale"] is False
    assert services["nextcloud"]["freshness"] == "fresh"
    assert services["dns"]["status"] == "unknown"
    assert services["dns"]["health"] == "unknown"
    assert services["dns"]["stale"] is False
    assert services["dns"]["freshness"] == "unknown"
    system = client.get("/api/v1/system").json()
    assert system["dns_cluster"]["status"] == "unknown"
    assert system["dns_cluster"]["primary"]["status"] == "unknown"
    assert "address" not in str(system["dns_cluster"]).lower()


def test_system_uses_canonical_dns_cluster_snapshot(client):
    checked = datetime.now(UTC)
    app.state.dns_health._snapshot = DnsClusterStatus(
        status="degraded",
        primary=DnsResolverStatus(
            id="primary", role="primary", status="healthy",
            last_checked=checked, response_time_ms=1.2,
        ),
        secondary=DnsResolverStatus(
            id="secondary", role="secondary", status="offline",
            last_checked=checked,
        ),
        last_checked=checked,
    )
    body = client.get("/api/v1/system").json()
    dns = next(service for service in body["services"] if service["id"] == "dns")
    assert dns == {
        "id": "dns",
        "status": "online",
        "health": "degraded",
        "source": "RoomOS functional DNS probe",
        "stale": False,
        "freshness": "fresh",
    }
    assert body["dns_cluster"]["status"] == "degraded"
    assert body["dns_cluster"]["primary"]["response_time_ms"] == 1.2
    assert "192.168" not in str(body)


def test_change_driven_service_timestamp_is_not_mistaken_for_poll_freshness(client, fake_ha):
    unchanged = (datetime.now(UTC) - timedelta(hours=12)).isoformat()
    fake_ha.states["sensor.primary_nextcloud"] = {
        "entity_id": "sensor.primary_nextcloud",
        "state": "OK",
        "last_updated": unchanged,
        "attributes": {},
    }
    services = {
        service["id"]: service for service in client.get("/api/v1/system").json()["services"]
    }
    assert services["nextcloud"]["status"] == "online"
    assert services["nextcloud"]["stale"] is False
    assert services["nextcloud"]["freshness"] == "fresh"


def test_metric_freshness_is_source_aware():
    now = datetime.now(UTC)
    old = (now - timedelta(minutes=6)).isoformat()
    state = {"state": "42", "last_updated": old, "attributes": {}}
    assert _metric("sensor.primary_cpu", state, now=now).freshness == "stale"
    assert _metric("sensor.primary_disk", state, now=now).freshness == "fresh"
    assert _metric(
        "sensor.primary_uptime",
        {**state, "last_updated": (now - timedelta(minutes=70)).isoformat()},
        now=now,
    ).freshness == "fresh"
    assert _metric(
        "sensor.primary_uptime",
        {**state, "last_updated": (now - timedelta(minutes=80)).isoformat()},
        now=now,
    ).freshness == "stale"


def test_metric_freshness_distinguishes_unknown_and_unavailable():
    unknown = _metric(
        "sensor.primary_cpu", {"state": "unknown", "attributes": {}},
    )
    unavailable = _metric(
        "sensor.primary_cpu", {"state": "unavailable", "attributes": {}},
    )
    missing = _metric("sensor.primary_cpu", None)
    assert (unknown.freshness, unknown.available, unknown.value) == ("unknown", False, None)
    assert (unavailable.freshness, unavailable.available, unavailable.value) == (
        "unavailable",
        False,
        None,
    )
    assert (missing.freshness, missing.available, missing.value) == ("unknown", False, None)


def test_offline_node_does_not_report_old_metrics_as_live(client, fake_ha):
    now = datetime.now(UTC).isoformat()
    fake_ha.states.update(
        {
            "binary_sensor.primary_online": {
                "state": "off", "last_updated": now, "attributes": {},
            },
            "sensor.primary_cpu": {
                "state": "12", "last_updated": now, "attributes": {"unit_of_measurement": "%"},
            },
        }
    )
    node = client.get("/api/v1/system/primary").json()
    assert node["online"] is False
    assert node["metrics"]["primary_cpu"]["freshness"] == "stale"
    assert node["metrics"]["primary_cpu"]["stale"] is True


def test_shared_node_heartbeat_keeps_unchanged_sibling_metrics_fresh(client, fake_ha):
    now = datetime.now(UTC)
    old = (now - timedelta(hours=4)).isoformat()
    fake_ha.states.update(
        {
            "binary_sensor.primary_online": {
                "state": "on", "last_updated": old, "attributes": {},
            },
            "sensor.primary_cpu": {
                "state": "0", "last_updated": old, "attributes": {"unit_of_measurement": "%"},
            },
            "sensor.primary_ram": {
                "state": "47", "last_updated": now.isoformat(), "attributes": {"unit_of_measurement": "%"},
            },
            "sensor.primary_disk": {
                "state": "29", "last_updated": old, "attributes": {"unit_of_measurement": "%"},
            },
        }
    )
    node = client.get("/api/v1/system/primary").json()
    assert node["online"] is True
    assert node["metrics"]["primary_cpu"]["freshness"] == "fresh"
    assert node["metrics"]["primary_disk"]["freshness"] == "fresh"
    assert node["metrics"]["primary_online"]["freshness"] == "fresh"


def test_invalid_and_arbitrary_entity_ids_are_rejected(client, fake_ha):
    for light_id in ("unknown", "light.living_room", "light.evil"):
        response = client.post(f"/api/v1/lights/{light_id}/power", json={"on": True})
        assert response.status_code == 404
    assert fake_ha.calls == []


def test_invalid_brightness_is_rejected_before_ha(client, fake_ha):
    for value in (0, 256, -1, "bright"):
        response = client.post(
            "/api/v1/lights/room_main/brightness", json={"brightness": value}
        )
        assert response.status_code == 422
    assert fake_ha.calls == []


def test_kelvin_respects_per_device_ranges(client, fake_ha):
    assert client.post(
        "/api/v1/lights/room_main/temperature", json={"kelvin": 7000}
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
        response = client.post("/api/v1/lights/room_main/color", json=payload)
        assert response.status_code == 422
    assert fake_ha.calls == []


def test_all_lights_expands_to_five_physical_entities(client, fake_ha):
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
    assert len(entity_ids) == len(set(entity_ids)) == 5
    assert "light.all_lights" not in entity_ids


def test_all_room_lights_group_exposes_only_unique_logical_ids(client):
    response = client.get("/api/v1/groups/all_room_lights")
    assert response.status_code == 200
    member_ids = response.json()["member_ids"]
    assert member_ids == ["room_main", "room_2", "room_3", "room_4", "desk_ambient"]
    assert len(member_ids) == len(set(member_ids)) == 5
    assert all(not member_id.startswith("light.") for member_id in member_ids)


def test_group_reports_unavailable_target_without_calling_it(client, fake_ha):
    fake_ha.states["light.kitchen"] = {
        "entity_id": "light.kitchen",
        "state": "unavailable",
        "last_updated": fake_ha.states["light.living_room"]["last_updated"],
        "attributes": {},
    }
    response = client.post("/api/v1/lights/all/power", json={"on": True})
    assert response.status_code == 207
    body = response.json()
    assert body["accepted"] is False
    assert body["succeeded"] == ["room_main", "room_2", "room_4", "desk_ambient"]
    assert body["failed"] == [{"device_id": "room_3", "reason": "unavailable"}]
    assert "light.kitchen" not in [call[2]["entity_id"] for call in fake_ha.calls]


def test_group_reports_service_failure_and_does_not_retry(client, fake_ha):
    original_call_service = fake_ha.call_service

    async def fail_room_2_once(domain, service, payload):
        if payload["entity_id"] == "light.bedroom":
            fake_ha.calls.append((domain, service, payload))
            raise HomeAssistantUnavailable("simulated")
        return await original_call_service(domain, service, payload)

    fake_ha.call_service = fail_room_2_once
    response = client.post("/api/v1/lights/all/power", json={"on": True})
    assert response.status_code == 207
    body = response.json()
    assert body["accepted"] is False
    assert body["succeeded"] == ["room_main", "room_3", "room_4", "desk_ambient"]
    assert body["failed"] == [{"device_id": "room_2", "reason": "service_failed"}]
    called_ids = [call[2]["entity_id"] for call in fake_ha.calls]
    assert called_ids == list(PHYSICAL_LIGHT_ENTITY_IDS)
    assert called_ids.count("light.bedroom") == 1


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

    ha = HomeAssistantClient(Settings(HA_TOKEN="not-a-real-token"))  # privacy-scan:allow fake test credential
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

    ha = HomeAssistantClient(Settings(HA_TOKEN="not-a-real-token"))  # privacy-scan:allow fake test credential
    await ha._http.aclose()
    ha._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await ha.get_state("light.desk_ambient") == expected
    assert ha.connected is True
    await ha.close()


def test_effect_must_exist_in_device_effect_list(client, fake_ha):
    response = client.post(
        "/api/v1/lights/room_main/effect", json={"effect": "Not Real"}
    )
    assert response.status_code == 422
    assert fake_ha.calls == []


def test_live_default_blocks_all_control_routes(client, fake_ha):
    app.dependency_overrides.pop(require_control_enabled, None)
    app.dependency_overrides.pop(get_settings, None)
    routes = (
        ("/api/v1/lights/room_main/power", {"on": True}),
        ("/api/v1/lights/room_main/brightness", {"brightness": 100}),
        ("/api/v1/lights/room_main/color", {"rgb": [1, 2, 3]}),
        ("/api/v1/lights/room_main/temperature", {"kelvin": 3000}),
        ("/api/v1/lights/room_main/effect", {"effect": "Party"}),
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
        ("/api/v1/lights/room_main/power", {"on": True}),
        ("/api/v1/lights/room_2/power", {"on": True}),
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
        ("/api/v1/lights/room_main/brightness", {"brightness": 100}),
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
        ("/api/v1/lights/room_main/temperature", {"kelvin": 4000}),
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


def test_l530_and_l920_temperature_ranges_are_independent(client, fake_ha):
    assert client.post(
        "/api/v1/lights/room_main/temperature", json={"kelvin": 6500}
    ).status_code == 200
    assert client.post(
        "/api/v1/lights/room_main/temperature", json={"kelvin": 6501}
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
    validate_kelvin(LIGHT_BY_ID["room_main"], 6500)
    for value in (6501, 9000):
        try:
            validate_kelvin(LIGHT_BY_ID["room_main"], value)
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 422
        else:
            raise AssertionError(f"L530 unexpectedly accepted {value} K")

    validate_kelvin(LIGHT_BY_ID["desk_ambient"], 6501)
    validate_kelvin(LIGHT_BY_ID["desk_ambient"], 9000)
    try:
        validate_kelvin(LIGHT_BY_ID["desk_ambient"], 9001)
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 422
    else:
        raise AssertionError("L920 unexpectedly accepted 9001 K")
    assert fake_ha.calls == []


def test_representative_l530_maps_to_exact_entity(client, fake_ha):
    app.dependency_overrides[get_settings] = lambda: Settings(
        ROOMOS_CONTROL_ENABLED=True,
        ROOMOS_CONTROL_POLICY="scoped",
        ROOMOS_CONTROL_DEVICE_IDS="room_main",
        ROOMOS_CONTROL_ACTIONS="power",
    )
    response = client.post("/api/v1/lights/room_main/power", json={"on": True})
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
        ("/api/v1/lights/room_main/color", {"rgb": [128, 0, 255]}),
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
