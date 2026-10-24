from datetime import UTC, datetime, timedelta

import pytest

from app.alerts import AlertEngine, AlertObservation, AlertRule
from app.auth import CSRF_COOKIE, SESSION_COOKIE
from app.main import app
from test_auth import auth_client  # noqa: F401 - shared isolated auth fixture


NOW = datetime(2026, 10, 16, 10, 0, tzinfo=UTC)


def observation(key, severity="warning", state="failed"):
    return AlertObservation(state=state, rule=AlertRule(
        key=key, severity=severity, type="fixture.alert", title=f"Alert {key}",
        message="Safe fixture message", source="fixture", failure_seconds=0,
        recovery_seconds=0,
    ))


@pytest.mark.asyncio
async def test_center_lists_active_and_resolved_with_filters_and_unread_count(tmp_path):
    engine = AlertEngine(str(tmp_path / "alerts.json"), clock=lambda: NOW)
    await engine.evaluate_observations([
        observation("fixture.warning"), observation("fixture.critical", "critical")
    ])
    await engine.evaluate_observations([observation("fixture.warning", state="healthy")])

    all_items = engine.list_notifications(status="all", severity=None, unread=None, source=None, limit=20, offset=0)
    assert [item.id for item in all_items.notifications] == ["fixture.critical", "fixture.warning"]
    assert (all_items.total, all_items.active_count, all_items.unread_count) == (2, 1, 2)
    active = engine.list_notifications(status="active", severity="critical", unread=True, source=None, limit=20, offset=0)
    assert [item.id for item in active.notifications] == ["fixture.critical"]


@pytest.mark.asyncio
async def test_read_ack_and_mute_are_persisted_but_never_resolve_fault(tmp_path):
    path = tmp_path / "alerts.json"
    engine = AlertEngine(str(path), clock=lambda: NOW)
    await engine.evaluate_observations([observation("fixture.critical", "critical")])

    read = await engine.mark_read("fixture.critical")
    acked = await engine.acknowledge("fixture.critical", actor="device-1")
    muted = await engine.mute("fixture.critical", until=NOW + timedelta(hours=2), actor="device-1")
    assert read.read_at == NOW
    assert acked.acknowledged_at == NOW and acked.acknowledged_by == "device-1"
    assert muted.muted_until == NOW + timedelta(hours=2)
    assert muted.status == "active"

    restored = AlertEngine(str(path), clock=lambda: NOW)
    item = restored.list_notifications(status="all", severity=None, unread=None, source=None, limit=20, offset=0).notifications[0]
    assert item.status == "active" and item.read_at == NOW and item.acknowledged_by == "device-1"


@pytest.mark.asyncio
async def test_operator_change_emits_safe_alert_updated_event(tmp_path):
    events = []
    async def publish(event):
        events.append(event)
    engine = AlertEngine(str(tmp_path / "alerts.json"), publish, clock=lambda: NOW)
    await engine.evaluate_observations([observation("fixture.warning")])
    await engine.mark_read("fixture.warning")
    assert events[-1].type == "alert.updated"
    assert events[-1].device_id == "fixture.warning"
    payload = str(events[-1].data)
    assert "token" not in payload and "session" not in payload and "192.168" not in payload


def test_notification_center_api_filters_and_mutations_do_not_resolve(client):
    client.portal.call(
        app.state.alerts.evaluate_observations,
        [observation("fixture.api", "critical")],
    )
    listed = client.get("/api/v1/notifications?status=active&severity=critical")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["notifications"]] == ["fixture.api"]
    assert client.post("/api/v1/notifications/fixture.api/read").status_code == 200
    assert client.post("/api/v1/notifications/fixture.api/acknowledge").json()["status"] == "active"
    muted = client.post("/api/v1/notifications/fixture.api/mute", json={"minutes": 60})
    assert muted.status_code == 200 and muted.json()["status"] == "active"


def test_read_scope_cannot_ack_or_mute_without_alerts_ack(auth_client):
    client, _, store, settings = auth_client
    issued = store.pair(
        store.create_pairing_window()[0],
        "read-only alerts",
        "fixture",
        scopes=("activity:read",),
    )
    client.cookies.set(SESSION_COOKIE, issued.token)
    client.cookies.set(CSRF_COOKIE, issued.csrf_token)
    client.portal.call(
        app.state.alerts.evaluate_observations,
        [observation("fixture.scoped")],
    )
    headers = {"Origin": settings.auth_allowed_origin, "X-CSRF-Token": issued.csrf_token}
    assert client.get("/api/v1/notifications").status_code == 200
    assert client.post("/api/v1/notifications/fixture.scoped/read", headers=headers).status_code == 200
    assert client.post("/api/v1/notifications/fixture.scoped/acknowledge", headers=headers).status_code == 403
    assert client.post("/api/v1/notifications/fixture.scoped/mute", json={"minutes": 60}, headers=headers).status_code == 403
