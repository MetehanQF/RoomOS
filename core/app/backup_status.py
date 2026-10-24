"""Read-only view of the host's non-secret RoomOS backup status file.

Core never sees backup artifacts or keys: only one small JSON file in a read-only mount.
Reading it is bounded (4 KB), cached by (mtime, size), and fails closed to ``invalid``.
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

MAX_BYTES = 4096
Reason = Literal[
    "source_validation_failed", "backup_failed", "integrity_failed", "restore_failed",
    "cleanup_failed", "retention_failed", "locked", "interrupted",
]


class BackupStatusFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    status: Literal["running", "success", "failed"]
    reason: Reason | None
    tool_version: Literal[2]
    schedule: str = Field(min_length=1, max_length=60)
    cadence_seconds: int = Field(ge=3600, le=7 * 86400)
    last_attempt_at: datetime | None
    last_finished_at: datetime | None
    last_success_at: datetime | None
    last_restore_test_at: datetime | None
    restore_tested: bool
    last_skipped_at: datetime | None
    last_skip_reason: Reason | None

    @model_validator(mode="after")
    def consistent(self):
        if (self.status == "failed") != (self.reason is not None):
            raise ValueError("reason must be present exactly for failed attempts")
        if self.restore_tested != (self.last_restore_test_at is not None):
            raise ValueError("restore_tested must match last_restore_test_at")
        for value in (self.last_attempt_at, self.last_finished_at, self.last_success_at,
                      self.last_restore_test_at, self.last_skipped_at):
            if value is not None and value.tzinfo is None:
                raise ValueError("timestamps must be timezone-aware")
        return self


@dataclass(frozen=True)
class BackupObservation:
    state: Literal["not_configured", "absent", "invalid", "ok"]
    document: BackupStatusFile | None = None


class BackupStatusReader:
    def __init__(self, path: str):
        self.path = Path(path)
        self._cache: tuple[tuple[int, int], BackupObservation] | None = None

    def read(self) -> BackupObservation:
        if not self.path.parent.is_dir():
            return BackupObservation("not_configured")
        try:
            info = os.stat(self.path)
        except FileNotFoundError:
            return BackupObservation("absent")
        except OSError:
            return BackupObservation("invalid")
        key = (info.st_mtime_ns, info.st_size)
        if self._cache is not None and self._cache[0] == key:
            return self._cache[1]
        if info.st_size > MAX_BYTES:
            observation = BackupObservation("invalid")
        else:
            try:
                document = BackupStatusFile.model_validate(json.loads(self.path.read_bytes()))
                observation = BackupObservation("ok", document)
            except (OSError, ValueError, ValidationError):
                observation = BackupObservation("invalid")
        self._cache = (key, observation)
        return observation
