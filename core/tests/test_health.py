import asyncio
import json
import os
import re
import sqlite3
import time
from datetime import UTC, datetime, timedelta

import pytest

from app.alerts import AlertEngine
from app.health import (
    CRITICAL,
    HA_RECONNECT_GRACE,
    HealthInputs,
    build_health,
    collect_inputs,
)
from app.main import app
from tests.test_auth import auth_client, pair  # noqa: F401  (fixture reuse)

NOW = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)
HEALTHY_STORES = {
    "Authentication": "ok", "Scene state": "ok", "Routines": "ok", "Smart automations": "ok",
    "Activity & alerts": "ok", "Notifications": "ok", "Notification incidents": "ok", "Alert policy": "ok",
}


def inputs(**overrides) -> HealthInputs:
    base = dict(
        now=NOW, ha_configured=True, ha_bridge_state="connected", ha_bridge_since=NOW - timedelta(hours=1),
        ha_bridge_alive=True, control_enabled=True, configured_scenes=8, stores=dict(HEALTHY_STORES),
        execution_status="ok", execution_count=0, routines_corrupt=False, scheduler_alive=True,
        routines_enabled=2, routines_total=2, notification_configuration="configured",
        automation_corrupt=False, automation_engine_alive=True, automation_enabled=1,
        automation_total=3, automation_last_tick=NOW, automation_queue_depth=0,
        automation_failures=0, automation_loop_preventions=2, automation_persistence_failures=0,
        notification_storage="ok", incident_storage="ok", notification_destinations=1,
        notification_delivery_enabled=True, dispatcher_alive=True, history_outcome="none",
    )
    base.update(overrides)
    return HealthInputs(**base)


def by_id(response):
    return {component.id: component for component in response.components}


def test_all_healthy_baseline_and_component_set():
    response = build_health(inputs())
    components = by_id(response)
    assert response.overall == "healthy"
    assert list(components) == [
        "home_assistant", "control", "persistent_state", "automations",
        "execution_history", "notifications", "telemetry_history", "backups",
        "background_services",
    ]
    assert components["background_services"].status == "unknown"
    assert components["home_assistant"].summary == "Connected · live updates active"
    assert components["control"].summary == "Ready · 8 scenes"
    assert components["execution_history"].status == "healthy"
    assert components["execution_history"].summary == "Writable · no executions yet"
    assert components["automations"].summary == "Engines running · 2 legacy + 1 smart enabled"
    assert components["automations"].details == ["Smart rules 1/3 · queue 0", "Safety blocks 2 · failures 0"]
    assert components["notifications"].summary == "Dispatcher ready · 1 destination"
    assert components["backups"].status == "unknown"
    assert {c.id for c in response.components if c.critical} == set(CRITICAL)
    assert all(c.checked_at == NOW for c in response.components)


def test_zero_executions_is_not_degraded_and_counts_are_reported():
    assert by_id(build_health(inputs(execution_count=0)))["execution_history"].status == "healthy"
    component = by_id(build_health(inputs(execution_count=3)))["execution_history"]
    assert (component.status, component.summary) == ("healthy", "Writable · 3 records")


@pytest.mark.parametrize(
    ("overrides", "component", "status", "overall"),
    [
        ({"ha_bridge_state": "disconnected", "ha_bridge_since": NOW - timedelta(seconds=30)}, "home_assistant", "degraded", "degraded"),
        ({"ha_bridge_state": "connecting", "ha_bridge_since": NOW - HA_RECONNECT_GRACE}, "home_assistant", "degraded", "degraded"),
        ({"ha_bridge_state": "disconnected", "ha_bridge_since": NOW - timedelta(minutes=10)}, "home_assistant", "unhealthy", "unhealthy"),
        ({"ha_bridge_alive": False}, "home_assistant", "unhealthy", "unhealthy"),
        ({"ha_configured": False, "ha_bridge_state": None}, "home_assistant", "unhealthy", "unhealthy"),
        ({"control_enabled": False}, "control", "degraded", "degraded"),
        ({"configured_scenes": 7}, "control", "degraded", "degraded"),
        ({"configured_scenes": 0}, "control", "unhealthy", "unhealthy"),
        ({"execution_status": "corrupt"}, "execution_history", "degraded", "degraded"),
        ({"execution_status": "write_failed"}, "execution_history", "degraded", "degraded"),
        ({"routines_corrupt": True}, "automations", "unhealthy", "unhealthy"),
        ({"scheduler_alive": False}, "automations", "unhealthy", "unhealthy"),
        ({"automation_engine_alive": False}, "automations", "unhealthy", "unhealthy"),
        ({"automation_corrupt": True}, "automations", "unhealthy", "unhealthy"),
        ({"automation_persistence_failures": 1}, "automations", "degraded", "degraded"),
        ({"dispatcher_alive": False}, "notifications", "unhealthy", "degraded"),
        ({"notification_configuration": "unavailable"}, "notifications", "unhealthy", "degraded"),
        ({"notification_configuration": "disabled", "notification_delivery_enabled": False}, "notifications", "degraded", "degraded"),
        ({"notification_destinations": 0, "notification_delivery_enabled": False}, "notifications", "degraded", "degraded"),
        ({"notification_storage": "corrupt"}, "notifications", "unhealthy", "degraded"),
        ({"incident_storage": "corrupt"}, "notifications", "unhealthy", "degraded"),
        ({"history_outcome": "stale_fallback"}, "telemetry_history", "degraded", "degraded"),
        ({"history_outcome": "failed"}, "telemetry_history", "degraded", "degraded"),
        ({"history_outcome": "ok"}, "telemetry_history", "healthy", "healthy"),
    ],
)
def test_aggregation_matrix(overrides, component, status, overall):
    response = build_health(inputs(**overrides))
    assert by_id(response)[component].status == status
    assert response.overall == overall


def test_ha_reconnect_cascades_to_control_but_not_to_unhealthy():
    response = build_health(inputs(ha_bridge_state="disconnected", ha_bridge_since=NOW - timedelta(seconds=20)))
    components = by_id(response)
    assert components["control"].summary == "Waiting for Home Assistant"
    assert components["control"].status == "degraded"
    assert response.overall == "degraded"


def test_persistent_state_store_degradation_and_control_store_corruption():
    one = build_health(inputs(stores={**HEALTHY_STORES, "Activity & alerts": "write_failed"}))
    component = by_id(one)["persistent_state"]
    assert (component.status, component.summary) == ("degraded", "1 store needs attention")
    assert component.details == ["Activity & alerts: writes failing"]
    assert one.overall == "degraded"
    severe = build_health(inputs(stores={**HEALTHY_STORES, "Routines": "corrupt", "Notifications": "corrupt"}))
    component = by_id(severe)["persistent_state"]
    assert component.status == "unhealthy" and component.summary == "2 stores need attention"
    assert component.details == ["Notifications: needs repair", "Routines: needs repair"]
    assert severe.overall == "unhealthy"


def test_multiple_simultaneous_degradations_stay_degraded():
    response = build_health(inputs(
        execution_status="write_failed", history_outcome="stale_fallback", control_enabled=False,
        stores={**HEALTHY_STORES, "Scene state": "write_failed"}, notification_destinations=0,
        notification_delivery_enabled=False,
    ))
    assert response.overall == "degraded"
    assert sum(c.status == "degraded" for c in response.components) == 5


def test_unknown_backup_never_changes_overall():
    assert build_health(inputs()).overall == "healthy"
    assert by_id(build_health(inputs()))["backups"].summary == "Not monitored · status source not mounted"


def test_aggregation_is_deterministic():
    sample = inputs(execution_status="corrupt", ha_bridge_state="connecting", ha_bridge_since=NOW)
    assert build_health(sample).model_dump_json() == build_health(sample).model_dump_json()


# --- runtime collection and observability flags ----------------------------------


def test_alert_write_failure_flag_is_set_and_recovers(tmp_path):
    engine = AlertEngine(str(tmp_path / "alerts.json"))
    engine._persist()
    assert engine.write_failed is False
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    engine.path = blocked / "alerts.json"
    os.chmod(blocked, 0o500)
    try:
        engine._persist()
        assert engine.write_failed is True
    finally:
        os.chmod(blocked, 0o700)
    engine._persist()
    assert engine.write_failed is False


def test_collect_inputs_reflects_injected_runtime_faults(client):
    state = app.state
    settings = type("S", (), {"ha_connect_enabled": True, "has_ha_credentials": True, "control_enabled": True})()
    healthy = collect_inputs(state, settings, now=NOW)
    assert healthy.stores == HEALTHY_STORES
    assert healthy.execution_status == "ok"
    original = (state.executions.storage_status, state.alerts.write_failed, state.routines.corrupt, state.alert_policy.write_failed)
    try:
        state.executions.storage_status = "write_failed"
        state.alerts.write_failed = True
        state.routines.corrupt = True
        state.alert_policy.write_failed = True
        faulty = collect_inputs(state, settings, now=NOW)
        assert faulty.execution_status == "write_failed"
        assert faulty.stores["Activity & alerts"] == "write_failed"
        assert faulty.stores["Routines"] == "corrupt"
        assert faulty.stores["Alert policy"] == "write_failed"
        response = build_health(faulty)
        assert by_id(response)["automations"].status == "unhealthy"
        assert response.overall == "unhealthy"
    finally:
        state.executions.storage_status, state.alerts.write_failed, state.routines.corrupt, state.alert_policy.write_failed = original


def test_scheduler_flag_tracks_loop_lifecycle(client):
    tasks = app.state.background_tasks
    assert tasks["scheduler"] is not None and not tasks["scheduler"].done()
    assert app.state.routines.scheduler_running is True


# --- public endpoint --------------------------------------------------------------

FORBIDDEN = [
    r"entity_id", r"\bsensor\.", r"\blight\.", r"binary_sensor", r"person\.", r"sun\.sun",
    r"192\.168\.", r"\b100\.\d+\.", r"token", r"bearer", r"firebase", r"private_key",
    r"/var/lib", r"\.json", r"http://", r"https://", r"Traceback", r"Error\b", r"secret",
]


def test_endpoint_contract_and_sanitization(client, fake_ha):
    started = time.perf_counter()
    response = client.get("/api/v1/system/health")
    elapsed = time.perf_counter() - started
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"overall", "checked_at", "components"}
    assert body["overall"] in {"healthy", "degraded", "unhealthy"}
    assert len(body["components"]) == 9
    for component in body["components"]:
        assert set(component) == {"id", "name", "status", "critical", "summary", "details", "checked_at"}
    lowered = response.text.lower()
    for pattern in FORBIDDEN:
        assert not re.search(pattern.lower(), lowered), pattern
    assert elapsed < 0.1
    assert fake_ha.calls == []
    assert getattr(fake_ha, "history_requests", []) == []


def test_endpoint_has_no_side_effects_on_stores(client, tmp_path):
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    for _ in range(5):
        assert client.get("/api/v1/system/health").status_code == 200
    after = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    assert before == after


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_write_methods_rejected(client, method):
    assert getattr(client, method)("/api/v1/system/health").status_code == 405


@pytest.mark.parametrize("query", ["verbose=1", "component=backups", "path=/var/lib"])
def test_query_parameters_rejected(client, query):
    assert client.get(f"/api/v1/system/health?{query}").status_code == 422


def test_endpoint_requires_system_read(auth_client):  # noqa: F811
    client, fake_ha, store, settings = auth_client
    assert client.get("/api/v1/system/health").status_code == 401
    device_id = pair(client, store).json()["device"]["id"]
    with sqlite3.connect(settings.auth_database_path) as db:
        db.execute("UPDATE devices SET scopes=? WHERE id=?", (json.dumps(["lights:read"]), device_id))
    assert client.get("/api/v1/system/health").status_code == 403
    with sqlite3.connect(settings.auth_database_path) as db:
        db.execute("UPDATE devices SET scopes=? WHERE id=?", (json.dumps(["system:read"]), device_id))
    assert client.get("/api/v1/system/health").status_code == 200
    assert fake_ha.calls == []


def test_build_is_fast():
    sample = inputs()
    started = time.perf_counter()
    for _ in range(1000):
        build_health(sample)
    assert (time.perf_counter() - started) / 1000 < 0.005


def test_history_outcome_counters():
    from app.history import HistoryQuery, HistoryService
    from app.ha_client import HomeAssistantUnavailable

    class Reader:
        def __init__(self, error=None):
            self.error = error

        async def get_history(self, entity_ids, start, end):
            if self.error:
                raise self.error
            return [[{"entity_id": entity_ids[0], "state": "1", "last_changed": start.isoformat()}]]

    clock = [0.0]
    svc = HistoryService(clock=lambda: clock[0], now=lambda: NOW)
    query = HistoryQuery(node="primary", metric="cpu", range="1h")
    assert svc.last_outcome == "none"
    asyncio.run(svc.get(Reader(), query))
    assert svc.last_outcome == "ok"
    clock[0] += 45
    asyncio.run(svc.get(Reader(HomeAssistantUnavailable("x")), query))
    assert svc.last_outcome == "stale_fallback"
    clock[0] += 100
    with pytest.raises(Exception):
        asyncio.run(svc.get(Reader(HomeAssistantUnavailable("x")), query))
    assert svc.last_outcome == "failed"


# --- backup health (Phase 6D.4) ---------------------------------------------------

from app.backup_status import BackupObservation, BackupStatusFile, BackupStatusReader  # noqa: E402


def status_doc(**overrides):
    base = dict(
        version=1, status="success", reason=None, tool_version=2, schedule="Daily 04:30 Europe/Istanbul",
        cadence_seconds=86400, last_attempt_at=NOW - timedelta(hours=3), last_finished_at=NOW - timedelta(hours=3),
        last_success_at=NOW - timedelta(hours=3), last_restore_test_at=NOW - timedelta(hours=3), restore_tested=True,
        last_skipped_at=None, last_skip_reason=None,
    )
    base.update(overrides)
    return BackupStatusFile(**base)


def backup_component(doc=None, state="ok"):
    observation = BackupObservation(state, doc)
    response = build_health(inputs(backup=observation))
    return by_id(response)["backups"], response.overall


@pytest.mark.parametrize(
    ("doc", "state", "status", "summary", "overall"),
    [
        (None, "not_configured", "unknown", "Not monitored · status source not mounted", "healthy"),
        (None, "absent", "unknown", "Waiting for the first backup status", "healthy"),
        (None, "invalid", "unhealthy", "Backup status is unreadable", "degraded"),
        ("healthy", "ok", "healthy", "Restore-tested · 3 h ago", "healthy"),
        ("failed_recent", "ok", "degraded", "Last backup failed · protected copy 3 h ago", "degraded"),
        ("stale", "ok", "degraded", "Last protected backup is overdue · 3 days ago", "degraded"),
        ("failed_stale", "ok", "unhealthy", "Backups failing · last protected copy 3 days ago", "degraded"),
        ("ancient", "ok", "unhealthy", "No restore-tested backup for 8 days", "degraded"),
        ("running", "ok", "degraded", "Backup in progress", "degraded"),
        ("running_long", "ok", "degraded", "Backup running longer than expected · 1 h", "degraded"),
        ("never", "ok", "unhealthy", "No restore-tested backup yet", "degraded"),
        ("first_running", "ok", "degraded", "First protected backup in progress", "degraded"),
    ],
)
def test_backup_health_matrix(doc, state, status, summary, overall):
    docs = {
        "healthy": status_doc(),
        "failed_recent": status_doc(status="failed", reason="restore_failed", last_attempt_at=NOW - timedelta(minutes=5)),
        "stale": status_doc(last_success_at=NOW - timedelta(days=3), last_restore_test_at=NOW - timedelta(days=3)),
        "failed_stale": status_doc(status="failed", reason="backup_failed", last_success_at=NOW - timedelta(days=3), last_restore_test_at=NOW - timedelta(days=3)),
        "ancient": status_doc(last_success_at=NOW - timedelta(days=8), last_restore_test_at=NOW - timedelta(days=8)),
        "running": status_doc(status="running", last_attempt_at=NOW - timedelta(minutes=2), last_finished_at=None),
        "running_long": status_doc(status="running", last_attempt_at=NOW - timedelta(minutes=75), last_finished_at=None),
        "never": status_doc(status="failed", reason="source_validation_failed", last_success_at=None, last_restore_test_at=None, restore_tested=False),
        "first_running": status_doc(status="running", last_success_at=None, last_restore_test_at=None, restore_tested=False, last_attempt_at=NOW - timedelta(minutes=1)),
    }
    component, overall_status = backup_component(docs.get(doc), state)
    assert (component.status, component.summary) == (status, summary)
    assert component.critical is False
    assert overall_status == overall


def test_backup_boundary_is_two_cadence_intervals():
    inside = status_doc(last_success_at=NOW - timedelta(hours=47, minutes=59), last_restore_test_at=NOW - timedelta(hours=47))
    outside = status_doc(last_success_at=NOW - timedelta(hours=48, minutes=1), last_restore_test_at=NOW - timedelta(hours=48))
    assert backup_component(inside)[0].status == "healthy"
    assert backup_component(outside)[0].status == "degraded"


def test_backup_details_are_safe_and_bounded():
    doc = status_doc(status="failed", reason="restore_failed", last_skipped_at=NOW - timedelta(minutes=30), last_skip_reason="locked")
    component, _ = backup_component(doc)
    assert component.details == [
        "Last restore-tested backup: 3 h ago", "Schedule: Daily 04:30 Europe/Istanbul",
        "Last attempt: restore test failed", "Last skipped run: another backup was running (30 min ago)",
    ]
    text = component.model_dump_json().lower()
    for forbidden in ("/home", "/var", ".roomos-backup", "key", "/root", "192.0.2", "traceback"):
        assert forbidden not in text


def write_status(path, **overrides):
    payload = status_doc(**overrides).model_dump(mode="json")
    path.write_text(json.dumps(payload))


def test_reader_states_and_cache(tmp_path):
    missing_dir = BackupStatusReader(str(tmp_path / "nope" / "status.json"))
    assert missing_dir.read().state == "not_configured"
    reader = BackupStatusReader(str(tmp_path / "status.json"))
    assert reader.read().state == "absent"
    write_status(tmp_path / "status.json")
    first = reader.read()
    assert first.state == "ok" and first.document.status == "success"
    assert reader.read() is first  # cached on unchanged (mtime, size)
    (tmp_path / "status.json").write_text("{not json")
    assert reader.read().state == "invalid"
    (tmp_path / "status.json").write_text(json.dumps({**status_doc().model_dump(mode="json"), "artifact": "/home/x"}))  # privacy-scan:allow fake path in a leak test
    assert reader.read().state == "invalid"  # unknown fields are rejected
    (tmp_path / "status.json").write_text("x" * 5000)
    assert reader.read().state == "invalid"
    inconsistent = {**status_doc().model_dump(mode="json"), "status": "failed", "reason": None}
    (tmp_path / "status.json").write_text(json.dumps(inconsistent))
    assert reader.read().state == "invalid"


def test_endpoint_reports_backup_from_mounted_status(client, tmp_path):
    status_dir = tmp_path / "backup-status"
    status_dir.mkdir()
    write_status(status_dir / "status.json", last_success_at=datetime.now(UTC) - timedelta(hours=1), last_restore_test_at=datetime.now(UTC) - timedelta(hours=1))
    original = app.state.backup_status
    try:
        app.state.backup_status = BackupStatusReader(str(status_dir / "status.json"))
        body = client.get("/api/v1/system/health").json()
        backup = next(c for c in body["components"] if c["id"] == "backups")
        assert backup["status"] == "healthy" and backup["summary"].startswith("Restore-tested")
        assert str(tmp_path) not in client.get("/api/v1/system/health").text
    finally:
        app.state.backup_status = original
