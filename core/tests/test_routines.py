import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.config import Settings
from app.main import app, get_routines
from app.routines import (
    ActivateSceneAction,
    EveningHomeTrigger,
    ManualTrigger,
    MANAGED_ALL_OFF_ID,
    MANAGED_EVENING_ID,
    Routine,
    RoutineCreate,
    RoutineDocument,
    RoutineEngine,
    RoutineUpdate,
    TimeTrigger,
    evening_window_open,
    load_routine_document,
)
from app.catalog import PRESENCE_ENTITY_ID, SOLAR_ENTITY_ID
from app.scenes import SceneActivationResult


NOW = datetime(2026, 9, 30, 19, 30, tzinfo=UTC)  # 22:30 Europe/Istanbul, Wednesday


def ha_state(value, at, **attributes):
    return {
        "state": value,
        "last_updated": at.isoformat(),
        "attributes": attributes,
    }


def solar_state(value, at):
    return ha_state(
        value,
        at,
        next_setting=datetime(2026, 10, 7, 15, 35, tzinfo=UTC).isoformat(),
        next_rising=datetime(2026, 10, 7, 4, 5, tzinfo=UTC).isoformat(),
    )


def managed_evening():
    return Routine(
        id=MANAGED_EVENING_ID,
        name="Evening Home Lighting",
        enabled=True,
        managed=True,
        trigger=EveningHomeTrigger(),
        action=ActivateSceneAction(scene_id="normal"),
        created_at=NOW,
        updated_at=NOW,
    )


def managed_all_off():
    return Routine(
        id=MANAGED_ALL_OFF_ID,
        name="02:00 All Off",
        enabled=True,
        managed=True,
        trigger=TimeTrigger(time="02:00", days=list(("mon", "tue", "wed", "thu", "fri", "sat", "sun"))),
        action=ActivateSceneAction(scene_id="all_off"),
        created_at=NOW,
        updated_at=NOW,
    )


def routine_record(
    routine_id="rt_11111111111111111111111111111111",
    *,
    enabled=True,
    trigger=None,
    scene_id="night",
):
    return Routine(
        id=routine_id,
        name="Night routine",
        enabled=enabled,
        trigger=trigger or TimeTrigger(time="22:30", days=["wed"]),
        action=ActivateSceneAction(scene_id=scene_id),
        created_at=NOW,
        updated_at=NOW,
    )


class FakeScenes:
    def __init__(self, *, status="success", fail_scene=None, wait=False):
        self.status = status
        self.fail_scene = fail_scene
        self.wait = wait
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = []

    async def record_rejection(self, *args, **kwargs):
        return "ex_" + "0" * 32

    async def activate(self, scene_id, ha, *, source="user", routine_id=None, routine_name=None, trigger="manual"):
        self.calls.append((scene_id, source, routine_id))
        self.started.set()
        if self.wait:
            await self.release.wait()
        if scene_id == self.fail_scene:
            raise HTTPException(status_code=503, detail="failed")
        return SceneActivationResult(
            scene=scene_id,
            name=scene_id,
            status=self.status,
            applied=["desk_ambient"] if self.status != "failed" else [],
            failed=[] if self.status != "failed" else [{"device": "desk_ambient", "reason": "service_failed"}],
            applied_at=NOW,
        )


def engine(tmp_path, routines, scenes=None, *, enabled=True, publish=None):
    settings = Settings(
        ROOMOS_CONTROL_ENABLED=enabled,
        ROOMOS_CONTROL_POLICY="production",
        ROOMOS_ROUTINES_PATH=str(tmp_path / "routines.json"),
        ROOMOS_ROUTINES_TIMEZONE="Europe/Istanbul",
    )
    return RoutineEngine(
        settings,
        scenes or FakeScenes(),
        object(),
        publish,
        document=RoutineDocument(routines=routines),
        clock=lambda: NOW,
    )


def test_routine_schema_rejects_unknown_actions_scenes_entities_time_days_and_fields():
    invalid = [
        {"name": "x", "trigger": {"type": "manual"}, "action": {"type": "light.turn_on", "scene_id": "night"}},
        {"name": "x", "trigger": {"type": "manual"}, "action": {"type": "activate_scene", "scene_id": "unknown"}},
        {"name": "x", "trigger": {"type": "time", "time": "25:00", "days": ["mon"]}, "action": {"type": "activate_scene", "scene_id": "night"}},
        {"name": "x", "trigger": {"type": "time", "time": "22:30", "days": ["funday"]}, "action": {"type": "activate_scene", "scene_id": "night"}},
        {"name": "x", "trigger": {"type": "manual", "entity_id": "light.living_room"}, "action": {"type": "activate_scene", "scene_id": "night"}},
        {"name": "x", "trigger": {"type": "manual"}, "action": {"type": "activate_scene", "scene_id": "night"}, "id": "user-id"},
    ]
    for payload in invalid:
        with pytest.raises(ValidationError):
            RoutineCreate.model_validate(payload)


@pytest.mark.asyncio
async def test_empty_ha_trigger_registry_rejects_logical_and_raw_ids(tmp_path):
    routines = engine(tmp_path, [])
    for trigger_id in ("gaming_pc", "light_living_room"):
        with pytest.raises(HTTPException) as rejected:
            await routines.create(
                RoutineCreate(
                    name="PC state",
                    trigger={"type": "ha_state", "trigger_id": trigger_id, "state": "on"},
                    action={"type": "activate_scene", "scene_id": "gaming_1"},
                )
            )
        assert rejected.value.status_code == 422


@pytest.mark.asyncio
async def test_disabled_routine_is_never_scheduled(tmp_path):
    scenes = FakeScenes()
    routines = engine(tmp_path, [routine_record(enabled=False)], scenes)
    assert await routines.tick(NOW) == []
    assert scenes.calls == []


@pytest.mark.asyncio
async def test_duplicate_tick_is_claimed_once_and_persisted(tmp_path):
    scenes = FakeScenes()
    routines = engine(tmp_path, [routine_record()], scenes)
    assert await routines.tick(NOW) == [routine_record().id]
    assert await routines.tick(NOW) == []
    assert len(scenes.calls) == 1
    assert routines.document.claimed_slots[routine_record().id] == "2026-09-30T19:30Z"


@pytest.mark.asyncio
async def test_claimed_schedule_slot_survives_restart(tmp_path):
    path = tmp_path / "routines.json"
    settings = Settings(
        ROOMOS_CONTROL_ENABLED=True,
        ROOMOS_CONTROL_POLICY="production",
        ROOMOS_ROUTINES_PATH=str(path),
        ROOMOS_ROUTINES_TIMEZONE="Europe/Istanbul",
    )
    first_scenes = FakeScenes()
    first = RoutineEngine(settings, first_scenes, object(), clock=lambda: NOW)
    created = await first.create(
        RoutineCreate(
            name="Night schedule",
            enabled=True,
            trigger={"type": "time", "time": "22:30", "days": ["wed"]},
            action={"type": "activate_scene", "scene_id": "night"},
        )
    )
    assert await first.tick(NOW) == [created.id]
    second_scenes = FakeScenes()
    restarted = RoutineEngine(settings, second_scenes, object(), clock=lambda: NOW)
    assert await restarted.tick(NOW) == []
    assert second_scenes.calls == []


@pytest.mark.asyncio
async def test_scheduler_does_not_catch_up_missed_minutes(tmp_path):
    scenes = FakeScenes()
    routines = engine(tmp_path, [routine_record()], scenes)
    later = datetime(2026, 9, 30, 19, 31, tzinfo=UTC)
    assert await routines.tick(later) == []
    assert scenes.calls == []


@pytest.mark.asyncio
async def test_per_routine_lock_rejects_concurrent_manual_run(tmp_path):
    scenes = FakeScenes(wait=True)
    routines = engine(tmp_path, [routine_record(trigger=ManualTrigger())], scenes)
    first = asyncio.create_task(routines.run(routine_record().id, source="manual"))
    await scenes.started.wait()
    with pytest.raises(HTTPException) as rejected:
        await routines.run(routine_record().id, source="manual")
    assert rejected.value.status_code == 429
    scenes.release.set()
    await first
    assert len(scenes.calls) == 1


@pytest.mark.asyncio
async def test_scheduler_failure_isolated_from_other_routine(tmp_path):
    first = routine_record(scene_id="night")
    second = routine_record(
        "rt_22222222222222222222222222222222",
        scene_id="normal",
    )
    scenes = FakeScenes(fail_scene="night")
    routines = engine(tmp_path, [first, second], scenes)
    assert await routines.tick(NOW) == [first.id, second.id]
    assert {call[0] for call in scenes.calls} == {"night", "normal"}
    assert routines._find(first.id).last_result.status == "failed"
    assert routines._find(second.id).last_result.status == "success"


@pytest.mark.asyncio
async def test_scheduler_execution_is_globally_bounded(tmp_path):
    class TrackingScenes(FakeScenes):
        def __init__(self):
            super().__init__()
            self.active = 0
            self.peak = 0

        async def activate(self, scene_id, ha, *, source="user", routine_id=None, routine_name=None, trigger="manual"):
            self.calls.append((scene_id, source, routine_id))
            self.active += 1
            self.peak = max(self.peak, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            return SceneActivationResult(
                scene=scene_id, name=scene_id, status="success",
                applied=["desk_ambient"], applied_at=NOW,
            )

    records = [
        routine_record(
            f"rt_{index:032x}",
            scene_id=("normal", "work_1", "night")[index - 1],
        )
        for index in range(1, 4)
    ]
    scenes = TrackingScenes()
    settings = Settings(
        ROOMOS_CONTROL_ENABLED=True,
        ROOMOS_CONTROL_POLICY="production",
        ROOMOS_ROUTINES_MAX_CONCURRENCY=2,
        ROOMOS_ROUTINES_TIMEZONE="Europe/Istanbul",
        ROOMOS_ROUTINES_PATH=str(tmp_path / "routines.json"),
    )
    routines = RoutineEngine(
        settings, scenes, object(), document=RoutineDocument(routines=records), clock=lambda: NOW
    )
    await routines.tick(NOW)
    assert scenes.peak == 2


@pytest.mark.asyncio
async def test_manual_run_uses_scene_engine_and_records_routine_source(tmp_path):
    routine = routine_record(trigger=ManualTrigger(), enabled=False)
    scenes = FakeScenes()
    events = []

    async def publish(event):
        events.append(event)

    routines = engine(tmp_path, [routine], scenes, publish=publish)
    response = await routines.run(routine.id, source="manual")
    assert response.scene.status == "success"
    assert response.routine.last_result.source == "manual"
    assert scenes.calls == [("night", "routine", routine.id)]
    assert any(event.type == "routine.run_completed" for event in events)


@pytest.mark.asyncio
async def test_kill_switch_blocks_before_scene_engine(tmp_path):
    routine = routine_record(trigger=ManualTrigger())
    scenes = FakeScenes()
    routines = engine(tmp_path, [routine], scenes, enabled=False)
    with pytest.raises(HTTPException) as rejected:
        await routines.run(routine.id, source="manual")
    assert rejected.value.status_code == 403
    assert scenes.calls == []
    assert routines._find(routine.id).last_result.detail == "control_disabled"


@pytest.mark.asyncio
async def test_scene_failed_result_propagates_to_routine(tmp_path):
    routine = routine_record(trigger=ManualTrigger())
    scenes = FakeScenes(status="failed")
    routines = engine(tmp_path, [routine], scenes)
    response = await routines.run(routine.id, source="manual")
    assert response.scene.status == "failed"
    assert response.routine.last_result.status == "failed"


@pytest.mark.asyncio
async def test_atomic_persistence_mode_restart_and_server_generated_id(tmp_path):
    path = tmp_path / "routines.json"
    settings = Settings(
        ROOMOS_CONTROL_ENABLED=True,
        ROOMOS_CONTROL_POLICY="production",
        ROOMOS_ROUTINES_PATH=str(path),
        ROOMOS_ROUTINES_TIMEZONE="Europe/Istanbul",
    )
    routines = RoutineEngine(settings, FakeScenes(), object(), clock=lambda: NOW)
    created = await routines.create(
        RoutineCreate(
            name="  Focus time  ",
            trigger={"type": "manual"},
            action={"type": "activate_scene", "scene_id": "work_1"},
        )
    )
    assert created.id.startswith("rt_") and len(created.id) == 35
    assert created.name == "Focus time"
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    restarted = RoutineEngine(settings, FakeScenes(), object(), clock=lambda: NOW)
    assert restarted.list(can_write=True, can_run=True).routines == [created]


@pytest.mark.asyncio
async def test_corrupt_persistence_is_fail_closed_and_not_overwritten(tmp_path):
    path = tmp_path / "routines.json"
    path.write_text('{"version":1,"routines":[', encoding="utf-8")
    before = path.read_bytes()
    settings = Settings(ROOMOS_ROUTINES_PATH=str(path))
    routines = RoutineEngine(settings, FakeScenes(), object())
    assert routines.corrupt is True
    assert routines.list(can_write=True, can_run=True).routines == []
    with pytest.raises(HTTPException) as rejected:
        await routines.create(
            RoutineCreate(
                name="Unsafe overwrite",
                trigger={"type": "manual"},
                action={"type": "activate_scene", "scene_id": "night"},
            )
        )
    assert rejected.value.status_code == 503
    assert path.read_bytes() == before


def test_tracked_example_is_inert_and_schema_valid():
    path = os.path.join(os.path.dirname(__file__), "..", "config", "routines.example.json")
    document, corrupt = load_routine_document(path)
    assert corrupt is False
    assert document.routines == []
    assert document.claimed_slots == {}


def test_update_requires_a_bounded_change():
    with pytest.raises(ValidationError):
        RoutineUpdate.model_validate({})
    with pytest.raises(ValidationError):
        RoutineUpdate(name="x" * 61)


def test_routine_crud_and_manual_run_use_bounded_api(client, fake_ha, tmp_path):
    scenes = FakeScenes()
    routines = engine(tmp_path, [], scenes)
    app.dependency_overrides[get_routines] = lambda: routines
    payload = {
        "name": "Focus",
        "enabled": False,
        "trigger": {"type": "manual"},
        "action": {"type": "activate_scene", "scene_id": "work_1"},
    }
    assert client.get("/api/v1/routines").json()["routines"] == []
    created = client.post("/api/v1/routines", json=payload)
    assert created.status_code == 201
    routine_id = created.json()["id"]
    updated = client.patch(f"/api/v1/routines/{routine_id}", json={"enabled": True})
    assert updated.status_code == 200
    assert updated.json()["enabled"] is True
    run = client.post(f"/api/v1/routines/{routine_id}/run")
    assert run.status_code == 200
    assert run.json()["scene"]["scene"] == "work_1"
    assert scenes.calls == [("work_1", "routine", routine_id)]
    assert client.delete(f"/api/v1/routines/{routine_id}").status_code == 204
    assert client.get("/api/v1/routines").json()["routines"] == []


def test_routine_api_rejects_raw_ha_and_malformed_payload_without_scene_call(client, tmp_path):
    scenes = FakeScenes()
    routines = engine(tmp_path, [], scenes)
    app.dependency_overrides[get_routines] = lambda: routines
    payloads = [
        {
            "name": "Raw entity",
            "trigger": {"type": "manual", "entity_id": "light.living_room"},
            "action": {"type": "activate_scene", "scene_id": "night"},
        },
        {
            "name": "Bad action",
            "trigger": {"type": "manual"},
            "action": {"type": "turn_on", "scene_id": "night"},
        },
        {
            "name": "Bad day",
            "trigger": {"type": "time", "time": "22:30", "days": ["neverday"]},
            "action": {"type": "activate_scene", "scene_id": "night"},
        },
    ]
    for payload in payloads:
        assert client.post("/api/v1/routines", json=payload).status_code == 422
    assert scenes.calls == []


@pytest.mark.asyncio
async def test_sunset_home_runs_normal_exactly_once_and_duplicate_is_ignored(tmp_path):
    scenes = FakeScenes()
    routines = engine(tmp_path, [managed_evening()], scenes)
    baseline = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    sunset = datetime(2026, 10, 6, 15, 36, tzinfo=UTC)
    routines.bootstrap_environment(
        ha_state("home", baseline), solar_state("above_horizon", baseline)
    )
    assert await routines.handle_environment_event(
        SOLAR_ENTITY_ID, solar_state("below_horizon", sunset), now=sunset
    ) == [MANAGED_EVENING_ID]
    assert await routines.handle_environment_event(
        SOLAR_ENTITY_ID,
        solar_state("below_horizon", sunset + timedelta(seconds=1)),
        now=sunset + timedelta(seconds=1),
    ) == []
    assert scenes.calls == [("normal", "routine", MANAGED_EVENING_ID)]


@pytest.mark.asyncio
async def test_sunset_away_does_nothing(tmp_path):
    scenes = FakeScenes()
    routines = engine(tmp_path, [managed_evening()], scenes)
    baseline = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    sunset = datetime(2026, 10, 6, 15, 36, tzinfo=UTC)
    routines.bootstrap_environment(
        ha_state("not_home", baseline), solar_state("above_horizon", baseline)
    )
    assert await routines.handle_environment_event(
        SOLAR_ENTITY_ID, solar_state("below_horizon", sunset), now=sunset
    ) == []
    assert scenes.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("now", "solar", "expected"),
    [
        (datetime(2026, 10, 6, 12, 30, tzinfo=UTC), "above_horizon", 0),
        (datetime(2026, 10, 6, 17, 45, tzinfo=UTC), "below_horizon", 1),
        (datetime(2026, 10, 6, 22, 45, tzinfo=UTC), "below_horizon", 1),
        (datetime(2026, 10, 6, 23, 15, tzinfo=UTC), "below_horizon", 0),
    ],
)
async def test_arrival_respects_sunset_to_0200_window(tmp_path, now, solar, expected):
    scenes = FakeScenes()
    routines = engine(tmp_path, [managed_evening()], scenes)
    baseline = now - timedelta(minutes=10)
    routines.bootstrap_environment(
        ha_state("not_home", baseline), solar_state(solar, baseline)
    )
    result = await routines.handle_environment_event(
        PRESENCE_ENTITY_ID, ha_state("home", now), now=now
    )
    assert len(result) == expected
    assert len(scenes.calls) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("initial", ["unknown", "unavailable"])
async def test_untrusted_presence_to_home_is_not_arrival(tmp_path, initial):
    scenes = FakeScenes()
    routines = engine(tmp_path, [managed_evening()], scenes)
    now = datetime(2026, 10, 6, 17, 45, tzinfo=UTC)
    routines.bootstrap_environment(
        ha_state(initial, now - timedelta(minutes=1)),
        solar_state("below_horizon", now - timedelta(minutes=1)),
    )
    assert await routines.handle_environment_event(
        PRESENCE_ENTITY_ID, ha_state("home", now), now=now
    ) == []
    assert scenes.calls == []


@pytest.mark.asyncio
async def test_startup_reconnect_home_and_duplicate_home_do_not_run(tmp_path):
    scenes = FakeScenes()
    routines = engine(tmp_path, [managed_evening()], scenes)
    now = datetime(2026, 10, 6, 17, 45, tzinfo=UTC)
    routines.bootstrap_environment(
        ha_state("home", now), solar_state("below_horizon", now)
    )
    assert scenes.calls == []
    assert await routines.handle_environment_event(
        PRESENCE_ENTITY_ID,
        ha_state("home", now + timedelta(seconds=1)),
        now=now + timedelta(seconds=1),
    ) == []
    routines.bootstrap_environment(
        ha_state("home", now + timedelta(seconds=2)),
        solar_state("below_horizon", now + timedelta(seconds=2)),
    )
    assert await routines.handle_environment_event(
        PRESENCE_ENTITY_ID,
        ha_state("home", now + timedelta(seconds=1)),
        now=now + timedelta(seconds=3),
    ) == []
    assert scenes.calls == []


@pytest.mark.asyncio
async def test_duplicate_arrival_event_runs_only_once(tmp_path):
    scenes = FakeScenes()
    routines = engine(tmp_path, [managed_evening()], scenes)
    now = datetime(2026, 10, 6, 17, 45, tzinfo=UTC)
    routines.bootstrap_environment(
        ha_state("not_home", now - timedelta(minutes=1)),
        solar_state("below_horizon", now - timedelta(minutes=1)),
    )
    await routines.handle_environment_event(
        PRESENCE_ENTITY_ID, ha_state("home", now), now=now
    )
    await routines.handle_environment_event(
        PRESENCE_ENTITY_ID,
        ha_state("home", now + timedelta(seconds=1)),
        now=now + timedelta(seconds=1),
    )
    assert scenes.calls == [("normal", "routine", MANAGED_EVENING_ID)]


@pytest.mark.asyncio
async def test_0200_all_off_claims_once_and_survives_restart(tmp_path):
    path = tmp_path / "routines.json"
    document = RoutineDocument(routines=[managed_all_off()])
    path.write_text(document.model_dump_json(), encoding="utf-8")
    os.chmod(path, 0o600)
    settings = Settings(
        ROOMOS_CONTROL_ENABLED=True,
        ROOMOS_CONTROL_POLICY="production",
        ROOMOS_ROUTINES_PATH=str(path),
        ROOMOS_ROUTINES_TIMEZONE="Europe/Istanbul",
    )
    due = datetime(2026, 10, 6, 23, 0, tzinfo=UTC)  # 02:00 Thursday
    first_scenes = FakeScenes()
    first = RoutineEngine(settings, first_scenes, object(), clock=lambda: due)
    assert await first.tick(due) == [MANAGED_ALL_OFF_ID]
    assert await first.tick(due) == []
    restarted_scenes = FakeScenes()
    restarted = RoutineEngine(settings, restarted_scenes, object(), clock=lambda: due)
    assert await restarted.tick(due) == []
    assert first_scenes.calls == [("all_off", "routine", MANAGED_ALL_OFF_ID)]
    assert restarted_scenes.calls == []


@pytest.mark.asyncio
async def test_evening_trigger_respects_kill_switch(tmp_path):
    scenes = FakeScenes()
    routines = engine(tmp_path, [managed_evening()], scenes, enabled=False)
    now = datetime(2026, 10, 6, 17, 45, tzinfo=UTC)
    routines.bootstrap_environment(
        ha_state("not_home", now - timedelta(minutes=1)),
        solar_state("below_horizon", now - timedelta(minutes=1)),
    )
    assert await routines.handle_environment_event(
        PRESENCE_ENTITY_ID, ha_state("home", now), now=now
    ) == [MANAGED_EVENING_ID]
    assert scenes.calls == []
    assert routines._find(MANAGED_EVENING_ID).last_result.detail == "control_disabled"


@pytest.mark.asyncio
async def test_server_managed_trigger_cannot_be_created_or_mutated_by_client(tmp_path):
    routines = engine(tmp_path, [managed_evening()])
    with pytest.raises(HTTPException) as rejected:
        await routines.create(
            RoutineCreate(
                name="Injected",
                trigger={"type": "evening_home"},
                action={"type": "activate_scene", "scene_id": "normal"},
            )
        )
    assert rejected.value.status_code == 422
    with pytest.raises(HTTPException) as update_rejected:
        await routines.update(MANAGED_EVENING_ID, RoutineUpdate(enabled=False))
    assert update_rejected.value.status_code == 409
    with pytest.raises(HTTPException) as run_rejected:
        await routines.run(MANAGED_EVENING_ID, source="manual")
    assert run_rejected.value.status_code == 409


def test_evening_window_is_fail_closed_for_unknown_solar_or_missing_rising():
    timezone = ZoneInfo("Europe/Istanbul")
    now = datetime(2026, 10, 6, 17, 45, tzinfo=UTC)
    assert evening_window_open("unknown", None, now, timezone) is False
    assert evening_window_open("below_horizon", None, now, timezone) is False
