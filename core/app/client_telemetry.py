"""Phase 7D.3: Android/PWA self-reported telemetry into the existing Device Registry.

Identity is the authenticated RoomOS session, never the payload. An operator allowlist binds
one paired app (auth device id) to one catalog device and one app kind; the bound app may
only write its own device, only with ``telemetry:write:self``, and only through the session
kind it was bound with (native bearer vs. PWA cookie+CSRF). ``devices:read`` grants nothing
here. Reports are validated per sensor; anything out of range becomes ``invalid_report``,
never 0.

Liveness: each report states when the next one is due (``next_report_seconds``) because
Android cadence legitimately varies (about a minute while RoomOS is open, ~15 minutes or
more in background/Doze). RoomOS keeps the device ``online`` and its values fresh only
until that deadline plus a grace; after that it is ``unknown`` (silence proves nothing),
never ``offline``. The device clock (``measured_at``) and RoomOS receipt time stay separate.
"""

import json
import logging
import math
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr, ValidationError

from .devices import (
    CATALOG,
    DeviceRegistry,
    DeviceSpec,
    Reading,
    SourceKind,
    SourceSpec,
    android_native_sensors,
    android_web_sensors,
)

logger = logging.getLogger(__name__)

TELEMETRY_SCOPE = "telemetry:write:self"
ClientApp = Literal["android", "web"]
APP_SOURCE: Mapping[str, SourceKind] = {"android": "roomos_native", "web": "roomos_web"}
BINDABLE_TYPES = frozenset({"android"})
AUTH_DEVICE_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

MAX_BODY_BYTES = 4096
MAX_REPORT_AGE = timedelta(minutes=10)
FUTURE_TOLERANCE = timedelta(seconds=60)
GRACE_SECONDS = 30
MIN_NEXT_REPORT, MAX_NEXT_REPORT = 30, 3600

PERCENT = (0.0, 100.0)
RANGES: Mapping[str, tuple[float, float]] = {
    "battery_percent": PERCENT, "ram_usage": PERCENT, "disk_usage": PERCENT,
    "battery_temperature": (-20.0, 80.0), "ram_total": (0.25, 64.0),
    "storage_free": (0.0, 4096.0), "storage_total": (1.0, 4096.0),
}
ENUMS: Mapping[str, frozenset[str]] = {
    "power_source": frozenset({"ac", "usb", "wireless", "dock", "battery"}),
    "battery_health": frozenset({"good", "overheat", "dead", "over_voltage", "cold", "failure"}),
    "network_type": frozenset({"wifi", "cellular", "ethernet", "vpn", "bluetooth", "other", "none"}),
    "thermal_status": frozenset({"none", "light", "moderate", "severe", "critical", "emergency", "shutdown"}),
}
TEXT_PATTERNS: Mapping[str, re.Pattern[str]] = {
    "android_version": re.compile(r"^[0-9]{1,2}(\.[0-9]{1,3}){0,2}$"),
    "device_model": re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()+-]{0,39}$"),
}


@dataclass(frozen=True)
class ClientBinding:
    auth_device_id: str
    device_id: str
    app: ClientApp

    @property
    def source(self) -> SourceKind:
        return APP_SOURCE[self.app]


def parse_bindings(raw: str | None, catalog: tuple[DeviceSpec, ...] = CATALOG) -> dict[str, ClientBinding]:
    """Validate the operator allowlist. Any invalid entry disables all phone telemetry (fail closed)."""
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or not data:
            raise ValueError("bindings must be a non-empty JSON object")
        types = {d.id: d.type for d in catalog}
        bindings: dict[str, ClientBinding] = {}
        for auth_id, target in data.items():
            if not isinstance(auth_id, str) or not AUTH_DEVICE_PATTERN.fullmatch(auth_id):
                raise ValueError("binding keys must be RoomOS auth device ids")
            if not isinstance(target, dict) or set(target) != {"device", "app"}:
                raise ValueError("binding values need exactly 'device' and 'app'")
            device, app = target["device"], target["app"]
            if types.get(device) not in BINDABLE_TYPES:
                raise ValueError(f"not a bindable phone/tablet: {device!r}")
            if app not in APP_SOURCE:
                raise ValueError(f"unknown app kind: {app!r}")
            bindings[auth_id] = ClientBinding(auth_id, device, app)
        devices = [b.device_id for b in bindings.values()]
        if len(devices) != len(set(devices)):
            # One authoritative source per device: never merge native and PWA reports.
            raise ValueError("a device can be bound to one app only")
        return bindings
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("Android telemetry disabled: invalid binding config (%s)", exc)
        return {}


def configured_catalog(
    catalog: tuple[DeviceSpec, ...], bindings: Mapping[str, ClientBinding]
) -> tuple[tuple[DeviceSpec, ...], dict[tuple[SourceKind, str], str]]:
    """Bound devices get the contract of their app (native or PWA) as a configured source."""
    by_device = {b.device_id: b for b in bindings.values()}
    out = []
    for device in catalog:
        binding = by_device.get(device.id)
        if binding is not None:
            sensors = android_native_sensors() if binding.app == "android" else android_web_sensors()
            device = replace(device, sources=(SourceSpec(binding.source, True),), sensors=sensors,
                             metadata={k: v for k, v in device.metadata.items() if k != "planned_phase"}
                             | {"reporter": "RoomOS Android app" if binding.app == "android" else "RoomOS web app (Chrome)"})
        out.append(device)
    return tuple(out), {(b.source, b.auth_device_id): b.device_id for b in bindings.values()}


Value = StrictBool | StrictInt | StrictFloat | StrictStr | None


class ClientTelemetryReport(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1]
    sequence: StrictInt = Field(ge=1, le=2**53)
    measured_at: datetime
    next_report_seconds: StrictInt = Field(ge=MIN_NEXT_REPORT, le=MAX_NEXT_REPORT)
    sensors: dict[str, Value] = Field(max_length=24)


class TelemetryRejected(Exception):
    def __init__(self, status: int, reason: str, retry_after: int | None = None) -> None:
        super().__init__(reason)
        self.status, self.reason, self.retry_after = status, reason, retry_after


def parse_value(sensor: str, kind: str, value: Any) -> Reading:
    """One reported value. ``None`` = the app could not read it now (unknown, not 0)."""
    if value is None:
        return Reading(state="unknown")
    if kind == "boolean":
        return Reading(value) if isinstance(value, bool) else Reading(value=None)
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
            return Reading(value=None)
        low, high = RANGES.get(sensor, (-math.inf, math.inf))
        return Reading(round(float(value), 1)) if low <= value <= high else Reading(value=None)
    if not isinstance(value, str):
        return Reading(value=None)
    if sensor in ENUMS:
        return Reading(value) if value in ENUMS[sensor] else Reading(value=None)
    pattern = TEXT_PATTERNS.get(sensor)
    return Reading(value) if pattern is not None and pattern.fullmatch(value) else Reading(value=None)


@dataclass
class _ClientState:
    sequence: int
    measured_at: datetime
    accepted_mono: float


class ClientTelemetryIngest:
    def __init__(
        self,
        registry: DeviceRegistry,
        bindings: Mapping[str, ClientBinding],
        *,
        min_interval_seconds: int = 15,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.registry = registry
        self.bindings = dict(bindings)
        self.min_interval = min_interval_seconds
        self.clock = clock or (lambda: datetime.now(UTC))
        self.monotonic = monotonic or time.monotonic
        self._clients: dict[str, _ClientState] = {}

    def binding_for(self, auth_device_id: str, session_kind: str | None) -> ClientBinding:
        binding = self.bindings.get(auth_device_id)
        if binding is None:
            raise TelemetryRejected(403, "unbound_client")
        if session_kind != binding.app:
            raise TelemetryRejected(403, "session_kind_mismatch")
        return binding

    def accept(self, auth_device_id: str, session_kind: str | None, body: bytes) -> tuple[str, int]:
        """Validate and store one report; returns (catalog device id, accepted next deadline)."""
        binding = self.binding_for(auth_device_id, session_kind)
        if len(body) > MAX_BODY_BYTES:
            raise TelemetryRejected(413, "too_large")
        try:
            report = ClientTelemetryReport.model_validate_json(body)
        except ValidationError:
            raise TelemetryRejected(422, "invalid_payload") from None
        now, mono = self.clock(), self.monotonic()
        measured = report.measured_at
        if measured.tzinfo is None or measured > now + FUTURE_TOLERANCE or measured < now - MAX_REPORT_AGE:
            raise TelemetryRejected(422, "invalid_timestamp")
        previous = self._clients.get(auth_device_id)
        if previous is not None:
            if report.sequence <= previous.sequence or measured <= previous.measured_at:
                raise TelemetryRejected(409, "replayed_or_out_of_order")
            wait = self.min_interval - (mono - previous.accepted_mono)
            if wait > 0:
                raise TelemetryRejected(429, "rate_limited", retry_after=math.ceil(wait))
        device = self.registry.catalog[binding.device_id]
        specs = {s.id: s for s in device.sensors if s.support != "unsupported" and s.source == binding.source}
        unknown = set(report.sensors) - set(specs)
        if unknown:
            raise TelemetryRejected(422, "unknown_sensor")
        readings = {
            sensor: replace(parse_value(sensor, specs[sensor].kind, value), measured_at=measured)
            for sensor, value in report.sensors.items()
        }
        stale_after = report.next_report_seconds + max(GRACE_SECONDS, report.next_report_seconds // 2)
        accepted = self.registry.ingest(binding.source, auth_device_id, readings,
                                        connection="online", stale_after_seconds=stale_after)
        if accepted is None:  # binding table and registry disagree: fail closed
            raise TelemetryRejected(403, "unbound_client")
        self._clients[auth_device_id] = _ClientState(report.sequence, measured, mono)
        return accepted, stale_after
