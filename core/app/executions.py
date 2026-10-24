"""Bounded diagnostics only: never a control, retry or notification dependency."""
import asyncio
import logging
import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from .catalog import LIGHT_BY_ID

logger = logging.getLogger(__name__)
ExecutionId = Annotated[str, Field(pattern=r'^ex_[a-f0-9]{32}$')]
SceneId = Literal['normal', 'work_1', 'work_2', 'gaming_1', 'gaming_2', 'movie', 'night', 'all_off']
Result = Literal['running', 'success', 'partial', 'failed', 'rejected', 'interrupted']
Reason = Literal['policy_rejected', 'invalid_action', 'not_configured', 'duplicate', 'control_disabled', 'internal_error', 'interrupted']
TargetReason = Literal['unavailable', 'ha_unavailable', 'capability_rejected', 'power_on_not_confirmed', 'ha_error']
MAX_DURATION_MS = 604800000  # Saturating diagnostic duration, not a control timeout.


class TargetExecution(BaseModel):
    model_config = ConfigDict(extra='forbid')
    device_id: str
    actions: list[Literal['power', 'brightness', 'temperature', 'color', 'effect']] = Field(max_length=5)
    result: Literal['applied', 'skipped', 'failed']
    reason: TargetReason | None = None
    stage: Literal['read', 'wake', 'apply']
    duration_ms: int = Field(ge=0, le=MAX_DURATION_MS)

    @model_validator(mode='after')
    def valid_target(self):
        if self.device_id not in LIGHT_BY_ID or len(set(self.actions)) != len(self.actions):
            raise ValueError('Invalid logical target')
        if (self.result == 'applied') != (self.reason is None):
            raise ValueError('Invalid target outcome')
        return self


class Execution(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: ExecutionId
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    duration_ms: int | None = Field(default=None, ge=0, le=MAX_DURATION_MS)
    source: Literal['manual', 'routine']
    trigger: Literal['manual', 'schedule', 'ha_state']
    routine_id: Annotated[str, Field(pattern=r'^rt_[a-f0-9]{32}$')] | None = None
    routine_name: str | None = Field(default=None, max_length=60, pattern=r'^[\w ()—:-]{1,60}$')
    scene_id: SceneId
    scene_name: str = Field(min_length=1, max_length=40)
    result: Result = 'running'
    reason: Reason | None = None
    targets: list[TargetExecution] = Field(default_factory=list, max_length=5)

    @model_validator(mode='after')
    def consistent(self):
        # Lazy import avoids the engine/store dependency cycle.
        from .scenes import SCENE_NAMES
        if self.scene_name != SCENE_NAMES[self.scene_id]:
            raise ValueError('Invalid scene name')
        if len({t.device_id for t in self.targets}) != len(self.targets):
            raise ValueError('Duplicate targets')
        if self.source == 'manual' and (self.routine_id or self.routine_name or self.trigger != 'manual'):
            raise ValueError('Invalid manual correlation')
        if self.source == 'routine' and (not self.routine_id or not self.routine_name):
            raise ValueError('Missing routine correlation')
        if self.result == 'running':
            if self.finished_at is not None or self.duration_ms is not None or self.reason is not None:
                raise ValueError('Invalid running outcome')
        elif self.finished_at is None or self.duration_ms is None or self.finished_at < self.started_at:
            raise ValueError('Invalid completion time')
        if self.result == 'interrupted' and self.reason != 'interrupted':
            raise ValueError('Invalid interruption reason')
        return self


class ExecutionDocument(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: Literal[1]
    executions: list[Execution] = Field(max_length=250)

    @model_validator(mode='after')
    def unique_ids(self):
        if len({r.id for r in self.executions}) != len(self.executions):
            raise ValueError('Duplicate execution IDs')
        return self


def duration_ms(seconds: float) -> int:
    return min(MAX_DURATION_MS, max(0, int(seconds * 1000)))


class ExecutionStore:
    def __init__(self, path: str | None, *, clock=None, monotonic=None):
        self.path = Path(path) if path else None
        self.clock = clock or (lambda: datetime.now(UTC))
        self.monotonic = monotonic or time.monotonic
        self.process_started_at = self.clock()
        self.storage_status = 'ok'
        self.document = ExecutionDocument(version=1, executions=[])
        self._lock = asyncio.Lock()
        self._started: dict[str, float] = {}
        self.recovered: list[Execution] = []
        if self.path:
            try:
                if not self.path.exists():
                    return
                if self.path.stat().st_size > 2_000_000:
                    raise ValueError('Oversized execution store')
                self.document = ExecutionDocument.model_validate_json(self.path.read_bytes())
            except (OSError, ValueError):
                self.storage_status = 'corrupt'
                logger.warning('Execution storage invalid; source preserved')
        # Only records loaded before serving requests can be interrupted. Even a
        # future timestamp (clock rollback) must not masquerade as an active run.
        for record in self.document.executions:
            if record.result == 'running':
                finished = max(self.process_started_at, record.started_at)
                record.result = 'interrupted'
                record.reason = 'interrupted'
                record.finished_at = finished
                record.duration_ms = duration_ms((finished - record.started_at).total_seconds())
                self.recovered.append(record.model_copy(deep=True))

    async def initialize(self):
        async with self._lock:
            self._persist()

    def get(self, execution_id: str | None) -> Execution | None:
        record = next((r for r in self.document.executions if r.id == execution_id), None)
        return record.model_copy(deep=True) if record else None

    def _persist(self):
        if self.path is None or self.storage_status == 'corrupt':
            return
        temporary = self.path.with_name(f'.{self.path.name}.{uuid.uuid4().hex}.tmp')
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as handle:
                handle.write(self.document.model_dump_json())
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            self.storage_status = 'ok'
        except OSError:
            self.storage_status = 'write_failed'
            logger.warning('Execution storage write failed; control continues')
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    async def begin(self, **context) -> Execution:
        record = Execution(id=f'ex_{uuid.uuid4().hex}', started_at=self.clock(), **context)
        async with self._lock:
            self._started[record.id] = self.monotonic()
            self.document.executions.append(record)
            self.document.executions = self.document.executions[-250:]
            retained = {r.id for r in self.document.executions}
            self._started = {k: v for k, v in self._started.items() if k in retained}
            self._persist()
        return record.model_copy(deep=True)

    async def finish(self, record: Execution, *, result: Result, reason: Reason | None = None, targets=()) -> Execution:
        async with self._lock:
            started = self._started.pop(record.id, self.monotonic())
            completed = Execution.model_validate({**record.model_dump(), 'result': result, 'reason': reason,
                'targets': list(targets), 'finished_at': max(record.started_at, self.clock()),
                'duration_ms': duration_ms(self.monotonic() - started)})
            for i, current in enumerate(self.document.executions):
                if current.id == record.id:
                    self.document.executions[i] = completed
                    break
            self._persist()
            return completed.model_copy(deep=True)
