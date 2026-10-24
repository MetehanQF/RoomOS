import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app import automations as automations_module
from app.automations import (
    AutomationCreate,
    AutomationDocument,
    AutomationEngine,
    AutomationHistory,
    AutomationStore,
    ConditionContext,
    device_trigger_snapshot,
    evaluate_condition,
)
from app.config import Settings
from app.main import app, get_automations


NOW = datetime(2026, 10, 19, 17, 0, tzinfo=UTC)  # 20:00 Europe/Istanbul


def time_rule(**changes):
    value = {
        "name": "Evening ambience",
        "trigger": {"type": "time", "time": "20:00", "days": ["mon"]},
        "conditions": {
            "kind": "predicate",
            "source": "light",
            "target_id": "desk_ambient",
            "field": "state",
            "operator": "equals",
            "value": "off",
        },
        "actions": [
            {
                "type": "light",
                "target_id": "desk_ambient",
                "operation": "brightness",
                "brightness": 77,
            }
        ],
    }
    value.update(changes)
    return value


def test_schema_is_closed_bounded_and_new_rules_are_disabled():
    created = AutomationCreate.model_validate(time_rule(enabled=True))
    assert created.enabled is False
    invalid = [
        time_rule(trigger={"type": "time", "time": "25:00", "days": ["mon"]}),
        time_rule(trigger={"type": "ha_state", "target_id": "light.raw_entity", "to": "on"}),
        time_rule(trigger={"type": "ha_state", "target_id": "desk_ambient", "operator": "above", "threshold": 50}),
        time_rule(trigger={"type": "device_health", "device_id": "invented_device", "metric": "online", "operator": "equals", "value": False}),
        time_rule(actions=[{"type": "shell", "command": "true"}]),
        time_rule(actions=[{"type": "light", "target_id": "desk_ambient", "operation": "brightness", "brightness": 0}]),
        time_rule(actions=[{"type": "light", "target_id": "main_room_lights", "operation": "color", "rgb": [1, 2, 3]}]),
        time_rule(extra="not allowed"),
        time_rule(actions=time_rule()["actions"] * 9),
        time_rule(conditions={
            "kind": "predicate", "source": "light", "target_id": "desk_ambient",
            "field": "cpu", "operator": "greater_than", "value": 80,
        }),
    ]
    for payload in invalid:
        with pytest.raises(ValidationError):
            AutomationCreate.model_validate(payload)
    assert AutomationCreate.model_validate(time_rule(trigger={
        "type": "device_health", "device_id": "workstation", "metric": "temperature", "operator": "above", "value": 80,
    })).trigger.device_id == "workstation"


def test_condition_tree_rejects_more_than_three_levels_or_twenty_leaves():
    leaf = time_rule()["conditions"]
    too_deep = leaf
    for _ in range(4):
        too_deep = {"kind": "group", "operator": "not", "children": [too_deep]}
    with pytest.raises(ValidationError):
        AutomationCreate.model_validate(time_rule(conditions=too_deep))
    with pytest.raises(ValidationError):
        AutomationCreate.model_validate(time_rule(conditions={
            "kind": "group", "operator": "and", "children": [leaf] * 21,
        }))


def test_three_valued_conditions_never_treat_unknown_as_true():
    leaf = AutomationCreate.model_validate(time_rule()).conditions
    assert evaluate_condition(leaf, ConditionContext()) .result == "unknown"
    fresh = ConditionContext(lights={"desk_ambient": {"state": "off", "fresh": True}})
    stale = ConditionContext(lights={"desk_ambient": {"state": "off", "fresh": False}})
    assert evaluate_condition(leaf, fresh).result == "true"
    assert evaluate_condition(leaf, stale).result == "unknown"
    grouped = AutomationCreate.model_validate(time_rule(conditions={
        "kind": "group", "operator": "and", "children": [
            time_rule()["conditions"],
            {**time_rule()["conditions"], "value": "on"},
        ],
    })).conditions
    assert evaluate_condition(grouped, ConditionContext()).result == "unknown"
    assert evaluate_condition(grouped, fresh).result == "false"


def test_device_invalid_zero_temperature_and_stale_metrics_are_unknown():
    condition = AutomationCreate.model_validate(time_rule(conditions={
        "kind": "predicate", "source": "device", "target_id": "workstation",
        "field": "temperature", "operator": "greater_than", "value": 80,
    })).conditions
    zero = ConditionContext(devices={"workstation": {"online": True, "fresh": True, "temperature": 0}})
    stale = ConditionContext(devices={"workstation": {"online": True, "fresh": False, "temperature": 95}})
    assert evaluate_condition(condition, zero).result == "unknown"
    assert evaluate_condition(condition, stale).result == "unknown"


def test_store_is_atomic_mode_0600_restart_safe_and_corrupt_fail_closed(tmp_path):
    path = tmp_path / "automations.json"
    store = AutomationStore(path)
    record = store.create(AutomationCreate.model_validate(time_rule()), now=NOW)
    assert record.id.startswith("au_") and record.enabled is False
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    restarted = AutomationStore(path)
    assert restarted.document.automations == [record]
    before = path.read_bytes()
    path.write_text('{"version":1,"automations":[', encoding="utf-8")
    broken = AutomationStore(path)
    assert broken.corrupt is True and broken.document.automations == []
    with pytest.raises(HTTPException) as rejected:
        broken.create(AutomationCreate.model_validate(time_rule()), now=NOW)
    assert rejected.value.status_code == 503
    assert path.read_bytes() != before and path.read_text().endswith("[")


def test_history_is_bounded_sanitized_paginated_and_simulation_is_not_persisted(tmp_path):
    path = tmp_path / "automation-history.jsonl"
    history = AutomationHistory(path, limit=3, retention_days=30, clock=lambda: NOW)
    for index in range(5):
        history.append({
            "automation_id": "au_" + f"{index:032x}", "automation_name": "Rule",
            "outcome": "success", "trigger_source": "manual", "trigger_reason": "test",
            "correlation_id": "ac_" + f"{index:032x}", "duration_ms": index,
            "error": "Bearer secret-token" if index == 4 else None,
        })
    assert len(history.list(limit=10).items) == 3
    assert "secret-token" not in path.read_text()
    first = history.list(limit=2)
    assert len(first.items) == 2 and first.next_cursor is not None
    assert len(history.list(limit=2, cursor=first.next_cursor).items) == 1
    before = path.read_bytes()
    history.simulation({"automation_id": "au_" + "f" * 32})
    assert path.read_bytes() == before


class ActionRecorder:
    def __init__(self, *, fail_at=None):
        self.calls = []
        self.fail_at = fail_at

    async def __call__(self, action, context):
        self.calls.append((action.type, action.target_id if hasattr(action, "target_id") else action.scene_id))
        if len(self.calls) == self.fail_at:
            raise HTTPException(status_code=503, detail="synthetic failure")
        return {"status": "success"}


def make_engine(tmp_path, *, rule=None, action=None, now=NOW, **engine_options):
    settings = Settings(
        ROOMOS_AUTOMATIONS_PATH=str(tmp_path / "automations.json"),
        ROOMOS_AUTOMATION_HISTORY_PATH=str(tmp_path / "automation-history.jsonl"),
        ROOMOS_ROUTINES_TIMEZONE="Europe/Istanbul",
    )
    store = AutomationStore(settings.automations_path, document=AutomationDocument())
    created = store.create(AutomationCreate.model_validate(rule or time_rule()), now=now)
    store.set_enabled(created.id, True, now=now)
    return AutomationEngine(
        settings,
        store=store,
        action_executor=action or ActionRecorder(),
        clock=lambda: now,
        **engine_options,
    ), created.id


@pytest.mark.asyncio
async def test_time_trigger_claims_once_and_restart_never_catches_up(tmp_path):
    recorder = ActionRecorder()
    engine, automation_id = make_engine(tmp_path, rule=time_rule(conditions=None), action=recorder)
    assert await engine.tick(NOW) == [automation_id]
    assert await engine.tick(NOW) == []
    assert len(recorder.calls) == 1
    assert await engine.tick(NOW + timedelta(minutes=1)) == []


@pytest.mark.asyncio
async def test_dry_run_explains_unknown_and_sends_no_action_or_history(tmp_path):
    recorder = ActionRecorder()
    engine, automation_id = make_engine(tmp_path, action=recorder)
    result = await engine.simulate(automation_id, context=ConditionContext())
    assert result.mode == "SIMULATION"
    assert result.condition.result == "unknown"
    assert result.outcome == "skipped"
    assert recorder.calls == []
    assert engine.history.list(limit=10).items == []


@pytest.mark.asyncio
async def test_duplicate_cooldown_and_automation_origin_are_explained_without_actions(tmp_path):
    recorder = ActionRecorder()
    rule = time_rule(
        trigger={"type": "ha_state", "target_id": "desk_ambient", "from_state": "off", "to_state": "on"},
        conditions=None,
        cooldown_seconds=300,
    )
    engine, automation_id = make_engine(tmp_path, rule=rule, action=recorder)
    event = {"event_id": "evt-1", "target_id": "desk_ambient", "old_state": "off", "new_state": "on", "fresh": True}
    assert await engine.handle_ha_event(event, origin="external") == [automation_id]
    assert await engine.handle_ha_event(event, origin="external") == []
    assert await engine.handle_ha_event({**event, "event_id": "evt-2"}, origin="automation") == []
    assert len(recorder.calls) == 1
    reasons = [item.reason for item in engine.history.list(limit=10).items]
    assert "duplicate_event" in reasons and "automation_origin_blocked" in reasons


@pytest.mark.asyncio
async def test_default_stop_on_failure_skips_later_actions_and_never_retries(tmp_path):
    recorder = ActionRecorder(fail_at=1)
    rule = time_rule(conditions=None, actions=[
        {"type": "scene", "scene_id": "night"},
        {"type": "scene", "scene_id": "all_off"},
    ])
    engine, automation_id = make_engine(tmp_path, rule=rule, action=recorder)
    result = await engine.run_manual(automation_id, context=ConditionContext())
    assert result.outcome == "failed"
    assert len(recorder.calls) == 1
    assert [item.status for item in result.actions] == ["failed", "skipped"]


@pytest.mark.asyncio
async def test_reentrancy_guard_blocks_parallel_execution(tmp_path):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow(action, context):
        entered.set()
        await release.wait()
        return {"status": "success"}

    engine, automation_id = make_engine(tmp_path, rule=time_rule(conditions=None), action=slow)
    first = asyncio.create_task(engine.run_manual(automation_id, context=ConditionContext()))
    await entered.wait()
    second = await engine.run_manual(automation_id, context=ConditionContext())
    assert second.outcome == "blocked" and second.reason == "reentrant"
    release.set()
    await first


@pytest.mark.asyncio
async def test_one_device_observation_evaluates_every_matching_rule(tmp_path):
    recorder = ActionRecorder()
    settings = Settings(
        ROOMOS_AUTOMATIONS_PATH=str(tmp_path / "automations.json"),
        ROOMOS_AUTOMATION_HISTORY_PATH=str(tmp_path / "automation-history.jsonl"),
        ROOMOS_ROUTINES_TIMEZONE="Europe/Istanbul",
    )
    store = AutomationStore(settings.automations_path)
    rule = time_rule(trigger={"type": "device_health", "device_id": "workstation", "metric": "temperature", "operator": "above", "value": 70}, conditions=None)
    ids = []
    for index in range(2):
        item = store.create(AutomationCreate.model_validate({**rule, "name": f"Heat guard {index}"}), now=NOW)
        store.set_enabled(item.id, True, now=NOW); ids.append(item.id)
    engine = AutomationEngine(settings, store=store, action_executor=recorder, clock=lambda: NOW)
    fired = await engine.handle_device_event({"id": "workstation", "temperature": 80, "fresh": True})
    assert fired == ids
    assert len(recorder.calls) == 2


@pytest.mark.asyncio
async def test_ha_hold_duration_requires_continuous_fresh_observation(tmp_path):
    current = NOW
    recorder = ActionRecorder()
    current_state = {"state": "on", "brightness": 60, "fresh": True}

    async def provide_context():
        return ConditionContext(
            lights={"desk_ambient": dict(current_state)},
            now=current,
        )

    rule = time_rule(
        trigger={"type": "ha_state", "target_id": "desk_ambient", "attribute": "brightness", "operator": "above", "threshold": 50, "for_seconds": 60},
        conditions=None,
    )
    engine, automation_id = make_engine(
        tmp_path,
        rule=rule,
        action=recorder,
        now=current,
        context_provider=provide_context,
    )
    engine.clock = lambda: current
    event = {
        "target_id": "desk_ambient",
        "new_state": "on",
        "attributes": {"brightness": 60},
        "fresh": True,
    }
    assert await engine.handle_ha_event({**event, "event_id": "one"}) == []
    current += timedelta(seconds=59)
    assert await engine.tick(current) == []
    current += timedelta(seconds=1)
    assert await engine.tick(current) == [automation_id]
    assert len(recorder.calls) == 1


@pytest.mark.asyncio
async def test_device_health_ignores_stale_and_invalid_zero_then_fires_once(tmp_path):
    recorder = ActionRecorder()
    rule = time_rule(
        trigger={"type": "device_health", "device_id": "workstation", "metric": "temperature", "operator": "above", "value": 80},
        conditions=None,
        actions=[{"type": "scene", "scene_id": "night"}],
    )
    engine, automation_id = make_engine(tmp_path, rule=rule, action=recorder)
    assert await engine.handle_device_event({"id": "workstation", "fresh": False, "temperature": 95, "event_id": "stale"}) == []
    assert await engine.handle_device_event({"id": "workstation", "fresh": True, "temperature": 0, "event_id": "zero"}) == []
    assert await engine.handle_device_event({"id": "workstation", "fresh": True, "temperature": 95, "event_id": "hot"}) == [automation_id]
    assert len(recorder.calls) == 1


@pytest.mark.asyncio
async def test_device_hold_duration_is_revalidated_by_scheduler_tick(tmp_path):
    current = NOW
    recorder = ActionRecorder()

    async def provide_context():
        return ConditionContext(
            devices={"workstation": {"temperature": 85, "fresh": True}},
            now=current,
        )

    rule = time_rule(
        trigger={
            "type": "device_health",
            "device_id": "workstation",
            "metric": "temperature",
            "operator": "above",
            "value": 80,
            "for_seconds": 60,
        },
        conditions=None,
        actions=[{"type": "scene", "scene_id": "night"}],
    )
    engine, automation_id = make_engine(
        tmp_path,
        rule=rule,
        action=recorder,
        now=current,
        context_provider=provide_context,
    )
    engine.clock = lambda: current
    assert await engine.handle_device_event({
        "id": "workstation", "fresh": True, "temperature": 85, "event_id": "hot",
    }) == []
    current += timedelta(seconds=60)
    assert await engine.tick(current) == [automation_id]
    assert recorder.calls == [("scene", "night")]


@pytest.mark.asyncio
async def test_device_online_trigger_uses_real_transition_not_repeated_observation(tmp_path):
    recorder = ActionRecorder()
    rule = time_rule(
        trigger={"type": "device_health", "device_id": "secondary", "metric": "online", "operator": "equals", "value": False},
        conditions=None,
    )
    engine, automation_id = make_engine(tmp_path, rule=rule, action=recorder)
    online = {"id": "secondary", "fresh": True, "online": True, "event_id": "online"}
    offline = {"id": "secondary", "fresh": True, "online": False, "event_id": "offline"}
    assert await engine.handle_device_event(online) == []
    assert await engine.handle_device_event(offline) == [automation_id]
    assert await engine.handle_device_event({**offline, "event_id": "offline-repeat"}) == []


@pytest.mark.asyncio
async def test_light_action_marks_matching_ha_feedback_as_automation_origin(tmp_path):
    recorder = ActionRecorder()
    engine, source_id = make_engine(tmp_path, rule=time_rule(conditions=None, actions=[
        {"type": "light", "target_id": "desk_ambient", "operation": "power", "on": True},
    ]), action=recorder)
    target = engine.store.create(AutomationCreate.model_validate(time_rule(
        name="Feedback target", conditions=None,
        trigger={"type": "ha_state", "target_id": "desk_ambient", "from_state": "off", "to_state": "on"},
        actions=[{"type": "scene", "scene_id": "night"}],
    )), now=NOW)
    engine.store.set_enabled(target.id, True, now=NOW)
    engine._locks[target.id] = asyncio.Lock()
    assert (await engine.run_manual(source_id, context=ConditionContext())).outcome == "success"
    assert await engine.handle_ha_event({
        "event_id": "feedback", "target_id": "desk_ambient", "old_state": "off",
        "new_state": "on", "fresh": True,
    }) == []
    assert len(recorder.calls) == 1
    assert "automation_origin_blocked" in [item.reason for item in engine.history.list(limit=10).items]


@pytest.mark.asyncio
async def test_scene_action_arms_server_resolved_targets_before_feedback(tmp_path):
    recorder = ActionRecorder()
    engine, source_id = make_engine(
        tmp_path,
        rule=time_rule(
            conditions=None,
            actions=[{"type": "scene", "scene_id": "night"}],
        ),
        action=recorder,
        action_effects=lambda _action: {"desk_ambient": "on"},
    )
    target = engine.store.create(AutomationCreate.model_validate(time_rule(
        name="Scene feedback target",
        conditions=None,
        trigger={"type": "ha_state", "target_id": "desk_ambient", "from_state": "off", "to_state": "on"},
        actions=[{"type": "scene", "scene_id": "all_off"}],
    )), now=NOW)
    engine.store.set_enabled(target.id, True, now=NOW)
    engine._locks[target.id] = asyncio.Lock()

    assert (await engine.run_manual(source_id, context=ConditionContext())).outcome == "success"
    assert await engine.handle_ha_event({
        "event_id": "scene-feedback", "target_id": "desk_ambient", "old_state": "off",
        "new_state": "on", "fresh": True,
    }) == []
    assert recorder.calls == [("scene", "night")]


@pytest.mark.asyncio
async def test_live_execution_uses_fresh_condition_context_provider(tmp_path):
    recorder = ActionRecorder()

    async def provide_context():
        return ConditionContext(
            lights={"desk_ambient": {"state": "off", "fresh": True}},
            now=NOW,
        )

    engine, automation_id = make_engine(
        tmp_path,
        action=recorder,
        context_provider=provide_context,
    )
    result = await engine.run_manual(automation_id)
    assert result.outcome == "success"
    assert recorder.calls == [("light", "desk_ambient")]


@pytest.mark.asyncio
async def test_execution_deadline_bounds_the_whole_action_list(tmp_path, monkeypatch):
    monkeypatch.setattr("app.automations.MAX_EXECUTION_SECONDS", 0.05)

    async def slow(_action, _context):
        await asyncio.sleep(0.04)
        return {"status": "success"}

    engine, automation_id = make_engine(
        tmp_path,
        rule=time_rule(
            conditions=None,
            failure_policy="continue",
            actions=[
                {"type": "scene", "scene_id": "night"},
                {"type": "scene", "scene_id": "all_off"},
            ],
        ),
        action=slow,
    )
    result = await engine.run_manual(automation_id, context=ConditionContext())
    assert result.outcome == "partial"
    assert [item.status for item in result.actions] == ["success", "failed"]
    assert result.duration_ms < 150


@pytest.mark.asyncio
async def test_global_execution_limit_bounds_distinct_automations(tmp_path):
    active = 0
    maximum = 0
    all_entered = asyncio.Event()
    release = asyncio.Event()

    async def bounded(_action, _context):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        if active == 4:
            all_entered.set()
        await release.wait()
        active -= 1
        return {"status": "success"}

    engine, first_id = make_engine(
        tmp_path,
        rule=time_rule(conditions=None, actions=[{"type": "scene", "scene_id": "night"}]),
        action=bounded,
    )
    ids = [first_id]
    for index in range(5):
        item = engine.store.create(AutomationCreate.model_validate(time_rule(
            name=f"Concurrent {index}", conditions=None,
            actions=[{"type": "scene", "scene_id": "night"}],
        )), now=NOW)
        engine.store.set_enabled(item.id, True, now=NOW)
        engine._locks[item.id] = asyncio.Lock()
        ids.append(item.id)
    tasks = [asyncio.create_task(engine.run_manual(item_id, context=ConditionContext())) for item_id in ids]
    await asyncio.wait_for(all_entered.wait(), timeout=1)
    await asyncio.sleep(0)
    observed_maximum = maximum
    release.set()
    results = await asyncio.gather(*tasks)
    assert observed_maximum == 4
    assert all(result.outcome == "success" for result in results)


def test_device_snapshot_uses_only_fresh_available_registry_values():
    detail = {
        "id": "workstation", "connection": "online", "telemetry_freshness": "fresh",
        "sensors": [
            {"id": "cpu_usage", "value": 91, "availability": "available", "freshness": "fresh"},
            {"id": "ram_usage", "value": 72, "availability": "available", "freshness": "stale"},
            {"id": "gpu_temperature", "value": 0, "availability": "available", "freshness": "fresh"},
            {"id": "disk_usage", "value": 88, "availability": "unavailable", "freshness": "unknown"},
        ],
    }
    snapshot = device_trigger_snapshot(detail)
    assert snapshot == {
        "id": "workstation", "fresh": True, "online": True, "cpu": 91,
        "ram": None, "disk": None, "temperature": None,
    }


def test_automation_api_crud_simulation_history_and_enable_are_bounded(client, tmp_path):
    recorder = ActionRecorder()
    engine, _ = make_engine(tmp_path, rule=time_rule(conditions=None), action=recorder)
    engine.store.document = AutomationDocument()
    engine._locks.clear()
    app.dependency_overrides[get_automations] = lambda: engine
    catalog = client.get("/api/v1/automations")
    assert catalog.status_code == 200 and catalog.json()["automations"] == []
    targets = catalog.json()["targets"]
    assert [item["id"] for item in targets["lights"]] == ["room_main", "room_2", "room_3", "room_4", "desk_ambient", "main_room_lights"]
    assert "workstation" in [item["id"] for item in targets["devices"]]
    assert "light.living_room" not in json.dumps(targets)
    created = client.post("/api/v1/automations", json=time_rule())
    assert created.status_code == 201 and created.json()["enabled"] is False
    automation_id = created.json()["id"]
    simulated = client.post(f"/api/v1/automations/{automation_id}/simulate", json={})
    assert simulated.status_code == 200 and simulated.json()["mode"] == "SIMULATION"
    assert recorder.calls == []
    assert client.patch(f"/api/v1/automations/{automation_id}", json={"name": "Evening glow"}).status_code == 200
    assert client.post(f"/api/v1/automations/{automation_id}/enabled", json={"enabled": True}).status_code == 200
    history = client.get("/api/v1/automations/history?limit=10")
    assert history.status_code == 200 and history.json()["items"] == []
    assert client.delete(f"/api/v1/automations/{automation_id}").status_code == 204


def test_automation_api_rejects_raw_ha_and_dry_run_never_executes(client, tmp_path):
    recorder = ActionRecorder()
    engine, _ = make_engine(tmp_path, rule=time_rule(conditions=None), action=recorder)
    engine.store.document = AutomationDocument()
    engine._locks.clear()
    app.dependency_overrides[get_automations] = lambda: engine
    malformed = time_rule(trigger={"type": "ha_state", "target_id": "light.living_room", "to_state": "on"})
    assert client.post("/api/v1/automations", json=malformed).status_code == 422
    assert recorder.calls == []


def test_automation_api_simulation_uses_live_allowlisted_context_without_actions(client, tmp_path):
    recorder = ActionRecorder()

    async def live_context():
        return ConditionContext(lights={"desk_ambient": {"state": "off", "fresh": True}}, now=NOW)

    engine, _ = make_engine(tmp_path, rule=time_rule(conditions=None), action=recorder, context_provider=live_context)
    engine.store.document = AutomationDocument()
    engine._locks.clear()
    app.dependency_overrides[get_automations] = lambda: engine
    created = client.post("/api/v1/automations", json=time_rule())
    result = client.post(f"/api/v1/automations/{created.json()['id']}/simulate", json={})
    assert result.status_code == 200
    assert result.json()["outcome"] == "success"
    assert result.json()["condition"]["result"] == "true"
    assert recorder.calls == []


def test_automation_api_manual_run_uses_live_allowlisted_context(client, tmp_path):
    recorder = ActionRecorder()

    async def live_context():
        return ConditionContext(lights={"desk_ambient": {"state": "off", "fresh": True}}, now=NOW)

    engine, _ = make_engine(tmp_path, rule=time_rule(conditions=None), action=recorder, context_provider=live_context)
    engine.store.document = AutomationDocument(); engine._locks.clear()
    app.dependency_overrides[get_automations] = lambda: engine
    created = client.post("/api/v1/automations", json=time_rule())
    result = client.post(f"/api/v1/automations/{created.json()['id']}/run", json={})
    assert result.status_code == 200 and result.json()["outcome"] == "success"
    assert len(recorder.calls) == 1


@pytest.mark.asyncio
async def test_live_context_builder_uses_allowlisted_ha_and_registry_snapshots(fake_ha):
    class Registry:
        def list(self, scopes):
            assert scopes == frozenset({"system:read", "devices:read"})
            return SimpleNamespace(devices=[])

    context = await automations_module.build_condition_context(
        fake_ha,
        Registry(),
        ZoneInfo("Europe/Istanbul"),
        now=NOW,
    )
    assert context.lights["desk_ambient"] == {
        "state": "off",
        "available": True,
        "fresh": True,
        "brightness": None,
        "temperature": None,
    }
    assert context.devices == {}
    assert context.now == NOW.astimezone(ZoneInfo("Europe/Istanbul"))
