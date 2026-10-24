import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.ha_client import HomeAssistantUnavailable
from app.main import app, get_scenes, require_control_enabled
from app.models import Capabilities, LightState
from app.scenes import (
    SceneConfigEntry,
    SceneConfigFile,
    SceneEngine,
    SceneLastApplied,
    all_off_scene,
    install_all_off_scene,
    target_matches_live,
)


def configured_scene(scene_id="movie", targets=None):
    return SceneConfigEntry.model_validate(
        {
            "id": scene_id,
            "targets": targets
            or [
                {"device_id": "room_main", "power": True},
                {"device_id": "desk_ambient", "brightness": 80},
            ],
        }
    )


def install_engine(scene, *, cooldown=0.5):
    settings = Settings(
        ROOMOS_AUTH_ENABLED=False,
        ROOMOS_CONTROL_ENABLED=True,
        ROOMOS_CONTROL_POLICY="production",
        ROOMOS_SCENE_DUPLICATE_WINDOW_SECONDS=cooldown,
        ROOMOS_SCENE_MAX_CONCURRENCY=2,
    )
    engine = SceneEngine(settings, {scene.id: scene})
    app.dependency_overrides[get_scenes] = lambda: engine
    return engine


def live_light(
    device_id,
    *,
    on,
    brightness=None,
    kelvin=None,
    rgb=None,
    effect=None,
    color_mode=None,
    available=True,
):
    return LightState(
        id=device_id,
        name=device_id,
        available=available,
        on=on if available else None,
        brightness=brightness,
        color_mode=color_mode,
        color_temperature_kelvin=kelvin,
        rgb=rgb,
        effect=effect,
        capabilities=Capabilities(min_kelvin=2500, max_kelvin=9000, effects=True),
    )


def full_scene(scene_id, room_target, desk_target):
    return configured_scene(
        scene_id=scene_id,
        targets=[
            {"device_id": device_id, **room_target}
            for device_id in ("room_main", "room_2", "room_3", "room_4")
        ]
        + [{"device_id": "desk_ambient", **desk_target}],
    )


def production_scene_set():
    return {
        scene.id: scene
        for scene in (
            full_scene("normal", {"power": True, "brightness": 191, "kelvin": 4000}, {"power": False}),
            full_scene("work_1", {"power": True, "brightness": 255, "kelvin": 5000}, {"power": False}),
            full_scene("work_2", {"power": False}, {"power": True, "brightness": 255, "kelvin": 6500}),
            full_scene("gaming_1", {"power": False}, {"power": True, "effect": "Aurora"}),
            full_scene("gaming_2", {"power": False}, {"power": True, "effect": "Lightning"}),
            full_scene("movie", {"power": False}, {"power": True, "brightness": 26, "kelvin": 2700}),
            full_scene("night", {"power": False}, {"power": True, "brightness": 13, "kelvin": 2500}),
            all_off_scene(),
        )
    }


def scene_live_state(scene_id):
    room_on = scene_id in {"normal", "work_1"}
    room_brightness = 191 if scene_id == "normal" else 255 if scene_id == "work_1" else 80
    room_kelvin = 4000 if scene_id == "normal" else 5000 if scene_id == "work_1" else 3500
    desk = {
        "normal": dict(on=False, brightness=77, kelvin=3100, effect="Aurora"),
        "work_1": dict(on=False, brightness=77, kelvin=3100, effect="Lightning"),
        "work_2": dict(on=True, brightness=255, kelvin=6500, effect="off"),
        "gaming_1": dict(on=True, brightness=123, kelvin=4200, effect="Aurora", color_mode="hs"),
        "gaming_2": dict(on=True, brightness=123, kelvin=4200, effect="Lightning", color_mode="hs"),
        "movie": dict(on=True, brightness=26, kelvin=2700, effect="Lightning", color_mode="color_temp"),
        "night": dict(on=True, brightness=13, kelvin=2500, effect="Aurora", color_mode="color_temp"),
        "all_off": dict(on=False, brightness=13, kelvin=2500, effect="Aurora", color_mode="color_temp"),
    }[scene_id]
    return [
        live_light(device_id, on=room_on, brightness=room_brightness, kelvin=room_kelvin, effect="off")
        for device_id in ("room_main", "room_2", "room_3", "room_4")
    ] + [live_light("desk_ambient", **desk)]


@pytest.mark.parametrize(
    "scene_id",
    ["normal", "work_1", "work_2", "gaming_1", "gaming_2", "movie", "night", "all_off"],
)
def test_live_state_matches_each_production_scene(scene_id):
    engine = SceneEngine(Settings(), production_scene_set())
    engine.reconcile(scene_live_state(scene_id))
    assert engine.active_scene == scene_id


@pytest.mark.parametrize(
    ("scene_id", "device_id", "change"),
    [
        ("night", "desk_ambient", {"brightness": 200}),
        ("normal", "room_2", {"on": False}),
        ("work_1", "room_3", {"brightness": 240}),
        ("work_2", "desk_ambient", {"color_temperature_kelvin": 6200}),
    ],
)
def test_manual_change_clears_active_scene(scene_id, device_id, change):
    engine = SceneEngine(Settings(), production_scene_set())
    lights = scene_live_state(scene_id)
    current = next(light for light in lights if light.id == device_id)
    changed = current.model_copy(update=change)
    engine.reconcile([changed if light.id == device_id else light for light in lights])
    assert engine.active_scene is None


def test_night_with_desk_turned_off_is_reported_as_all_off():
    engine = SceneEngine(Settings(), production_scene_set())
    lights = scene_live_state("night")
    engine.reconcile(
        [light.model_copy(update={"on": False}) if light.id == "desk_ambient" else light for light in lights]
    )
    assert engine.active_scene == "all_off"


def test_off_target_ignores_stale_attributes_and_effects_are_exact():
    off = SceneConfigEntry.model_validate(
        {"id": "night", "targets": [{"device_id": "desk_ambient", "power": False}]}
    ).targets[0]
    assert target_matches_live(
        off,
        live_light(
            "desk_ambient", on=False, brightness=255, kelvin=6500, rgb=(1, 2, 3), effect="Aurora"
        ),
    )
    aurora = SceneConfigEntry.model_validate(
        {"id": "gaming_1", "targets": [{"device_id": "desk_ambient", "power": True, "effect": "Aurora"}]}
    ).targets[0]
    assert target_matches_live(
        aurora, live_light("desk_ambient", on=True, effect="Aurora", color_mode="hs")
    )
    assert not target_matches_live(
        aurora, live_light("desk_ambient", on=True, effect="Lightning", color_mode="hs")
    )
    assert not target_matches_live(
        aurora, live_light("desk_ambient", on=True, effect="Aurora", color_mode="color_temp")
    )


def test_matching_tolerances_are_narrow_and_explicit():
    target = SceneConfigEntry.model_validate(
        {
            "id": "normal",
            "targets": [
                {
                    "device_id": "room_main",
                    "power": True,
                    "brightness": 191,
                    "kelvin": 4000,
                    "rgb": [150, 70, 255],
                }
            ],
        }
    ).targets[0]
    assert target_matches_live(
        target,
        live_light("room_main", on=True, brightness=189, kelvin=4050, rgb=(148, 73, 252)),
    )
    assert not target_matches_live(
        target,
        live_light("room_main", on=True, brightness=188, kelvin=4000, rgb=(150, 70, 255)),
    )
    assert not target_matches_live(
        target,
        live_light("room_main", on=True, brightness=191, kelvin=4051, rgb=(150, 70, 255)),
    )
    assert not target_matches_live(
        target,
        live_light("room_main", on=True, brightness=191, kelvin=4000, rgb=(146, 70, 255)),
    )


def test_ambiguous_match_prefers_history_then_catalog_order():
    normal = configured_scene("normal", [{"device_id": "desk_ambient", "power": False}])
    work = configured_scene("work_1", [{"device_id": "desk_ambient", "power": False}])
    engine = SceneEngine(Settings(), {"normal": normal, "work_1": work})
    lights = [live_light("desk_ambient", on=False, effect="Aurora")]
    engine.reconcile(lights)
    assert engine.active_scene == "normal"
    engine._last_applied = SceneLastApplied(
        scene="work_1", name="Work 1", status="success", applied_at="2026-09-30T00:00:00Z"
    )
    engine.reconcile(lights)
    assert engine.active_scene == "work_1"


def test_scene_list_is_server_owned_and_unconfigured_by_default(client, fake_ha):
    response = client.get("/api/v1/scenes")
    assert response.status_code == 200
    assert [(scene["id"], scene["name"]) for scene in response.json()["scenes"]] == [
        ("normal", "Normal"),
        ("work_1", "Work 1"),
        ("work_2", "Work 2"),
        ("gaming_1", "Game 1"),
        ("gaming_2", "Game 2"),
        ("movie", "Film"),
        ("night", "Gece"),
        ("all_off", "Tümünü Kapat"),
    ]
    assert all(not scene["configured"] for scene in response.json()["scenes"])
    assert all(not scene["activatable"] for scene in response.json()["scenes"])
    assert client.post("/api/v1/scenes/movie/activate").status_code == 409
    assert fake_ha.calls == []


def test_legacy_scene_ids_are_rejected_and_tracked_example_remains_inert():
    with pytest.raises(ValidationError):
        configured_scene(scene_id="work")
    with pytest.raises(ValidationError):
        configured_scene(scene_id="gaming")
    example_path = Path(__file__).parents[1] / "config" / "scenes.example.json"
    example = SceneConfigFile.model_validate_json(example_path.read_text(encoding="utf-8"))
    assert example.scenes == []


def test_all_off_scene_is_fixed_power_only_for_all_logical_lights():
    scene = all_off_scene()
    assert scene.id == "all_off"
    assert [target.device_id for target in scene.targets] == [
        "room_main", "room_2", "room_3", "room_4", "desk_ambient"
    ]
    assert all(target.power is False for target in scene.targets)
    assert all(target.actions() == ("power",) for target in scene.targets)
    assert all(
        target.brightness is target.rgb is target.kelvin is target.effect is None
        for target in scene.targets
    )


def test_all_off_installer_preserves_existing_seven_semantically(tmp_path):
    path = tmp_path / "scenes.json"
    original = SceneConfigFile(scenes=list(production_scene_set().values())[:-1])
    path.write_text(original.model_dump_json(exclude_none=True), encoding="utf-8")
    installed = install_all_off_scene(str(path))
    assert len(installed.scenes) == 8
    assert installed.scenes[:7] == original.scenes
    assert installed.scenes[-1] == all_off_scene()
    assert path.stat().st_mode & 0o777 == 0o600


def test_game_scenes_use_only_approved_logical_ids_and_effect_actions():
    for scene_id, effect in (("gaming_1", "Aurora"), ("gaming_2", "Lightning")):
        scene = configured_scene(
            scene_id=scene_id,
            targets=[
                {"device_id": "room_main", "power": False},
                {"device_id": "room_2", "power": False},
                {"device_id": "room_3", "power": False},
                {"device_id": "room_4", "power": False},
                {"device_id": "desk_ambient", "power": True, "effect": effect},
            ],
        )
        assert scene.targets[-1].actions() == ("power", "effect")
        assert scene.targets[-1].brightness is None
        assert scene.targets[-1].rgb is None
        assert scene.targets[-1].kelvin is None


def test_scene_full_success_and_last_applied(client, fake_ha):
    install_engine(configured_scene())
    response = client.post("/api/v1/scenes/movie/activate")
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert response.json()["applied"] == ["room_main", "desk_ambient"]
    assert len(fake_ha.calls) == 2
    catalog = client.get("/api/v1/scenes").json()
    assert catalog["last_applied"]["scene"] == "movie"
    assert next(scene for scene in catalog["scenes"] if scene["id"] == "movie")["activatable"] is True


def test_unavailable_target_is_skipped_and_partial_does_not_rollback(client, fake_ha):
    fake_ha.states["light.living_room"]["state"] = "unavailable"
    install_engine(configured_scene())
    response = client.post("/api/v1/scenes/movie/activate")
    body = response.json()
    assert body["status"] == "partial"
    assert body["applied"] == ["desk_ambient"]
    assert body["skipped"] == [{"device": "room_main", "reason": "unavailable"}]
    assert body["failed"] == []
    assert len(fake_ha.calls) == 1
    assert fake_ha.calls[0][2]["entity_id"] == "light.desk_ambient"


def test_one_ha_failure_is_reported_once_without_retry(client, fake_ha):
    original = fake_ha.call_service
    attempts = 0

    async def fail_one(domain, service, payload):
        nonlocal attempts
        attempts += 1
        if payload["entity_id"] == "light.living_room":
            raise HomeAssistantUnavailable("Home Assistant is unavailable")
        return await original(domain, service, payload)

    fake_ha.call_service = fail_one
    install_engine(configured_scene())
    body = client.post("/api/v1/scenes/movie/activate").json()
    assert body["status"] == "partial"
    assert body["failed"] == [{"device": "room_main", "reason": "service_failed"}]
    assert body["applied"] == ["desk_ambient"]
    assert attempts == 2


def test_off_target_is_woken_before_attributes_are_applied():
    class StatefulHA:
        def __init__(self):
            self.state = "off"
            self.calls = []

        async def get_state(self, entity_id):
            return {
                "entity_id": entity_id,
                "state": self.state,
                "attributes": {
                    "brightness": 1 if self.state == "on" else None,
                    "color_mode": "color_temp" if self.state == "on" else None,
                    "min_color_temp_kelvin": 2500,
                    "max_color_temp_kelvin": 9000,
                },
                "last_updated": None,
            }

        async def call_service(self, domain, service, payload):
            self.calls.append((domain, service, payload))
            if service == "turn_on":
                self.state = "on"
            return []

    scene = configured_scene(
        scene_id="work_2",
        targets=[
            {
                "device_id": "desk_ambient",
                "power": True,
                "brightness": 255,
                "kelvin": 6500,
            }
        ],
    )
    settings = Settings(
        ROOMOS_CONTROL_ENABLED=True,
        ROOMOS_CONTROL_POLICY="production",
    )
    ha = StatefulHA()
    result = asyncio.run(SceneEngine(settings, {"work_2": scene}).activate("work_2", ha))
    assert result.status == "success"
    assert ha.calls == [
        ("light", "turn_on", {"entity_id": "light.desk_ambient"}),
        (
            "light",
            "turn_on",
            {
                "entity_id": "light.desk_ambient",
                "brightness": 255,
                "color_temp_kelvin": 6500,
            },
        ),
    ]


def test_unconfirmed_power_on_stops_before_attribute_call():
    class NonConfirmingHA:
        def __init__(self):
            self.calls = []

        async def get_state(self, entity_id):
            return {
                "entity_id": entity_id,
                "state": "off",
                "attributes": {
                    "brightness": None,
                    "color_mode": None,
                    "min_color_temp_kelvin": 2500,
                    "max_color_temp_kelvin": 9000,
                },
                "last_updated": None,
            }

        async def call_service(self, domain, service, payload):
            self.calls.append((domain, service, payload))
            return []

    scene = configured_scene(
        scene_id="work_2",
        targets=[
            {
                "device_id": "desk_ambient",
                "power": True,
                "brightness": 255,
                "kelvin": 6500,
            }
        ],
    )
    settings = Settings(
        ROOMOS_CONTROL_ENABLED=True,
        ROOMOS_CONTROL_POLICY="production",
    )
    ha = NonConfirmingHA()
    result = asyncio.run(SceneEngine(settings, {"work_2": scene}).activate("work_2", ha))
    assert result.status == "failed"
    assert result.failed[0].reason == "power_on_not_confirmed"
    assert ha.calls == [
        ("light", "turn_on", {"entity_id": "light.desk_ambient"}),
    ]


def test_invalid_scene_and_duplicate_activation_stop_before_extra_ha_calls(client, fake_ha):
    install_engine(configured_scene(), cooldown=2.0)
    assert client.post("/api/v1/scenes/not-a-scene/activate").status_code == 404
    assert client.post("/api/v1/scenes/movie/activate").status_code == 200
    first_count = len(fake_ha.calls)
    assert client.post("/api/v1/scenes/movie/activate").status_code == 429
    assert len(fake_ha.calls) == first_count


def test_scene_activation_rejects_arbitrary_client_payload(client, fake_ha):
    install_engine(configured_scene())
    response = client.post(
        "/api/v1/scenes/movie/activate",
        json={"device_id": "light.living_room", "service": "turn_on"},
    )
    assert response.status_code == 422
    assert fake_ha.calls == []


def test_concurrent_duplicate_activation_is_rejected():
    class SlowHA:
        def __init__(self):
            self.calls = []

        async def get_state(self, entity_id):
            return {"entity_id": entity_id, "state": "off", "attributes": {}, "last_updated": None}

        async def call_service(self, domain, service, payload):
            self.calls.append((domain, service, payload))
            await asyncio.sleep(0.05)

    settings = Settings(
        ROOMOS_CONTROL_ENABLED=True,
        ROOMOS_CONTROL_POLICY="production",
        ROOMOS_SCENE_DUPLICATE_WINDOW_SECONDS=2,
    )
    engine = SceneEngine(settings, {"movie": configured_scene(targets=[{"device_id": "room_main", "power": True}])})
    ha = SlowHA()

    async def exercise():
        first = asyncio.create_task(engine.activate("movie", ha))
        await asyncio.sleep(0.01)
        with pytest.raises(Exception) as rejected:
            await engine.activate("movie", ha)
        assert getattr(rejected.value, "status_code", None) == 429
        await first

    asyncio.run(exercise())
    assert len(ha.calls) == 1


def test_scene_execution_respects_bounded_concurrency():
    class TrackingHA:
        def __init__(self):
            self.active = 0
            self.peak = 0
            self.calls = 0

        async def get_state(self, entity_id):
            return {"entity_id": entity_id, "state": "off", "attributes": {}, "last_updated": None}

        async def call_service(self, domain, service, payload):
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.calls += 1
            await asyncio.sleep(0.02)
            self.active -= 1

    settings = Settings(
        ROOMOS_CONTROL_ENABLED=True,
        ROOMOS_CONTROL_POLICY="production",
        ROOMOS_SCENE_MAX_CONCURRENCY=2,
    )
    targets = [{"device_id": device_id, "power": True} for device_id in (
        "room_main", "room_2", "room_3", "room_4", "desk_ambient"
    )]
    engine = SceneEngine(settings, {"normal": configured_scene("normal", targets)})
    ha = TrackingHA()
    result = asyncio.run(engine.activate("normal", ha))
    assert result.status == "success"
    assert ha.calls == 5
    assert ha.peak == 2


def test_malformed_server_scene_values_and_raw_entity_ids_are_impossible(fake_ha):
    invalid_payloads = [
        {"scenes": [{"id": "movie", "targets": [{"device_id": "light.living_room", "power": True}]}]},
        {"scenes": [{"id": "movie", "targets": [{"device_id": "room_main", "brightness": 0}]}]},
        {"scenes": [{"id": "movie", "targets": [{"device_id": "room_main", "service": "turn_on"}]}]},
    ]
    for payload in invalid_payloads:
        with pytest.raises(ValidationError):
            SceneConfigFile.model_validate(payload)
    assert fake_ha.calls == []


def test_kill_switch_rejects_scene_before_ha(client, fake_ha):
    install_engine(configured_scene())
    app.dependency_overrides.pop(require_control_enabled, None)
    app.dependency_overrides[get_settings] = lambda: Settings(
        ROOMOS_AUTH_ENABLED=False,
        ROOMOS_CONTROL_ENABLED=False,
        ROOMOS_CONTROL_POLICY="read_only",
    )
    assert client.post("/api/v1/scenes/movie/activate").status_code == 403
    assert fake_ha.calls == []
