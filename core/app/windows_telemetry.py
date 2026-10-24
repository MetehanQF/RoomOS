"""Phase 7D.2: Workstation telemetry from HASS.Agent via Home Assistant.

Data path: HASS.Agent -> MQTT discovery -> Home Assistant entities -> RoomOS HA event stream.
RoomOS holds no MQTT credentials, opens no new connection and adds no polling: it consumes
``state_changed`` events it already receives, plus one bounded REST read per HA (re)connect.

HASS.Agent semantics this module is built around (verified in HASS.Agent 2.2.1 source):

* A sensor is published only when its value changes, retained by default, and without
  ``expire_after``. An unchanged battery value is never re-sent, and after a Home Assistant
  restart retained values are replayed with new HA timestamps. HA ``last_updated`` therefore
  proves neither measurement time nor agent liveness.
* Agent liveness comes from a dedicated heartbeat entity (HASS.Agent "Dummy" sensor, a new
  random value every interval). A heartbeat counts only for a live event in the current
  connection epoch whose previous state was a real value (a retained replay after restart
  starts from unknown/unavailable and is never counted).
* The agent's MQTT availability/LWT topic makes every entity ``unavailable`` when it goes
  offline. That proves the agent is disconnected, not that Windows is off: sleep, shutdown,
  hibernation, network loss and agent exit look the same here and are not distinguished.
* The Storage drive sensor's state is the volume label; disk usage lives in its attributes.

Values are fresh only while the agent heartbeat is live; otherwise the last values are kept
but marked stale (or unknown when unavailable). HASS.Agent publishes no measurement time, so
``measured_at`` stays unset. The adapter never emits ``system.*`` events (the System page
refreshes on those; 7D.1-S2) and the registry publishes only meaningful changes.
"""

import json
import logging
import math
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from .devices import BINDINGS, CATALOG, Connection, DeviceRegistry, DeviceSpec, Reading, SourceKind, SourceSpec

logger = logging.getLogger(__name__)

DEVICE_ID = "workstation"
SOURCE: SourceKind = "home_assistant"
HEARTBEAT = "heartbeat"
ENTITY_PATTERN = re.compile(r"^(sensor|binary_sensor)\.[a-z0-9_]{1,120}$")
AGENT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
NOT_A_VALUE = {None, "", "unknown", "unavailable", "none", "None"}

PERCENT = (0.0, 100.0)
TEMPERATURE = (-20.0, 130.0)
RANGES: Mapping[str, tuple[float, float]] = {
    "cpu_usage": PERCENT, "ram_usage": PERCENT, "disk_usage": PERCENT, "gpu_usage": PERCENT,
    "battery_percent": PERCENT, "cpu_temperature": TEMPERATURE, "gpu_temperature": TEMPERATURE,
}


@dataclass(frozen=True)
class WindowsAgentConfig:
    agent_id: str
    entities: Mapping[str, str]  # sensor id (or "heartbeat") -> HA entity_id, explicit allowlist
    heartbeat_seconds: int

    @property
    def binding(self) -> str:
        # HASS.Agent's own device identifier ("hass.agent-<device name>"). Renaming the agent's
        # device name changes this on purpose: it is a different identity until re-verified.
        return f"hass.agent-{self.agent_id}"


def parse_config(agent_id: str | None, entities_json: str | None, heartbeat_seconds: int,
                 *, reserved_entities: frozenset[str] = frozenset()) -> WindowsAgentConfig | None:
    """Validate operator onboarding config. Invalid config disables the adapter (fail closed)."""
    if not agent_id and not entities_json:
        return None
    try:
        if not agent_id or not AGENT_PATTERN.fullmatch(agent_id):
            raise ValueError("agent id must be a lowercase HASS.Agent device name")
        entities = json.loads(entities_json or "")
        if not isinstance(entities, dict) or not entities:
            raise ValueError("entities must be a non-empty JSON object")
        workstation = next(d for d in CATALOG if d.id == DEVICE_ID)
        allowed = {s.id for s in workstation.sensors if s.support != "unsupported"} | {HEARTBEAT}
        if HEARTBEAT not in entities:
            raise ValueError("a heartbeat entity is required for liveness")
        unknown = set(entities) - allowed
        if unknown:
            raise ValueError(f"unknown sensor ids: {sorted(unknown)}")
        values = list(entities.values())
        if len(set(values)) != len(values):
            raise ValueError("one entity cannot feed two sensors")
        for entity_id in values:
            if not isinstance(entity_id, str) or not ENTITY_PATTERN.fullmatch(entity_id):
                raise ValueError(f"invalid entity id: {entity_id!r}")
            if entity_id in reserved_entities:
                raise ValueError(f"entity already used by another RoomOS source: {entity_id}")
        return WindowsAgentConfig(agent_id, dict(entities), heartbeat_seconds)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("Windows telemetry disabled: invalid onboarding config (%s)", exc)
        return None


def configured_catalog(config: WindowsAgentConfig | None) -> tuple[tuple[DeviceSpec, ...], dict[tuple[SourceKind, str], str]]:
    """Catalog/bindings with Workstation enabled only when an explicit, valid mapping exists.

    Sensors without a mapped entity become ``unsupported`` for this deployment instead of
    waiting forever; Primary/Secondary and every other device stay exactly as catalogued.
    """
    if config is None:
        return CATALOG, dict(BINDINGS)
    catalog = []
    for device in CATALOG:
        if device.id == DEVICE_ID:
            sensors = tuple(s if s.id in config.entities else replace(s, support="unsupported", source=None)
                            for s in device.sensors)
            device = replace(device, sources=(SourceSpec(SOURCE, True),), sensors=sensors)
        catalog.append(device)
    return tuple(catalog), {**BINDINGS, (SOURCE, config.binding): DEVICE_ID}


def _number(sensor: str, raw: str) -> Reading:
    try:
        value = float(str(raw).replace(",", "."))
    except ValueError:
        return Reading(value=None)  # unparseable -> invalid_report
    low, high = RANGES.get(sensor, (-math.inf, math.inf))
    if not math.isfinite(value) or not low <= value <= high:
        return Reading(value=None)  # e.g. Windows' 255% "no battery" sentinel
    return Reading(value=round(value, 1))


# Temperatures HASS.Agent publishes as exactly 0 when the hardware sensor is not readable.
AGENT_ZERO_SENTINEL = frozenset({"cpu_temperature", "gpu_temperature"})


def _timestamp(raw: str) -> Reading:
    """HASS.Agent timestamp sensor (device_class timestamp): normalized to UTC ISO-8601."""
    try:
        value = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return Reading(value=None)
    if value.tzinfo is None:
        return Reading(value=None)  # a naive local time cannot be placed on the timeline
    return Reading(value=value.astimezone(UTC).isoformat().replace("+00:00", "Z"))


SYSTEM_DRIVE = "C"


def _finite(raw: Any) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return None
    try:
        value = float(str(raw).replace(",", "."))
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _system_drive_usage(state: Mapping[str, Any]) -> Reading:
    """HASS.Agent Storage drive sensor: the entity state is the volume label ("Windows"); the
    used percentage is only in the attributes. HASS.Agent computes it as an int-rounded
    UsedSpaceMB/TotalSizeMB without guarding TotalSizeMB == 0, so the percentage is accepted
    only for drive C: with a positive total and, when present, consistent MB figures."""
    attributes = state.get("attributes")
    if not isinstance(attributes, Mapping):
        return Reading(value=None)
    name = attributes.get("Name")
    if not isinstance(name, str) or name.strip().rstrip(":\\").upper() != SYSTEM_DRIVE:
        return Reading(value=None)  # a different drive (or no drive info) is never C:
    percent = _finite(attributes.get("UsedSpacePercentage"))
    total = _finite(attributes.get("TotalSizeMB"))
    if percent is None or total is None or total <= 0 or not 0 <= percent <= 100:
        return Reading(value=None)
    if "UsedSpaceMB" in attributes:
        used = _finite(attributes["UsedSpaceMB"])
        if used is None or not 0 <= used <= total or abs(used / total * 100 - percent) > 1:
            return Reading(value=None)
    return Reading(value=round(percent, 1))


def system_drive_free_gb(state: Mapping[str, Any] | None) -> float | None:
    """7I: real free space on C: (GB) from the same HASS.Agent attributes, validated like the
    percentage; None when the figures are missing or inconsistent (never guessed)."""
    if _system_drive_usage(state or {}).value is None:
        return None
    attributes = (state or {}).get("attributes") or {}
    available = _finite(attributes.get("AvailableSpaceMB"))
    total = _finite(attributes.get("TotalSizeMB"))
    if available is None or total is None or not 0 <= available <= total:
        return None
    return round(available / 1024, 1)


def parse_state(sensor: str, state: Mapping[str, Any] | None) -> Reading:
    raw = (state or {}).get("state")
    if state is None or raw == "unknown":
        return Reading(state="unknown")
    if raw == "unavailable":
        return Reading(state="unavailable")
    if raw in NOT_A_VALUE:
        return Reading(state="unknown")
    text = str(raw)
    if sensor == "ac_connected":  # HASS.Agent powerline_status: Online / Offline / Unknown
        return {"online": Reading(True), "offline": Reading(False)}.get(text.lower(), Reading(state="unknown"))
    if sensor == "charging":  # HASS.Agent charge_status: BatteryChargeStatus flags, e.g. "High, Charging"
        flags = {f.strip().lower() for f in text.split(",")}
        if "nosystembattery" in flags:
            return Reading(state="unavailable")
        if "unknown" in flags or not flags - {""}:
            return Reading(state="unknown")
        return Reading("charging" in flags)
    if sensor == "network_connected":
        # HASS.Agent Network sensor: one entity per adapter, state = OperationalStatus (Up/Down/...).
        return {"on": Reading(True), "off": Reading(False), "true": Reading(True), "false": Reading(False),
                "up": Reading(True), "down": Reading(False)}.get(text.lower(), Reading(value=None))
    if sensor == "last_boot":
        return Reading(value=text[:64])
    if sensor == "last_active":
        return _timestamp(text)
    if sensor in AGENT_ZERO_SENTINEL and _finite(text) == 0:
        return Reading(value=None)  # HASS.Agent reports 0 °C when it cannot read the sensor
    if sensor == "disk_usage":
        return _system_drive_usage(state)
    return _number(sensor, text)


class WindowsTelemetryAdapter:
    def __init__(self, registry: DeviceRegistry, config: WindowsAgentConfig, *,
                 monotonic: Callable[[], float] | None = None) -> None:
        self.registry = registry
        self.config = config
        self.monotonic = monotonic or time.monotonic
        self.by_entity = {entity: sensor for sensor, entity in config.entities.items()}
        self._values: dict[str, Reading] = {}  # last real (or invalid) report per sensor
        self._unavailable: set[str] = set()  # sensors currently reported "unavailable"
        self._heartbeat_mono: float | None = None
        self._agent_offline = False
        self.system_drive_free_gb: float | None = None  # 7I resource policy evidence only
        self._live = False  # True only between a successful bootstrap and the next disconnect
        self.connection: Connection = "unknown"

    @property
    def entity_ids(self) -> tuple[str, ...]:
        return tuple(self.config.entities.values())

    def handles(self, entity_id: str | None) -> bool:
        return entity_id in self.by_entity

    def _derive_connection(self) -> Connection:
        if not self._live:
            return "unknown"
        if self._agent_offline:
            return "offline"  # agent LWT/availability: disconnected, cause unknown
        if self._heartbeat_mono is not None and self.monotonic() - self._heartbeat_mono < self.config.heartbeat_seconds:
            return "online"
        return "unknown"

    def _push(self) -> None:
        self.connection = self._derive_connection()
        fresh = self.connection == "online"
        readings = {s: replace(r, source_fresh=fresh) for s, r in self._values.items()}
        for sensor in self._unavailable:
            # The agent's LWT makes every entity unavailable: then keep the last value, stale.
            # While the agent is demonstrably alive, "unavailable" is a real sensor problem.
            if fresh or sensor not in readings:
                readings[sensor] = Reading(state="unavailable")
        self.registry.ingest(SOURCE, self.config.binding, readings, connection=self.connection)

    def bootstrap(self, states: Mapping[str, Mapping[str, Any] | None]) -> None:
        """New HA connection epoch. Snapshot values (possibly retained replays) are kept but
        never fresh until a live heartbeat proves the agent is reporting now."""
        self._live = True
        self._heartbeat_mono = None
        self._values, self._unavailable = {}, set()
        for entity, sensor in self.by_entity.items():
            if sensor != HEARTBEAT:
                self._record(sensor, states.get(entity))
        heartbeat = states.get(self.config.entities[HEARTBEAT])
        self._agent_offline = (heartbeat or {}).get("state") == "unavailable"
        self._push()

    def observe(self, entity_id: str, new_state: Mapping[str, Any] | None, old_state: Mapping[str, Any] | None) -> None:
        sensor = self.by_entity.get(entity_id)
        if sensor is None or not self._live:
            return
        new_raw = (new_state or {}).get("state")
        old_raw = (old_state or {}).get("state")
        if sensor == HEARTBEAT:
            if new_raw == "unavailable":
                self._agent_offline = True
                # An explicit disconnect ends the heartbeat epoch, even if the agent
                # reconnects before the previous heartbeat would have expired.
                self._heartbeat_mono = None
            elif new_raw not in NOT_A_VALUE:
                self._agent_offline = False
                # Retained replays after an HA restart start from unknown/unavailable and do
                # not count; only a genuine change between two real values proves liveness.
                if old_state is not None and old_raw not in NOT_A_VALUE and old_raw != new_raw:
                    self._heartbeat_mono = self.monotonic()
        else:
            self._record(sensor, new_state)
        self._push()

    def _record(self, sensor: str, state: Mapping[str, Any] | None) -> None:
        if sensor == "disk_usage":
            self.system_drive_free_gb = system_drive_free_gb(state)
        reading = parse_state(sensor, state)
        if reading.state == "unavailable":
            self._unavailable.add(sensor)  # never overwrites the last real value
            return
        self._unavailable.discard(sensor)
        self._values[sensor] = reading

    def tick(self) -> None:
        """Heartbeat expiry without new events (agent silent, PC asleep/off/disconnected)."""
        if self._live and self._derive_connection() != self.connection:
            self._push()

    def reset(self) -> None:
        """HA connection lost: keep last values, but nothing about the agent is known now."""
        if self._live or self.connection != "unknown":
            self._live = False
            self._heartbeat_mono = None
            self._push()
