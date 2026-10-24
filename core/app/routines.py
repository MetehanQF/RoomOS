from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .routine_evaluations import EvaluationStore, Origin
from .presence import PresenceObserver
from .executions import ExecutionId
from .config import Settings
from .ha_client import HomeAssistantClient, HomeAssistantUnavailable
from .models import EventType, RoomOSEvent
from .scenes import SCENE_NAMES, SceneActivationResult, SceneEngine

logger = logging.getLogger("roomos.routines")

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SceneId = Literal[
    "normal", "work_1", "work_2", "gaming_1", "gaming_2", "movie", "night", "all_off"
]
Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
HA_TRIGGER_REGISTRY: dict[str, str] = {}
MANAGED_EVENING_ID = "rt_6b200000000000000000000000000001"
MANAGED_ALL_OFF_ID = "rt_6b200000000000000000000000000002"
PresenceState = Literal["home", "away", "unknown"]
SolarState = Literal["above_horizon", "below_horizon", "unknown"]


class ManualTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["manual"] = "manual"


class TimeTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["time"] = "time"
    time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    days: list[Weekday] = Field(min_length=1, max_length=7)

    @field_validator("days")
    @classmethod
    def unique_days(cls, value: list[Weekday]) -> list[Weekday]:
        if len(value) != len(set(value)):
            raise ValueError("Weekdays must be unique")
        return sorted(value, key=WEEKDAYS.index)


class HAStateTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["ha_state"] = "ha_state"
    trigger_id: str = Field(min_length=1, max_length=60, pattern=r"^[a-z][a-z0-9_]*$")
    state: str = Field(min_length=1, max_length=60)


class EveningHomeTrigger(BaseModel):
    """Fixed server-owned presence/sunset trigger with no client payload."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["evening_home"] = "evening_home"


RoutineTrigger = Annotated[
    ManualTrigger | TimeTrigger | HAStateTrigger | EveningHomeTrigger,
    Field(discriminator="type"),
]


class ActivateSceneAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["activate_scene"] = "activate_scene"
    scene_id: SceneId


class RoutineResult(BaseModel):
    execution_id: ExecutionId | None = None
    model_config = ConfigDict(extra="forbid")
    status: Literal["success", "partial", "failed"]
    scene_status: Literal["success", "partial", "failed"] | None = None
    source: Literal["manual", "schedule", "ha_state"]
    detail: str | None = Field(default=None, max_length=120)


class Routine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^rt_[a-f0-9]{32}$")
    name: str = Field(min_length=1, max_length=60)
    enabled: bool = False
    managed: bool = False
    trigger: RoutineTrigger
    action: ActivateSceneAction
    created_at: datetime
    updated_at: datetime
    last_run: datetime | None = None
    last_result: RoutineResult | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = " ".join(value.strip().split())
        if not normalized or not normalized.isprintable():
            raise ValueError("Routine name is invalid")
        return normalized

    @model_validator(mode="after")
    def managed_trigger_is_consistent(self):
        if isinstance(self.trigger, EveningHomeTrigger) and not self.managed:
            raise ValueError("evening-home routines must be server-managed")
        if self.managed:
            if self.id == MANAGED_EVENING_ID:
                if (
                    self.name != "Evening Home Lighting"
                    or not isinstance(self.trigger, EveningHomeTrigger)
                    or self.action.scene_id != "normal"
                ):
                    raise ValueError("managed evening routine contract is invalid")
            elif self.id == MANAGED_ALL_OFF_ID:
                if (
                    self.name != "02:00 All Off"
                    or not isinstance(self.trigger, TimeTrigger)
                    or self.trigger.time != "02:00"
                    or self.trigger.days != list(WEEKDAYS)
                    or self.action.scene_id != "all_off"
                ):
                    raise ValueError("managed all-off routine contract is invalid")
            else:
                raise ValueError("unknown server-managed routine")
        return self


class RoutineCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=60)
    enabled: bool = False
    trigger: RoutineTrigger
    action: ActivateSceneAction

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return Routine.normalize_name(value)


class RoutineUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=60)
    enabled: bool | None = None
    trigger: RoutineTrigger | None = None
    action: ActivateSceneAction | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        return None if value is None else Routine.normalize_name(value)

    @model_validator(mode="after")
    def require_change(self):
        if not self.model_fields_set:
            raise ValueError("At least one field must be updated")
        return self


class RoutineDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    routines: list[Routine] = Field(default_factory=list, max_length=100)
    claimed_slots: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_ids(self):
        ids = [routine.id for routine in self.routines]
        if len(ids) != len(set(ids)):
            raise ValueError("Routine IDs must be unique")
        if not set(self.claimed_slots).issubset(ids):
            raise ValueError("Claimed slots must reference known routines")
        return self


class RoutineListResponse(BaseModel):
    diagnostics: dict[str, Any] | None = None
    routines: list[Routine]
    timezone: str
    storage_status: Literal["ok", "corrupt"]
    can_write: bool
    can_run: bool
    ha_trigger_ids: list[str] = Field(default_factory=list)
    scenes: list[dict[str, str]]


class RoutineRunResponse(BaseModel):
    routine: Routine
    scene: SceneActivationResult


PublishRoutineEvent = Callable[[RoomOSEvent], Awaitable[None]]


def _validate_trigger(trigger: RoutineTrigger, *, managed: bool = False) -> None:
    if isinstance(trigger, EveningHomeTrigger) and not managed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Server-managed trigger type",
        )
    if isinstance(trigger, HAStateTrigger) and trigger.trigger_id not in HA_TRIGGER_REGISTRY:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Unknown logical trigger ID",
        )


def _atomic_write(path: Path, document: RoutineDocument) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    payload = json.dumps(document.model_dump(mode="json"), separators=(",", ":"))
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def load_routine_document(path: str) -> tuple[RoutineDocument, bool]:
    source = Path(path)
    if not source.exists():
        return RoutineDocument(), False
    try:
        document = RoutineDocument.model_validate_json(source.read_text(encoding="utf-8"))
        for routine in document.routines:
            _validate_trigger(routine.trigger, managed=routine.managed)
        return document, False
    except (OSError, ValidationError, ValueError, HTTPException):
        logger.error("Routine persistence is invalid; scheduler is fail-closed")
        return RoutineDocument(), True


def _aware_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def normalize_presence(state: dict[str, Any] | None) -> PresenceState:
    if not state:
        return "unknown"
    raw = str(state.get("state", "")).strip().lower()
    if raw in {"", "unknown", "unavailable", "none"}:
        return "unknown"
    return "home" if raw == "home" else "away"


def normalize_solar(
    state: dict[str, Any] | None,
) -> tuple[SolarState, datetime | None, datetime | None]:
    if not state:
        return "unknown", None, None
    raw = str(state.get("state", "")).strip().lower()
    attributes = state.get("attributes")
    if not isinstance(attributes, dict):
        return "unknown", None, None
    next_setting = _aware_timestamp(attributes.get("next_setting"))
    next_rising = _aware_timestamp(attributes.get("next_rising"))
    if raw not in {"above_horizon", "below_horizon"} or None in {
        next_setting,
        next_rising,
    }:
        return "unknown", None, None
    return raw, next_setting, next_rising  # type: ignore[return-value]


def evening_window_open(
    solar: SolarState,
    next_rising: datetime | None,
    now: datetime,
    timezone: ZoneInfo,
) -> bool:
    """Return true only from a trusted sunset until the following local 02:00."""

    if solar != "below_horizon" or next_rising is None or now.tzinfo is None:
        return False
    local = now.astimezone(timezone)
    if local.hour < 2:
        return True
    return next_rising.astimezone(timezone).date() > local.date()


class RoutineEngine:
    def __init__(
        self,
        settings: Settings,
        scenes: SceneEngine,
        ha: HomeAssistantClient,
        publish: PublishRoutineEvent | None = None,
        *,
        document: RoutineDocument | None = None,
        clock: Callable[[], datetime] | None = None,
        evaluations: EvaluationStore | None = None,
    ) -> None:
        self.settings = settings
        self.evaluations = evaluations or EvaluationStore(None, clock=clock)
        self.presence_observer = PresenceObserver(settings.presence_reporting_threshold_seconds, clock=clock)
        self._diagnostic_signature = None
        self.scenes = scenes
        self.ha = ha
        self.publish = publish
        self.clock = clock or (lambda: datetime.now(UTC))
        try:
            self.timezone = ZoneInfo(settings.routines_timezone)
        except ZoneInfoNotFoundError as exc:
            raise RuntimeError("RoomOS routines timezone is invalid") from exc
        if document is None:
            self.document, self.corrupt = load_routine_document(settings.routines_path)
            self.persist_enabled = True
        else:
            self.document, self.corrupt, self.persist_enabled = document, False, False
        self._locks = {routine.id: asyncio.Lock() for routine in self.document.routines}
        self._semaphore = asyncio.Semaphore(settings.routines_max_concurrency)
        self._environment_lock = asyncio.Lock()
        self._environment_initialized = False
        self._presence: PresenceState = "unknown"
        self._presence_observed_at: datetime | None = None
        self._solar: SolarState = "unknown"
        self._solar_next_setting: datetime | None = None
        self._solar_next_rising: datetime | None = None
        self._solar_observed_at: datetime | None = None
        self._stop = asyncio.Event()
        self.write_failed = False
        self.scheduler_running = False

    def _require_storage(self) -> None:
        if self.corrupt:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Routine storage requires operator repair",
            )

    def _persist(self) -> None:
        self._require_storage()
        if self.persist_enabled:
            try:
                _atomic_write(Path(self.settings.routines_path), self.document)
            except OSError:
                self.write_failed = True
                raise
            self.write_failed = False

    def _find(self, routine_id: str) -> Routine:
        routine = next((item for item in self.document.routines if item.id == routine_id), None)
        if routine is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown routine ID")
        return routine

    def list(self, *, can_write: bool, can_run: bool) -> RoutineListResponse:
        return RoutineListResponse(
            diagnostics=self.diagnostics(),
            routines=self.document.routines,
            timezone=self.settings.routines_timezone,
            storage_status="corrupt" if self.corrupt else "ok",
            can_write=can_write,
            can_run=can_run,
            ha_trigger_ids=sorted(HA_TRIGGER_REGISTRY),
            scenes=[{"id": scene_id, "name": name} for scene_id, name in SCENE_NAMES.items()],
        )

    def diagnostics(self) -> dict[str, Any]:
        return {
            "presence": self.presence_observer.snapshot().model_dump(mode="json"),
            "storage_status": self.evaluations.storage_status,
            "retention_days": 30,
            "record_limit": 250,
            "evaluations": [r.model_dump(mode="json") for r in self.evaluations.records()],
        }

    async def publish_diagnostics(self, *, force: bool = False) -> None:
        try:
            data = self.diagnostics()
            signature = json.dumps(data, sort_keys=True)
            if self.publish is not None and (force or signature != self._diagnostic_signature):
                await self.publish(RoomOSEvent(type="routine.diagnostics", data={"diagnostics": data}))
            self._diagnostic_signature = signature
        except Exception:
            logger.warning("Routine diagnostics unavailable; automation unaffected")

    async def diagnostics_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(30)
            await self.publish_diagnostics()

    async def _begin_evaluation(self, routine_id: str, origin: Origin) -> str | None:
        reporting = self.presence_observer.snapshot().reporting if origin in {"sunset", "arrival"} else None
        return await self.evaluations.begin(routine_id, origin, presence_reporting=reporting)

    async def _skip_evaluation(self, routine: Routine, origin: Origin, reason: str) -> None:
        eid = await self._begin_evaluation(routine.id, origin)
        await self.evaluations.finish(eid, outcome="skipped", reason_code=reason)
        await self.publish_diagnostics()

    async def _emit(self, event_type: EventType, routine: Routine | None = None, routine_id: str | None = None) -> None:
        if self.publish is None:
            return
        data: dict[str, object] = {"routine_id": routine_id or (routine.id if routine else None)}
        if routine is not None:
            data["routine"] = routine.model_dump(mode="json")
        await self.publish(RoomOSEvent(type=event_type, data=data))

    async def create(self, body: RoutineCreate) -> Routine:
        self._require_storage()
        _validate_trigger(body.trigger)
        now = self.clock().astimezone(UTC)
        routine = Routine(
            id=f"rt_{uuid.uuid4().hex}",
            created_at=now,
            updated_at=now,
            **body.model_dump(),
        )
        self.document.routines.append(routine)
        self._locks[routine.id] = asyncio.Lock()
        self._persist()
        await self._emit("routine.changed", routine)
        return routine

    async def update(self, routine_id: str, body: RoutineUpdate) -> Routine:
        self._require_storage()
        current = self._find(routine_id)
        if current.managed:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Server-managed routine cannot be modified",
            )
        changes = body.model_dump(exclude_unset=True)
        if body.trigger is not None:
            _validate_trigger(body.trigger)
        updated = current.model_copy(update={**changes, "updated_at": self.clock().astimezone(UTC)})
        updated = Routine.model_validate(updated)
        index = self.document.routines.index(current)
        self.document.routines[index] = updated
        self._persist()
        await self._emit("routine.changed", updated)
        return updated

    async def delete(self, routine_id: str) -> None:
        self._require_storage()
        routine = self._find(routine_id)
        if routine.managed:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Server-managed routine cannot be deleted",
            )
        self.document.routines.remove(routine)
        self.document.claimed_slots.pop(routine_id, None)
        self._locks.pop(routine_id, None)
        self._persist()
        await self._emit("routine.deleted", routine_id=routine_id)

    async def _record_result(
        self,
        routine: Routine,
        *,
        source: Literal["manual", "schedule", "ha_state"],
        scene: SceneActivationResult | None,
        detail: str | None = None,
        execution_id: str | None = None,
    ) -> Routine:
        status_value: Literal["success", "partial", "failed"] = (
            scene.status if scene is not None else "failed"
        )
        result = RoutineResult(
            execution_id=scene.execution_id if scene is not None else execution_id,
            status=status_value,
            scene_status=scene.status if scene is not None else None,
            source=source,
            detail=detail,
        )
        updated = routine.model_copy(
            update={
                "last_run": self.clock().astimezone(UTC),
                "last_result": result,
                "updated_at": self.clock().astimezone(UTC),
            }
        )
        index = self.document.routines.index(routine)
        self.document.routines[index] = updated
        self._persist()
        await self._emit("routine.run_completed", updated)
        return updated

    async def run(
        self, routine_id: str, *, source: Literal["manual", "schedule", "ha_state"],
        trigger_origin: Origin | None = None,
    ) -> RoutineRunResponse:
        origin = trigger_origin or (source if source in {"manual", "schedule"} else None)
        # Unknown IDs and unusable routine storage are request errors, not evaluations of a routine.
        known = not self.corrupt and any(item.id == routine_id for item in self.document.routines)
        eid = await self._begin_evaluation(routine_id, origin) if origin and known else None
        try:
            response = await self._run(routine_id, source=source)
        except Exception as exc:
            reason = "ha_unavailable" if isinstance(exc, HomeAssistantUnavailable) else (
                "control_disabled" if isinstance(exc, HTTPException) and exc.status_code == 403 and not self.settings.control_enabled else
                "execution_rejected" if isinstance(exc, HTTPException) and exc.status_code in {403, 409, 429} else
                "execution_failed")
            await self.evaluations.finish(eid, outcome="failed", reason_code=reason,
                                          execution_id=getattr(exc, "execution_id", None))
            await self.publish_diagnostics()
            raise
        await self.evaluations.finish(eid,
            outcome="failed" if response.scene.status == "failed" else "executed",
            reason_code="execution_failed" if response.scene.status == "failed" else None,
            execution_id=response.scene.execution_id, execution_result=response.scene.status)
        await self.publish_diagnostics()
        return response

    async def _run(
        self,
        routine_id: str,
        *,
        source: Literal["manual", "schedule", "ha_state"],
    ) -> RoutineRunResponse:
        self._require_storage()
        routine = self._find(routine_id)
        if routine.managed and source == "manual":
            execution_id = await self.scenes.record_rejection(routine.action.scene_id, reason="policy_rejected",
                routine_id=routine.id, routine_name=routine.name, trigger=source)
            exc = HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Server-managed routine cannot be run manually")
            exc.execution_id = execution_id
            raise exc
        if not self.settings.control_enabled:
            execution_id = await self.scenes.record_rejection(routine.action.scene_id, reason="control_disabled",
                routine_id=routine.id, routine_name=routine.name, trigger=source)
            await self._record_result(routine, source=source, scene=None, detail="control_disabled", execution_id=execution_id)
            exc = HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="RoomOS control is disabled")
            exc.execution_id = execution_id
            raise exc
        lock = self._locks[routine.id]
        if lock.locked():
            execution_id = await self.scenes.record_rejection(routine.action.scene_id, reason="duplicate",
                routine_id=routine.id, routine_name=routine.name, trigger=source)
            exc = HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Routine is already running")
            exc.execution_id = execution_id
            raise exc
        async with lock, self._semaphore:
            try:
                scene = await self.scenes.activate(
                    routine.action.scene_id,
                    self.ha,
                    source="routine",
                    routine_id=routine.id,
                    routine_name=routine.name,
                    trigger=source,
                )
            except (HomeAssistantUnavailable, HTTPException) as exc:
                detail = "scene_activation_failed"
                await self._record_result(routine, source=source, scene=None, detail=detail, execution_id=getattr(exc, "execution_id", None))
                raise exc
            updated = await self._record_result(routine, source=source, scene=scene)
            return RoutineRunResponse(routine=updated, scene=scene)

    def _schedule_matches(self, routine: Routine, local: datetime) -> bool:
        trigger = routine.trigger
        return bool(
            routine.enabled
            and isinstance(trigger, TimeTrigger)
            and trigger.time == local.strftime("%H:%M")
            and WEEKDAYS[local.weekday()] in trigger.days
        )

    async def tick(self, now: datetime) -> list[str]:
        self._require_storage()
        local = now.astimezone(self.timezone)
        slot = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ")
        due = [routine for routine in tuple(self.document.routines) if self._schedule_matches(routine, local)]
        claimed: list[Routine] = []
        for routine in due:
            if self.document.claimed_slots.get(routine.id) == slot:
                continue
            self.document.claimed_slots[routine.id] = slot
            claimed.append(routine)
        if claimed:
            self._persist()

        async def isolated(routine: Routine) -> None:
            try:
                await self.run(routine.id, source="schedule")
            except Exception as exc:
                logger.warning("Routine schedule execution failed routine_id=%s error=%s", routine.id, type(exc).__name__)

        await asyncio.gather(*(isolated(routine) for routine in claimed))
        return [routine.id for routine in claimed]

    async def handle_ha_state(self, trigger_id: str, state_value: str) -> list[str]:
        if trigger_id not in HA_TRIGGER_REGISTRY:
            return []
        due = [
            routine
            for routine in tuple(self.document.routines)
            if routine.enabled
            and isinstance(routine.trigger, HAStateTrigger)
            and routine.trigger.trigger_id == trigger_id
            and routine.trigger.state == state_value
        ]
        for routine in due:
            try:
                await self.run(routine.id, source="ha_state")
            except Exception as exc:
                logger.warning("Routine HA trigger failed routine_id=%s error=%s", routine.id, type(exc).__name__)
        return [routine.id for routine in due]

    def bootstrap_environment(
        self,
        presence_state: dict[str, Any] | None,
        solar_state: dict[str, Any] | None,
    ) -> None:
        """Set a reconnect/startup baseline without producing an automation event."""

        self.presence_observer.bootstrap(presence_state)
        self._presence = normalize_presence(presence_state)
        self._presence_observed_at = _aware_timestamp(
            (presence_state or {}).get("last_updated")
        )
        (
            self._solar,
            self._solar_next_setting,
            self._solar_next_rising,
        ) = normalize_solar(solar_state)
        self._solar_observed_at = _aware_timestamp((solar_state or {}).get("last_updated"))
        self._environment_initialized = True

    def environment_decision(self, *, now: datetime | None = None) -> dict[str, object]:
        """Secret-free deterministic decision surface used by operator dry-runs."""

        observed_at = now or self.clock()
        return {
            "presence": self._presence,
            "solar": self._solar,
            "evening_window_open": evening_window_open(
                self._solar,
                self._solar_next_rising,
                observed_at,
                self.timezone,
            ),
            "initialized": self._environment_initialized,
        }

    async def _run_evening_home(self, origin: Origin, skip_reason: str | None = None) -> list[str]:
        due = [
            routine
            for routine in tuple(self.document.routines)
            if routine.managed
            and isinstance(routine.trigger, EveningHomeTrigger)
        ]

        async def isolated(routine: Routine) -> None:
            try:
                if not routine.enabled or skip_reason:
                    await self._skip_evaluation(routine, origin, "routine_disabled" if not routine.enabled else skip_reason)
                else:
                    await self.run(routine.id, source="ha_state", trigger_origin=origin)
            except Exception as exc:
                logger.warning(
                    "Managed evening routine failed routine_id=%s error=%s",
                    routine.id,
                    type(exc).__name__,
                )

        await asyncio.gather(*(isolated(routine) for routine in due))
        return [routine.id for routine in due if routine.enabled and not skip_reason]

    async def handle_environment_event(
        self,
        entity_id: str,
        state: dict[str, Any] | None,
        *,
        now: datetime | None = None,
    ) -> list[str]:
        """Consume only fixed internal presence/solar mappings and verified transitions."""

        from .catalog import PRESENCE_ENTITY_ID, SOLAR_ENTITY_ID

        observed_at = _aware_timestamp((state or {}).get("last_updated"))
        current_time = now or self.clock()
        should_run = False
        origin: Origin | None = None
        async with self._environment_lock:
            if not self._environment_initialized or observed_at is None:
                return []
            if entity_id == PRESENCE_ENTITY_ID:
                if self._presence_observed_at is not None and observed_at <= self._presence_observed_at:
                    return []
                self.presence_observer.person(state)
                previous = self._presence
                current = normalize_presence(state)
                if previous == "away" and current == "home":
                    origin = "arrival"
                self._presence = current
                self._presence_observed_at = observed_at
                should_run = bool(
                    previous == "away"
                    and current == "home"
                    and evening_window_open(
                        self._solar,
                        self._solar_next_rising,
                        current_time,
                        self.timezone,
                    )
                )
            elif entity_id == SOLAR_ENTITY_ID:
                if self._solar_observed_at is not None and observed_at <= self._solar_observed_at:
                    return []
                previous = self._solar
                current, next_setting, next_rising = normalize_solar(state)
                if previous == "above_horizon" and current == "below_horizon":
                    origin = "sunset"
                self._solar = current
                self._solar_next_setting = next_setting
                self._solar_next_rising = next_rising
                self._solar_observed_at = observed_at
                should_run = bool(
                    previous == "above_horizon"
                    and current == "below_horizon"
                    and self._presence == "home"
                    and evening_window_open(
                        current,
                        next_rising,
                        current_time,
                        self.timezone,
                    )
                )
            else:
                return []
            reason = None
            if origin and not should_run:
                if origin == "sunset" and self._presence != "home":
                    reason = "presence_away" if self._presence == "away" else "presence_unknown"
                elif self._solar == "unknown" or self._solar_next_rising is None:
                    reason = "solar_unknown"
                else:
                    reason = "outside_window"
        return await self._run_evening_home(origin, reason) if origin else []

    async def install_production_automations(self) -> list[Routine]:
        """Install the exact approved managed catalog without executing it."""

        self._require_storage()
        if any(not routine.managed for routine in self.document.routines):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Unmanaged routines must be reviewed before production install",
            )
        if not {"normal", "all_off"}.issubset(self.scenes.configured):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Required production scenes are not configured",
            )
        now = self.clock().astimezone(UTC)
        existing = {routine.id: routine for routine in self.document.routines}
        routines = [
            Routine(
                id=MANAGED_EVENING_ID,
                name="Evening Home Lighting",
                enabled=True,
                managed=True,
                trigger=EveningHomeTrigger(),
                action=ActivateSceneAction(scene_id="normal"),
                created_at=existing.get(MANAGED_EVENING_ID, None).created_at
                if MANAGED_EVENING_ID in existing
                else now,
                updated_at=now,
            ),
            Routine(
                id=MANAGED_ALL_OFF_ID,
                name="02:00 All Off",
                enabled=True,
                managed=True,
                trigger=TimeTrigger(time="02:00", days=list(WEEKDAYS)),
                action=ActivateSceneAction(scene_id="all_off"),
                created_at=existing.get(MANAGED_ALL_OFF_ID, None).created_at
                if MANAGED_ALL_OFF_ID in existing
                else now,
                updated_at=now,
            ),
        ]
        self.document.routines = routines
        self.document.claimed_slots = {
            routine_id: slot
            for routine_id, slot in self.document.claimed_slots.items()
            if routine_id in {MANAGED_EVENING_ID, MANAGED_ALL_OFF_ID}
        }
        self._locks = {routine.id: asyncio.Lock() for routine in routines}
        self._persist()
        for routine in routines:
            await self._emit("routine.changed", routine)
        return routines

    async def scheduler_loop(self) -> None:
        initial_minute = self.clock().astimezone(UTC).replace(second=0, microsecond=0)
        last_seen = initial_minute
        self.scheduler_running = True
        try:
            while not self._stop.is_set():
                await asyncio.sleep(self.settings.routines_scheduler_interval_seconds)
                current = self.clock().astimezone(UTC).replace(second=0, microsecond=0)
                if current == last_seen:
                    continue
                last_seen = current
                try:
                    await self.tick(current)
                except Exception as exc:
                    logger.error("Routine scheduler tick failed error=%s", type(exc).__name__)
        except asyncio.CancelledError:
            raise
        finally:
            self.scheduler_running = False

    def stop(self) -> None:
        self._stop.set()
