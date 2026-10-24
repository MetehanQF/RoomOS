"""Phase 7G push delivery: per-device registry, policy and a bounded persistent outbox.

AlertEngine stays the only alarm lifecycle. This module only turns its started / escalated /
resolved transitions into per-device push routes, decides whether and when each route may be
sent, and delivers through providers (FCM, Web Push). Provider acceptance is recorded as
``accepted``; ``displayed`` / ``opened`` exist only when the receiving client reports them.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

# Only names that already existed in 7F's app.alerts: the backup tool loads this module inside a
# rolled-back 7F Core image to validate 7G state files.
from .alerts import AlertRecord, AlertSeverity

logger = logging.getLogger(__name__)

ChannelKind = Literal["fcm", "webpush"]
ChannelStatus = Literal["active", "invalid", "suspended"]
RouteStatus = Literal[
    "pending", "deferred", "accepted", "displayed", "opened", "failed", "expired",
    "superseded", "skipped", "aggregated", "invalid", "cancelled",
]
ProviderResult = Literal["accepted", "temporary_failure", "permanent_failure", "invalid_target"]
MessageTransition = Literal["started", "escalated", "resolved", "summary"]
LifecycleTransition = Literal["started", "escalated", "resolved"]

OUTBOX_LIMIT = 500
ATTEMPT_LOG_LIMIT = 8
MAX_ATTEMPTS = 8
RETRY_BASE_SECONDS = 30.0
RETRY_CAP_SECONDS = 1800.0
RATE_WINDOW_SECONDS = 600.0
RATE_LIMIT_PER_WINDOW = 6
COOLDOWN_SECONDS = 1800.0
SEND_CONCURRENCY = 4
TTL_SECONDS: dict[str, int] = {"critical": 6 * 3600, "warning": 12 * 3600, "info": 2 * 3600}
RECOVERY_TTL_SECONDS = 3600
CANARY_TITLE = "RoomOS — Test"
CANARY_BODY = "Bu bir RoomOS test bildirimidir. Gerçek bir alarm yok."
CANARY_TTL_SECONDS = 600
CANARY_GUARD_SECONDS = 600
TERMINAL: frozenset[str] = frozenset(
    {"accepted", "displayed", "opened", "failed", "expired", "superseded", "skipped",
     "aggregated", "invalid", "cancelled"}
)
ACCEPTED_LEVELS: frozenset[str] = frozenset({"accepted", "displayed", "opened"})
_HHMM = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return value.astimezone(UTC)


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ----- registry ----------------------------------------------------------------------------


class QuietHours(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    start: str = "23:00"
    end: str = "08:00"
    allow_critical: bool = True

    @field_validator("start", "end")
    @classmethod
    def hhmm(cls, value: str) -> str:
        if not _HHMM.match(value):
            raise ValueError("time must be HH:MM")
        return value


class PushPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    critical: bool = True
    warning: bool = True
    info: bool = False
    recovery: Literal["off", "critical", "all"] = "critical"
    quiet_hours: QuietHours = Field(default_factory=QuietHours)
    lock_screen_details: bool = False
    snooze_until: datetime | None = None
    updated_at: datetime | None = None

    @field_validator("snooze_until", "updated_at")
    @classmethod
    def tz_aware(cls, value):
        return _aware(value)


class PushPreferencesUpdate(BaseModel):
    """Partial update accepted from a device for itself."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    critical: bool | None = None
    warning: bool | None = None
    info: bool | None = None
    recovery: Literal["off", "critical", "all"] | None = None
    quiet_hours: QuietHours | None = None
    lock_screen_details: bool | None = None
    snooze_minutes: int | None = Field(default=None, ge=0, le=1440)


class FcmChannel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_ref: str = Field(pattern=r"^[a-z0-9._-]{1,80}$")
    token_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    registered_at: datetime
    status: ChannelStatus = "active"
    last_accepted_at: datetime | None = None
    consecutive_failures: int = Field(default=0, ge=0, le=1000)
    invalidated_at: datetime | None = None

    @field_validator("registered_at", "last_accepted_at", "invalidated_at")
    @classmethod
    def tz_aware(cls, value):
        return _aware(value)


class WebPushChannel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # endpoint/p256dh/auth are credentials for this browser; they are wiped on invalidation.
    endpoint: str | None = Field(default=None, max_length=2048)
    p256dh: str | None = Field(default=None, max_length=200)
    auth: str | None = Field(default=None, max_length=64)
    endpoint_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    endpoint_host: str = Field(min_length=1, max_length=253)
    subscribed_at: datetime
    status: ChannelStatus = "active"
    last_accepted_at: datetime | None = None
    consecutive_failures: int = Field(default=0, ge=0, le=1000)
    invalidated_at: datetime | None = None

    @field_validator("subscribed_at", "last_accepted_at", "invalidated_at")
    @classmethod
    def tz_aware(cls, value):
        return _aware(value)


class PushDevice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str = Field(min_length=1, max_length=80)
    display_name: str = Field(min_length=1, max_length=100)
    preferences: PushPreferences = Field(default_factory=PushPreferences)
    fcm: FcmChannel | None = None
    webpush: WebPushChannel | None = None
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def tz_aware(cls, value):
        return _aware(value)


class PushRegistryFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    devices: dict[str, PushDevice] = Field(default_factory=dict, max_length=50)


# ----- outbox ------------------------------------------------------------------------------


class PushAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    at: datetime
    result: ProviderResult
    code: str | None = Field(default=None, max_length=60)


class PushMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^psh_[a-f0-9]{32}$")
    dedupe_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    device_id: str = Field(min_length=1, max_length=80)
    channel: ChannelKind
    transition: MessageTransition
    alert_id: str | None = Field(default=None, max_length=80)
    alert_started_at: datetime | None = None
    severity: AlertSeverity
    title: str = Field(min_length=1, max_length=100)
    body: str = Field(min_length=1, max_length=240)
    public_title: str = Field(min_length=1, max_length=100)
    public_body: str = Field(min_length=1, max_length=240)
    path: str = Field(pattern=r"^/notifications(\?alert=[a-z0-9._-]{1,80})?$")
    created_at: datetime
    expires_at: datetime
    status: RouteStatus = "pending"
    reason: str | None = Field(default=None, max_length=60)
    attempt_count: int = Field(default=0, ge=0, le=MAX_ATTEMPTS)
    next_attempt_at: datetime | None = None
    in_flight: bool = False
    accepted_at: datetime | None = None
    displayed_at: datetime | None = None
    opened_at: datetime | None = None
    attempts: list[PushAttempt] = Field(default_factory=list, max_length=ATTEMPT_LOG_LIMIT)
    aggregated_count: int = Field(default=0, ge=0, le=OUTBOX_LIMIT)

    @field_validator("alert_started_at", "created_at", "expires_at", "next_attempt_at", "accepted_at", "displayed_at", "opened_at",)
    @classmethod
    def tz_aware(cls, value):
        return _aware(value)


class PushOutboxFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    messages: list[PushMessage] = Field(default_factory=list, max_length=OUTBOX_LIMIT)
    attempts_total: int = Field(default=0, ge=0)
    accepted_total: int = Field(default=0, ge=0)
    failed_total: int = Field(default=0, ge=0)
    last_accepted_at: datetime | None = None


class PushProvider(Protocol):
    kind: ChannelKind

    async def send(
        self, message: PushMessage, device: PushDevice, ttl_seconds: int
    ) -> tuple[ProviderResult, str | None]: ...


# ----- rendering ---------------------------------------------------------------------------

SEVERITY_LABEL = {"critical": "Kritik", "warning": "Uyarı", "info": "Bilgi"}
SOURCE_LABEL = {
    "primary": "Primary", "secondary": "Secondary", "workstation": "Workstation", "frigate": "Frigate",
    "nextcloud": "Nextcloud", "dns": "DNS", "home_assistant": "Home Assistant",
    "mqtt": "MQTT", "roomos": "RoomOS",
}
RESOURCE_LABEL = {"cpu": "CPU kullanımı", "ram": "RAM kullanımı", "gpu": "GPU kullanımı",
                  "disk": "disk doluluğu", "temperature": "sıcaklığı"}
_VALUE = re.compile(r" is (-?\d+(?:\.\d+)?)(%|°C)")


def _source_label(alert: AlertRecord) -> str:
    if alert.source in SOURCE_LABEL:
        return SOURCE_LABEL[alert.source]
    head = re.split(r" (?:is|needs|stopped|reports)\b", alert.title, maxsplit=1)[0].strip()
    return head[:40] or "RoomOS"


def _resource(alert: AlertRecord) -> str | None:
    tail = alert.id.rsplit(".", 1)[-1]
    tail = tail.removesuffix("_usage")
    return tail if tail in RESOURCE_LABEL else None


def _value(alert: AlertRecord) -> tuple[str, str] | None:
    match = _VALUE.search(alert.message)
    return (f"{float(match.group(1)):g}", match.group(2)) if match else None


def render_body(alert: AlertRecord, *, recovered: bool) -> str:
    """Turkish copy from real lifecycle data only. Unknown types fall back to the alert title."""
    label = _source_label(alert)
    kind = alert.type
    if kind == "node.offline":
        return f"{label} yeniden çevrimiçi." if recovered else f"{label} çevrimdışı."
    if kind == "device.offline":
        return f"{label} yeniden erişilebilir." if recovered else f"{label} kullanılamıyor."
    if kind == "service.offline":
        if alert.source == "home_assistant":
            return ("Home Assistant bağlantısı yeniden sağlandı." if recovered
                    else "Home Assistant bağlantısı kesildi.")
        return f"{label} servisi yeniden kullanılabilir." if recovered else f"{label} servisi kullanılamıyor."
    if kind == "service.degraded":
        if alert.source == "dns":
            return "DNS yedekliliği normale döndü." if recovered else "DNS yedekliliği azaldı."
        return f"{label} normale döndü." if recovered else f"{label} sorunlu çalışıyor."
    if kind == "resource.high":
        resource = _resource(alert)
        name = RESOURCE_LABEL.get(resource or "", "kaynak kullanımı")
        if recovered:
            return f"{label} {name} normale döndü."
        value = _value(alert)
        if resource == "disk" and value and value[1] == "%":
            if alert.source == "workstation":
                return f"Workstation: C: sürücüsü %{value[0]} dolu."
            return f"{label} diski %{value[0]} dolu."
        if resource == "temperature" and value:
            return f"{label} sıcaklığı yüksek: {value[0]}°C."
        if value and value[1] == "%":
            return f"{label} {name} %{value[0]}."
        return f"{label} {name} yüksek."
    if kind == "node.throttled":
        return f"{label} kısılması sona erdi." if recovered else f"{label} kısılma (throttling) bildiriyor."
    if kind == "telemetry.degraded":
        return f"{label} telemetrisi normale döndü." if recovered else f"{label} telemetrisi eksik veya eski."
    if kind == "routine.failed":
        return "Rutin değerlendirmesi yeniden başarılı." if recovered else "Bir rutin değerlendirmesi başarısız oldu."
    if kind == "health.degraded":
        component = alert.title.removesuffix(" needs attention").strip()[:60] or "Bir bileşen"
        return f"{component} normale döndü." if recovered else f"{component} dikkat gerektiriyor."
    if kind == "background.stopped":
        component = alert.title.removesuffix(" stopped").strip()[:60] or "Arka plan görevi"
        return f"{component} yeniden çalışıyor." if recovered else f"{component} arka plan görevi durdu."
    return (f"{alert.title} — düzeldi." if recovered else alert.title)[:240]


def render(alert: AlertRecord, transition: LifecycleTransition) -> tuple[str, str, str, str]:
    """(title, body, public_title, public_body)."""
    recovered = transition == "resolved"
    label = "Düzeldi" if recovered else SEVERITY_LABEL[alert.severity]
    title = f"RoomOS — {label}"
    return title, render_body(alert, recovered=recovered), title, "Ayrıntılar için RoomOS'u açın."


# ----- policy ------------------------------------------------------------------------------


def _parse_hhmm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def quiet_window_end(prefs: PushPreferences, now: datetime, zone: ZoneInfo) -> datetime | None:
    """End of the quiet window containing ``now`` (UTC), or None when outside it."""
    quiet = prefs.quiet_hours
    if not quiet.enabled or quiet.start == quiet.end:
        return None
    local = now.astimezone(zone)
    start, end = _parse_hhmm(quiet.start), _parse_hhmm(quiet.end)
    current = local.timetz().replace(tzinfo=None)
    if start < end:
        inside = start <= current < end
        end_day = local.date()
    else:  # crosses midnight
        inside = current >= start or current < end
        end_day = local.date() + timedelta(days=1) if current >= start else local.date()
    if not inside:
        return None
    # Build the wall-clock end in the zone (DST-safe through zoneinfo).
    return datetime.combine(end_day, end, tzinfo=zone).astimezone(UTC)


Decision = tuple[Literal["send", "defer", "skip"], datetime | str | None]


def decide(
    prefs: PushPreferences,
    *,
    transition: MessageTransition,
    severity: str,
    now: datetime,
    zone: ZoneInfo,
) -> Decision:
    if not prefs.enabled:
        return "skip", "disabled"
    critical = severity == "critical"
    if transition == "resolved":
        if prefs.recovery == "off" or (prefs.recovery == "critical" and not critical):
            return "skip", "recovery_disabled"
        if prefs.snooze_until and prefs.snooze_until > now:
            return "skip", "snoozed"
        if quiet_window_end(prefs, now, zone):
            return "skip", "quiet_hours"
        return "send", None
    if transition != "summary" and not getattr(prefs, severity):
        return "skip", "severity_filtered"
    bypass = critical and prefs.quiet_hours.allow_critical
    if prefs.snooze_until and prefs.snooze_until > now and not bypass:
        return "skip", "snoozed"
    end = quiet_window_end(prefs, now, zone)
    if end and not bypass:
        return "defer", end
    return "send", None


# ----- engine ------------------------------------------------------------------------------

AccessCheck = Callable[[str], tuple[frozenset[str], frozenset[str]] | None]
PhysicalId = Callable[[str], str]
TokenLookup = Callable[[str], str | None]


class PushSummary(BaseModel):
    status: Literal["disabled", "healthy", "degraded", "unavailable", "corrupt"]
    configured_providers: int
    configured_destinations: int
    pending_count: int
    failed_count: int
    external_attempts: int
    external_deliveries: int
    last_delivery: datetime | None


class PushEngine:
    """Registry + outbox + dispatcher. All file writes are atomic 0600 replacements."""

    def __init__(
        self,
        registry_path: str,
        outbox_path: str,
        *,
        providers: Mapping[str, PushProvider] | None = None,
        access: AccessCheck = lambda _device: None,
        physical_id: PhysicalId = lambda device: device,
        alert_lookup: Callable[[str], AlertRecord | None] = lambda _id: None,
        suppressed: Callable[[str, str], bool] = lambda _id, _severity: False,
        timezone: str = "Europe/Istanbul",
        provider_timeout_seconds: float = 10.0,
        max_sleep_seconds: float = 60.0,
        configuration_status: Literal["disabled", "configured", "unavailable"] = "configured",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.registry_path = Path(registry_path)
        self.outbox_path = Path(outbox_path)
        self.providers: dict[str, PushProvider] = dict(providers or {})
        self.access = access
        self.physical_id = physical_id
        self.alert_lookup = alert_lookup
        self.suppressed = suppressed  # 7I per-alert snooze
        self.zone = ZoneInfo(timezone)
        self.provider_timeout_seconds = provider_timeout_seconds
        self.max_sleep_seconds = max_sleep_seconds
        self.configuration_status = configuration_status
        self.clock = clock or (lambda: datetime.now(UTC))
        self.storage_status: Literal["ok", "corrupt"] = "ok"
        self.write_failed = False
        self._persist_enabled = True
        self._lock = asyncio.Lock()
        self._dispatch_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self.registry = self._load(self.registry_path, PushRegistryFile)
        self.outbox = self._load(self.outbox_path, PushOutboxFile)
        recovered = 0
        for message in self.outbox.messages:
            if message.in_flight:  # crashed mid-attempt: at-least-once retry
                message.in_flight = False
                recovered += 1
        if recovered:
            logger.info("Push outbox recovered in-flight routes=%d", recovered)

    # -- persistence --
    def _load(self, path: Path, model):
        try:
            if not path.exists():
                return model()
            return model.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, json.JSONDecodeError):
            logger.error("Push persistence is corrupt; delivery disabled file=%s", path.name)
            self.storage_status = "corrupt"
            self._persist_enabled = False
            return model()

    def _write(self, path: Path, value: BaseModel) -> None:
        if not self._persist_enabled:
            return
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(value.model_dump_json(exclude_none=True))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            self.write_failed = False
            directory_fd = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            self.write_failed = True
            logger.exception("Push persistence failed file=%s", path.name)
            temporary.unlink(missing_ok=True)

    def _persist_registry(self) -> None:
        self._write(self.registry_path, self.registry)

    def _persist_outbox(self) -> None:
        self.outbox.messages = self._bounded(self.outbox.messages)
        self._write(self.outbox_path, self.outbox)

    @staticmethod
    def _bounded(messages: list[PushMessage]) -> list[PushMessage]:
        if len(messages) <= OUTBOX_LIMIT:
            return messages
        # Drop the oldest terminal messages first; live routes are never evicted silently.
        ordered = sorted(messages, key=lambda item: item.created_at)
        excess = len(ordered) - OUTBOX_LIMIT
        kept: list[PushMessage] = []
        for item in ordered:
            if excess and item.status in TERMINAL:
                excess -= 1
                continue
            kept.append(item)
        if excess:  # still over: oldest live ones expire explicitly
            for item in kept[:excess]:
                item.status, item.reason, item.next_attempt_at = "expired", "outbox_full", None
            kept = kept[excess:]
        return kept

    # -- registry API (device-bound; device_id always comes from the session) --
    def device(self, device_id: str) -> PushDevice | None:
        item = self.registry.devices.get(device_id)
        return item.model_copy(deep=True) if item is not None else None

    def _ensure(self, device_id: str, display_name: str, now: datetime) -> PushDevice:
        item = self.registry.devices.get(device_id)
        if item is None:
            if len(self.registry.devices) >= 50:
                raise ValueError("push registry is full")
            item = PushDevice(device_id=device_id, display_name=display_name[:100] or "Device",
                              created_at=now)
            self.registry.devices[device_id] = item
        return item

    async def seed_legacy_fcm(
        self, *, device_id: str, display_name: str, target_ref: str, token_fingerprint: str,
        registered_at: datetime, enabled: bool,
    ) -> None:
        """Adopt an existing 7F FCM registration once; never overrides a device's own choice."""
        async with self._lock:
            if self.storage_status == "corrupt" or device_id in self.registry.devices:
                return
            item = self._ensure(device_id, display_name, self.clock())
            item.fcm = FcmChannel(target_ref=target_ref, token_fingerprint=token_fingerprint,
                                  registered_at=registered_at)
            item.preferences.enabled = enabled
            self._persist_registry()

    async def bind_fcm(
        self, *, device_id: str, display_name: str, target_ref: str, token_fingerprint: str,
    ) -> PushDevice:
        async with self._lock:
            now = self.clock()
            item = self._ensure(device_id, display_name, now)
            rotated = item.fcm is not None and item.fcm.token_fingerprint != token_fingerprint
            item.fcm = FcmChannel(target_ref=target_ref, token_fingerprint=token_fingerprint,
                                  registered_at=now,
                                  last_accepted_at=item.fcm.last_accepted_at if item.fcm and not rotated else None)
            self._persist_registry()
            self._wake.set()
            return item.model_copy(deep=True)

    async def subscribe_webpush(
        self, *, device_id: str, display_name: str, endpoint: str, host: str, p256dh: str, auth: str,
    ) -> PushDevice:
        async with self._lock:
            now = self.clock()
            item = self._ensure(device_id, display_name, now)
            item.webpush = WebPushChannel(endpoint=endpoint, p256dh=p256dh, auth=auth,
                                          endpoint_fingerprint=fingerprint(endpoint),
                                          endpoint_host=host, subscribed_at=now)
            self._persist_registry()
            return item.model_copy(deep=True)

    async def rotate_webpush(
        self, *, device_id: str, old_endpoint: str, endpoint: str, host: str, p256dh: str, auth: str,
    ) -> PushDevice | None:
        """Service-worker rotation: proven by knowledge of the currently registered endpoint."""
        async with self._lock:
            item = self.registry.devices.get(device_id)
            if item is None or item.webpush is None or item.webpush.endpoint_fingerprint != fingerprint(old_endpoint):
                return None
            item.webpush = WebPushChannel(endpoint=endpoint, p256dh=p256dh, auth=auth,
                                          endpoint_fingerprint=fingerprint(endpoint),
                                          endpoint_host=host, subscribed_at=self.clock())
            self._persist_registry()
            return item.model_copy(deep=True)

    async def unsubscribe_webpush(self, device_id: str) -> bool:
        async with self._lock:
            item = self.registry.devices.get(device_id)
            if item is None or item.webpush is None:
                return False
            item.webpush = None
            self._cancel_pending(device_id, "webpush", "unsubscribed")
            self._persist_registry()
            self._persist_outbox()
            return True

    async def update_preferences(self, device_id: str, display_name: str, update: PushPreferencesUpdate) -> PushDevice:
        async with self._lock:
            now = self.clock()
            item = self._ensure(device_id, display_name, now)
            prefs = item.preferences
            for name in ("enabled", "critical", "warning", "info", "recovery", "quiet_hours", "lock_screen_details"):
                value = getattr(update, name)
                if value is not None:
                    setattr(prefs, name, value)
            if update.snooze_minutes is not None:
                prefs.snooze_until = now + timedelta(minutes=update.snooze_minutes) if update.snooze_minutes else None
            prefs.updated_at = now
            if not prefs.enabled:
                self._cancel_pending(device_id, None, "disabled")
                self._persist_outbox()
            self._persist_registry()
            return item.model_copy(deep=True)

    def _cancel_pending(self, device_id: str, channel: str | None, reason: str) -> None:
        for message in self.outbox.messages:
            if message.device_id == device_id and (channel is None or message.channel == channel) \
                    and message.status in {"pending", "deferred"} and not message.in_flight:
                message.status, message.reason, message.next_attempt_at = "cancelled", reason, None

    # -- enqueue --
    def _deliverable_channel(self, item: PushDevice) -> ChannelKind | None:
        if item.fcm is not None and item.fcm.status == "active" and "fcm" in self.providers:
            return "fcm"
        if (item.webpush is not None and item.webpush.status == "active" and item.webpush.endpoint
                and "webpush" in self.providers):
            return "webpush"
        return None

    def _authorized(self, device_id: str, channel: ChannelKind) -> bool:
        access = self.access(device_id)
        if access is None:
            return False
        scopes, kinds = access
        return "activity:read" in scopes and ("android" if channel == "fcm" else "web") in kinds

    def _targets(self) -> list[tuple[PushDevice, ChannelKind]]:
        """One channel per device; Web Push is dropped for a physical device already reached natively."""
        chosen: list[tuple[PushDevice, ChannelKind]] = []
        for item in self.registry.devices.values():
            channel = self._deliverable_channel(item)
            if channel is not None:
                chosen.append((item, channel))
        native_physical = {self.physical_id(item.device_id) for item, channel in chosen
                           if channel == "fcm" and item.preferences.enabled}
        return [(item, channel) for item, channel in chosen
                if not (channel == "webpush" and self.physical_id(item.device_id) in native_physical)]

    def _dedupe(self, alert: AlertRecord, transition: str, device_id: str, channel: str) -> str:
        started = alert.started_at.astimezone(UTC).isoformat()
        return fingerprint(f"{alert.id}\0{started}\0{transition}\0{device_id}\0{channel}")

    def _accepted_opening(self, alert: AlertRecord, device_id: str) -> bool:
        started = alert.started_at.astimezone(UTC)
        return any(
            message.device_id == device_id and message.alert_id == alert.id
            and message.alert_started_at == started and message.transition in {"started", "escalated"}
            and message.status in ACCEPTED_LEVELS
            for message in self.outbox.messages
        )

    def _recently_pushed(self, alert: AlertRecord, device_id: str, now: datetime) -> bool:
        started = alert.started_at.astimezone(UTC)
        return any(
            message.device_id == device_id and message.alert_id == alert.id
            and message.transition == "started" and message.alert_started_at != started
            and message.accepted_at is not None
            and (now - message.accepted_at).total_seconds() < COOLDOWN_SECONDS
            for message in self.outbox.messages
        )

    async def handle_lifecycle(self, transition: LifecycleTransition, alert: AlertRecord) -> int:
        """AlertEngine sink. Creates at most one route per device; never sends."""
        if self.storage_status == "corrupt":
            return 0
        async with self._lock:
            now = self.clock()
            created = 0
            for item, channel in self._targets():
                key = self._dedupe(alert, transition, item.device_id, channel)
                if any(message.dedupe_key == key for message in self.outbox.messages):
                    continue  # duplicate lifecycle observation
                title, body, public_title, public_body = render(alert, transition)
                ttl = RECOVERY_TTL_SECONDS if transition == "resolved" else TTL_SECONDS[alert.severity]
                message = PushMessage(
                    id=f"psh_{uuid.uuid4().hex}", dedupe_key=key, device_id=item.device_id,
                    channel=channel, transition=transition, alert_id=alert.id,
                    alert_started_at=alert.started_at.astimezone(UTC), severity=alert.severity,
                    title=title, body=body, public_title=public_title, public_body=public_body,
                    path=f"/notifications?alert={alert.id}", created_at=now,
                    expires_at=now + timedelta(seconds=ttl),
                )
                status, detail = self._initial_status(message, item, alert, now)
                message.status = status
                if status == "deferred":
                    message.next_attempt_at = detail  # type: ignore[assignment]
                elif status == "skipped":
                    message.reason = detail  # type: ignore[assignment]
                self.outbox.messages.append(message)
                created += 1
            if created:
                self._persist_outbox()
                self._wake.set()
            return created

    def _initial_status(self, message: PushMessage, item: PushDevice, alert: AlertRecord, now: datetime):
        if message.transition in {"started", "escalated", "resolved"} and self.suppressed(alert.id, alert.severity):
            return "skipped", "snoozed_alert"
        if message.transition == "resolved" and not self._accepted_opening(alert, item.device_id):
            return "skipped", "opening_not_delivered"
        if message.transition == "started" and self._recently_pushed(alert, item.device_id, now):
            return "skipped", "cooldown"
        verdict, detail = decide(item.preferences, transition=message.transition,
                                 severity=message.severity, now=now, zone=self.zone)
        if verdict == "skip":
            return "skipped", detail
        if verdict == "defer":
            return "deferred", detail
        return "pending", None

    # -- dispatch --
    def _still_relevant(self, message: PushMessage) -> bool:
        if message.transition == "summary" or message.alert_id is None:
            return True
        alert = self.alert_lookup(message.alert_id)
        if alert is None or alert.started_at.astimezone(UTC) != message.alert_started_at:
            return False
        if message.transition == "resolved":
            return alert.status == "resolved"
        return alert.status == "active"

    def _rate_limited(self, device_id: str, now: datetime) -> bool:
        window_start = now - timedelta(seconds=RATE_WINDOW_SECONDS)
        recent = sum(1 for message in self.outbox.messages
                     if message.device_id == device_id and message.accepted_at
                     and message.accepted_at >= window_start and message.transition != "summary")
        return recent >= RATE_LIMIT_PER_WINDOW

    def _aggregate(self, message: PushMessage, now: datetime) -> None:
        bucket = int(now.timestamp() // RATE_WINDOW_SECONDS)
        key = fingerprint(f"summary\0{message.device_id}\0{message.channel}\0{bucket}")
        message.status, message.reason, message.next_attempt_at = "aggregated", "rate_limited", None
        summary = next((item for item in self.outbox.messages if item.dedupe_key == key), None)
        if summary is None:
            summary = PushMessage(
                id=f"psh_{uuid.uuid4().hex}", dedupe_key=key, device_id=message.device_id,
                channel=message.channel, transition="summary", severity=message.severity,
                title="RoomOS — Toplu bildirim", body="1 yeni RoomOS bildirimi. Ayrıntılar için dokunun.",
                public_title="RoomOS — Toplu bildirim", public_body="Ayrıntılar için RoomOS'u açın.",
                path="/notifications", created_at=now,
                expires_at=now + timedelta(seconds=TTL_SECONDS["warning"]),
                next_attempt_at=now + timedelta(seconds=RATE_WINDOW_SECONDS - now.timestamp() % RATE_WINDOW_SECONDS),
                aggregated_count=1,
            )
            self.outbox.messages.append(summary)
        elif summary.status in {"pending", "deferred"}:
            summary.aggregated_count = min(OUTBOX_LIMIT, summary.aggregated_count + 1)
            summary.body = f"{summary.aggregated_count} yeni RoomOS bildirimi. Ayrıntılar için dokunun."
            if message.severity == "critical":
                summary.severity = "critical"

    def _due(self, now: datetime) -> list[PushMessage]:
        due: list[PushMessage] = []
        for message in self.outbox.messages:
            if message.in_flight or message.status not in {"pending", "deferred"}:
                continue
            if message.next_attempt_at and message.next_attempt_at > now:
                continue
            if now >= message.expires_at:
                message.status, message.reason, message.next_attempt_at = "expired", "ttl", None
                continue
            if not self._still_relevant(message):
                message.status, message.reason, message.next_attempt_at = "superseded", "lifecycle_changed", None
                continue
            item = self.registry.devices.get(message.device_id)
            if item is None or self._deliverable_channel(item) != message.channel:
                message.status, message.reason = "cancelled", "channel_unavailable"
                continue
            if not self._authorized(message.device_id, message.channel):
                message.status, message.reason = "cancelled", "unauthorized"
                channel = item.fcm if message.channel == "fcm" else item.webpush
                if channel is not None:
                    channel.status = "suspended"
                continue
            if message.status == "deferred":
                # Re-evaluate at the end of the quiet window with current preferences.
                verdict, detail = decide(item.preferences, transition=message.transition,
                                         severity=message.severity, now=now, zone=self.zone)
                if verdict == "skip":
                    message.status, message.reason = "skipped", str(detail)
                    continue
                if verdict == "defer":
                    message.next_attempt_at = detail  # type: ignore[assignment]
                    continue
                message.status = "pending"
            if message.transition != "summary" and message.attempt_count == 0 and self._rate_limited(message.device_id, now):
                self._aggregate(message, now)
                continue
            message.in_flight = True
            due.append(message)
        return due

    async def _send_one(self, message: PushMessage, device: PushDevice, now: datetime):
        provider = self.providers[message.channel]
        ttl = max(1, int((message.expires_at - now).total_seconds()))
        try:
            return await asyncio.wait_for(provider.send(message, device, ttl), timeout=self.provider_timeout_seconds)
        except TimeoutError:
            return "temporary_failure", "timeout"
        except Exception as exc:
            logger.warning("Push provider isolated error=%s channel=%s", type(exc).__name__, message.channel)
            return "temporary_failure", type(exc).__name__[:60]

    def _apply(self, message: PushMessage, result: ProviderResult, code: str | None, now: datetime) -> None:
        message.in_flight = False
        message.attempt_count = min(MAX_ATTEMPTS, message.attempt_count + 1)
        message.attempts = (message.attempts + [PushAttempt(at=now, result=result, code=code)])[-ATTEMPT_LOG_LIMIT:]
        self.outbox.attempts_total += 1
        item = self.registry.devices.get(message.device_id)
        channel = None
        if item is not None:
            channel = item.fcm if message.channel == "fcm" else item.webpush
        if result == "accepted":
            message.status, message.accepted_at, message.next_attempt_at = "accepted", now, None
            self.outbox.accepted_total += 1
            self.outbox.last_accepted_at = now
            if channel is not None:
                channel.last_accepted_at, channel.consecutive_failures = now, 0
            return
        if channel is not None:
            channel.consecutive_failures = min(1000, channel.consecutive_failures + 1)
        if result == "invalid_target":
            message.status, message.reason, message.next_attempt_at = "invalid", code or "invalid_target", None
            self.outbox.failed_total += 1
            if channel is not None:
                channel.status, channel.invalidated_at = "invalid", now
                if isinstance(channel, WebPushChannel):
                    channel.endpoint = channel.p256dh = channel.auth = None
            self._cancel_pending(message.device_id, message.channel, "channel_invalid")
            return
        if result == "permanent_failure" or message.attempt_count >= MAX_ATTEMPTS:
            message.status, message.reason, message.next_attempt_at = "failed", code or result, None
            self.outbox.failed_total += 1
            return
        delay = min(RETRY_BASE_SECONDS * 2 ** (message.attempt_count - 1), RETRY_CAP_SECONDS)
        retry_at = now + timedelta(seconds=delay)
        if retry_at >= message.expires_at:
            message.status, message.reason, message.next_attempt_at = "expired", "ttl", None
            return
        message.next_attempt_at = retry_at

    async def dispatch_pending(self) -> int:
        if self.storage_status == "corrupt" or not self.providers:
            return 0
        async with self._dispatch_lock:
            async with self._lock:
                now = self.clock()
                due = self._due(now)
                devices = {message.device_id: self.registry.devices[message.device_id].model_copy(deep=True)
                           for message in due}
                snapshots = [message.model_copy(deep=True) for message in due]
                # Durable in-flight marker before any network I/O (restart ⇒ retry, not loss).
                self._persist_outbox()
                self._persist_registry()
            if not due:
                return 0
            semaphore = asyncio.Semaphore(SEND_CONCURRENCY)

            async def bounded(snapshot: PushMessage):
                async with semaphore:
                    return await self._send_one(snapshot, devices[snapshot.device_id], now)

            results = await asyncio.gather(*(bounded(item) for item in snapshots))
            async with self._lock:
                finished = self.clock()
                by_id = {message.id: message for message in self.outbox.messages}
                for snapshot, (result, code) in zip(snapshots, results, strict=True):
                    live = by_id.get(snapshot.id)
                    if live is not None:
                        self._apply(live, result, code, finished)
                self._persist_outbox()
                self._persist_registry()
            return len(due)

    async def send_canary(self, device_id: str, provider) -> PushMessage:
        """Operator test: one informational message to one native device, exactly one attempt.

        No alert lifecycle is created or touched (transition "summary", no alert id), preferences are
        not consulted (explicit operator action), and a temporary failure is final, never retried.
        The message lives in this engine's outbox so the phone's displayed/opened receipts land on it.
        """
        async with self._lock:
            if self.storage_status == "corrupt":
                raise ValueError("push persistence is unavailable")
            now = self.clock()
            item = self.registry.devices.get(device_id)
            if item is None or item.fcm is None or item.fcm.status != "active":
                raise ValueError("device has no active native push registration")
            if not self._authorized(device_id, "fcm"):
                raise ValueError("device is not authorized to receive alert content")
            recent = now - timedelta(seconds=CANARY_GUARD_SECONDS)
            if any(m.device_id == device_id and m.title == CANARY_TITLE and m.created_at >= recent
                   for m in self.outbox.messages):
                raise ValueError("a canary was already sent to this device in the last 10 minutes")
            message = PushMessage(
                id=f"psh_{uuid.uuid4().hex}",
                dedupe_key=fingerprint(f"operator-canary\0{device_id}\0{now.isoformat()}\0{uuid.uuid4().hex}"),
                device_id=device_id, channel="fcm", transition="summary", severity="warning",
                title=CANARY_TITLE, body=CANARY_BODY, public_title=CANARY_TITLE,
                public_body="Ayrıntılar için RoomOS'u açın.", path="/notifications", created_at=now,
                expires_at=now + timedelta(seconds=CANARY_TTL_SECONDS), in_flight=True,
            )
            self.outbox.messages.append(message)
            device = item.model_copy(deep=True)
            snapshot = message.model_copy(deep=True)
            self._persist_outbox()
        try:
            result, code = await asyncio.wait_for(
                provider.send(snapshot, device, CANARY_TTL_SECONDS, high_priority=True),
                timeout=self.provider_timeout_seconds)
        except TimeoutError:
            result, code = "temporary_failure", "timeout"
        except Exception as exc:
            result, code = "temporary_failure", type(exc).__name__[:60]
        async with self._lock:
            live = next(m for m in self.outbox.messages if m.id == snapshot.id)
            self._apply(live, result, code, self.clock())
            if live.status in {"pending", "deferred"}:  # single attempt: never retried
                live.status, live.reason, live.next_attempt_at = "failed", f"canary_{code or 'failed'}"[:60], None
                self.outbox.failed_total += 1
            self._persist_outbox()
            self._persist_registry()
            return live.model_copy(deep=True)

    async def record_receipt(self, device_id: str, message_id: str, event: Literal["displayed", "opened"]) -> bool:
        async with self._lock:
            message = next((item for item in self.outbox.messages
                            if item.id == message_id and item.device_id == device_id), None)
            if message is None or message.accepted_at is None:
                return False
            now = self.clock()
            if event == "displayed" and message.displayed_at is None:
                message.displayed_at = now
                if message.status == "accepted":
                    message.status = "displayed"
            if event == "opened" and message.opened_at is None:
                message.opened_at = now
                message.displayed_at = message.displayed_at or now
                message.status = "opened"
            self._persist_outbox()
            return True

    def _next_delay(self) -> float:
        now = self.clock()
        upcoming = [message.next_attempt_at or now for message in self.outbox.messages
                    if message.status in {"pending", "deferred"} and not message.in_flight]
        if not upcoming:
            return self.max_sleep_seconds
        return min(max(0.0, (min(upcoming) - now).total_seconds()), self.max_sleep_seconds)

    @property
    def delivery_enabled(self) -> bool:
        return self.storage_status == "ok" and self.configuration_status == "configured" and bool(self.providers)

    async def dispatcher_loop(self) -> None:
        if not self.delivery_enabled:
            return
        while not self._stop.is_set():
            self._wake.clear()
            try:
                await self.dispatch_pending()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Push dispatcher isolated error=%s", type(exc).__name__)
            wake = asyncio.create_task(self._wake.wait())
            stop = asyncio.create_task(self._stop.wait())
            done, pending = await asyncio.wait((wake, stop), timeout=self._next_delay(),
                                               return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if stop in done:
                return

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    # -- health / API views --
    @property
    def destinations(self) -> list[str]:
        return [item.device_id for item, _channel in self._targets() if item.preferences.enabled]

    def summary(self) -> PushSummary:
        pending = sum(1 for item in self.outbox.messages if item.status in {"pending", "deferred"})
        failed = sum(1 for item in self.outbox.messages if item.status in {"failed", "invalid"})
        if self.storage_status == "corrupt":
            status = "corrupt"
        elif self.configuration_status == "unavailable":
            status = "unavailable"
        elif not self.delivery_enabled or not self.destinations:
            status = "disabled"
        elif failed or any(item.attempt_count and item.status == "pending" for item in self.outbox.messages):
            status = "degraded"
        else:
            status = "healthy"
        return PushSummary(
            status=status, configured_providers=len(self.providers),
            configured_destinations=len(self.destinations), pending_count=pending, failed_count=failed,
            external_attempts=self.outbox.attempts_total, external_deliveries=self.outbox.accepted_total,
            last_delivery=self.outbox.last_accepted_at,
        )

    def recent_for_device(self, device_id: str, limit: int = 10) -> list[PushMessage]:
        mine = [item for item in self.outbox.messages if item.device_id == device_id]
        return [item.model_copy(deep=True) for item in sorted(mine, key=lambda m: m.created_at)[-limit:]]


IntentSink = Callable[[object], Awaitable[object]]


async def shadow_intent_sink(_intent: object) -> bool:
    """Smart mode keeps the 7F incident policy evaluating (rollback continuity) but routes
    nothing through the legacy outbox; AlertEngine lifecycles drive delivery instead."""
    return True


# ----- API views (never contain tokens, endpoints or key material) ---------------------------


class WebPushSubscriptionKeys(BaseModel):
    model_config = ConfigDict(extra="forbid")

    p256dh: str = Field(min_length=80, max_length=100, pattern=r"^[A-Za-z0-9_-]+=*$")
    auth: str = Field(min_length=16, max_length=32, pattern=r"^[A-Za-z0-9_-]+=*$")


class WebPushSubscriptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    endpoint: str = Field(min_length=12, max_length=2048)
    keys: WebPushSubscriptionKeys
    expiration_time: int | None = Field(default=None, alias="expirationTime")


class WebPushRotateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    old_endpoint: str = Field(min_length=12, max_length=2048)
    subscription: WebPushSubscriptionRequest


class PushReceiptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(pattern=r"^psh_[a-f0-9]{32}$")
    event: Literal["displayed", "opened"]


class ChannelView(BaseModel):
    kind: ChannelKind
    status: ChannelStatus
    since: datetime
    last_accepted_at: datetime | None = None
    endpoint_host: str | None = None


class DeliveryView(BaseModel):
    id: str
    transition: MessageTransition
    severity: AlertSeverity
    status: RouteStatus
    reason: str | None = None
    created_at: datetime
    accepted_at: datetime | None = None
    displayed_at: datetime | None = None
    opened_at: datetime | None = None


class PushCapabilities(BaseModel):
    mode: Literal["legacy", "smart", "off"]
    delivery_active: bool
    webpush_available: bool
    webpush_public_key: str | None = None
    fcm_available: bool
    timezone: str


class PushSettingsResponse(BaseModel):
    device_id: str
    display_name: str
    session_kind: str | None
    can_manage: bool
    can_receive: bool
    preferences: PushPreferences
    channels: list[ChannelView]
    recent: list[DeliveryView]
    capabilities: PushCapabilities


def settings_view(engine: PushEngine, *, device_id: str, display_name: str, session_kind: str | None,
                  can_manage: bool, can_receive: bool, capabilities: PushCapabilities) -> PushSettingsResponse:
    item = engine.device(device_id)
    channels: list[ChannelView] = []
    if item is not None and item.fcm is not None:
        channels.append(ChannelView(kind="fcm", status=item.fcm.status, since=item.fcm.registered_at,
                                    last_accepted_at=item.fcm.last_accepted_at))
    if item is not None and item.webpush is not None:
        channels.append(ChannelView(kind="webpush", status=item.webpush.status, since=item.webpush.subscribed_at,
                                    last_accepted_at=item.webpush.last_accepted_at,
                                    endpoint_host=item.webpush.endpoint_host))
    recent = [DeliveryView(**message.model_dump(include=set(DeliveryView.model_fields)))
              for message in engine.recent_for_device(device_id)]
    return PushSettingsResponse(
        device_id=device_id, display_name=item.display_name if item else display_name,
        session_kind=session_kind, can_manage=can_manage, can_receive=can_receive,
        preferences=item.preferences if item else PushPreferences(), channels=channels,
        recent=recent, capabilities=capabilities,
    )
