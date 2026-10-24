import asyncio
import json
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .models import ServiceStatus, SystemMetric, SystemNode, SystemResponse
from .notifications import ProductionNotificationIntent

logger = logging.getLogger(__name__)

ObservationState = Literal["healthy", "failed", "hold", "unknown"]
IncidentPhase = Literal["unknown", "healthy", "pending_open", "open", "pending_recovery"]
IntentSink = Callable[[ProductionNotificationIntent], Awaitable[object]]

NODE_IDS = ("primary", "secondary")
SERVICE_ALLOWLIST = ("frigate", "nextcloud", "dns")
TEMPERATURE_HIGH_C = 75.0
TEMPERATURE_RECOVER_C = 70.0
NODE_FAILURE_SECONDS = 60.0
NODE_RECOVERY_SECONDS = 30.0
SERVICE_FAILURE_SECONDS = 60.0
SERVICE_RECOVERY_SECONDS = 30.0
TEMPERATURE_FAILURE_SECONDS = 120.0
TEMPERATURE_RECOVERY_SECONDS = 120.0
MAX_OBSERVATION_GAP_SECONDS = 180.0


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: ObservationState
    value: float | None = None


class IncidentState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase: IncidentPhase = "unknown"
    transition_started_at: datetime | None = None
    last_observed_at: datetime | None = None
    observations: int = Field(default=0, ge=0, le=1000000)
    incident_id: str | None = Field(default=None, pattern=r"^inc_[a-f0-9]{32}$")

    @field_validator("transition_started_at", "last_observed_at")
    @classmethod
    def timestamp_must_be_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("incident timestamps must include a timezone")
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def phase_fields_are_consistent(self):
        pending = self.phase in {"pending_open", "pending_recovery"}
        if pending and (
            self.transition_started_at is None
            or self.last_observed_at is None
            or self.observations < 1
        ):
            raise ValueError("pending incidents require bounded transition metadata")
        if not pending and self.transition_started_at is not None:
            raise ValueError("stable incidents cannot retain a transition timestamp")
        if self.phase in {"open", "pending_recovery"} and self.incident_id is None:
            raise ValueError("open incidents require an incident ID")
        if self.phase in {"unknown", "healthy"} and self.incident_id is not None:
            raise ValueError("inactive incidents cannot retain an incident ID")
        return self


class ProductionIncidentStateFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    rollout_epoch: datetime | None = None
    incidents: dict[str, IncidentState] = Field(default_factory=dict, max_length=20)

    @field_validator("rollout_epoch")
    @classmethod
    def rollout_epoch_must_be_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("rollout epoch must include a timezone")
        return value.astimezone(UTC) if value is not None else None


class PolicySpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    failure_seconds: float
    recovery_seconds: float


def _specs() -> tuple[PolicySpec, ...]:
    return (
        *(
            PolicySpec(
                key=f"node.{node}",
                failure_seconds=NODE_FAILURE_SECONDS,
                recovery_seconds=NODE_RECOVERY_SECONDS,
            )
            for node in NODE_IDS
        ),
        *(
            PolicySpec(
                key=f"service.{service}",
                failure_seconds=SERVICE_FAILURE_SECONDS,
                recovery_seconds=SERVICE_RECOVERY_SECONDS,
            )
            for service in SERVICE_ALLOWLIST
        ),
        *(
            PolicySpec(
                key=f"temperature.{node}",
                failure_seconds=TEMPERATURE_FAILURE_SECONDS,
                recovery_seconds=TEMPERATURE_RECOVERY_SECONDS,
            )
            for node in NODE_IDS
        ),
    )


POLICY_SPECS = _specs()
NODE_LABELS = {"primary": "Primary", "secondary": "Secondary"}
SERVICE_LABELS = {"frigate": "Frigate", "nextcloud": "Nextcloud", "dns": "DNS"}


def _fresh(metric: SystemMetric | None) -> bool:
    return bool(
        metric
        and metric.available
        and not metric.stale
        and metric.freshness == "fresh"
    )


def _node_observation(node: SystemNode) -> Observation:
    online = node.metrics.get(f"{node.id}_online")
    if not _fresh(online) or not isinstance(online.value, bool):
        return Observation(state="unknown")
    return Observation(state="healthy" if online.value else "failed")


def _service_observation(service: ServiceStatus | None) -> Observation:
    if service is None or service.stale or service.freshness != "fresh":
        return Observation(state="unknown")
    if service.status == "offline" and service.health == "offline":
        return Observation(state="failed")
    if service.status == "online" and service.health == "healthy":
        return Observation(state="healthy")
    return Observation(state="hold")


def _temperature_observation(node: SystemNode) -> Observation:
    metric = node.metrics.get(f"{node.id}_temperature")
    if not _fresh(metric) or metric.unit not in {"°C", "C", "°c"}:
        return Observation(state="unknown")
    if isinstance(metric.value, bool) or not isinstance(metric.value, (int, float)):
        return Observation(state="unknown")
    value = float(metric.value)
    if not -20.0 <= value <= 125.0:
        return Observation(state="unknown")
    if value >= TEMPERATURE_HIGH_C:
        return Observation(state="failed", value=value)
    if value <= TEMPERATURE_RECOVER_C:
        return Observation(state="healthy", value=value)
    return Observation(state="hold", value=value)


def observations_from_system(system: SystemResponse) -> dict[str, Observation]:
    services = {service.id: service for service in system.services}
    result: dict[str, Observation] = {}
    for node in (system.primary, system.secondary):
        result[f"node.{node.id}"] = _node_observation(node)
        result[f"temperature.{node.id}"] = _temperature_observation(node)
    for service_id in SERVICE_ALLOWLIST:
        result[f"service.{service_id}"] = _service_observation(services.get(service_id))
    return result


def _format_temperature(value: float | None) -> str:
    if value is None:
        raise ValueError("temperature intent requires a bounded value")
    return f"{value:g}°C"


def render_intent(
    key: str,
    *,
    recovered: bool,
    incident_id: str,
    occurred_at: datetime,
    value: float | None,
) -> ProductionNotificationIntent:
    kind, subject = key.split(".", 1)
    if kind == "node":
        label = NODE_LABELS[subject]
        return ProductionNotificationIntent(
            event_type="node.recovered" if recovered else "node.offline",
            severity="info" if recovered else "critical",
            subject=key,
            incident_id=incident_id,
            title="RoomOS",
            body=f"{label} tekrar çevrimiçi." if recovered else f"{label} çevrimdışı.",
            source=subject,
            occurred_at=occurred_at,
        )
    if kind == "service":
        label = SERVICE_LABELS[subject]
        return ProductionNotificationIntent(
            event_type="service.recovered" if recovered else "service.down",
            severity="info" if recovered else "warning",
            subject=key,
            incident_id=incident_id,
            title="RoomOS",
            body=(
                f"{label} servisi tekrar kullanılabilir."
                if recovered
                else f"{label} servisi kullanılamıyor."
            ),
            source=subject,
            occurred_at=occurred_at,
        )
    label = NODE_LABELS[subject]
    return ProductionNotificationIntent(
        event_type="temperature.recovered" if recovered else "temperature.high",
        severity="info" if recovered else "warning",
        subject=key,
        incident_id=incident_id,
        title="RoomOS",
        body=(
            f"{label} sıcaklığı normale döndü: {_format_temperature(value)}."
            if recovered
            else f"{label} sıcaklığı yüksek: {_format_temperature(value)}."
        ),
        source=subject,
        occurred_at=occurred_at,
    )


class ProductionNotificationPolicy:
    """Persistent, fail-closed incident lifecycle for the fixed production catalog."""

    def __init__(
        self,
        path: str,
        sink: IntentSink,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self.sink = sink
        self.clock = clock or (lambda: datetime.now(UTC))
        self.storage_status: Literal["ok", "corrupt"] = "ok"
        self._persist_enabled = True
        self.write_failed = False
        self._lock = asyncio.Lock()
        self._state = self._load()

    def _load(self) -> ProductionIncidentStateFile:
        try:
            if not self.path.exists():
                return ProductionIncidentStateFile()
            return ProductionIncidentStateFile.model_validate_json(
                self.path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError, json.JSONDecodeError):
            logger.error(
                "Production notification incident persistence is corrupt; policy disabled"
            )
            self.storage_status = "corrupt"
            self._persist_enabled = False
            return ProductionIncidentStateFile()

    def _persist(self) -> None:
        if not self._persist_enabled:
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(self._state.model_dump_json(exclude_none=True))
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
        except OSError:
            self.write_failed = True
            logger.exception("Production notification incident persistence failed")
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _begin(state: IncidentState, phase: IncidentPhase, now: datetime) -> None:
        state.phase = phase
        state.transition_started_at = now
        state.last_observed_at = now
        state.observations = 1

    @staticmethod
    def _stable(state: IncidentState, phase: Literal["healthy", "open"], now: datetime) -> None:
        state.phase = phase
        state.transition_started_at = None
        state.last_observed_at = now
        state.observations = 0
        if phase == "healthy":
            state.incident_id = None

    def _advance_pending(
        self,
        state: IncidentState,
        *,
        now: datetime,
        required_seconds: float,
    ) -> bool:
        started = state.transition_started_at
        last = state.last_observed_at
        if (
            started is None
            or last is None
            or now < started
            or now < last
            or (now - last).total_seconds() > MAX_OBSERVATION_GAP_SECONDS
        ):
            state.transition_started_at = now
            state.last_observed_at = now
            state.observations = 1
            return False
        state.last_observed_at = now
        state.observations += 1
        return state.observations >= 2 and (now - started).total_seconds() >= required_seconds

    async def _emit(
        self,
        key: str,
        state: IncidentState,
        *,
        recovered: bool,
        now: datetime,
        value: float | None,
    ) -> None:
        if state.incident_id is None:
            state.incident_id = f"inc_{uuid.uuid4().hex}"
            self._persist()
        intent = render_intent(
            key,
            recovered=recovered,
            incident_id=state.incident_id,
            occurred_at=now,
            value=value,
        )
        if await self.sink(intent) is None:
            raise RuntimeError("notification outbox rejected production intent")

    async def _evaluate_one(
        self,
        spec: PolicySpec,
        observation: Observation,
        now: datetime,
    ) -> None:
        state = self._state.incidents.setdefault(spec.key, IncidentState())
        if state.phase == "unknown":
            if observation.state == "healthy":
                self._stable(state, "healthy", now)
            elif observation.state == "failed":
                self._begin(state, "pending_open", now)
            return
        if state.phase == "healthy":
            state.last_observed_at = now
            if observation.state == "failed":
                self._begin(state, "pending_open", now)
            return
        if state.phase == "pending_open":
            if observation.state == "unknown":
                state.phase = "unknown"
                state.transition_started_at = None
                state.last_observed_at = now
                state.observations = 0
                state.incident_id = None
                return
            if observation.state != "failed":
                self._stable(state, "healthy", now)
                return
            if self._advance_pending(state, now=now, required_seconds=spec.failure_seconds):
                await self._emit(
                    spec.key,
                    state,
                    recovered=False,
                    now=now,
                    value=observation.value,
                )
                self._stable(state, "open", now)
            return
        if state.phase == "open":
            state.last_observed_at = now
            if observation.state == "healthy":
                self._begin(state, "pending_recovery", now)
            return
        if observation.state != "healthy":
            self._stable(state, "open", now)
            return
        if self._advance_pending(state, now=now, required_seconds=spec.recovery_seconds):
            await self._emit(
                spec.key,
                state,
                recovered=True,
                now=now,
                value=observation.value,
            )
            self._stable(state, "healthy", now)

    async def evaluate(self, system: SystemResponse) -> None:
        if self.storage_status == "corrupt":
            return
        observations = observations_from_system(system)
        async with self._lock:
            now = self.clock().astimezone(UTC)
            if self._state.rollout_epoch is not None and now <= self._state.rollout_epoch:
                return
            for spec in POLICY_SPECS:
                try:
                    await self._evaluate_one(spec, observations[spec.key], now)
                except Exception as exc:
                    logger.warning(
                        "Production notification intent rejected subject=%s error=%s",
                        spec.key,
                        type(exc).__name__,
                    )
            self._persist()

    async def initialize_rollout(
        self, system: SystemResponse, *, epoch: datetime
    ) -> datetime:
        """Create a notification-free healthy baseline at an explicit cutoff."""

        if self.storage_status == "corrupt":
            raise RuntimeError("production incident persistence is unavailable")
        if epoch.tzinfo is None:
            raise ValueError("rollout epoch must include a timezone")
        cutoff = epoch.astimezone(UTC)
        observations = observations_from_system(system)
        unsafe = sorted(
            key for key, observation in observations.items() if observation.state != "healthy"
        )
        if unsafe:
            raise ValueError(
                "rollout baseline is not fully healthy: " + ",".join(unsafe)
            )
        async with self._lock:
            self._state.rollout_epoch = cutoff
            self._state.incidents = {
                spec.key: IncidentState(
                    phase="healthy",
                    last_observed_at=cutoff,
                )
                for spec in POLICY_SPECS
            }
            self._persist()
        return cutoff
