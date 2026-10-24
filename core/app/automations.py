from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Awaitable, Callable, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .catalog import LIGHT_BY_ID, MAIN_LIGHT_IDS
from .config import Settings
from .devices import CATALOG as DEVICE_CATALOG, DEVICE_SCOPES
from .ha_client import HomeAssistantUnavailable
from .models import RoomOSEvent
from .service import list_lights

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
LightTarget = Literal["room_main", "room_2", "room_3", "room_4", "desk_ambient", "main_room_lights"]
TriState = Literal["true", "false", "unknown"]
MAX_CONDITION_DEPTH = 3
MAX_CONDITIONS = 20
MAX_ACTIONS = 8
MAX_EXECUTION_SECONDS = 15
MAX_STARTS_PER_MINUTE = 20
MAX_CONCURRENT_EXECUTIONS = 4
TOKEN_PATTERN = re.compile(r"(?i)(bearer\s+\S+|token[=: ]+\S+|csrf[=: ]+\S+|cookie[=: ]+\S+)")


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
    target_id: LightTarget
    from_state: Literal["on", "off"] | None = None
    to_state: Literal["on", "off"] | None = None
    attribute: Literal["brightness", "temperature"] | None = None
    operator: Literal["above", "below"] | None = None
    threshold: float | None = Field(default=None, ge=0, le=100000)
    for_seconds: int = Field(default=0, ge=0, le=86400)

    @model_validator(mode="after")
    def require_transition_or_threshold(self):
        transition = self.from_state is not None or self.to_state is not None
        threshold = self.operator is not None or self.threshold is not None
        if transition == threshold or transition and self.to_state is None:
            raise ValueError("Choose one complete transition or threshold")
        if transition and self.attribute is not None:
            raise ValueError("State transitions do not accept an attribute")
        if threshold and (self.attribute is None or self.operator is None or self.threshold is None):
            raise ValueError("Numeric trigger requires attribute, operator, and threshold")
        return self


class DeviceHealthTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["device_health"] = "device_health"
    device_id: str = Field(min_length=1, max_length=60, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    metric: Literal["online", "cpu", "ram", "disk", "temperature"]
    operator: Literal["equals", "above", "below"]
    value: bool | float
    for_seconds: int = Field(default=0, ge=0, le=86400)

    @field_validator("device_id")
    @classmethod
    def known_device(cls, value: str) -> str:
        if value not in {device.id for device in DEVICE_CATALOG}:
            raise ValueError("Unknown logical device ID")
        return value

    @model_validator(mode="after")
    def compatible_operator(self):
        if self.metric == "online" and (self.operator != "equals" or not isinstance(self.value, bool)):
            raise ValueError("Online uses boolean equals")
        if self.metric != "online" and (self.operator == "equals" or isinstance(self.value, bool)):
            raise ValueError("Metrics use numeric above/below")
        return self


AutomationTrigger = Annotated[TimeTrigger | HAStateTrigger | DeviceHealthTrigger, Field(discriminator="type")]


class PredicateCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["predicate"] = "predicate"
    source: Literal["light", "device", "time"]
    target_id: str | None = Field(default=None, max_length=60, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    field: Literal["state", "available", "online", "cpu", "ram", "disk", "temperature", "local_time"]
    operator: Literal["equals", "not_equals", "greater_than", "less_than", "between"]
    value: str | float | bool | None = None
    upper: float | None = None

    @model_validator(mode="after")
    def validate_shape(self):
        if self.source in {"light", "device"} and not self.target_id:
            raise ValueError("Target is required")
        if self.source == "light" and self.target_id not in {*LIGHT_BY_ID, "main_room_lights"}:
            raise ValueError("Unknown logical light ID")
        if self.source == "time" and (self.field != "local_time" or self.target_id is not None):
            raise ValueError("Time condition shape is invalid")
        allowed_fields = {
            "light": {"state", "available"},
            "device": {"online", "cpu", "ram", "disk", "temperature"},
            "time": {"local_time"},
        }
        if self.field not in allowed_fields[self.source]:
            raise ValueError("Condition field is invalid for its source")
        if self.operator == "between":
            if not isinstance(self.value, (int, float)) or self.upper is None or float(self.value) > self.upper:
                raise ValueError("Between requires ordered numeric bounds")
        elif self.upper is not None:
            raise ValueError("Upper is only valid for between")
        return self


class ConditionGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["group"] = "group"
    operator: Literal["and", "or", "not"]
    children: list["Condition"] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def not_is_unary(self):
        if self.operator == "not" and len(self.children) != 1:
            raise ValueError("NOT accepts one child")
        return self


Condition = Annotated[PredicateCondition | ConditionGroup, Field(discriminator="kind")]


class SceneAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["scene"] = "scene"
    scene_id: Literal["normal", "work_1", "work_2", "gaming_1", "gaming_2", "movie", "night", "all_off"]


class RoutineAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["routine"] = "routine"
    routine_id: str = Field(pattern=r"^rt_[a-f0-9]{32}$")


class LightAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["light"] = "light"
    target_id: LightTarget
    operation: Literal["power", "brightness", "color", "temperature"]
    on: bool | None = None
    brightness: int | None = Field(default=None, ge=1, le=255)
    rgb: tuple[int, int, int] | None = None
    kelvin: int | None = Field(default=None, ge=2500, le=9000)

    @field_validator("rgb")
    @classmethod
    def valid_rgb(cls, value):
        if value is not None and any(channel < 0 or channel > 255 for channel in value):
            raise ValueError("RGB channels must be 0..255")
        return value

    @model_validator(mode="after")
    def exactly_one_payload(self):
        expected = {"power": "on", "brightness": "brightness", "color": "rgb", "temperature": "kelvin"}[self.operation]
        values = {"on": self.on, "brightness": self.brightness, "rgb": self.rgb, "kelvin": self.kelvin}
        if values[expected] is None or any(v is not None for key, v in values.items() if key != expected):
            raise ValueError("Action payload does not match operation")
        if self.target_id == "main_room_lights" and self.operation == "color":
            raise ValueError("Main group has no color action")
        if self.target_id == "main_room_lights" and self.operation == "temperature" and not 2500 <= self.kelvin <= 6500:  # type: ignore[operator]
            raise ValueError("Group Kelvin is outside shared capability")
        return self


AutomationAction = Annotated[SceneAction | RoutineAction | LightAction, Field(discriminator="type")]


def _condition_size(value: Condition | None, depth: int = 1) -> tuple[int, int]:
    if value is None:
        return 0, 0
    if isinstance(value, PredicateCondition):
        return depth, 1
    sizes = [_condition_size(child, depth + 1) for child in value.children]
    return max(item[0] for item in sizes), sum(item[1] for item in sizes)


class AutomationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=240)
    enabled: bool = False
    trigger: AutomationTrigger
    conditions: Condition | None = None
    actions: list[AutomationAction] = Field(min_length=1, max_length=MAX_ACTIONS)
    cooldown_seconds: int = Field(default=0, ge=0, le=604800)
    failure_policy: Literal["stop_on_failure", "continue"] = "stop_on_failure"

    @field_validator("name", "description")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = " ".join(value.strip().split())
        if not normalized.isprintable():
            raise ValueError("Text is invalid")
        return normalized

    @model_validator(mode="after")
    def safe_defaults_and_complexity(self):
        self.enabled = False
        depth, leaves = _condition_size(self.conditions)
        if depth > MAX_CONDITION_DEPTH or leaves > MAX_CONDITIONS:
            raise ValueError("Condition tree exceeds safety limits")
        return self


class AutomationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=60)
    description: str | None = Field(default=None, max_length=240)
    trigger: AutomationTrigger | None = None
    conditions: Condition | None = None
    actions: list[AutomationAction] | None = Field(default=None, min_length=1, max_length=MAX_ACTIONS)
    cooldown_seconds: int | None = Field(default=None, ge=0, le=604800)
    failure_policy: Literal["stop_on_failure", "continue"] | None = None

    @model_validator(mode="after")
    def validate_update(self):
        if not self.model_fields_set:
            raise ValueError("At least one field must change")
        depth, leaves = _condition_size(self.conditions)
        if depth > MAX_CONDITION_DEPTH or leaves > MAX_CONDITIONS:
            raise ValueError("Condition tree exceeds safety limits")
        return self


class Automation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    id: str = Field(pattern=r"^au_[a-f0-9]{32}$")
    name: str
    description: str = ""
    enabled: bool = False
    trigger: AutomationTrigger
    conditions: Condition | None = None
    actions: list[AutomationAction]
    cooldown_seconds: int = 0
    failure_policy: Literal["stop_on_failure", "continue"] = "stop_on_failure"
    created_at: datetime
    updated_at: datetime
    last_triggered: datetime | None = None
    last_outcome: Literal["success", "partial", "failed", "skipped", "blocked"] | None = None


class AutomationDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    automations: list[Automation] = Field(default_factory=list, max_length=100)
    claimed_slots: dict[str, str] = Field(default_factory=dict)
    cooldown_until: dict[str, datetime] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [item.id for item in self.automations]
        if len(ids) != len(set(ids)) or not set(self.claimed_slots).issubset(ids) or not set(self.cooldown_until).issubset(ids):
            raise ValueError("Automation runtime state is inconsistent")
        return self


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


class AutomationStore:
    def __init__(self, path: str | Path, *, document: AutomationDocument | None = None):
        self.path = Path(path)
        self.persist_enabled = document is None
        self.corrupt = False
        self.write_failed = False
        if document is not None:
            self.document = document
        elif not self.path.exists():
            self.document = AutomationDocument()
        else:
            try:
                self.document = AutomationDocument.model_validate_json(self.path.read_bytes())
            except (OSError, ValidationError, ValueError):
                self.document, self.corrupt = AutomationDocument(), True

    def _require(self):
        if self.corrupt:
            raise HTTPException(status_code=503, detail="Automation storage requires operator repair")

    def persist(self):
        self._require()
        if self.persist_enabled:
            try:
                _atomic_write(self.path, json.dumps(self.document.model_dump(mode="json"), separators=(",", ":")).encode())
            except OSError:
                self.write_failed = True
                raise
            self.write_failed = False

    def find(self, automation_id: str) -> Automation:
        item = next((item for item in self.document.automations if item.id == automation_id), None)
        if item is None:
            raise HTTPException(status_code=404, detail="Unknown automation ID")
        return item

    def create(self, body: AutomationCreate, *, now: datetime) -> Automation:
        self._require()
        item = Automation(id=f"au_{uuid.uuid4().hex}", created_at=now, updated_at=now, **body.model_dump())
        self.document.automations.append(item)
        self.persist()
        return item

    def update(self, automation_id: str, body: AutomationUpdate, *, now: datetime) -> Automation:
        self._require()
        current = self.find(automation_id)
        candidate = Automation.model_validate(current.model_copy(update={**body.model_dump(exclude_unset=True), "updated_at": now}))
        depth, leaves = _condition_size(candidate.conditions)
        if depth > MAX_CONDITION_DEPTH or leaves > MAX_CONDITIONS:
            raise HTTPException(status_code=422, detail="Condition tree exceeds safety limits")
        self.document.automations[self.document.automations.index(current)] = candidate
        self.persist()
        return candidate

    def set_enabled(self, automation_id: str, enabled: bool, *, now: datetime) -> Automation:
        current = self.find(automation_id)
        updated = current.model_copy(update={"enabled": enabled, "updated_at": now})
        self.document.automations[self.document.automations.index(current)] = updated
        self.persist()
        return updated

    def delete(self, automation_id: str) -> None:
        self._require()
        current = self.find(automation_id)
        self.document.automations.remove(current)
        self.document.claimed_slots.pop(automation_id, None)
        self.document.cooldown_until.pop(automation_id, None)
        self.persist()


class ConditionContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lights: dict[str, dict[str, Any]] = Field(default_factory=dict)
    devices: dict[str, dict[str, Any]] = Field(default_factory=dict)
    now: datetime | None = None


def device_trigger_snapshot(detail: dict[str, Any]) -> dict[str, Any]:
    fresh = detail.get("telemetry_freshness") == "fresh"
    values: dict[str, Any] = {}
    for sensor in detail.get("sensors") or []:
        usable = sensor.get("availability") == "available" and sensor.get("freshness") == "fresh"
        values[sensor.get("id", "")] = sensor.get("value") if usable else None
    temperature = values.get("cpu_temperature")
    if not isinstance(temperature, (int, float)) or isinstance(temperature, bool) or temperature <= 0:
        temperature = values.get("gpu_temperature")
    if not isinstance(temperature, (int, float)) or isinstance(temperature, bool) or temperature <= 0:
        temperature = None
    return {
        "id": detail.get("id"),
        "fresh": fresh,
        "online": detail.get("connection") == "online" if detail.get("connection") in {"online", "offline"} else None,
        "cpu": values.get("cpu_usage"),
        "ram": values.get("ram_usage"),
        "disk": values.get("disk_usage"),
        "temperature": temperature,
    }


async def build_condition_context(
    client: Any,
    devices: Any,
    timezone: ZoneInfo,
    *,
    now: datetime | None = None,
) -> ConditionContext:
    try:
        lights = await list_lights(client)
    except HomeAssistantUnavailable:
        lights = []
    light_values = {
        light.id: {
            "state": "on" if light.on is True else "off" if light.on is False else None,
            "available": light.available,
            "fresh": light.available,
            "brightness": light.brightness,
            "temperature": light.color_temperature_kelvin,
        }
        for light in lights
    }
    device_values = {
        detail.id: device_trigger_snapshot(detail.model_dump(mode="json"))
        for detail in devices.list(DEVICE_SCOPES).devices
    }
    instant = now or datetime.now(UTC)
    return ConditionContext(
        lights=light_values,
        devices=device_values,
        now=instant.astimezone(timezone),
    )


class ConditionTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result: TriState
    summary: str
    children: list["ConditionTrace"] = Field(default_factory=list)


def _compare(actual: Any, operator: str, value: Any, upper: Any) -> TriState:
    if actual is None:
        return "unknown"
    try:
        if operator == "equals": return "true" if actual == value else "false"
        if operator == "not_equals": return "true" if actual != value else "false"
        if operator == "greater_than": return "true" if float(actual) > float(value) else "false"
        if operator == "less_than": return "true" if float(actual) < float(value) else "false"
        if operator == "between": return "true" if float(value) <= float(actual) <= float(upper) else "false"
    except (TypeError, ValueError):
        return "unknown"
    return "unknown"


def _ha_trigger_matches(trigger: HAStateTrigger, state: dict[str, Any]) -> bool:
    if state.get("fresh") is not True:
        return False
    if trigger.operator is None:
        return trigger.to_state == state.get("state") and (
            trigger.from_state is None or trigger.from_state == state.get("old_state")
        )
    actual = state.get(trigger.attribute or "")
    try:
        numeric = float(actual)
    except (TypeError, ValueError):
        return False
    return numeric > trigger.threshold if trigger.operator == "above" else numeric < trigger.threshold


def _device_trigger_matches(trigger: DeviceHealthTrigger, state: dict[str, Any]) -> bool:
    actual = state.get(trigger.metric)
    fresh = state.get("fresh") is True
    if trigger.metric == "temperature" and (
        actual is None
        or isinstance(actual, bool)
        or isinstance(actual, (int, float)) and actual <= 0
    ):
        fresh = False
    if not fresh:
        return False
    if trigger.operator == "equals":
        return actual == trigger.value
    try:
        numeric = float(actual)
        threshold = float(trigger.value)
    except (TypeError, ValueError):
        return False
    return numeric > threshold if trigger.operator == "above" else numeric < threshold


def evaluate_condition(condition: Condition, context: ConditionContext) -> ConditionTrace:
    if isinstance(condition, PredicateCondition):
        source = (context.lights if condition.source == "light" else context.devices).get(condition.target_id or "") if condition.source != "time" else {"fresh": context.now is not None, "local_time": context.now.strftime("%H:%M") if context.now else None}
        if not source or source.get("fresh") is not True:
            result: TriState = "unknown"
        else:
            actual = source.get(condition.field)
            result = "unknown" if condition.field == "temperature" and (actual is None or float(actual) <= 0) else _compare(actual, condition.operator, condition.value, condition.upper)
        return ConditionTrace(result=result, summary=f"{condition.source}.{condition.field} {condition.operator}")
    children = [evaluate_condition(child, context) for child in condition.children]
    values = [child.result for child in children]
    if condition.operator == "not": result = "unknown" if values[0] == "unknown" else "false" if values[0] == "true" else "true"
    elif condition.operator == "and": result = "false" if "false" in values else "unknown" if "unknown" in values else "true"
    else: result = "true" if "true" in values else "unknown" if "unknown" in values else "false"
    return ConditionTrace(result=result, summary=condition.operator.upper(), children=children)


class ActionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int
    status: Literal["success", "failed", "skipped"]
    detail: str | None = Field(default=None, max_length=120)


class ExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["LIVE", "SIMULATION"] = "LIVE"
    correlation_id: str = Field(pattern=r"^ac_[a-f0-9]{32}$")
    automation_id: str
    outcome: Literal["success", "partial", "failed", "skipped", "blocked"]
    reason: str | None = None
    condition: ConditionTrace | None = None
    actions: list[ActionResult] = Field(default_factory=list)
    duration_ms: int = 0


class HistoryRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")
    timestamp: datetime
    automation_id: str
    automation_name: str = ""
    outcome: Literal["success", "partial", "failed", "skipped", "blocked"]
    trigger_source: str = "unknown"
    trigger_reason: str = ""
    reason: str | None = None
    correlation_id: str
    duration_ms: int = Field(ge=0)
    error: str | None = None


class HistoryPage(BaseModel):
    items: list[HistoryRecord]
    next_cursor: str | None = None


class AutomationHistory:
    def __init__(self, path: str | Path, *, limit: int = 500, retention_days: int = 30, clock: Callable[[], datetime] | None = None):
        self.path, self.limit, self.retention_days = Path(path), limit, retention_days
        self.clock = clock or (lambda: datetime.now(UTC))

    def _records(self) -> list[HistoryRecord]:
        if not self.path.exists(): return []
        records = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try: records.append(HistoryRecord.model_validate_json(line))
            except (ValidationError, ValueError): continue
        cutoff = self.clock() - timedelta(days=self.retention_days)
        return [record for record in records if record.timestamp >= cutoff][-self.limit:]

    def append(self, value: dict[str, Any]) -> None:
        safe = {key: TOKEN_PATTERN.sub("[redacted]", item) if isinstance(item, str) else item for key, item in value.items()}
        records = [*self._records(), HistoryRecord(timestamp=self.clock(), **safe)][-self.limit:]
        payload = b"".join(json.dumps(item.model_dump(mode="json"), separators=(",", ":")).encode() + b"\n" for item in records)
        _atomic_write(self.path, payload)

    def list(self, *, limit: int = 50, cursor: str | None = None) -> HistoryPage:
        records, start = list(reversed(self._records())), int(cursor or 0)
        page = records[start:start + min(max(limit, 1), 100)]
        return HistoryPage(items=page, next_cursor=str(start + len(page)) if start + len(page) < len(records) else None)

    @staticmethod
    def simulation(value: dict[str, Any]) -> dict[str, Any]:
        return {"mode": "SIMULATION", **value}


ActionExecutor = Callable[[AutomationAction, ConditionContext], Awaitable[dict[str, Any]]]
ActionEffects = Callable[[AutomationAction], dict[str, Literal["on", "off"]]]
ContextProvider = Callable[[], Awaitable[ConditionContext]]


def _default_action_effects(action: AutomationAction) -> dict[str, Literal["on", "off"]]:
    if not isinstance(action, LightAction):
        return {}
    targets = MAIN_LIGHT_IDS if action.target_id == "main_room_lights" else (action.target_id,)
    expected: Literal["on", "off"] = "off" if action.operation == "power" and action.on is False else "on"
    return {target_id: expected for target_id in targets}


class AutomationEngine:
    def __init__(
        self,
        settings: Settings,
        *,
        store: AutomationStore | None = None,
        action_executor: ActionExecutor,
        clock: Callable[[], datetime] | None = None,
        publish: Callable[[RoomOSEvent], Awaitable[None]] | None = None,
        action_effects: ActionEffects | None = None,
        context_provider: ContextProvider | None = None,
    ):
        self.settings, self.clock = settings, clock or (lambda: datetime.now(UTC))
        try: self.timezone = ZoneInfo(settings.routines_timezone)
        except ZoneInfoNotFoundError as exc: raise RuntimeError("RoomOS automation timezone is invalid") from exc
        self.store = store or AutomationStore(settings.automations_path)
        self.history = AutomationHistory(settings.automation_history_path, limit=settings.automation_history_limit, retention_days=settings.automation_history_retention_days, clock=self.clock)
        self.action_executor = action_executor
        self.action_effects = action_effects or _default_action_effects
        self.context_provider = context_provider
        self.publish = publish
        self._locks = {item.id: asyncio.Lock() for item in self.store.document.automations}
        self._seen_events: deque[str] = deque(maxlen=512)
        self._starts: deque[float] = deque()
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_EXECUTIONS)
        self._pending_since: dict[str, datetime] = {}
        self._device_values: dict[tuple[str, str], Any] = {}
        self._origin_suppressions: dict[str, tuple[str, float]] = {}
        self.running, self.last_tick, self.failures, self.loop_preventions, self.last_successful_execution = False, None, 0, 0, None
        self._stop = asyncio.Event()

    async def _condition_context(self, provided: ConditionContext | None) -> ConditionContext:
        if provided is not None:
            return provided
        if self.context_provider is not None:
            try:
                return await self.context_provider()
            except Exception:
                pass
        return ConditionContext(now=self.clock().astimezone(self.timezone))

    def _arm_action_effects(self, action: AutomationAction) -> None:
        expires = time.monotonic() + 10
        for target_id, expected in self.action_effects(action).items():
            self._origin_suppressions[target_id] = (expected, expires)

    def _record(self, item: Automation, result: ExecutionResult, source: str, reason: str):
        self.history.append({"automation_id": item.id, "automation_name": item.name, "outcome": result.outcome, "trigger_source": source, "trigger_reason": reason, "reason": result.reason, "correlation_id": result.correlation_id, "duration_ms": result.duration_ms, "error": next((a.detail for a in result.actions if a.status == "failed"), None)})

    async def _emit(self, event_type: Literal["automation.changed", "automation.deleted", "automation.executed"], item: Automation | None = None, *, automation_id: str | None = None, result: ExecutionResult | None = None) -> None:
        if self.publish is None: return
        data: dict[str, Any] = {"automation_id": automation_id or (item.id if item else None)}
        if item is not None: data["automation"] = item.model_dump(mode="json")
        if result is not None: data["result"] = result.model_dump(mode="json")
        await self.publish(RoomOSEvent(type=event_type, data=data))

    async def _blocked(self, item: Automation, reason: str, source: str) -> ExecutionResult:
        self.loop_preventions += int(reason in {"reentrant", "automation_origin_blocked", "rate_limited", "duplicate_event"})
        result = ExecutionResult(correlation_id=f"ac_{uuid.uuid4().hex}", automation_id=item.id, outcome="blocked", reason=reason)
        self._record(item, result, source, reason)
        await self._emit("automation.executed", item, result=result)
        return result

    async def simulate(self, automation_id: str, *, context: ConditionContext | None = None) -> ExecutionResult:
        item = self.store.find(automation_id)
        resolved = await self._condition_context(context) if item.conditions is not None else context or ConditionContext(now=self.clock().astimezone(self.timezone))
        trace = evaluate_condition(item.conditions, resolved) if item.conditions is not None else None
        allowed = trace is None or trace.result == "true"
        return ExecutionResult(mode="SIMULATION", correlation_id=f"ac_{uuid.uuid4().hex}", automation_id=item.id, outcome="success" if allowed else "skipped", reason=None if allowed else "condition_unknown" if trace and trace.result == "unknown" else "condition_false", condition=trace, actions=[ActionResult(index=i, status="skipped", detail="would_run") for i, _ in enumerate(item.actions)] if allowed else [])

    async def _execute(self, item: Automation, context: ConditionContext, source: str, reason: str) -> ExecutionResult:
        lock = self._locks.setdefault(item.id, asyncio.Lock())
        if lock.locked(): return await self._blocked(item, "reentrant", source)
        now_mono = time.monotonic()
        while self._starts and self._starts[0] <= now_mono - 60: self._starts.popleft()
        if len(self._starts) >= MAX_STARTS_PER_MINUTE: return await self._blocked(item, "rate_limited", source)
        cooldown = self.store.document.cooldown_until.get(item.id)
        if cooldown is not None and self.clock() < cooldown:
            result = ExecutionResult(correlation_id=f"ac_{uuid.uuid4().hex}", automation_id=item.id, outcome="skipped", reason="cooldown")
            self._record(item, result, source, reason); return result
        trace = evaluate_condition(item.conditions, context) if item.conditions is not None else None
        if trace is not None and trace.result != "true":
            result = ExecutionResult(correlation_id=f"ac_{uuid.uuid4().hex}", automation_id=item.id, outcome="skipped", reason="condition_unknown" if trace.result == "unknown" else "condition_false", condition=trace)
            self._record(item, result, source, reason); return result
        started, outcomes = time.monotonic(), []
        self._starts.append(now_mono)
        async with lock:
            current_index = 0
            try:
                async with asyncio.timeout(MAX_EXECUTION_SECONDS):
                    async with self._semaphore:
                        for index, action in enumerate(item.actions):
                            current_index = index
                            try:
                                self._arm_action_effects(action)
                                await self.action_executor(action, context)
                            except Exception as exc:
                                self.failures += 1
                                outcomes.append(ActionResult(index=index, status="failed", detail=type(exc).__name__))
                                if item.failure_policy == "stop_on_failure":
                                    outcomes.extend(ActionResult(index=i, status="skipped", detail="stopped_after_failure") for i in range(index + 1, len(item.actions)))
                                    break
                            else:
                                outcomes.append(ActionResult(index=index, status="success"))
            except TimeoutError:
                self.failures += 1
                if len(outcomes) <= current_index:
                    outcomes.append(ActionResult(index=current_index, status="failed", detail="TimeoutError"))
                outcomes.extend(
                    ActionResult(index=i, status="skipped", detail="execution_deadline")
                    for i in range(current_index + 1, len(item.actions))
                )
        succeeded, failed = sum(x.status == "success" for x in outcomes), sum(x.status == "failed" for x in outcomes)
        outcome: Literal["success", "partial", "failed"] = "success" if not failed else "partial" if succeeded else "failed"
        result = ExecutionResult(correlation_id=f"ac_{uuid.uuid4().hex}", automation_id=item.id, outcome=outcome, condition=trace, actions=outcomes, duration_ms=max(0, round((time.monotonic() - started) * 1000)))
        now, current = self.clock(), self.store.find(item.id)
        self.store.document.automations[self.store.document.automations.index(current)] = current.model_copy(update={"last_triggered": now, "last_outcome": outcome})
        if item.cooldown_seconds: self.store.document.cooldown_until[item.id] = now + timedelta(seconds=item.cooldown_seconds)
        self.store.persist()
        if outcome == "success": self.last_successful_execution = now
        self._record(item, result, source, reason)
        await self._emit("automation.executed", self.store.find(item.id), result=result)
        return result

    async def run_manual(self, automation_id: str, *, context: ConditionContext | None = None) -> ExecutionResult:
        item = self.store.find(automation_id)
        return await self._execute(
            item,
            await self._condition_context(context) if item.conditions is not None else context or ConditionContext(now=self.clock().astimezone(self.timezone)),
            "manual",
            "manual_run",
        )

    async def tick(self, now: datetime | None = None, *, context: ConditionContext | None = None) -> list[str]:
        instant = (now or self.clock()).astimezone(UTC); self.last_tick = instant
        held: list[str] = []
        held_items = [
            item
            for item in self.store.document.automations
            if item.enabled
            and isinstance(item.trigger, (HAStateTrigger, DeviceHealthTrigger))
            and item.trigger.for_seconds
            and item.id in self._pending_since
            and (instant - self._pending_since[item.id]).total_seconds() >= item.trigger.for_seconds
        ]
        if held_items:
            held_context = await self._condition_context(context)
            for item in held_items:
                self._pending_since.pop(item.id, None)
                if isinstance(item.trigger, HAStateTrigger):
                    state = held_context.lights.get(item.trigger.target_id, {})
                    matches = _ha_trigger_matches(item.trigger, state)
                    source, reason = "ha_state", "state_held"
                else:
                    state = held_context.devices.get(item.trigger.device_id, {})
                    matches = _device_trigger_matches(item.trigger, state)
                    source = "device_health"
                    reason = f"{item.trigger.metric}_{item.trigger.operator}_held"
                if matches:
                    result = await self._execute(
                        item,
                        held_context,
                        source,
                        reason,
                    )
                    if result.outcome not in {"blocked", "skipped"}:
                        held.append(item.id)
        local, slot = instant.astimezone(self.timezone), instant.strftime("%Y-%m-%dT%H:%MZ")
        due = [item for item in self.store.document.automations if item.enabled and isinstance(item.trigger, TimeTrigger) and item.trigger.time == local.strftime("%H:%M") and WEEKDAYS[local.weekday()] in item.trigger.days and self.store.document.claimed_slots.get(item.id) != slot]
        resolved_context = (
            await self._condition_context(context)
            if any(item.conditions is not None for item in due)
            else context or ConditionContext(now=local)
        ) if due else None
        for item in due:
            self.store.document.claimed_slots[item.id] = slot; self.store.persist()
            await self._execute(item, resolved_context or ConditionContext(now=local), "schedule", f"scheduled {item.trigger.time}")
        return [*held, *(item.id for item in due)]

    async def handle_ha_event(self, event: dict[str, Any], *, origin: Literal["external", "automation"] = "external", context: ConditionContext | None = None) -> list[str]:
        candidates = [item for item in self.store.document.automations if item.enabled and isinstance(item.trigger, HAStateTrigger) and item.trigger.target_id == event.get("target_id")]
        fingerprint = str(event.get("event_id") or "")
        if fingerprint and fingerprint in self._seen_events:
            for item in candidates: await self._blocked(item, "duplicate_event", "ha_state")
            return []
        if fingerprint: self._seen_events.append(fingerprint)
        suppression = self._origin_suppressions.get(str(event.get("target_id")))
        if suppression is not None:
            expected, expires = suppression
            if time.monotonic() <= expires and event.get("new_state") == expected:
                origin = "automation"
                self._origin_suppressions.pop(str(event.get("target_id")), None)
            elif time.monotonic() > expires:
                self._origin_suppressions.pop(str(event.get("target_id")), None)
        if origin == "automation":
            for item in candidates: await self._blocked(item, "automation_origin_blocked", "ha_state")
            return []
        resolved_context = (
            await self._condition_context(context)
            if any(item.conditions is not None for item in candidates)
            else context or ConditionContext(now=self.clock().astimezone(self.timezone))
        )
        resolved_context.lights[str(event.get("target_id"))] = {
            "state": event.get("new_state"),
            "fresh": event.get("fresh") is True,
        }
        fired = []
        for item in candidates:
            trigger, matches = item.trigger, False
            attributes = event.get("attributes") or {}
            event_state = {
                "state": event.get("new_state"),
                "old_state": event.get("old_state"),
                "brightness": attributes.get("brightness"),
                "temperature": attributes.get("color_temp_kelvin"),
                "fresh": event.get("fresh") is True,
            }
            matches = _ha_trigger_matches(trigger, event_state)
            if not matches:
                self._pending_since.pop(item.id, None)
                continue
            if trigger.for_seconds:
                since = self._pending_since.setdefault(item.id, self.clock())
                if (self.clock() - since).total_seconds() < trigger.for_seconds:
                    continue
                self._pending_since.pop(item.id, None)
            if matches:
                result = await self._execute(item, resolved_context, "ha_state", "state_transition")
                if result.outcome not in {"blocked", "skipped"}: fired.append(item.id)
        return fired

    async def handle_device_event(self, detail: dict[str, Any], *, context: ConditionContext | None = None) -> list[str]:
        device_id = detail.get("id")
        candidates = [item for item in self.store.document.automations if item.enabled and isinstance(item.trigger, DeviceHealthTrigger) and item.trigger.device_id == device_id]
        previous_values = dict(self._device_values)
        fired = []
        for item in candidates:
            trigger = item.trigger
            key = (trigger.device_id, trigger.metric)
            # Every rule evaluates the same observation against the same pre-event value.
            # Updating one rule must not make later rules see this event as already handled.
            previous = previous_values.get(key)
            actual = detail.get(trigger.metric)
            fresh = detail.get("fresh") is True
            if trigger.metric == "temperature" and (actual is None or isinstance(actual, (int, float)) and actual <= 0):
                fresh = False
            matches = _device_trigger_matches(trigger, {**detail, "fresh": fresh})
            self._device_values[key] = actual if fresh else None
            if not matches:
                self._pending_since.pop(item.id, None)
                continue
            if trigger.metric == "online" and previous is None:
                continue
            previous_matched = previous == trigger.value if trigger.operator == "equals" else (
                isinstance(previous, (int, float)) and (previous > trigger.value if trigger.operator == "above" else previous < trigger.value)
            )
            if trigger.for_seconds:
                since = self._pending_since.setdefault(item.id, self.clock())
                if (self.clock() - since).total_seconds() < trigger.for_seconds: continue
                self._pending_since.pop(item.id, None)
            elif previous_matched:
                continue
            event_context = (
                await self._condition_context(context)
                if item.conditions is not None
                else context or ConditionContext(now=self.clock().astimezone(self.timezone))
            )
            event_context.devices[str(device_id)] = {**detail, "fresh": fresh}
            result = await self._execute(item, event_context, "device_health", f"{trigger.metric}_{trigger.operator}")
            if result.outcome not in {"blocked", "skipped"}: fired.append(item.id)
        return fired

    async def scheduler_loop(self) -> None:
        self.running = True
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.settings.routines_scheduler_interval_seconds)
                except TimeoutError:
                    try:
                        await self.tick()
                    except Exception:
                        self.failures += 1
        finally:
            self.running = False

    def stop(self) -> None:
        self._stop.set()
