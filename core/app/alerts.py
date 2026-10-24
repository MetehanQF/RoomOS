import asyncio
import json
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .executions import Execution, ExecutionId
from .models import RoomOSEvent, SystemResponse

logger = logging.getLogger(__name__)

AlertSeverity = Literal["info", "warning", "critical"]
AlertStatus = Literal["active", "resolved"]
ObservationState = Literal["healthy", "failed", "hold", "unknown"]
ActivityKind = Literal["alert_started", "alert_resolved", "scene_activated", "scene_failed"]
REPEAT_OBSERVATION_SECONDS = 300.0


class AlertRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9._-]{1,80}$")
    type: str = Field(min_length=1, max_length=60)
    severity: AlertSeverity
    title: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=240)
    source: str = Field(min_length=1, max_length=40)
    started_at: datetime
    updated_at: datetime
    status: AlertStatus
    occurrence_count: int = Field(default=1, ge=1, le=1000000)
    last_observed_at: datetime | None = None
    last_occurrence_at: datetime | None = None
    read_at: datetime | None = None
    acknowledged_at: datetime | None = None
    acknowledged_by: str | None = Field(default=None, max_length=80)
    muted_until: datetime | None = None
    muted_by: str | None = Field(default=None, max_length=80)


class ActivityRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^act_[a-f0-9]{32}$")
    type: ActivityKind
    title: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=240)
    source: str = Field(min_length=1, max_length=40)
    occurred_at: datetime
    severity: AlertSeverity | None = None
    reference_id: str = Field(min_length=1, max_length=80)
    execution_id: ExecutionId | None = None


class PendingAlert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first_seen: datetime
    observations: int = Field(ge=1, le=1000)
    transition: Literal["opening", "recovering"] = "opening"
    last_seen: datetime | None = None


class AlertStateFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    alerts: list[AlertRecord] = Field(default_factory=list, max_length=200)
    activity: list[ActivityRecord] = Field(default_factory=list, max_length=300)
    pending: dict[str, PendingAlert] = Field(default_factory=dict)


class AlertSummary(BaseModel):
    status: Literal["operational", "attention", "critical", "unknown"]
    active_count: int
    highest_severity: AlertSeverity | None


class AlertListResponse(BaseModel):
    active: list[AlertRecord]
    active_count: int
    highest_severity: AlertSeverity | None
    storage_status: Literal["ok", "corrupt"]


class NotificationCenterResponse(BaseModel):
    notifications: list[AlertRecord]
    # 7I (response-only, never persisted): active snoozes and the evidence behind resource alerts.
    snoozes: dict[str, dict] = Field(default_factory=dict)
    insights: dict[str, dict] = Field(default_factory=dict)
    total: int
    unread_count: int
    active_count: int
    highest_severity: AlertSeverity | None
    limit: int
    offset: int
    storage_status: Literal["ok", "corrupt"]


class NotificationMuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    minutes: int = Field(ge=5, le=10080)


class ActivityDetail(ActivityRecord):
    execution: Execution | None = None
    device_names: dict[str, str] = Field(default_factory=dict, max_length=5)


class ActivityListResponse(BaseModel):
    execution_storage_status: Literal["ok", "corrupt", "write_failed"] = "ok"
    activity: list[ActivityDetail]
    total: int
    limit: int
    offset: int
    storage_status: Literal["ok", "corrupt"]


class RuleState(BaseModel):
    severity: AlertSeverity
    type: str
    title: str
    message: str
    source: str
    confirmation_required: bool = False


class AlertRule(RuleState):
    key: str = Field(pattern=r"^[a-z0-9._-]{1,80}$")
    failure_seconds: float = Field(default=0, ge=0, le=86400)
    recovery_seconds: float = Field(default=0, ge=0, le=86400)


class AlertObservation(BaseModel):
    state: ObservationState
    rule: AlertRule


Publish = Callable[[RoomOSEvent], Awaitable[None]]
NotificationSink = Callable[[AlertRecord], Awaitable[object]]
LifecycleTransition = Literal["started", "escalated", "resolved"]
LifecycleSink = Callable[[LifecycleTransition, AlertRecord], Awaitable[object]]


def _metric_bool(system: SystemResponse, node: str, suffix: str) -> bool | None:
    target = system.primary if node == "primary" else system.secondary
    metric = target.metrics.get(f"{node}_{suffix}")
    if metric is None or not metric.available:
        return None
    return metric.value if isinstance(metric.value, bool) else None


def rules_from_system(system: SystemResponse) -> dict[str, RuleState]:
    rules: dict[str, RuleState] = {}
    for node_id, node, label in (
        ("primary", system.primary, "Primary"),
        ("secondary", system.secondary, "Secondary"),
    ):
        if node.online is False:
            rules[f"node.{node_id}.offline"] = RuleState(
                severity="critical",
                type="node.offline",
                title=f"{label} is offline",
                message=f"{label} has not passed its canonical online check.",
                source=node_id,
                confirmation_required=True,
            )

    if _metric_bool(system, "primary", "throttled") is True:
        rules["node.primary.throttled"] = RuleState(
            severity="warning",
            type="node.throttled",
            title="Primary is throttled",
            message="Primary reports an active throttling condition.",
            source="primary",
        )

    dns = system.dns_cluster.status
    if dns == "degraded":
        rules["service.dns.degraded"] = RuleState(
            severity="warning",
            type="service.degraded",
            title="DNS redundancy degraded",
            message="Only part of the DNS cluster is currently functional.",
            source="dns",
        )
    elif dns == "offline":
        rules["service.dns.offline"] = RuleState(
            severity="critical",
            type="service.offline",
            title="DNS cluster is offline",
            message="Neither DNS resolver is currently functional.",
            source="dns",
        )

    by_id = {service.id: service for service in system.services}
    for service_id, label in (("frigate", "Frigate"), ("nextcloud", "Nextcloud")):
        service = by_id.get(service_id)
        if service is None:
            continue
        if service.health == "degraded":
            rules[f"service.{service_id}.degraded"] = RuleState(
                severity="warning",
                type="service.degraded",
                title=f"{label} is degraded",
                message=f"{label} reports a degraded canonical health state.",
                source=service_id,
            )
        elif service.health == "offline":
            rules[f"service.{service_id}.offline"] = RuleState(
                severity="critical",
                type="service.offline",
                title=f"{label} is offline",
                message=f"{label} is unavailable according to its trusted health source.",
                source=service_id,
            )
    return rules


class AlertEngine:
    def __init__(
        self,
        path: str,
        publish: Publish | None = None,
        *,
        history_limit: int = 300,
        confirmation_seconds: float = 30.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self.publish = publish
        self.history_limit = history_limit
        self.confirmation_seconds = confirmation_seconds
        self.clock = clock or (lambda: datetime.now(UTC))
        self.storage_status: Literal["ok", "corrupt"] = "ok"
        self._persist_enabled = True
        self.write_failed = False
        # (path, exact document, file identity) last made durable: file fsync + rename + directory
        # fsync all succeeded. None until the first successful write of this process and after
        # any failure.
        self._durable: tuple[Path, str, tuple[int, int, int, int]] | None = None
        self._evaluated = False
        self._lock = asyncio.Lock()
        self._notification_sink: NotificationSink | None = None
        self._lifecycle_sinks: list[LifecycleSink] = []
        self.policy = None  # 7I AlertPolicyStore (workload-aware resources, snooze); None = 7H rules
        self._state = self._load()

    def set_notification_sink(self, sink: NotificationSink) -> None:
        self._notification_sink = sink

    def add_lifecycle_sink(self, sink: LifecycleSink) -> None:
        """7G: observe started / escalated (to critical while active) / resolved transitions.
        Sinks are isolated: a failure never affects the alert lifecycle."""
        self._lifecycle_sinks.append(sink)

    def alert_by_id(self, alert_id: str) -> AlertRecord | None:
        alert = next((item for item in self._state.alerts if item.id == alert_id), None)
        return alert.model_copy(deep=True) if alert is not None else None

    async def _notify_lifecycle(self, transition: LifecycleTransition, alert: AlertRecord) -> None:
        if transition == "escalated" and self.policy is not None:
            snooze = self.policy.snooze(alert.id)
            if snooze is not None and snooze.severity != "critical":
                self.policy.clear_snooze(alert.id)  # a warning-time snooze never hides the escalation
        if not self._lifecycle_sinks:
            return
        # The lifecycle timestamp must be durable before a consumer derives a dedupe key from it.
        self._persist()
        for sink in self._lifecycle_sinks:
            try:
                await sink(transition, alert.model_copy(deep=True))
            except Exception as exc:
                logger.warning(
                    "Alert lifecycle sink failed transition=%s error=%s",
                    transition,
                    type(exc).__name__,
                )

    @staticmethod
    def _escalates(previous: str, current: str) -> bool:
        return current == "critical" and previous != "critical"

    def _load(self) -> AlertStateFile:
        try:
            if not self.path.exists():
                return AlertStateFile()
            return AlertStateFile.model_validate_json(self.path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, json.JSONDecodeError):
            logger.error("RoomOS alert persistence is corrupt; running fail-safe in memory")
            self.storage_status = "corrupt"
            self._persist_enabled = False
            return AlertStateFile()

    def _persist(self) -> None:
        if not self._persist_enabled:
            return
        payload = self._state.model_dump_json(exclude_none=True)
        if self._durable is not None and self._durable[:2] == (self.path, payload):
            # 7J.2: evaluations run several times a minute but rarely change the document. While
            # the file is still the one written here, its identical bytes are already durable.
            try:
                if self._file_identity() == self._durable[2]:
                    return
            except OSError:
                pass
        self._durable = None
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            self.write_failed = False
            directory_fd = os.open(self.path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            self._durable = (self.path, payload, self._file_identity())
        except OSError:
            self.write_failed = True
            logger.exception("RoomOS alert persistence failed")
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _file_identity(self) -> tuple[int, int, int, int]:
        stat = os.stat(self.path)
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)

    def _active(self) -> dict[str, AlertRecord]:
        return {alert.id: alert for alert in self._state.alerts if alert.status == "active"}

    def summary(self) -> AlertSummary:
        active = list(self._active().values())
        highest = self._highest(active)
        status_value: Literal["operational", "attention", "critical", "unknown"]
        if not self._evaluated:
            status_value = "unknown"
        elif highest == "critical":
            status_value = "critical"
        elif highest in {"warning", "info"}:
            status_value = "attention"
        else:
            status_value = "operational"
        return AlertSummary(
            status=status_value,
            active_count=len(active),
            highest_severity=highest,
        )

    @staticmethod
    def _highest(alerts: list[AlertRecord]) -> AlertSeverity | None:
        priorities = {"info": 1, "warning": 2, "critical": 3}
        return max((alert.severity for alert in alerts), key=priorities.get, default=None)

    def list_alerts(self) -> AlertListResponse:
        priorities = {"critical": 0, "warning": 1, "info": 2}
        active = sorted(
            self._active().values(),
            key=lambda item: (priorities[item.severity], item.started_at, item.id),
        )
        return AlertListResponse(
            active=active,
            active_count=len(active),
            highest_severity=self._highest(active),
            storage_status=self.storage_status,
        )

    def list_notifications(
        self,
        *,
        status: Literal["all", "active", "resolved"],
        severity: AlertSeverity | None,
        unread: bool | None,
        source: str | None,
        limit: int,
        offset: int,
    ) -> NotificationCenterResponse:
        records = list(self._state.alerts)
        if status != "all":
            records = [item for item in records if item.status == status]
        if severity is not None:
            records = [item for item in records if item.severity == severity]
        if unread is not None:
            records = [item for item in records if (item.read_at is None) == unread]
        if source is not None:
            records = [item for item in records if item.source == source]
        priorities = {"critical": 0, "warning": 1, "info": 2}
        ordered = sorted(
            records,
            key=lambda item: (
                0 if item.status == "active" else 1,
                priorities[item.severity],
                -item.updated_at.timestamp(),
                item.id,
            ),
        )
        active = [item for item in records if item.status == "active"]
        return NotificationCenterResponse(
            notifications=[item.model_copy(deep=True) for item in ordered[offset : offset + limit]],
            total=len(ordered),
            unread_count=sum(item.read_at is None for item in records),
            active_count=len(active),
            highest_severity=self._highest(active),
            limit=limit,
            offset=offset,
            storage_status=self.storage_status,
        )

    def list_activity(
        self,
        *,
        limit: int,
        offset: int,
        category: Literal["all", "alerts", "scenes"],
    ) -> ActivityListResponse:
        records = self._state.activity
        if category == "alerts":
            records = [item for item in records if item.type.startswith("alert_")]
        elif category == "scenes":
            records = [item for item in records if item.type.startswith("scene_")]
        ordered = sorted(records, key=lambda item: item.occurred_at, reverse=True)
        return ActivityListResponse(
            activity=[ActivityDetail(**item.model_dump()) for item in ordered[offset : offset + limit]],
            total=len(ordered),
            limit=limit,
            offset=offset,
            storage_status=self.storage_status,
        )

    def _append_activity(self, item: ActivityRecord) -> None:
        self._state.activity.append(item)
        self._state.activity = sorted(
            self._state.activity, key=lambda record: record.occurred_at
        )[-self.history_limit :]

    async def _emit(self, event: RoomOSEvent) -> None:
        if self.publish is not None:
            await self.publish(event)

    def _activity(
        self,
        kind: ActivityKind,
        *,
        title: str,
        message: str,
        source: str,
        reference_id: str,
        severity: AlertSeverity | None = None,
        occurred_at: datetime | None = None,
    ) -> ActivityRecord:
        return ActivityRecord(
            id=f"act_{uuid.uuid4().hex}",
            type=kind,
            title=title,
            message=message,
            source=source,
            occurred_at=occurred_at or self.clock(),
            severity=severity,
            reference_id=reference_id,
        )

    async def _activate(self, fingerprint: str, rule: RuleState, now: datetime) -> None:
        existing = next((item for item in self._state.alerts if item.id == fingerprint), None)
        if existing is not None and existing.status == "active":
            if (existing.severity, existing.title, existing.message) != (
                rule.severity,
                rule.title,
                rule.message,
            ):
                escalated = self._escalates(existing.severity, rule.severity)
                existing.severity = rule.severity
                existing.title = rule.title
                existing.message = rule.message
                existing.updated_at = now
                if escalated:
                    await self._notify_lifecycle("escalated", existing)
            return
        if existing is None:
            existing = AlertRecord(
                id=fingerprint,
                type=rule.type,
                severity=rule.severity,
                title=rule.title,
                message=rule.message,
                source=rule.source,
                started_at=now,
                updated_at=now,
                status="active",
                last_observed_at=now,
                last_occurrence_at=now,
            )
            self._state.alerts.append(existing)
        else:
            existing.type = rule.type
            existing.severity = rule.severity
            existing.title = rule.title
            existing.message = rule.message
            existing.source = rule.source
            existing.started_at = now
            existing.updated_at = now
            existing.status = "active"
            existing.occurrence_count = 1
            existing.last_observed_at = now
            existing.last_occurrence_at = now
            existing.read_at = None
            existing.acknowledged_at = None
            existing.acknowledged_by = None
            existing.muted_until = None
        activity = self._activity(
            "alert_started",
            title=rule.title,
            message=rule.message,
            source=rule.source,
            reference_id=fingerprint,
            severity=rule.severity,
            occurred_at=now,
        )
        self._append_activity(activity)
        await self._emit(
            RoomOSEvent(
                type="alert.started",
                device_id=fingerprint,
                data={
                    "alert": existing.model_dump(mode="json"),
                    "activity": activity.model_dump(mode="json"),
                },
            )
        )
        if self._notification_sink is not None:
            # Make the lifecycle timestamp durable before the outbox derives its dedupe key.
            self._persist()
            try:
                await self._notification_sink(existing.model_copy(deep=True))
            except Exception as exc:
                logger.warning(
                    "Notification candidate generation failed error=%s", type(exc).__name__
                )
        await self._notify_lifecycle("started", existing)

    async def _resolve(self, alert: AlertRecord, now: datetime) -> None:
        if alert.status != "active":
            return
        alert.status = "resolved"
        alert.updated_at = now
        activity = self._activity(
            "alert_resolved",
            title=f"{alert.title} resolved",
            message="The canonical RoomOS state has returned to normal.",
            source=alert.source,
            reference_id=alert.id,
            severity=alert.severity,
            occurred_at=now,
        )
        self._append_activity(activity)
        await self._emit(
            RoomOSEvent(
                type="alert.resolved",
                device_id=alert.id,
                data={
                    "alert": alert.model_dump(mode="json"),
                    "activity": activity.model_dump(mode="json"),
                },
            )
        )
        await self._notify_lifecycle("resolved", alert)

    async def _operator_update(self, alert: AlertRecord) -> AlertRecord:
        self._persist()
        await self._emit(
            RoomOSEvent(
                type="alert.updated",
                device_id=alert.id,
                data={"alert": alert.model_dump(mode="json")},
            )
        )
        return alert.model_copy(deep=True)

    async def mark_read(self, alert_id: str) -> AlertRecord | None:
        async with self._lock:
            alert = next((item for item in self._state.alerts if item.id == alert_id), None)
            if alert is None:
                return None
            alert.read_at = self.clock()
            return await self._operator_update(alert)

    async def acknowledge(self, alert_id: str, *, actor: str) -> AlertRecord | None:
        async with self._lock:
            alert = next((item for item in self._state.alerts if item.id == alert_id), None)
            if alert is None:
                return None
            alert.acknowledged_at = self.clock()
            alert.acknowledged_by = actor
            alert.read_at = alert.read_at or alert.acknowledged_at
            return await self._operator_update(alert)

    async def mute(
        self, alert_id: str, *, until: datetime, actor: str
    ) -> AlertRecord | None:
        async with self._lock:
            alert = next((item for item in self._state.alerts if item.id == alert_id), None)
            if alert is None:
                return None
            alert.muted_until = until
            alert.muted_by = actor
            alert.read_at = alert.read_at or self.clock()
            return await self._operator_update(alert)

    @staticmethod
    def _transition_ready(
        pending: PendingAlert,
        *,
        now: datetime,
        required_seconds: float,
    ) -> bool:
        return (
            pending.observations >= 2
            and (now - pending.first_seen).total_seconds() >= required_seconds
        )

    async def _observe_one(self, observation: AlertObservation, now: datetime) -> None:
        rule = observation.rule
        active = self._active().get(rule.key)
        pending = self._state.pending.get(rule.key)

        if observation.state in {"unknown", "hold"}:
            self._state.pending.pop(rule.key, None)
            return

        if observation.state == "failed":
            if active is not None:
                self._state.pending.pop(rule.key, None)
                if (active.severity, active.title, active.message) != (
                    rule.severity, rule.title, rule.message
                ):
                    escalated = self._escalates(active.severity, rule.severity)
                    active.severity = rule.severity
                    active.title = rule.title
                    active.message = rule.message
                    active.updated_at = now
                    if escalated:
                        await self._notify_lifecycle("escalated", active)
                last = active.last_occurrence_at or active.started_at
                if (now - last).total_seconds() >= REPEAT_OBSERVATION_SECONDS:
                    active.occurrence_count = min(1000000, active.occurrence_count + 1)
                    active.last_observed_at = now
                    active.last_occurrence_at = now
                    active.updated_at = now
                return
            if rule.failure_seconds == 0:
                await self._activate(rule.key, rule, now)
                return
            if pending is None or pending.transition != "opening" or now < pending.first_seen:
                self._state.pending[rule.key] = PendingAlert(
                    first_seen=now,
                    observations=1,
                    transition="opening",
                    last_seen=now,
                )
                return
            pending.observations = min(1000, pending.observations + 1)
            pending.last_seen = now
            if self._transition_ready(pending, now=now, required_seconds=rule.failure_seconds):
                await self._activate(rule.key, rule, now)
                self._state.pending.pop(rule.key, None)
            return

        if active is None:
            self._state.pending.pop(rule.key, None)
            return
        if rule.recovery_seconds == 0:
            await self._resolve(active, now)
            self._state.pending.pop(rule.key, None)
            return
        if pending is None or pending.transition != "recovering" or now < pending.first_seen:
            self._state.pending[rule.key] = PendingAlert(
                first_seen=now,
                observations=1,
                transition="recovering",
                last_seen=now,
            )
            return
        pending.observations = min(1000, pending.observations + 1)
        pending.last_seen = now
        if self._transition_ready(pending, now=now, required_seconds=rule.recovery_seconds):
            await self._resolve(active, now)
            self._state.pending.pop(rule.key, None)

    async def evaluate_observations(
        self, observations: list[AlertObservation]
    ) -> AlertSummary:
        """Apply trusted observations to the one persisted alert lifecycle.

        Unknown/hold evidence resets an in-flight transition and never resolves an
        active incident. Callers must supply one observation per managed fingerprint.
        """

        async with self._lock:
            now = self.clock()
            for observation in observations:
                await self._observe_one(observation, now)
            self._evaluated = True
            self._state.alerts = sorted(
                self._state.alerts, key=lambda item: item.updated_at
            )[-200:]
            self._persist()
            return self.summary()

    async def evaluate(self, system: SystemResponse) -> AlertSummary:
        from .smart_alerts import system_observations

        return await self.evaluate_observations(
            system_observations(
                system, node_failure_seconds=self.confirmation_seconds, store=self.policy
            )
        )

    async def evaluate_ha_connection(self, connected: bool) -> AlertSummary:
        async with self._lock:
            now = self.clock()
            fingerprint = "service.home_assistant.offline"
            active = self._active().get(fingerprint)
            if connected:
                if active is not None:
                    await self._resolve(active, now)
            else:
                await self._activate(
                    fingerprint,
                    RuleState(
                        severity="critical",
                        type="service.offline",
                        title="Home Assistant is offline",
                        message="RoomOS cannot reach its trusted Home Assistant connection.",
                        source="home_assistant",
                    ),
                    now,
                )
            self._evaluated = True
            self._persist()
            return self.summary()

    async def record_scene_activation(
        self,
        *,
        scene_id: str,
        scene_name: str,
        status: str,
        occurred_at: datetime,
        source: str,
        execution_id: ExecutionId | None = None,
    ) -> None:
        async with self._lock:
            if execution_id and any(r.execution_id == execution_id for r in self._state.activity):
                return
            activity = self._activity(
                "scene_activated" if status in {"success", "partial"} else "scene_failed",
                title=f"{scene_name} activated" if status in {"success", "partial"} else f"{scene_name} {status}",
                message=(
                    "Scene applied successfully."
                    if status == "success"
                    else "Scene execution completed with partial results." if status == "partial"
                    else "Scene execution did not complete successfully."
                ),
                source="routine" if source == "routine" else "scenes",
                reference_id=f"scene.{scene_id}",
                occurred_at=occurred_at,
            )
            activity.execution_id = execution_id
            self._append_activity(activity)
            self._persist()
            await self._emit(
                RoomOSEvent(
                    type="activity.recorded",
                    device_id=scene_id,
                    data={"activity": activity.model_dump(mode="json")},
                )
            )
