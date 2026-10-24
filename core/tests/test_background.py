import asyncio
import logging
from datetime import UTC, datetime

import pytest

from app.background import TASK_NAMES, BackgroundTasks
from app.config import Settings
from app.events import HAEventBridge
from app.health import HealthInputs, build_health
from app.main import app

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def inputs(registry: BackgroundTasks, **overrides) -> HealthInputs:
    values = dict(
        now=NOW, ha_configured=True, ha_bridge_state="connected", ha_bridge_since=NOW,
        ha_bridge_alive=True, control_enabled=True, configured_scenes=8,
        background=registry.snapshot(),
    )
    values.update(overrides)
    return HealthInputs(**values)


def by_id(response):
    return {component.id: component for component in response.components}


async def _forever(stop: asyncio.Event):
    await stop.wait()


async def _crash(message: str):
    await asyncio.sleep(0)
    raise RuntimeError(message)


def test_registry_names_every_background_task():
    assert TASK_NAMES == (
        "scheduler", "ha_bridge", "notification_dispatcher", "devices",
        "system_monitor", "camera", "dns_health", "routines.diagnostics", "smart_automations",
    )


@pytest.mark.asyncio
async def test_crashed_devices_task_is_visible_in_health_and_logged_without_detail(caplog):
    registry = BackgroundTasks()
    stop = asyncio.Event()
    for name in TASK_NAMES:
        if name != "devices":
            registry.start(name, _forever(stop))
    registry.start("devices", _crash("token=very-secret http://10.0.0.1"))  # privacy-scan:allow fake value in a log-redaction test
    with caplog.at_level(logging.ERROR, logger="app.background"):
        await asyncio.sleep(0.05)
    states = dict(registry.snapshot())
    assert states["devices"] == "failed" and states["scheduler"] == "running"
    component = by_id(build_health(inputs(registry)))["background_services"]
    assert component.status == "degraded" and component.critical is False
    assert component.summary == "1 of 9 background services stopped"
    assert component.details == ["Device registry stopped unexpectedly"]
    # A non-critical task never makes RoomOS unhealthy on its own.
    assert build_health(inputs(registry)).overall == "degraded"
    logged = caplog.text
    assert "task=devices" in logged and "RuntimeError" in logged
    assert "very-secret" not in logged and "10.0.0.1" not in logged  # privacy-scan:allow fake value in a log-redaction test
    registry.begin_shutdown(); stop.set()
    await registry.wait()


@pytest.mark.asyncio
async def test_normal_shutdown_and_cancellation_are_not_failures(caplog):
    registry = BackgroundTasks()
    stop = asyncio.Event()
    for name in TASK_NAMES[:-1]:
        registry.start(name, _forever(stop))
    registry.start("routines.diagnostics", asyncio.Event().wait())
    with caplog.at_level(logging.ERROR, logger="app.background"):
        registry.begin_shutdown()
        stop.set()
        registry.get("routines.diagnostics").cancel()
        await registry.wait()
    assert caplog.records == []
    assert {state for _, state in registry.snapshot()} == {"stopped"}
    assert registry.failures == {}


@pytest.mark.asyncio
async def test_unexpected_clean_return_is_reported_as_stopped():
    registry = BackgroundTasks()
    stop = asyncio.Event()
    for name in TASK_NAMES:
        if name != "camera":
            registry.start(name, _forever(stop))
    registry.start("camera", asyncio.sleep(0))
    await asyncio.sleep(0.05)
    assert dict(registry.snapshot())["camera"] == "failed"
    assert by_id(build_health(inputs(registry)))["background_services"].details == ["Camera monitor stopped unexpectedly"]
    registry.begin_shutdown(); stop.set()
    await registry.wait()


@pytest.mark.asyncio
async def test_unexpected_task_exit_notifies_bounded_observer_but_shutdown_does_not():
    registry = BackgroundTasks()
    seen = []

    async def observe(name, state):
        seen.append((name, state))

    registry.listeners.append(observe)
    registry.start("scheduler", _crash("secret detail"))
    await asyncio.sleep(0.05)
    assert seen == [("scheduler", "failed")]

    registry.begin_shutdown()
    task = registry.start("devices", asyncio.Event().wait())
    task.cancel()
    await registry.wait()
    assert seen == [("scheduler", "failed")]


@pytest.mark.asyncio
async def test_disabled_tasks_are_not_counted_and_critical_failure_stays_critical():
    registry = BackgroundTasks()
    stop = asyncio.Event()
    for name in ("scheduler", "devices", "routines.diagnostics"):
        registry.start(name, _forever(stop))
    for name in ("notification_dispatcher", "system_monitor", "camera", "dns_health"):
        registry.disabled(name)
    registry.start("ha_bridge", _crash("boom"))
    await asyncio.sleep(0.05)
    response = build_health(inputs(registry, ha_bridge_alive=registry.alive("ha_bridge")))
    components = by_id(response)
    assert components["home_assistant"].status == "unhealthy"
    assert components["background_services"].summary == "1 of 4 background services stopped"
    assert response.overall == "unhealthy"
    registry.begin_shutdown(); stop.set()
    await registry.wait()


def test_health_reports_all_running_background_services(client):
    response = client.get("/api/v1/system/health").json()
    component = {item["id"]: item for item in response["components"]}["background_services"]
    assert component["status"] == "healthy"
    assert component["critical"] is False
    registry = app.state.background_tasks
    assert {name for name, _ in registry.snapshot()} == set(TASK_NAMES)


def test_health_without_registry_is_not_monitored():
    component = by_id(build_health(HealthInputs(
        now=NOW, ha_configured=True, ha_bridge_state="connected", ha_bridge_since=NOW,
        ha_bridge_alive=True, control_enabled=True, configured_scenes=8,
    )))["background_services"]
    assert component.status == "unknown"


class _Broker:
    def __init__(self):
        self.events = []

    async def broadcast(self, event, **_):
        self.events.append(event)


class _FlakyScenes:
    """Raises an unexpected error on the first N subscriptions, then works."""

    def __init__(self, failures: int):
        self.failures = failures
        self.active_scene = None

    async def refresh_active(self, client):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("unexpected bug with token=very-secret")
        return False

    def reconcile_light(self, light):
        return False


class _ReconnectingHA:
    def __init__(self):
        self.connections = 0
        self.closed = 0

    async def event_stream(self):
        self.connections += 1
        try:
            yield {"type": "result", "success": True}
            raise asyncio.CancelledError
        finally:
            self.closed += 1


@pytest.mark.asyncio
async def test_ha_bridge_recovers_after_unexpected_error(monkeypatch, caplog):
    sleeps = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    settings = Settings(HA_TOKEN="not-a-real-token", ROOMOS_HA_RECONNECT_MIN_SECONDS=1,  # privacy-scan:allow fake value in a log-redaction test
                        ROOMOS_HA_RECONNECT_MAX_SECONDS=5)
    ha = _ReconnectingHA()
    broker = _Broker()
    bridge = HAEventBridge(settings, ha, broker, _FlakyScenes(failures=1))
    with caplog.at_level(logging.ERROR, logger="app.events"), pytest.raises(asyncio.CancelledError):
        await bridge.run()
    assert ha.connections == 2 and ha.closed == 2  # the failed stream is closed, not leaked
    assert sleeps == [1]
    states = [event.data.get("home_assistant") for event in broker.events if event.type == "roomos.connection"]
    assert states == ["connecting", "disconnected", "connecting", "connected"]
    assert bridge.state == "connected"
    assert "RuntimeError" in caplog.text and "very-secret" not in caplog.text


@pytest.mark.asyncio
async def test_ha_bridge_repeated_unexpected_errors_back_off_exponentially(monkeypatch):
    sleeps = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    settings = Settings(HA_TOKEN="not-a-real-token", ROOMOS_HA_RECONNECT_MIN_SECONDS=1,  # privacy-scan:allow fake value in a log-redaction test
                        ROOMOS_HA_RECONNECT_MAX_SECONDS=5)
    # Each failure happens right after a successful subscription; it must not reset to
    # the minimum delay and spin in a fast restart loop.
    bridge = HAEventBridge(settings, _ReconnectingHA(), _Broker(), _FlakyScenes(failures=5))
    with pytest.raises(asyncio.CancelledError):
        await bridge.run()
    assert sleeps == [1, 2, 4, 5, 5]


def test_lifespan_shutdown_is_not_reported_as_failure(monkeypatch, tmp_path, caplog):
    from fastapi.testclient import TestClient

    from app.main import get_settings

    monkeypatch.setenv("ROOMOS_AUTH_ENABLED", "false")
    for name in ("AUTH_DATABASE", "ROUTINES", "AUTOMATIONS", "AUTOMATION_HISTORY", "SCENE_STATE", "ALERTS", "NOTIFICATIONS", "NOTIFICATION_INCIDENTS"):
        monkeypatch.setenv(f"ROOMOS_{name}_PATH", str(tmp_path / f"{name.lower()}.json"))
    monkeypatch.setenv("ROOMOS_AUTH_SECRET_PATH", str(tmp_path / "auth.secret"))
    get_settings.cache_clear()
    try:
        with caplog.at_level(logging.ERROR, logger="app.background"):
            with TestClient(app):
                registry = app.state.background_tasks
                running = {name for name, state in registry.snapshot() if state == "running"}
                assert {"scheduler", "smart_automations", "devices", "routines.diagnostics"} <= running
        assert registry.failures == {}
        assert "stopped unexpectedly" not in caplog.text
        assert all(state in {"stopped", "disabled"} for _, state in registry.snapshot())
    finally:
        get_settings.cache_clear()
