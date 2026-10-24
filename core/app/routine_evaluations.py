"""Private bounded decision evidence; never a control/retry dependency."""
import asyncio
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from .executions import ExecutionId

logger = logging.getLogger(__name__)
Origin = Literal['sunset', 'arrival', 'schedule', 'manual']
Reason = Literal['presence_away', 'presence_unknown', 'solar_unknown', 'outside_window',
                 'routine_disabled', 'control_disabled', 'execution_rejected', 'ha_unavailable', 'execution_failed']
MAX_RECORDS = 250
MAX_BYTES = 512 * 1024
RETENTION = timedelta(days=30)


class Evaluation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    evaluation_id: str = Field(pattern=r'^ev_[a-f0-9]{32}$')
    routine_id: str = Field(pattern=r'^rt_[a-f0-9]{32}$')
    evaluated_at: AwareDatetime
    trigger_origin: Origin | None = None
    completion_state: Literal['pending', 'complete', 'incomplete'] = 'pending'
    outcome: Literal['executed', 'skipped', 'failed'] | None = None
    reason_code: Reason | None = None
    execution_id: ExecutionId | None = None
    execution_result: Literal['success', 'partial', 'failed', 'rejected', 'interrupted'] | None = None
    # Tracker reporting recency when a presence-driven candidate was evaluated; never GPS age.
    presence_reporting: Literal['fresh', 'stale', 'unknown'] | None = None

    @model_validator(mode='after')
    def consistent(self):
        if (self.completion_state == 'complete') != (self.outcome is not None):
            raise ValueError('Completion requires an observed outcome')
        if self.outcome in {'skipped', 'failed'} and self.reason_code is None:
            raise ValueError('A non-executed decision requires a reason')
        if self.outcome == 'executed' and (self.reason_code is not None or self.execution_result not in {'success', 'partial'}):
            raise ValueError('Invalid executed result')
        if self.outcome is None and (self.reason_code or self.execution_result):
            raise ValueError('Incomplete observation has no invented result')
        return self


class EvaluationDocument(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: Literal[1] = 1
    evaluations: list[Evaluation] = Field(default_factory=list, max_length=MAX_RECORDS)

    @model_validator(mode='after')
    def unique(self):
        if len({r.evaluation_id for r in self.evaluations}) != len(self.evaluations):
            raise ValueError('Duplicate evaluation')
        return self


class EvaluationStore:
    def __init__(self, path: str | None, *, clock=None):
        self.path = Path(path) if path else None
        self.clock = clock or (lambda: datetime.now(UTC))
        self.storage_status = 'ok'
        self.document = EvaluationDocument()
        self._lock = asyncio.Lock()
        self.listeners: list[Callable[[Evaluation], Awaitable[None]]] = []
        if self.path:
            try:
                if self.path.is_symlink():
                    raise ValueError('Symlink evaluation store')
                if not self.path.exists():
                    return
                if self.path.stat().st_size > MAX_BYTES:
                    raise ValueError('Oversized evaluation store')
                self.document = EvaluationDocument.model_validate_json(self.path.read_bytes())
                if any(r.evaluated_at > self.clock() + timedelta(seconds=30) for r in self.document.evaluations):
                    raise ValueError('Future evaluation timestamp')
            except (OSError, ValueError):
                self.document = EvaluationDocument()
                self.storage_status = 'corrupt'
                logger.warning('Evaluation storage invalid; source preserved')
        for record in self.document.evaluations:
            if record.completion_state == 'pending':
                record.completion_state = 'incomplete'
        # No startup write, no backfill, no action recovery.

    def records(self, routine_id: str | None = None) -> list[Evaluation]:
        now = self.clock()
        return [r.model_copy(deep=True) for r in reversed(self.document.evaluations)
                if now - RETENTION <= r.evaluated_at <= now + timedelta(seconds=30)
                and (routine_id is None or r.routine_id == routine_id)]

    def _persist(self):
        if self.path is None or self.storage_status == 'corrupt':
            return
        temp = self.path.with_name(f'.{self.path.name}.{uuid.uuid4().hex}.tmp')
        try:
            payload = self.document.model_dump_json()
            if len(payload.encode()) > MAX_BYTES:
                raise ValueError('Oversized evaluation payload')
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, self.path)
            directory = os.open(self.path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            self.storage_status = 'ok'
        except (OSError, ValueError):
            self.storage_status = 'write_failed'
            logger.warning('Evaluation persistence failed; automation unaffected')
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    async def begin(self, routine_id: str, origin: Origin, presence_reporting=None) -> str | None:
        try:
            async with self._lock:
                record = Evaluation(evaluation_id=f'ev_{uuid.uuid4().hex}', routine_id=routine_id,
                                    evaluated_at=self.clock(), trigger_origin=origin,
                                    presence_reporting=presence_reporting)
                self.document.evaluations = list(reversed(self.records()))[-(MAX_RECORDS - 1):] + [record]
                self._persist()
                return record.evaluation_id
        except Exception:
            logger.warning('Evaluation observation failed; automation unaffected')
            return None

    async def finish(self, evaluation_id: str | None, **result):
        if evaluation_id is None:
            return
        completed = None
        try:
            async with self._lock:
                for index, current in enumerate(self.document.evaluations):
                    if current.evaluation_id == evaluation_id:
                        self.document.evaluations[index] = Evaluation.model_validate({
                            **current.model_dump(), 'completion_state': 'complete', **result})
                        completed = self.document.evaluations[index].model_copy(deep=True)
                        self._persist()
                        break
        except Exception:
            logger.warning('Evaluation completion unavailable; automation unaffected')
            return
        if completed is not None:
            for listener in self.listeners:
                try:
                    await listener(completed.model_copy(deep=True))
                except Exception as exc:
                    logger.warning(
                        'Evaluation listener failed error=%s; automation unaffected',
                        type(exc).__name__,
                    )
