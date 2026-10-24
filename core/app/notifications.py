import asyncio
import hashlib
import json
import logging
import os
import re
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .alerts import AlertRecord, AlertSeverity

logger = logging.getLogger(__name__)

DeliveryResult = Literal["delivered", "temporary_failure", "permanent_failure"]
DeliveryStatus = Literal["pending", "delivered", "failed"]
ConfigurationStatus = Literal["disabled", "configured", "unavailable"]
ProductionEventType = Literal[
    "node.offline",
    "node.recovered",
    "service.down",
    "service.recovered",
    "temperature.high",
    "temperature.recovered",
]


class NotificationDestination(BaseModel):
    """Logical future delivery target; it deliberately contains no provider token."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9._-]{1,80}$")
    display_name: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    activated_at: datetime | None = None
    provider: str | None = Field(default=None, pattern=r"^[a-z0-9._-]{1,80}$")
    provider_target_ref: str | None = Field(
        default=None, pattern=r"^[a-z0-9._-]{1,80}$"
    )
    severity_policy: frozenset[AlertSeverity] = Field(
        default_factory=lambda: frozenset({"warning", "critical"}),
        min_length=1,
        max_length=3,
    )

    @field_validator("activated_at")
    @classmethod
    def activation_must_be_timezone_aware(
        cls, value: datetime | None
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("destination activation must include a timezone")
        return value.astimezone(UTC)


class NotificationDelivery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str = Field(pattern=r"^[a-z0-9._-]{1,80}$")
    destination_id: str = Field(pattern=r"^[a-z0-9._-]{1,80}$")
    status: DeliveryStatus = "pending"
    attempt_count: int = Field(default=0, ge=0, le=100)
    next_attempt_at: datetime | None = None
    last_attempt_at: datetime | None = None
    delivered_at: datetime | None = None


class NotificationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^ntf_[a-f0-9]{32}$")
    event_type: Literal[
        "alert.started",
        "system.canary",
        "node.offline",
        "node.recovered",
        "service.down",
        "service.recovered",
        "temperature.high",
        "temperature.recovered",
    ] = "alert.started"
    severity: AlertSeverity
    title: str = Field(min_length=1, max_length=100)
    body: str = Field(min_length=1, max_length=240)
    source: str = Field(min_length=1, max_length=40)
    created_at: datetime
    dedupe_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    deliveries: list[NotificationDelivery] = Field(default_factory=list, max_length=50)


class NotificationStateFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    notifications: list[NotificationRecord] = Field(default_factory=list, max_length=1000)
    external_attempts: int = Field(default=0, ge=0)
    external_deliveries: int = Field(default=0, ge=0)
    last_delivery: datetime | None = None


class ProductionNotificationIntent(BaseModel):
    """Server-owned event intent; no public API accepts this model."""

    model_config = ConfigDict(extra="forbid")

    event_type: ProductionEventType
    severity: AlertSeverity
    subject: Literal[
        "node.primary",
        "node.secondary",
        "service.frigate",
        "service.nextcloud",
        "service.dns",
        "temperature.primary",
        "temperature.secondary",
    ]
    incident_id: str = Field(pattern=r"^inc_[a-f0-9]{32}$")
    title: Literal["RoomOS"] = "RoomOS"
    body: str = Field(min_length=1, max_length=240)
    source: Literal["primary", "secondary", "frigate", "nextcloud", "dns"]
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("production intent timestamp must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def matches_server_catalog(self):
        expected = {
            "node.offline": "critical",
            "node.recovered": "info",
            "service.down": "warning",
            "service.recovered": "info",
            "temperature.high": "warning",
            "temperature.recovered": "info",
        }[self.event_type]
        if self.severity != expected:
            raise ValueError("production event severity does not match the server catalog")
        kind, logical_id = self.subject.split(".", 1)
        expected_kind = self.event_type.split(".", 1)[0]
        if kind != expected_kind or self.source != logical_id:
            raise ValueError("production intent subject does not match the event catalog")
        recovered = self.event_type.endswith(".recovered")
        if kind == "node":
            label = {"primary": "Primary", "secondary": "Secondary"}[logical_id]
            expected_body = (
                f"{label} tekrar çevrimiçi." if recovered else f"{label} çevrimdışı."
            )
            if self.body != expected_body:
                raise ValueError("production node payload does not match its template")
        elif kind == "service":
            label = {
                "frigate": "Frigate",
                "nextcloud": "Nextcloud",
                "dns": "DNS",
            }[logical_id]
            expected_body = (
                f"{label} servisi tekrar kullanılabilir."
                if recovered
                else f"{label} servisi kullanılamıyor."
            )
            if self.body != expected_body:
                raise ValueError("production service payload does not match its template")
        else:
            label = {"primary": "Primary", "secondary": "Secondary"}[logical_id]
            prefix = (
                f"{label} sıcaklığı normale döndü: "
                if recovered
                else f"{label} sıcaklığı yüksek: "
            )
            match = re.fullmatch(re.escape(prefix) + r"(-?\d+(?:\.\d+)?)°C\.", self.body)
            if match is None or not -20.0 <= float(match.group(1)) <= 125.0:
                raise ValueError(
                    "production temperature payload does not match its template"
                )
        return self


class NotificationSummary(BaseModel):
    status: Literal["disabled", "healthy", "degraded", "unavailable", "corrupt"]
    configured_providers: int
    configured_destinations: int
    pending_count: int
    failed_count: int
    external_attempts: int
    external_deliveries: int
    last_delivery: datetime | None


class NotificationProvider(Protocol):
    id: str

    async def send(
        self, notification: NotificationRecord, destination: NotificationDestination
    ) -> DeliveryResult: ...


class NotificationPolicy:
    """Pure candidate policy; delivery routing and quiet hours remain separate concerns."""

    def __init__(self, severities: frozenset[AlertSeverity] | None = None) -> None:
        self.severities = severities or frozenset({"warning", "critical"})

    def accepts(
        self,
        alert: AlertRecord,
        *,
        destination: NotificationDestination | None = None,
        at: datetime | None = None,
    ) -> bool:
        # Destination and time are explicit extension points for future routing/quiet hours.
        del destination, at
        return alert.status == "active" and alert.severity in self.severities


def _dedupe_key(alert: AlertRecord) -> str:
    lifecycle = f"{alert.id}\0{alert.started_at.astimezone(UTC).isoformat()}"
    return hashlib.sha256(lifecycle.encode("utf-8")).hexdigest()


class NotificationEngine:
    def __init__(
        self,
        path: str,
        *,
        providers: Sequence[NotificationProvider] = (),
        destinations: Sequence[NotificationDestination] = (),
        policy: NotificationPolicy | None = None,
        queue_limit: int = 200,
        max_attempts: int = 3,
        retry_base_seconds: float = 30.0,
        provider_timeout_seconds: float = 10.0,
        configuration_status: ConfigurationStatus = "configured",
        dispatcher_max_sleep_seconds: float = 60.0,
        clock=None,
    ) -> None:
        self.path = Path(path)
        self.providers = {provider.id: provider for provider in providers}
        # A destination is routable only after an explicit, persisted activation
        # boundary. Enabled destinations without one remain safely inert.
        self.destinations = {
            item.id: item
            for item in destinations
            if item.enabled and item.activated_at is not None
        }
        self.policy = policy or NotificationPolicy()
        self.queue_limit = queue_limit
        self.max_attempts = max_attempts
        self.retry_base_seconds = retry_base_seconds
        self.provider_timeout_seconds = provider_timeout_seconds
        self.configuration_status = configuration_status
        self.dispatcher_max_sleep_seconds = dispatcher_max_sleep_seconds
        self.clock = clock or (lambda: datetime.now(UTC))
        self.storage_status: Literal["ok", "corrupt"] = "ok"
        self._persist_enabled = True
        self.write_failed = False
        self._lock = asyncio.Lock()
        self._dispatch_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._state = self._load()

    def _load(self) -> NotificationStateFile:
        try:
            if not self.path.exists():
                return NotificationStateFile()
            return NotificationStateFile.model_validate_json(
                self.path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError, json.JSONDecodeError):
            logger.error("RoomOS notification persistence is corrupt; delivery disabled")
            self.storage_status = "corrupt"
            self._persist_enabled = False
            return NotificationStateFile()

    def _persist(self) -> None:
        if not self._persist_enabled:
            return
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
            directory_fd = os.open(self.path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            self.write_failed = True
            logger.exception("RoomOS notification persistence failed")
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _ensure_delivery_routes(self, notification: NotificationRecord) -> None:
        # Canary routes are created explicitly for exactly one destination.
        if notification.event_type == "system.canary":
            return
        existing = {
            (item.provider_id, item.destination_id) for item in notification.deliveries
        }
        for provider_id in sorted(self.providers):
            for destination_id in sorted(self.destinations):
                destination = self.destinations[destination_id]
                if notification.created_at.astimezone(UTC) < destination.activated_at:
                    continue
                if destination.provider not in {None, provider_id}:
                    continue
                if notification.severity not in destination.severity_policy:
                    continue
                if (provider_id, destination_id) not in existing:
                    notification.deliveries.append(
                        NotificationDelivery(
                            provider_id=provider_id,
                            destination_id=destination_id,
                        )
                    )

    async def create_canary(self, destination_id: str) -> NotificationRecord:
        """Enqueue one fixed safe canary for one explicitly activated destination.

        This is an operator-maintenance primitive, not a user-facing send API. It
        accepts no title/body input and performs no network delivery itself.
        """

        async with self._lock:
            if self.storage_status == "corrupt":
                raise RuntimeError("notification persistence is unavailable")
            destination = self.destinations.get(destination_id)
            if destination is None or destination.activated_at is None:
                raise ValueError("destination is not explicitly activated")
            provider_id = destination.provider
            if provider_id is None or provider_id not in self.providers:
                raise ValueError("destination provider is unavailable")
            now = self.clock().astimezone(UTC)
            if now < destination.activated_at:
                raise ValueError("destination activation is in the future")
            nonce = uuid.uuid4().hex
            notification = NotificationRecord(
                id=f"ntf_{uuid.uuid4().hex}",
                event_type="system.canary",
                severity="warning",
                title="RoomOS notification test",
                body="This is a one-time RoomOS delivery canary.",
                source="roomos",
                created_at=now,
                dedupe_key=hashlib.sha256(
                    f"canary\0{destination_id}\0{now.isoformat()}\0{nonce}".encode(
                        "utf-8"
                    )
                ).hexdigest(),
                deliveries=[
                    NotificationDelivery(
                        provider_id=provider_id,
                        destination_id=destination_id,
                    )
                ],
            )
            self._state.notifications.append(notification)
            self._state.notifications = sorted(
                self._state.notifications, key=lambda item: item.created_at
            )[-self.queue_limit :]
            self._persist()
            self._wake.set()
            logger.info(
                "RoomOS notification canary enqueued destination=%s notification=%s",
                destination_id,
                notification.id,
            )
            return notification.model_copy(deep=True)

    async def handle_alert_started(self, alert: AlertRecord) -> NotificationRecord | None:
        """Create one candidate for one alert lifecycle; it never performs delivery."""

        if not self.policy.accepts(alert, at=self.clock()):
            return None
        async with self._lock:
            key = _dedupe_key(alert)
            existing = next(
                (item for item in self._state.notifications if item.dedupe_key == key), None
            )
            if existing is not None:
                return existing.model_copy(deep=True)
            if self.storage_status == "corrupt":
                return None
            notification = NotificationRecord(
                id=f"ntf_{uuid.uuid4().hex}",
                severity=alert.severity,
                title=alert.title,
                body=alert.message,
                source=alert.source,
                created_at=self.clock(),
                dedupe_key=key,
            )
            self._ensure_delivery_routes(notification)
            self._state.notifications.append(notification)
            self._state.notifications = sorted(
                self._state.notifications, key=lambda item: item.created_at
            )[-self.queue_limit :]
            self._persist()
            self._wake.set()
            return notification.model_copy(deep=True)

    async def handle_production_intent(
        self, intent: ProductionNotificationIntent
    ) -> NotificationRecord | None:
        """Persist one server-derived production intent without sending it."""

        async with self._lock:
            if self.storage_status == "corrupt":
                return None
            key = hashlib.sha256(
                (
                    f"production\0{intent.event_type}\0{intent.subject}\0"
                    f"{intent.incident_id}"
                ).encode("utf-8")
            ).hexdigest()
            existing = next(
                (item for item in self._state.notifications if item.dedupe_key == key),
                None,
            )
            if existing is not None:
                return existing.model_copy(deep=True)
            notification = NotificationRecord(
                id=f"ntf_{uuid.uuid4().hex}",
                event_type=intent.event_type,
                severity=intent.severity,
                title=intent.title,
                body=intent.body,
                source=intent.source,
                created_at=intent.occurred_at.astimezone(UTC),
                dedupe_key=key,
            )
            self._ensure_delivery_routes(notification)
            self._state.notifications.append(notification)
            self._state.notifications = sorted(
                self._state.notifications, key=lambda item: item.created_at
            )[-self.queue_limit :]
            self._persist()
            self._wake.set()
            return notification.model_copy(deep=True)

    def notifications(self) -> list[NotificationRecord]:
        return [item.model_copy(deep=True) for item in self._state.notifications]

    def summary(self) -> NotificationSummary:
        pending = 0
        failed = 0
        for item in self._state.notifications:
            if not item.deliveries or any(delivery.status == "pending" for delivery in item.deliveries):
                pending += 1
            failed += sum(delivery.status == "failed" for delivery in item.deliveries)
        configured = bool(self.providers and self.destinations)
        status: Literal["disabled", "healthy", "degraded", "unavailable", "corrupt"]
        if self.storage_status == "corrupt":
            status = "corrupt"
        elif self.configuration_status == "unavailable":
            status = "unavailable"
        elif not configured:
            status = "disabled"
        elif failed or any(
            delivery.status == "pending" and delivery.attempt_count > 0
            for item in self._state.notifications
            for delivery in item.deliveries
        ):
            status = "degraded"
        else:
            status = "healthy"
        return NotificationSummary(
            status=status,
            configured_providers=len(self.providers),
            configured_destinations=len(self.destinations),
            pending_count=pending,
            failed_count=failed,
            external_attempts=self._state.external_attempts,
            external_deliveries=self._state.external_deliveries,
            last_delivery=self._state.last_delivery,
        )

    async def dispatch_pending(self) -> int:
        """Bounded explicit dispatcher hook; production has no providers and never calls it."""

        if self.storage_status == "corrupt" or not self.providers or not self.destinations:
            return 0
        attempted = 0
        async with self._dispatch_lock:
            async with self._lock:
                now = self.clock()
                for notification in self._state.notifications:
                    self._ensure_delivery_routes(notification)
                    for delivery in notification.deliveries:
                        if delivery.status != "pending":
                            continue
                        if delivery.next_attempt_at and delivery.next_attempt_at > now:
                            continue
                        provider = self.providers.get(delivery.provider_id)
                        destination = self.destinations.get(delivery.destination_id)
                        if provider is None or destination is None:
                            continue
                        attempted += 1
                        delivery.attempt_count += 1
                        delivery.last_attempt_at = now
                        self._state.external_attempts += 1
                        try:
                            result = await asyncio.wait_for(
                                provider.send(notification, destination),
                                timeout=self.provider_timeout_seconds,
                            )
                        except Exception as exc:
                            logger.warning(
                                "Notification provider failed provider=%s error=%s",
                                delivery.provider_id,
                                type(exc).__name__,
                            )
                            result = "temporary_failure"
                        if result == "delivered":
                            delivery.status = "delivered"
                            delivery.delivered_at = now
                            delivery.next_attempt_at = None
                            self._state.external_deliveries += 1
                            self._state.last_delivery = now
                        elif result == "permanent_failure" or delivery.attempt_count >= self.max_attempts:
                            delivery.status = "failed"
                            delivery.next_attempt_at = None
                        else:
                            delay = min(
                                self.retry_base_seconds * 2 ** (delivery.attempt_count - 1),
                                3600.0,
                            )
                            delivery.next_attempt_at = now + timedelta(seconds=delay)
                if attempted:
                    self._persist()
        return attempted

    @property
    def delivery_enabled(self) -> bool:
        return (
            self.storage_status == "ok"
            and self.configuration_status == "configured"
            and bool(self.providers)
            and bool(self.destinations)
        )

    def _next_dispatch_delay(self) -> float:
        now = self.clock()
        due: list[datetime] = []
        has_pending = False
        for notification in self._state.notifications:
            self._ensure_delivery_routes(notification)
            for delivery in notification.deliveries:
                if delivery.status != "pending":
                    continue
                has_pending = True
                if delivery.next_attempt_at is None:
                    return 0.0
                due.append(delivery.next_attempt_at)
        if not has_pending:
            return self.dispatcher_max_sleep_seconds
        return min(
            max(0.0, (min(due) - now).total_seconds()),
            self.dispatcher_max_sleep_seconds,
        )

    async def dispatcher_loop(self) -> None:
        """One restart-safe dispatcher; never started without a complete configuration."""

        if not self.delivery_enabled:
            return
        while not self._stop.is_set():
            self._wake.clear()
            try:
                await self.dispatch_pending()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Notification dispatcher isolated error=%s", type(exc).__name__)
            delay = self._next_dispatch_delay()
            wake_task = asyncio.create_task(self._wake.wait())
            stop_task = asyncio.create_task(self._stop.wait())
            done, pending = await asyncio.wait(
                (wake_task, stop_task),
                timeout=delay,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if stop_task in done and stop_task.result():
                return

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
