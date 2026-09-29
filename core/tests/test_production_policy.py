import logging

from app.audit import command_audit
from app.catalog import LIGHTS
from app.config import Settings
from app.main import app, get_settings, require_control_enabled
from app.policy import PHYSICAL_ACTIONS, PRODUCTION_POLICY, writable_permissions


def production_settings(*, enabled: bool = True) -> Settings:
    return Settings(
        ROOMOS_CONTROL_ENABLED=enabled,
        ROOMOS_CONTROL_POLICY="production",
    )


def test_production_policy_is_fixed_and_group_is_power_only():
    assert set(PRODUCTION_POLICY) == {light.id for light in LIGHTS} | {"all_room_lights"}
    for light in LIGHTS:
        assert PRODUCTION_POLICY[light.id] == PHYSICAL_ACTIONS
    assert PRODUCTION_POLICY["all_room_lights"] == frozenset({"power"})


def test_health_publishes_exact_per_device_permissions(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["control_policy"] == "production"
    assert body["writable_permissions"]["all_room_lights"] == ["power"]
    assert body["writable_permissions"]["desk_ambient"] == sorted(PHYSICAL_ACTIONS)


def test_unknown_raw_and_unregistered_ids_never_reach_ha(client, fake_ha):
    for device_id in ("arbitrary", "light.living_room", "light.not_in_roomos"):
        response = client.post(f"/api/v1/lights/{device_id}/power", json={"on": True})
        assert response.status_code == 404
    assert fake_ha.calls == []


def test_group_rejects_every_non_power_action_before_ha(client, fake_ha):
    cases = (
        ("brightness", {"brightness": 100}),
        ("color", {"rgb": [128, 0, 255]}),
        ("temperature", {"kelvin": 4000}),
        ("effect", {"effect": "Party"}),
    )
    for action, payload in cases:
        response = client.post(f"/api/v1/lights/all_room_lights/{action}", json=payload)
        assert response.status_code == 403
    assert fake_ha.calls == []


def test_unavailable_device_is_rejected_before_service(client, fake_ha):
    fake_ha.states["light.living_room"]["state"] = "unavailable"
    response = client.post("/api/v1/lights/living_room/power", json={"on": True})
    assert response.status_code == 409
    assert fake_ha.calls == []


def test_effect_requires_live_advertisement(client, fake_ha):
    fake_ha.states["light.living_room"]["attributes"]["effect_list"] = []
    response = client.post("/api/v1/lights/living_room/effect", json={"effect": "Party"})
    assert response.status_code == 422
    assert fake_ha.calls == []


def test_kill_switch_overrides_production_policy(client, fake_ha):
    app.dependency_overrides.pop(require_control_enabled, None)
    app.dependency_overrides[get_settings] = lambda: production_settings(enabled=False)
    response = client.post("/api/v1/lights/living_room/power", json={"on": True})
    assert response.status_code == 403
    assert writable_permissions(production_settings(enabled=False)) == {}
    assert fake_ha.calls == []


def test_safe_audit_contains_logical_result_and_no_credentials(caplog):
    with caplog.at_level(logging.INFO, logger="roomos.audit"):
        command_audit("living_room", "power", "success")
    rendered = caplog.text
    assert "timestamp=" in rendered
    assert "target=living_room" in rendered
    assert "action=power" in rendered
    assert "result=success" in rendered
    assert "Authorization" not in rendered
    assert "Bearer" not in rendered
    assert "light.living_room" not in rendered
