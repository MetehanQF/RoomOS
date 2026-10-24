"""RoomOS operational health: is RoomOS itself working, and which subsystem needs attention?

Everything here is derived from in-memory runtime state that already exists. Building the
view performs no network calls, no Home Assistant traffic, no filesystem scan, no backup
access and no control action. Output is server-owned text only: no paths, entity IDs,
addresses, URLs, exception text or credentials.

Aggregation:
  overall = unhealthy  if any CRITICAL component is unhealthy
            degraded   if any monitored component is degraded or unhealthy
            healthy    otherwise
Components with status ``unknown`` (not monitored / no status yet) never affect ``overall``.
Backups are non-critical: a failed or stale backup degrades RoomOS but never makes it
unhealthy as a runtime, because control does not depend on backups.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from .background import TASK_LABELS
from .backup_status import BackupObservation

ComponentId = Literal[
    "home_assistant",
    "control",
    "persistent_state",
    "automations",
    "execution_history",
    "notifications",
    "telemetry_history",
    "backups",
    "background_services",
]
ComponentStatus = Literal["healthy", "degraded", "unhealthy", "unknown"]
Overall = Literal["healthy", "degraded", "unhealthy"]
StoreStatus = Literal["ok", "corrupt", "write_failed"]

CRITICAL: frozenset[str] = frozenset({"home_assistant", "control", "persistent_state", "automations"})
# Three times the HA reconnect backoff ceiling (60 s): one missed reconnect cycle is a
# transient degradation, a sustained outage is unhealthy.
HA_RECONNECT_GRACE = timedelta(seconds=180)
EXPECTED_SCENES = 8
# Stores whose corruption stops RoomOS from controlling the room safely.
CONTROL_STORES = frozenset({"Routines", "Smart automations"})


class HealthComponent(BaseModel):
    id: ComponentId
    name: str = Field(max_length=40)
    status: ComponentStatus
    critical: bool
    summary: str = Field(max_length=120)
    details: list[str] = Field(default_factory=list, max_length=8)
    checked_at: datetime


class HealthResponse(BaseModel):
    overall: Overall
    checked_at: datetime
    components: list[HealthComponent]


@dataclass(frozen=True)
class HealthInputs:
    now: datetime
    ha_configured: bool
    ha_bridge_state: str | None  # connecting | connected | disconnected | None (no bridge)
    ha_bridge_since: datetime | None
    ha_bridge_alive: bool
    control_enabled: bool
    configured_scenes: int
    stores: dict[str, StoreStatus] = field(default_factory=dict)
    execution_status: str = "ok"
    execution_count: int = 0
    routines_corrupt: bool = False
    scheduler_alive: bool = True
    routines_enabled: int = 0
    routines_total: int = 0
    automation_corrupt: bool = False
    automation_engine_alive: bool = True
    automation_enabled: int = 0
    automation_total: int = 0
    automation_last_tick: datetime | None = None
    automation_queue_depth: int = 0
    automation_failures: int = 0
    automation_loop_preventions: int = 0
    automation_persistence_failures: int = 0
    notification_configuration: str = "configured"
    notification_storage: str = "ok"
    incident_storage: str = "ok"
    notification_destinations: int = 0
    notification_delivery_enabled: bool = False
    dispatcher_alive: bool = False
    history_outcome: str = "none"
    backup: BackupObservation = field(default_factory=lambda: BackupObservation("not_configured"))
    # (task name, running | stopped | failed | disabled); empty = not monitored.
    background: tuple[tuple[str, str], ...] = ()


def _component(inputs: HealthInputs, id_: ComponentId, name: str, status: ComponentStatus, summary: str, details: list[str] | None = None) -> HealthComponent:
    return HealthComponent(
        id=id_,
        name=name,
        status=status,
        critical=id_ in CRITICAL,
        summary=summary,
        details=details or [],
        checked_at=inputs.now,
    )


def _minutes(delta: timedelta) -> str:
    minutes = max(1, int(delta.total_seconds() // 60))
    return f"{minutes} min"


def home_assistant(inputs: HealthInputs) -> HealthComponent:
    name = "Home Assistant"
    if not inputs.ha_configured or inputs.ha_bridge_state is None:
        return _component(inputs, "home_assistant", name, "unhealthy", "Live connection is not configured")
    if not inputs.ha_bridge_alive:
        return _component(inputs, "home_assistant", name, "unhealthy", "Connection handler has stopped")
    if inputs.ha_bridge_state == "connected":
        return _component(inputs, "home_assistant", name, "healthy", "Connected · live updates active")
    elapsed = inputs.now - (inputs.ha_bridge_since or inputs.now)
    if elapsed <= HA_RECONNECT_GRACE:
        return _component(inputs, "home_assistant", name, "degraded", "Reconnecting")
    return _component(inputs, "home_assistant", name, "unhealthy", f"Disconnected for {_minutes(elapsed)}")


def control(inputs: HealthInputs, ha: HealthComponent) -> HealthComponent:
    name = "Control"
    if inputs.configured_scenes == 0:
        return _component(inputs, "control", name, "unhealthy", "No scenes are configured")
    if not inputs.control_enabled:
        return _component(inputs, "control", name, "degraded", "Disabled by operator setting")
    if ha.status != "healthy":
        return _component(inputs, "control", name, "degraded", "Waiting for Home Assistant")
    if inputs.configured_scenes < EXPECTED_SCENES:
        return _component(inputs, "control", name, "degraded", f"{inputs.configured_scenes} of {EXPECTED_SCENES} scenes configured")
    return _component(inputs, "control", name, "healthy", f"Ready · {inputs.configured_scenes} scenes")


def persistent_state(inputs: HealthInputs) -> HealthComponent:
    name = "Persistent state"
    problems = {label: state for label, state in inputs.stores.items() if state != "ok"}
    if not problems:
        return _component(inputs, "persistent_state", name, "healthy", "All stores healthy")
    details = [
        f"{label}: {'needs repair' if state == 'corrupt' else 'writes failing'}"
        for label, state in sorted(problems.items())
    ]
    severe = any(label in CONTROL_STORES and state == "corrupt" for label, state in problems.items())
    count = len(problems)
    summary = f"{count} store{'s' if count != 1 else ''} {'need' if count != 1 else 'needs'} attention"
    return _component(inputs, "persistent_state", name, "unhealthy" if severe else "degraded", summary, details)


def execution_history(inputs: HealthInputs) -> HealthComponent:
    name = "Execution history"
    if inputs.execution_status == "corrupt":
        return _component(inputs, "execution_history", name, "degraded", "Store unreadable · kept in memory only; control continues")
    if inputs.execution_status == "write_failed":
        return _component(inputs, "execution_history", name, "degraded", "Writes are currently failing; control continues")
    if inputs.execution_status != "ok":
        return _component(inputs, "execution_history", name, "degraded", "Storage state is unknown")
    if inputs.execution_count == 0:
        return _component(inputs, "execution_history", name, "healthy", "Writable · no executions yet")
    plural = "s" if inputs.execution_count != 1 else ""
    return _component(inputs, "execution_history", name, "healthy", f"Writable · {inputs.execution_count} record{plural}")


def automations(inputs: HealthInputs) -> HealthComponent:
    name = "Automations"
    if inputs.routines_corrupt:
        return _component(inputs, "automations", name, "unhealthy", "Routine storage needs repair; scheduler is paused")
    if not inputs.scheduler_alive:
        return _component(inputs, "automations", name, "unhealthy", "Scheduler is not running")
    if inputs.automation_corrupt:
        return _component(inputs, "automations", name, "unhealthy", "Smart automation storage needs repair; engine is paused")
    if not inputs.automation_engine_alive:
        return _component(inputs, "automations", name, "unhealthy", "Smart automation engine is not running")
    details = [
        f"Smart rules {inputs.automation_enabled}/{inputs.automation_total} · queue {inputs.automation_queue_depth}",
        f"Safety blocks {inputs.automation_loop_preventions} · failures {inputs.automation_failures}",
    ]
    if inputs.automation_persistence_failures:
        return _component(inputs, "automations", name, "degraded", "Automation history or state writes are failing", details)
    return _component(inputs, "automations", name, "healthy", f"Engines running · {inputs.routines_enabled} legacy + {inputs.automation_enabled} smart enabled", details)


def notifications(inputs: HealthInputs) -> HealthComponent:
    name = "Notifications"
    if inputs.notification_configuration == "unavailable":
        return _component(inputs, "notifications", name, "unhealthy", "Push provider setup could not be loaded")
    if inputs.notification_storage == "corrupt":
        return _component(inputs, "notifications", name, "unhealthy", "Notification storage needs repair")
    if inputs.incident_storage == "corrupt":
        return _component(inputs, "notifications", name, "unhealthy", "Incident policy storage needs repair")
    if inputs.notification_configuration == "disabled":
        return _component(inputs, "notifications", name, "degraded", "Push delivery is turned off")
    if inputs.notification_destinations == 0:
        return _component(inputs, "notifications", name, "degraded", "No enabled destination")
    if inputs.notification_delivery_enabled and not inputs.dispatcher_alive:
        return _component(inputs, "notifications", name, "unhealthy", "Dispatcher is not running")
    plural = "s" if inputs.notification_destinations != 1 else ""
    return _component(inputs, "notifications", name, "healthy", f"Dispatcher ready · {inputs.notification_destinations} destination{plural}")


def telemetry_history(inputs: HealthInputs) -> HealthComponent:
    name = "Telemetry history"
    if inputs.history_outcome == "stale_fallback":
        return _component(inputs, "telemetry_history", name, "degraded", "Showing cached trends · last refresh failed")
    if inputs.history_outcome == "failed":
        return _component(inputs, "telemetry_history", name, "degraded", "Last history request failed")
    if inputs.history_outcome == "ok":
        return _component(inputs, "telemetry_history", name, "healthy", "Recorder history available")
    return _component(inputs, "telemetry_history", name, "healthy", "Ready · no trend requested yet")


# Backup thresholds are multiples of the configured cadence (daily → 24 h):
BACKUP_HEALTHY_INTERVALS = 2      # newest restore-tested backup ≤ 48 h old → healthy
BACKUP_UNHEALTHY_INTERVALS = 7    # older than 7 days → unhealthy regardless of the last attempt
BACKUP_RUN_EXPECTED = timedelta(minutes=30)
BACKUP_REASONS = {
    "source_validation_failed": "source validation failed",
    "backup_failed": "backup creation failed",
    "integrity_failed": "integrity check failed",
    "restore_failed": "restore test failed",
    "cleanup_failed": "cleanup failed",
    "retention_failed": "retention failed",
    "locked": "another backup was running",
    "interrupted": "backup was interrupted",
}


def _age(delta: timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 3600:
        return f"{max(1, seconds // 60)} min ago"
    if seconds < 2 * 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} days ago"


def backups(inputs: HealthInputs) -> HealthComponent:
    name = "Backups"
    observation = inputs.backup
    if observation.state == "not_configured":
        return _component(inputs, "backups", name, "unknown", "Not monitored · status source not mounted")
    if observation.state == "absent":
        return _component(inputs, "backups", name, "unknown", "Waiting for the first backup status")
    if observation.state == "invalid" or observation.document is None:
        return _component(inputs, "backups", name, "unhealthy", "Backup status is unreadable")
    doc = observation.document
    cadence = timedelta(seconds=doc.cadence_seconds)
    details = [f"Schedule: {doc.schedule}"]
    if doc.last_success_at is not None:
        details.insert(0, f"Last restore-tested backup: {_age(inputs.now - doc.last_success_at)}")
    if doc.status == "failed" and doc.reason:
        details.append(f"Last attempt: {BACKUP_REASONS[doc.reason]}")
    if doc.last_skip_reason and doc.last_skipped_at is not None:
        details.append(f"Last skipped run: {BACKUP_REASONS[doc.last_skip_reason]} ({_age(inputs.now - doc.last_skipped_at)})")
    running_for = inputs.now - doc.last_attempt_at if doc.status == "running" and doc.last_attempt_at else None
    if doc.last_success_at is None or not doc.restore_tested:
        if running_for is not None and running_for <= BACKUP_RUN_EXPECTED:
            return _component(inputs, "backups", name, "degraded", "First protected backup in progress", details)
        return _component(inputs, "backups", name, "unhealthy", "No restore-tested backup yet", details)
    age = inputs.now - doc.last_success_at
    when = _age(age)
    if age > cadence * BACKUP_UNHEALTHY_INTERVALS:
        return _component(inputs, "backups", name, "unhealthy", f"No restore-tested backup for {when.removesuffix(' ago')}", details)
    recent = age <= cadence * BACKUP_HEALTHY_INTERVALS
    if doc.status == "failed":
        if recent:
            return _component(inputs, "backups", name, "degraded", f"Last backup failed · protected copy {when}", details)
        return _component(inputs, "backups", name, "unhealthy", f"Backups failing · last protected copy {when}", details)
    if running_for is not None:
        if running_for > BACKUP_RUN_EXPECTED:
            return _component(inputs, "backups", name, "degraded", f"Backup running longer than expected · {_age(running_for).removesuffix(' ago')}", details)
        return _component(inputs, "backups", name, "degraded", "Backup in progress", details)
    if not recent:
        return _component(inputs, "backups", name, "degraded", f"Last protected backup is overdue · {when}", details)
    return _component(inputs, "backups", name, "healthy", f"Restore-tested · {when}", details)


def background_services(inputs: HealthInputs) -> HealthComponent:
    """Non-critical by design: a stopped critical task (bridge, scheduler, dispatcher)
    already makes its own component unhealthy; this view only adds completeness."""
    name = "Background services"
    if not inputs.background:
        return _component(inputs, "background_services", name, "unknown", "Not monitored")
    expected = [(task, state) for task, state in inputs.background if state != "disabled"]
    stopped = [task for task, state in expected if state != "running"]
    if not stopped:
        return _component(inputs, "background_services", name, "healthy", f"All {len(expected)} running")
    details = [f"{TASK_LABELS.get(task, 'Service')} stopped unexpectedly" for task in stopped]
    return _component(
        inputs, "background_services", name, "degraded",
        f"{len(stopped)} of {len(expected)} background services stopped", details,
    )


def build_health(inputs: HealthInputs) -> HealthResponse:
    ha = home_assistant(inputs)
    components = [
        ha,
        control(inputs, ha),
        persistent_state(inputs),
        automations(inputs),
        execution_history(inputs),
        notifications(inputs),
        telemetry_history(inputs),
        backups(inputs),
        background_services(inputs),
    ]
    monitored = [component for component in components if component.status != "unknown"]
    if any(component.critical and component.status == "unhealthy" for component in monitored):
        overall: Overall = "unhealthy"
    elif any(component.status != "healthy" for component in monitored):
        overall = "degraded"
    else:
        overall = "healthy"
    return HealthResponse(overall=overall, checked_at=inputs.now, components=components)


def _task_alive(task) -> bool:
    return task is not None and not task.done()


def _background(tasks) -> tuple[tuple[str, str], ...]:
    snapshot = getattr(tasks, "snapshot", None)
    return snapshot() if callable(snapshot) else ()


def collect_inputs(state, settings, *, now: datetime | None = None) -> HealthInputs:
    """Read existing runtime objects only. Never awaits I/O."""

    tasks = getattr(state, "background_tasks", {}) or {}
    bridge = getattr(state, "ha_bridge", None)
    scenes = state.scenes
    routines = state.routines
    smart_automations = getattr(state, "automations", None)
    alerts = state.alerts
    notification_engine = state.notifications
    push_runtime = getattr(state, "push", None)
    if getattr(settings, "push_mode", "legacy") == "smart" and push_runtime is not None:
        # 7G: the push engine exposes the same health surface as the legacy outbox.
        notification_engine = push_runtime.engine
    policy = state.notification_policy
    alert_policy = getattr(state, "alert_policy", None)
    executions = state.executions
    history = state.history

    def store(status: str, write_failed: bool) -> StoreStatus:
        if status == "corrupt":
            return "corrupt"
        return "write_failed" if write_failed else "ok"

    stores: dict[str, StoreStatus] = {
        # Authentication is exercised by this very request (session lookup in the auth DB).
        "Authentication": "ok",
        "Scene state": store("ok", getattr(scenes, "state_write_failed", False)),
        "Routines": store("corrupt" if routines.corrupt else "ok", getattr(routines, "write_failed", False)),
        "Smart automations": store(
            "corrupt" if smart_automations is not None and smart_automations.store.corrupt else "ok",
            bool(smart_automations is not None and smart_automations.store.write_failed),
        ),
        "Activity & alerts": store(alerts.storage_status, getattr(alerts, "write_failed", False)),
        "Notifications": store(notification_engine.storage_status, getattr(notification_engine, "write_failed", False)),
        "Notification incidents": store(policy.storage_status, getattr(policy, "write_failed", False)),
        "Alert policy": store(getattr(alert_policy, "storage_status", "ok"), getattr(alert_policy, "write_failed", False)),
    }
    return HealthInputs(
        now=now or datetime.now(UTC),
        ha_configured=bool(settings.ha_connect_enabled and settings.has_ha_credentials),
        ha_bridge_state=getattr(bridge, "state", None) if bridge is not None else None,
        ha_bridge_since=getattr(bridge, "state_since", None) if bridge is not None else None,
        ha_bridge_alive=_task_alive(tasks.get("ha_bridge")),
        control_enabled=settings.control_enabled,
        configured_scenes=len(scenes.configured),
        stores=stores,
        execution_status=executions.storage_status,
        execution_count=len(executions.document.executions),
        routines_corrupt=routines.corrupt,
        scheduler_alive=_task_alive(tasks.get("scheduler")) and routines.scheduler_running,
        routines_enabled=sum(1 for routine in routines.document.routines if routine.enabled),
        routines_total=len(routines.document.routines),
        automation_corrupt=bool(smart_automations is not None and smart_automations.store.corrupt),
        automation_engine_alive=(
            smart_automations is not None
            and _task_alive(tasks.get("smart_automations"))
            and smart_automations.running
        ),
        automation_enabled=sum(1 for item in smart_automations.store.document.automations if item.enabled) if smart_automations is not None else 0,
        automation_total=len(smart_automations.store.document.automations) if smart_automations is not None else 0,
        automation_last_tick=smart_automations.last_tick if smart_automations is not None else None,
        automation_queue_depth=sum(lock.locked() for lock in smart_automations._locks.values()) if smart_automations is not None else 0,
        automation_failures=smart_automations.failures if smart_automations is not None else 0,
        automation_loop_preventions=smart_automations.loop_preventions if smart_automations is not None else 0,
        automation_persistence_failures=int(bool(smart_automations is not None and smart_automations.store.write_failed)),
        notification_configuration=notification_engine.configuration_status,
        notification_storage=notification_engine.storage_status,
        incident_storage=policy.storage_status,
        notification_destinations=len(notification_engine.destinations),
        notification_delivery_enabled=notification_engine.delivery_enabled,
        dispatcher_alive=_task_alive(tasks.get("notification_dispatcher")),
        history_outcome=history.last_outcome,
        backup=state.backup_status.read() if getattr(state, "backup_status", None) is not None else BackupObservation("not_configured"),
        background=_background(tasks),
    )
