"""Device Registry (Phase 7D.1): reviewed catalog plus in-memory latest telemetry.

Metadata is code, like ``catalog.py``; there is no persistent registry file and no
telemetry history. The latest-value cache starts empty on every Core start, so a
value seen before a restart is never presented as current afterwards.

Identity is explicit: a report is accepted only for an exact ``(source, external_id)``
binding declared here. Unknown identities are counted and dropped; devices are never
created or merged from report contents. Each sensor has one authoritative source.

Connection (did a source recently report reachability?) and telemetry freshness (how
old is each value?) are separate. ``observed_at`` is when RoomOS received a value;
``measured_at`` is only set when a source contract states a measurement time. Ages use
a monotonic clock; a wall/monotonic discontinuity makes freshness unknown.

No commands, subscriptions, HA entities or MQTT topics are added by this module.
"""

import asyncio
import json
import logging
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from .models import RoomOSEvent, SystemNode, SystemResponse

logger = logging.getLogger(__name__)

DeviceType = Literal["windows", "android", "linux", "roomos_panel", "other"]
SourceKind = Literal["system_monitor", "home_assistant", "mqtt", "roomos_native", "roomos_web"]
Support = Literal["supported", "unverified", "unsupported"]
Availability = Literal["available", "unavailable", "unsupported", "unknown"]
Freshness = Literal["fresh", "stale", "unknown"]
Connection = Literal["online", "offline", "unknown"]
DeviceHealth = Literal["healthy", "degraded", "offline", "unknown"]
SensorReason = Literal[
    "source_not_configured", "awaiting_first_report", "source_unavailable", "invalid_report",
    "source_reported_unknown", "invalid_timestamp", "clock_discontinuity", "not_supported_by_device",
]
DeviceReason = Literal["source_not_configured", "awaiting_first_report"]

CLOCK_TOLERANCE_SECONDS = 60.0
FUTURE_TOLERANCE = timedelta(seconds=60)
DEVICE_ID_PATTERN = r"^[a-z0-9_]{1,40}$"
DEVICE_SCOPES = frozenset({"system:read", "devices:read"})


@dataclass(frozen=True)
class SensorSpec:
    id: str
    kind: Literal["number", "boolean", "text"]
    support: Support
    source: SourceKind | None = None
    unit: str | None = None


@dataclass(frozen=True)
class SourceSpec:
    kind: SourceKind
    configured: bool


@dataclass(frozen=True)
class DeviceSpec:
    id: str
    name: str
    type: DeviceType
    platform: str
    required_scope: Literal["system:read", "devices:read"]
    sources: tuple[SourceSpec, ...]
    sensors: tuple[SensorSpec, ...]
    metadata: Mapping[str, str] = field(default_factory=dict)


def _linux_sensors() -> tuple[SensorSpec, ...]:
    src: SourceKind = "system_monitor"
    return (
        SensorSpec("cpu_usage", "number", "supported", src, "%"),
        SensorSpec("ram_usage", "number", "supported", src, "%"),
        SensorSpec("disk_usage", "number", "supported", src, "%"),
        SensorSpec("cpu_temperature", "number", "supported", src, "°C"),
        SensorSpec("uptime", "text", "supported", src),
        SensorSpec("docker_running", "boolean", "supported", src),
        SensorSpec("battery_percent", "number", "unsupported"),
    )


def _panel_sensors() -> tuple[SensorSpec, ...]:
    """Panel phone (7E.1): unchanged planned contract."""
    src: SourceKind = "roomos_native"
    return (
        SensorSpec("battery_percent", "number", "supported", src, "%"),
        SensorSpec("charging", "boolean", "supported", src),
        SensorSpec("battery_temperature", "number", "unverified", src, "°C"),
        SensorSpec("wifi_connected", "boolean", "supported", src),
        SensorSpec("cpu_usage", "number", "unsupported"),
        SensorSpec("ram_usage", "number", "unsupported"),
        SensorSpec("cpu_temperature", "number", "unsupported"),
        SensorSpec("panel_connected", "boolean", "supported", src),
    )


def android_native_sensors() -> tuple[SensorSpec, ...]:
    """7D.3: what the RoomOS Android app reads from public framework APIs (no permissions
    beyond ACCESS_NETWORK_STATE). CPU load and CPU temperature have no public API for apps
    since Android 8 (/proc/stat is blocked), so they are declared unsupported, never 0."""
    src: SourceKind = "roomos_native"
    return (
        SensorSpec("battery_percent", "number", "supported", src, "%"),      # BatteryManager
        SensorSpec("charging", "boolean", "supported", src),
        SensorSpec("power_source", "text", "supported", src),                # EXTRA_PLUGGED
        SensorSpec("battery_temperature", "number", "supported", src, "°C"),  # EXTRA_TEMPERATURE
        SensorSpec("battery_health", "text", "supported", src),              # EXTRA_HEALTH status
        SensorSpec("ram_usage", "number", "supported", src, "%"),            # ActivityManager.MemoryInfo
        SensorSpec("ram_total", "number", "supported", src, "GB"),
        SensorSpec("disk_usage", "number", "supported", src, "%"),           # StatFs(/data)
        SensorSpec("storage_free", "number", "supported", src, "GB"),
        SensorSpec("storage_total", "number", "supported", src, "GB"),
        SensorSpec("network_type", "text", "supported", src),                # ConnectivityManager
        SensorSpec("network_connected", "boolean", "supported", src),        # NET_CAPABILITY_VALIDATED
        SensorSpec("thermal_status", "text", "unverified", src),             # PowerManager, API 29+
        SensorSpec("android_version", "text", "supported", src),
        SensorSpec("device_model", "text", "supported", src),
        SensorSpec("cpu_usage", "number", "unsupported"),
        SensorSpec("cpu_temperature", "number", "unsupported"),
    )


def android_web_sensors() -> tuple[SensorSpec, ...]:
    """7D.3: what Chrome for Android exposes to the RoomOS PWA. Battery Status, Network
    Information and UA Client Hints are browser APIs that may be missing or coarse, hence
    ``unverified``. Browser storage quota is not device storage; RAM, storage, temperatures
    and CPU are unsupported in a browser."""
    src: SourceKind = "roomos_web"
    return (
        SensorSpec("battery_percent", "number", "unverified", src, "%"),     # navigator.getBattery()
        SensorSpec("charging", "boolean", "unverified", src),
        SensorSpec("network_type", "text", "unverified", src),               # navigator.connection.type
        SensorSpec("network_connected", "boolean", "supported", src),        # navigator.onLine
        SensorSpec("android_version", "text", "unverified", src),            # UA Client Hints
        SensorSpec("device_model", "text", "unverified", src),
        SensorSpec("power_source", "text", "unsupported"),
        SensorSpec("battery_temperature", "number", "unsupported"),
        SensorSpec("battery_health", "text", "unsupported"),
        SensorSpec("ram_usage", "number", "unsupported"),
        SensorSpec("ram_total", "number", "unsupported"),
        SensorSpec("disk_usage", "number", "unsupported"),
        SensorSpec("storage_free", "number", "unsupported"),
        SensorSpec("storage_total", "number", "unsupported"),
        SensorSpec("thermal_status", "text", "unsupported"),
        SensorSpec("cpu_usage", "number", "unsupported"),
        SensorSpec("cpu_temperature", "number", "unsupported"),
    )


# Location is deliberately not a registry sensor: device telemetry must not become a
# location feed. Presence stays inside the routine engine (Phase 7C.3.2).
CATALOG: tuple[DeviceSpec, ...] = (
    DeviceSpec("primary", "Raspberry Pi 5", "linux", "Linux arm64", "system:read",
               (SourceSpec("system_monitor", True),), _linux_sensors(), {"role": "primary_server"}),
    DeviceSpec("secondary", "Secondary", "linux", "Linux x86_64", "system:read",
               (SourceSpec("system_monitor", True),), _linux_sensors(), {"role": "secondary_server_dns"}),
    DeviceSpec("workstation", "Workstation", "windows", "Windows", "devices:read",
               (SourceSpec("home_assistant", False),), (
                   # 7D.2: HASS.Agent via MQTT discovery -> Home Assistant -> RoomOS HA stream.
                   SensorSpec("cpu_usage", "number", "supported", "home_assistant", "%"),
                   SensorSpec("ram_usage", "number", "supported", "home_assistant", "%"),
                   SensorSpec("disk_usage", "number", "supported", "home_assistant", "%"),
                   SensorSpec("battery_percent", "number", "supported", "home_assistant", "%"),
                   SensorSpec("ac_connected", "boolean", "supported", "home_assistant"),
                   SensorSpec("charging", "boolean", "supported", "home_assistant"),
                   # A boot timestamp, not a ticking uptime: a value that changes every
                   # second would publish a device event every second.
                   SensorSpec("last_boot", "text", "supported", "home_assistant"),
                   # 7D.4.1: HASS.Agent LastActive (last user input), a timestamp like last_boot.
                   SensorSpec("last_active", "text", "supported", "home_assistant"),
                   SensorSpec("cpu_temperature", "number", "unverified", "home_assistant", "°C"),
                   SensorSpec("gpu_usage", "number", "unverified", "home_assistant", "%"),
                   SensorSpec("gpu_temperature", "number", "unverified", "home_assistant", "°C"),
                   SensorSpec("network_connected", "boolean", "unverified", "home_assistant"),
               ), {"role": "laptop", "planned_phase": "7D.2"}),
    # 7D.3: phones/tablets report through a paired RoomOS app. Unbound they show the native
    # contract, not configured; an operator binding selects native or PWA (client_telemetry).
    DeviceSpec("phone", "Phone", "android", "Android", "devices:read",
               (SourceSpec("roomos_native", False),), android_native_sensors(),
               {"role": "personal_phone", "planned_phase": "7D.3"}),
    DeviceSpec("tablet", "Tablet", "android", "Android", "devices:read",
               (SourceSpec("roomos_native", False),), android_native_sensors(),
               {"role": "tablet", "planned_phase": "7D.3"}),
    # A second phone. Device ids are stable: renaming one would orphan its binding and history.
    DeviceSpec("phone_2", "Second phone", "android", "Android", "devices:read",
               (SourceSpec("roomos_native", False),), android_native_sensors(),
               {"role": "phone", "planned_phase": "7D.3"}),
    DeviceSpec("roomos_panel_phone", "RoomOS panel phone", "roomos_panel", "Android Chrome PWA", "devices:read",
               (SourceSpec("roomos_native", False),), _panel_sensors(),
               {"role": "fixed_control_panel", "planned_phase": "7D.3/7E.1"}),
)

# Exact identity bindings. Future sources add rows here after the device identity is
# proven (7D.2/7D.3); nothing is inferred from names, hostnames or report contents.
BINDINGS: Mapping[tuple[SourceKind, str], str] = {
    ("system_monitor", "primary"): "primary",
    ("system_monitor", "secondary"): "secondary",
}


def validate_catalog(catalog: tuple[DeviceSpec, ...], bindings: Mapping[tuple[str, str], str]) -> None:
    import re

    ids = [d.id for d in catalog]
    if len(ids) != len(set(ids)) or not all(re.fullmatch(DEVICE_ID_PATTERN, i) for i in ids):
        raise ValueError("Device IDs must be unique stable slugs")
    by_id = {d.id: d for d in catalog}
    for device in catalog:
        sensor_ids = [s.id for s in device.sensors]
        if len(sensor_ids) != len(set(sensor_ids)):
            raise ValueError(f"Duplicate sensor on {device.id}")
        kinds = {s.kind for s in device.sources}
        for sensor in device.sensors:
            if (sensor.support == "unsupported") != (sensor.source is None):
                raise ValueError(f"{device.id}.{sensor.id}: unsupported sensors have no source")
            if sensor.source is not None and sensor.source not in kinds:
                raise ValueError(f"{device.id}.{sensor.id}: undeclared source")
    for (source, _external), device_id in bindings.items():
        device = by_id.get(device_id)
        if device is None or source not in {s.kind for s in device.sources}:
            raise ValueError("Binding targets an unknown device or undeclared source")


validate_catalog(CATALOG, BINDINGS)


@dataclass(frozen=True)
class Reading:
    """One source report for one sensor; ``state`` records what the source itself said."""

    value: Any = None
    unit: str | None = None
    state: Literal["value", "unavailable", "unknown"] = "value"
    measured_at: datetime | None = None
    source_fresh: bool | None = None


@dataclass
class _SensorState:
    value: Any
    unit: str | None
    available: bool
    reason: SensorReason | None
    source_fresh: bool | None
    observed_at: datetime
    observed_mono: float
    measured_at: datetime | None
    age_at_receipt: float  # seconds between measurement (or receipt) and receipt


@dataclass
class _SourceState:
    connection: Connection
    seen_at: datetime
    seen_mono: float
    # Per-report staleness (7D.3): phones announce their own next-report deadline because
    # Android background limits make the cadence vary (1 min in the app, ~15 min in Doze).
    stale_after: int | None = None


class SourceView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: SourceKind
    configured: bool
    connection: Connection
    last_seen_at: datetime | None
    stale_after_seconds: int


class SensorView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    kind: Literal["number", "boolean", "text"]
    support: Support
    source: SourceKind | None
    value: str | float | int | bool | None
    unit: str | None
    availability: Availability
    freshness: Freshness
    observed_at: datetime | None
    measured_at: datetime | None
    reason: SensorReason | None


class DeviceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    type: DeviceType
    platform: str
    connection: Connection
    health: DeviceHealth
    telemetry_freshness: Freshness
    last_seen_at: datetime | None
    last_telemetry_at: datetime | None
    status_reason: DeviceReason | None
    capabilities: dict[str, Support]
    sources: list[SourceView]
    metadata: dict[str, str]


class DeviceDetail(DeviceSummary):
    sensors: list[SensorView]


class DeviceListResponse(BaseModel):
    """Visible devices with their sensors (7D.4): the same shape as a ``device.updated``
    event, so the Device Center loads in one request and merges events without extra GETs."""

    model_config = ConfigDict(extra="forbid")
    devices: list[DeviceDetail]
    generated_at: datetime


class DeviceTelemetryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_id: str
    sensors: list[SensorView]
    generated_at: datetime


Publish = Callable[[RoomOSEvent, str], Awaitable[None]]


def _valid_value(spec: SensorSpec, value: Any) -> bool:
    if spec.kind == "boolean":
        return isinstance(value, bool)
    if spec.kind == "number":
        return isinstance(value, int | float) and not isinstance(value, bool) and value == value and abs(value) != float("inf")
    return isinstance(value, str | int | float) and not isinstance(value, bool) and len(str(value)) <= 64


class DeviceRegistry:
    def __init__(
        self,
        stale_after: Mapping[SourceKind, int],
        publish: Publish | None = None,
        *,
        catalog: tuple[DeviceSpec, ...] = CATALOG,
        bindings: Mapping[tuple[SourceKind, str], str] = BINDINGS,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        validate_catalog(catalog, bindings)
        self.catalog = {d.id: d for d in catalog}
        self.bindings = dict(bindings)
        self.stale_after = dict(stale_after)
        self.publish = publish
        self.clock = clock or (lambda: datetime.now(UTC))
        self.monotonic = monotonic or time.monotonic
        self.rejected = Counter[str]()
        self._sensors: dict[tuple[str, str], _SensorState] = {}
        self._sources: dict[tuple[str, SourceKind], _SourceState] = {}
        self._signatures: dict[str, str] = {}
        self._stop = asyncio.Event()
        self.tickers: list[Callable[[], None]] = []
        self.change_listeners: list[Callable[[DeviceDetail], Awaitable[None]]] = []
        self.snapshot_listeners: list[Callable[[DeviceDetail], Awaitable[None]]] = []

    # ----- ingestion -------------------------------------------------------------------
    def ingest(
        self,
        source: SourceKind,
        external_id: str,
        readings: Mapping[str, Reading],
        *,
        connection: Connection = "unknown",
        stale_after_seconds: int | None = None,
    ) -> str | None:
        """Accept one report for an exactly bound identity; returns the device ID or None."""
        device_id = self.bindings.get((source, external_id))
        if device_id is None:
            self.rejected["unbound_identity"] += 1
            return None
        device = self.catalog[device_id]
        now, mono = self.clock(), self.monotonic()
        self._sources[(device_id, source)] = _SourceState(connection, now, mono, stale_after_seconds)
        specs = {s.id: s for s in device.sensors}
        for sensor_id, reading in readings.items():
            spec = specs.get(sensor_id)
            if spec is None or spec.source != source:
                self.rejected["foreign_sensor"] += 1
                continue
            current = self._sensors.get((device_id, sensor_id))
            if (current is not None and current.measured_at is not None
                    and isinstance(reading.measured_at, datetime) and reading.measured_at.tzinfo is not None
                    and reading.measured_at < current.measured_at):
                # A delayed or replayed report must not replace a newer measurement.
                self.rejected["out_of_order"] += 1
                continue
            self._sensors[(device_id, sensor_id)] = self._sensor_state(spec, reading, now, mono)
        return device_id

    def _sensor_state(self, spec: SensorSpec, reading: Reading, now: datetime, mono: float) -> _SensorState:
        def state(value, available, reason, measured=None, age=0.0):
            return _SensorState(value, reading.unit or spec.unit, available, reason, reading.source_fresh,
                                now, mono, measured, age)

        if reading.state != "value":
            return state(None, False, "source_unavailable" if reading.state == "unavailable" else "source_reported_unknown")
        measured = reading.measured_at
        if measured is not None:
            if not isinstance(measured, datetime) or measured.tzinfo is None or measured > now + FUTURE_TOLERANCE:
                return state(None, False, "invalid_timestamp")
        if not _valid_value(spec, reading.value):
            return state(None, False, "invalid_report")
        age = max(0.0, (now - measured).total_seconds()) if measured is not None else 0.0
        return state(reading.value, True, None, measured, age)

    def ingest_system_snapshot(self, snapshot: SystemResponse) -> None:
        """Adapter for the existing canonical SystemMonitor snapshot (Primary/Secondary)."""
        for node in (snapshot.primary, snapshot.secondary):
            self.ingest("system_monitor", node.id, _node_readings(node),
                        connection="online" if node.online is True else "offline" if node.online is False else "unknown")

    async def on_system_snapshot(self, snapshot: SystemResponse) -> None:
        self.ingest_system_snapshot(snapshot)
        await self.publish_changes()

    # ----- views -----------------------------------------------------------------------
    def _elapsed(self, observed_at: datetime, observed_mono: float) -> float | None:
        elapsed = self.monotonic() - observed_mono
        wall = (self.clock() - observed_at).total_seconds()
        if elapsed < 0 or abs(wall - elapsed) > CLOCK_TOLERANCE_SECONDS:
            return None
        return elapsed

    def _sensor_view(self, device: DeviceSpec, spec: SensorSpec) -> SensorView:
        base = dict(id=spec.id, kind=spec.kind, support=spec.support, source=spec.source, unit=spec.unit,
                    value=None, observed_at=None, measured_at=None)
        if spec.support == "unsupported":
            return SensorView(**base, availability="unsupported", freshness="unknown", reason="not_supported_by_device")
        configured = any(s.kind == spec.source and s.configured for s in device.sources)
        current = self._sensors.get((device.id, spec.id))
        if current is None:
            return SensorView(**base, availability="unknown", freshness="unknown",
                              reason="awaiting_first_report" if configured else "source_not_configured")
        base |= dict(unit=current.unit, observed_at=current.observed_at, measured_at=current.measured_at)
        if not current.available:
            availability: Availability = "unavailable" if current.reason == "source_unavailable" else "unknown"
            return SensorView(**base, availability=availability, freshness="unknown", reason=current.reason)
        elapsed = self._elapsed(current.observed_at, current.observed_mono)
        if elapsed is None:
            return SensorView(**base, availability="unknown", freshness="unknown", reason="clock_discontinuity")
        age = current.age_at_receipt + elapsed
        fresh = age < self._threshold(device.id, spec.source) and current.source_fresh is not False
        return SensorView(**base | {"value": current.value}, availability="available",
                          freshness="fresh" if fresh else "stale", reason=None)

    def _threshold(self, device_id: str, source: SourceKind) -> int:
        current = self._sources.get((device_id, source))
        if current is not None and current.stale_after is not None:
            return current.stale_after
        return self.stale_after[source]

    def _source_view(self, device: DeviceSpec, source: SourceSpec) -> SourceView:
        threshold = self._threshold(device.id, source.kind)
        current = self._sources.get((device.id, source.kind))
        connection: Connection = "unknown"
        if current is not None:
            elapsed = self._elapsed(current.seen_at, current.seen_mono)
            # A silent source proves nothing: reachability older than the threshold is unknown.
            if elapsed is not None and elapsed < threshold:
                connection = current.connection
        return SourceView(kind=source.kind, configured=source.configured, connection=connection,
                          last_seen_at=current.seen_at if current else None, stale_after_seconds=threshold)

    def _detail(self, device: DeviceSpec) -> DeviceDetail:
        sources = [self._source_view(device, s) for s in device.sources]
        sensors = [self._sensor_view(device, s) for s in device.sensors]
        connections = {s.connection for s in sources if s.configured}
        connection: Connection = (
            "online" if "online" in connections
            else "offline" if connections == {"offline"} else "unknown"
        )
        reported = [s for s in sensors if s.support != "unsupported" and s.availability == "available"]
        expected = [s for s in sensors if s.support == "supported"]
        telemetry: Freshness = (
            "unknown" if not reported else "stale" if any(s.freshness == "stale" for s in reported) else "fresh"
        )
        if connection == "offline":
            health: DeviceHealth = "offline"
        elif connection == "unknown" or not reported:
            health = "unknown"
        elif telemetry == "stale" or any(s.availability != "available" for s in expected):
            health = "degraded"
        else:
            health = "healthy"
        configured = any(s.configured for s in device.sources)
        seen = [s.last_seen_at for s in sources if s.last_seen_at is not None]
        observed = [s.observed_at for s in reported if s.observed_at is not None]
        return DeviceDetail(
            id=device.id, name=device.name, type=device.type, platform=device.platform,
            connection=connection, health=health, telemetry_freshness=telemetry,
            last_seen_at=max(seen) if seen else None, last_telemetry_at=max(observed) if observed else None,
            status_reason=None if seen else "awaiting_first_report" if configured else "source_not_configured",
            capabilities={s.id: s.support for s in device.sensors}, sources=sources,
            metadata=dict(device.metadata), sensors=sensors,
        )

    def visible(self, scopes: frozenset[str]) -> list[DeviceSpec]:
        return [d for d in self.catalog.values() if d.required_scope in scopes]

    def list(self, scopes: frozenset[str]) -> DeviceListResponse:
        return DeviceListResponse(devices=[self._detail(d) for d in self.visible(scopes)], generated_at=self.clock())

    def detail(self, device_id: str, scopes: frozenset[str]) -> DeviceDetail | None:
        device = self.catalog.get(device_id)
        # Invisible and unknown devices are indistinguishable to the caller.
        if device is None or device.required_scope not in scopes:
            return None
        return self._detail(device)

    def telemetry(self, device_id: str, scopes: frozenset[str]) -> DeviceTelemetryResponse | None:
        detail = self.detail(device_id, scopes)
        if detail is None:
            return None
        return DeviceTelemetryResponse(device_id=device_id, sensors=detail.sensors, generated_at=self.clock())

    # ----- events ----------------------------------------------------------------------
    async def publish_changes(self) -> int:
        """Publish ``device.updated`` only for meaningful changes (deduplicated).

        Receipt timestamps (``last_seen_at``, ``last_telemetry_at``, sensor ``observed_at``)
        advance on every report, including identical polls and reads triggered by clients.
        They are excluded from the change signature; otherwise every ``GET /api/v1/system``
        published an event that made the System page fetch again (7D.1 production loop).
        Values, availability, freshness, connection, health and source-stated
        ``measured_at`` still publish; time-driven fresh→stale transitions come from ``run``.
        REST always returns current receipt timestamps.
        """
        sent = 0
        for device in self.catalog.values():
            try:
                detail = self._detail(device)
                for listener in self.snapshot_listeners:
                    try:
                        await listener(detail.model_copy(deep=True))
                    except Exception as exc:
                        logger.warning("Device snapshot listener failed error=%s", type(exc).__name__)
                signature = _change_signature(detail)
                if signature == self._signatures.get(device.id):
                    continue
                if self.publish is not None:
                    await self.publish(
                        RoomOSEvent(type="device.updated", data={"device": detail.model_dump(mode="json")}),
                        device.required_scope,
                    )
                    sent += 1
                for listener in self.change_listeners:
                    try:
                        await listener(detail.model_copy(deep=True))
                    except Exception as exc:
                        logger.warning(
                            "Device change listener failed device_id=%s error=%s",
                            device.id,
                            type(exc).__name__,
                        )
                self._signatures[device.id] = signature
            except Exception:
                logger.warning("Device view unavailable device_id=%s; monitoring unaffected", device.id)
        return sent

    async def run(self, interval_seconds: float = 15.0) -> None:
        """Publishes time-driven transitions (fresh→stale, online→unknown). No source I/O."""
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval_seconds)
            except TimeoutError:
                for tick in self.tickers:
                    try:
                        tick()  # in-memory liveness expiry only (e.g. agent heartbeat)
                    except Exception:
                        logger.warning("Device ticker failed; monitoring unaffected")
                await self.publish_changes()

    def stop(self) -> None:
        self._stop.set()


_RECEIPT_FIELDS = ("last_seen_at", "last_telemetry_at")


def _change_signature(detail: DeviceDetail) -> str:
    view = detail.model_dump(mode="json", exclude=set(_RECEIPT_FIELDS))
    for source in view["sources"]:
        source.pop("last_seen_at", None)
    for sensor in view["sensors"]:
        sensor.pop("observed_at", None)
    return json.dumps(view, sort_keys=True)


SYSTEM_SENSOR_SUFFIXES = {
    "cpu_usage": "cpu", "ram_usage": "ram", "disk_usage": "disk",
    "cpu_temperature": "temperature", "uptime": "uptime", "docker_running": "docker",
}


def _node_readings(node: SystemNode) -> dict[str, Reading]:
    readings = {}
    for sensor_id, suffix in SYSTEM_SENSOR_SUFFIXES.items():
        metric = node.metrics.get(f"{node.id}_{suffix}")
        if metric is None:
            continue
        if not metric.available:
            readings[sensor_id] = Reading(state="unavailable" if metric.freshness == "unavailable" else "unknown")
            continue
        # HA last_updated is a state-change time, not a measurement time: measured_at stays
        # unset and the monitor's own freshness verdict is carried as source_fresh.
        readings[sensor_id] = Reading(value=metric.value, unit=metric.unit, source_fresh=metric.freshness == "fresh")
    return readings
