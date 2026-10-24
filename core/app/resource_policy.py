"""Phase 7I workload-aware resource alert policy, usage profiles and per-alert snooze.

The AlertEngine remains the only lifecycle. This module only decides, from bounded per-minute evidence,
whether a resource condition is *proven sustained*; it owns no task (sampling rides the existing
60 s System monitor) and stores its small state in ``alert-policy.json`` (additive; ``alerts.json`` is
untouched so rollback needs no conversion).

Sustained rule: over the last D seconds, >= 90 % of minutes carry a fresh sample, every covered
minute's *minimum* meets the threshold, no gap exceeds 5 minutes, and the evidence actually starts
at the window start. Missing data is never treated as high usage.
"""

import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)

Profile = Literal["normal", "gaming", "development"]
PROFILE_DEVICES = ("workstation", "primary", "secondary")
WINDOW_MINUTES = 6 * 60 + 10
MIN_COVERAGE = 0.9
MAX_GAP_MINUTES = 5
WINDOW_PERSIST_SECONDS = 300
SNOOZE_HOURS = (2, 8, 24, 168)  # offered: 2 h, 8 h, 24 h, 7 days
LEGACY_SNOOZE_HOURS = (1, 4)  # still accepted from older Android client builds
SUSTAINED_UTILIZATION_MINUTES = 6 * 60


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return value.astimezone(UTC)


class DeviceProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: Profile = "normal"
    until: datetime | None = None
    set_at: datetime
    set_by: str = Field(min_length=1, max_length=80)

    @field_validator("until", "set_at")
    @classmethod
    def tz_aware(cls, value):
        return _aware(value)


class AlertSnooze(BaseModel):
    model_config = ConfigDict(extra="forbid")

    until: datetime
    severity: Literal["info", "warning", "critical"]
    set_at: datetime
    set_by: str = Field(min_length=1, max_length=80)

    @field_validator("until", "set_at")
    @classmethod
    def tz_aware(cls, value):
        return _aware(value)


class AlertPolicyFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    profiles: dict[str, DeviceProfile] = Field(default_factory=dict, max_length=10)
    snoozes: dict[str, AlertSnooze] = Field(default_factory=dict, max_length=200)
    # metric key -> [[minute_epoch, min, max, count], ...] bounded to WINDOW_MINUTES entries
    windows: dict[str, list[list[float]]] = Field(default_factory=dict, max_length=64)


@dataclass(frozen=True)
class Sustained:
    met: bool
    coverage: float
    since: datetime | None  # start of the current proven streak, if any


class AlertPolicyStore:
    def __init__(self, path: str, *, clock=None) -> None:
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.storage_status: Literal["ok", "corrupt"] = "ok"
        self.write_failed = False
        self._persist_enabled = True
        self._last_window_persist: datetime | None = None
        self.insights: dict[str, dict] = {}  # in-memory explanation per alert key (UI only)
        self._state = self._load()

    # -- persistence --
    def _load(self) -> AlertPolicyFile:
        try:
            if not self.path.exists():
                return AlertPolicyFile()
            return AlertPolicyFile.model_validate_json(self.path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, json.JSONDecodeError):
            logger.error("Alert policy persistence is corrupt; defaults in memory, file kept")
            self.storage_status = "corrupt"
            self._persist_enabled = False
            return AlertPolicyFile()

    def persist(self) -> bool:
        if not self._persist_enabled:
            return False
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
            return True
        except OSError:
            self.write_failed = True
            logger.exception("Alert policy persistence failed")
            temporary.unlink(missing_ok=True)
            return False

    def _persist_write(self, previous: AlertPolicyFile) -> None:
        if not self.persist():
            self._state = previous
            raise OSError("alert policy persistence unavailable")

    def maybe_persist_windows(self) -> None:
        now = self.clock()
        if self._last_window_persist is None or (now - self._last_window_persist).total_seconds() >= WINDOW_PERSIST_SECONDS:
            self._last_window_persist = now
            self.persist()

    # -- evidence --
    def record(self, key: str, value: float | None) -> None:
        if value is None or not math.isfinite(value):
            return
        minute = int(self.clock().timestamp() // 60)
        buckets = self._state.windows.setdefault(key, [])
        if buckets and int(buckets[-1][0]) == minute:
            bucket = buckets[-1]
            bucket[1], bucket[2], bucket[3] = min(bucket[1], value), max(bucket[2], value), bucket[3] + 1
        else:
            buckets.append([float(minute), value, value, 1.0])
            cutoff = minute - WINDOW_MINUTES
            while buckets and buckets[0][0] < cutoff:
                buckets.pop(0)

    def sustained(self, key: str, threshold: float, seconds: float) -> Sustained:
        now_minute = int(self.clock().timestamp() // 60)
        span = max(1, int(math.ceil(seconds / 60)))
        start = now_minute - span + 1
        buckets = {int(b[0]): b for b in self._state.windows.get(key, []) if start <= b[0] <= now_minute}
        coverage = len(buckets) / span
        index = {int(b[0]): b for b in self._state.windows.get(key, [])}
        streak_start = None
        for minute in range(now_minute, now_minute - WINDOW_MINUTES, -1):  # current proven streak
            bucket = index.get(minute)
            if bucket is None:
                continue
            if bucket[1] < threshold:
                break
            streak_start = minute
        if not buckets or coverage < MIN_COVERAGE:
            return Sustained(False, round(coverage, 3), self._minute(streak_start))
        covered = sorted(buckets)
        if covered[0] > start + MAX_GAP_MINUTES or covered[-1] < now_minute - MAX_GAP_MINUTES:
            return Sustained(False, round(coverage, 3), self._minute(streak_start))
        if any(b - a > MAX_GAP_MINUTES + 1 for a, b in zip(covered, covered[1:])):
            return Sustained(False, round(coverage, 3), self._minute(streak_start))
        met = all(buckets[m][1] >= threshold for m in covered)
        return Sustained(met, round(coverage, 3), self._minute(streak_start))

    @staticmethod
    def _minute(value: int | None) -> datetime | None:
        return datetime.fromtimestamp(value * 60, UTC) if value is not None else None

    # -- profiles --
    def profile(self, device: str) -> Profile:
        item = self._state.profiles.get(device)
        if item is None:
            return "normal"
        if item.until is not None and item.until <= self.clock():
            del self._state.profiles[device]
            self.persist()
            return "normal"
        return item.profile

    def profile_view(self, device: str) -> DeviceProfile | None:
        self.profile(device)  # expire first
        item = self._state.profiles.get(device)
        return item.model_copy() if item else None

    def set_profile(self, device: str, profile: Profile, *, hours: int | None, actor: str) -> DeviceProfile | None:
        if device not in PROFILE_DEVICES:
            raise ValueError("unknown device")
        current = self.profile_view(device)
        if profile == "normal" and current is None:
            return None
        if current is not None and current.profile == profile and current.set_by == actor and (
            (hours is None and current.until is None) or
            (hours is not None and current.until is not None and current.until - current.set_at == timedelta(hours=hours))
        ):
            return current
        previous = self._state.model_copy(deep=True)
        now = self.clock()
        if profile == "normal":
            self._state.profiles.pop(device, None)
            self._persist_write(previous)
            return None
        item = DeviceProfile(profile=profile, until=now + timedelta(hours=hours) if hours else None,
                             set_at=now, set_by=actor)
        self._state.profiles[device] = item
        self._persist_write(previous)
        return item.model_copy()

    # -- snoozes --
    def snooze(self, alert_id: str) -> AlertSnooze | None:
        item = self._state.snoozes.get(alert_id)
        if item is None:
            return None
        if item.until <= self.clock():  # expiry is silent: no event, no notification
            del self._state.snoozes[alert_id]
            self.persist()
            return None
        return item.model_copy()

    def snoozes(self) -> dict[str, AlertSnooze]:
        return {key: value for key in list(self._state.snoozes) if (value := self.snooze(key)) is not None}

    def set_snooze(self, alert_id: str, *, hours: int, severity: str, actor: str) -> AlertSnooze:
        if hours not in SNOOZE_HOURS + LEGACY_SNOOZE_HOURS:
            raise ValueError("unsupported snooze duration")
        current = self.snooze(alert_id)
        if current is not None and current.severity == severity and current.set_by == actor and current.until - current.set_at == timedelta(hours=hours):
            return current
        previous = self._state.model_copy(deep=True)
        now = self.clock()
        item = AlertSnooze(until=now + timedelta(hours=hours), severity=severity, set_at=now, set_by=actor)
        self._state.snoozes[alert_id] = item
        self._persist_write(previous)
        return item.model_copy()

    def clear_snooze(self, alert_id: str) -> bool:
        previous = self._state.model_copy(deep=True)
        removed = self._state.snoozes.pop(alert_id, None) is not None
        if removed:
            self._persist_write(previous)
        return removed

    def suppresses(self, alert_id: str, severity: str) -> bool:
        """A warning-time snooze never hides a critical lifecycle."""
        item = self.snooze(alert_id)
        if item is None:
            return False
        return not (severity == "critical" and item.severity != "critical")


# ----- policy table ---------------------------------------------------------------------------
# Durations in minutes. Utilization (CPU/GPU/RAM) is never critical: high usage is normal while gaming
# or building, so it is only *abnormal* after six unbroken hours, and the Gaming/Development profiles
# pause it entirely. Temperature, disk exhaustion and services are profile-independent. Temperature
# limits come from the hardware (see report): Primary firmware throttles at 85 °C; Secondary's observed hwmon
# temp_crit is 105 °C.


@dataclass(frozen=True)
class Level:
    threshold: float
    minutes: dict[str, int]  # per profile
    recover: float


@dataclass(frozen=True)
class MetricPolicy:
    name: str
    unit: str
    warning: Level | None
    critical: Level | None
    profile_sensitive: bool  # True: a non-normal profile pauses this metric's alerts


def _all(minutes: int) -> dict[str, int]:
    return {"normal": minutes, "gaming": minutes, "development": minutes}


UTILIZATION = {
    "cpu": MetricPolicy("CPU usage", "%", Level(90, _all(SUSTAINED_UTILIZATION_MINUTES), 80), None, True),
    "gpu": MetricPolicy("GPU usage", "%", Level(90, _all(SUSTAINED_UTILIZATION_MINUTES), 75), None, True),
    "ram": MetricPolicy("RAM usage", "%", Level(92, _all(SUSTAINED_UTILIZATION_MINUTES), 85), None, True),
    "disk": MetricPolicy("disk usage", "%", Level(85, _all(30), 80), Level(97, _all(10), 95), False),
}
TEMPERATURE = {
    "primary": MetricPolicy("temperature", "°C", Level(80, _all(5), 75), Level(85, _all(2), 80), False),
    "secondary": MetricPolicy("temperature", "°C", Level(90, _all(5), 85), Level(100, _all(2), 95), False),
}
DISK_FREE_CRITICAL_GB = 10.0
DISK_FREE_RECOVER_GB = 15.0


def valid_temperature(value: float | None) -> float | None:
    """Rejects agent sentinels such as a 0 °C GPU reading; plausible chip range only."""
    if value is None or not math.isfinite(value) or not 10 <= value <= 125:
        return None
    return value
