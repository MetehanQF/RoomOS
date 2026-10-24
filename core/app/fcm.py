import asyncio
import hashlib
import json
import logging
import os
import stat
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol

import httpx
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .notifications import (
    ConfigurationStatus,
    DeliveryResult,
    NotificationDestination,
    NotificationProvider,
    NotificationRecord,
)

logger = logging.getLogger(__name__)

FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
TEMPORARY_FCM_ERRORS = {
    "INTERNAL",
    "QUOTA_EXCEEDED",
    "RESOURCE_EXHAUSTED",
    "UNAVAILABLE",
}
PERMANENT_FCM_ERRORS = {
    "INVALID_ARGUMENT",
    "NOT_FOUND",
    "PERMISSION_DENIED",
    "SENDER_ID_MISMATCH",
    "THIRD_PARTY_AUTH_ERROR",
    "UNREGISTERED",
}


class AccessTokenSource(Protocol):
    async def token(self) -> str: ...

    def invalidate(self) -> None: ...


class GoogleServiceAccountTokenSource:
    """Caches a short-lived OAuth token without exposing credential material."""

    def __init__(
        self,
        credentials_path: str,
        *,
        credentials_factory: Callable | None = None,
        request_factory: Callable[[], object] = GoogleAuthRequest,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        factory = credentials_factory or service_account.Credentials.from_service_account_file
        self._credentials = factory(credentials_path, scopes=[FCM_SCOPE])
        self.project_id = str(getattr(self._credentials, "project_id", "") or "")
        if not self.project_id:
            raise ValueError("FCM credential has no project ID")
        self._request_factory = request_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = asyncio.Lock()

    def _usable(self) -> bool:
        token = getattr(self._credentials, "token", None)
        expiry = getattr(self._credentials, "expiry", None)
        if not token or expiry is None:
            return False
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        return expiry > self._clock() + timedelta(seconds=60)

    async def token(self) -> str:
        async with self._lock:
            if not self._usable():
                await asyncio.to_thread(
                    self._credentials.refresh, self._request_factory()
                )
            token = getattr(self._credentials, "token", None)
            if not token:
                raise RuntimeError("OAuth token acquisition failed")
            return str(token)

    def invalidate(self) -> None:
        self._credentials.token = None
        self._credentials.expiry = None


class FcmTargetMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str = Field(min_length=1, max_length=80)
    registered_at: datetime
    token_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: Literal["active"] = "active"

    @field_validator("registered_at")
    @classmethod
    def registered_at_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("registration timestamp must include a timezone")
        return value.astimezone(UTC)


class FcmTargetFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1, ge=1, le=1)
    targets: dict[str, str] = Field(default_factory=dict, max_length=50)
    registrations: dict[str, FcmTargetMetadata] = Field(
        default_factory=dict, max_length=50
    )

    @model_validator(mode="after")
    def registrations_match_targets(self):
        if set(self.registrations) - set(self.targets):
            raise ValueError("FCM registration metadata has no matching target")
        return self


class DestinationFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1, ge=1, le=1)
    destinations: list[NotificationDestination] = Field(
        default_factory=list, max_length=50
    )


class FcmRegistrationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["fcm"] = "fcm"
    device_id: str
    target_ref: str
    destination_id: str
    registered_at: datetime
    token_fingerprint: str
    destination_enabled: bool
    activated_at: datetime


class FcmRuntime:
    def __init__(
        self,
        *,
        providers: Sequence[NotificationProvider] = (),
        destinations: Sequence[NotificationDestination] = (),
        status: ConfigurationStatus = "disabled",
    ) -> None:
        self.providers = tuple(providers)
        self.destinations = tuple(destinations)
        self.status = status

    async def close(self) -> None:
        for provider in self.providers:
            close = getattr(provider, "close", None)
            if close is not None:
                await close()


def _private_json(path: str, model):
    target = Path(path)
    info = target.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise ValueError("private notification file permissions are unsafe")
    return model.model_validate_json(target.read_text(encoding="utf-8"))


def _read_or_empty_private_json(path: str, model):
    target = Path(path)
    if not target.exists():
        return model()
    return _private_json(path, model)


def _write_private_json(path: str, value: BaseModel) -> None:
    target = Path(path)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    temporary = target.with_name(f".{target.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(value.model_dump_json(exclude_none=True))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        directory_fd = os.open(target.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def register_fcm_target(
    *,
    targets_path: str,
    destinations_path: str,
    device_id: str,
    display_name: str,
    registration_token: str,
    clock: Callable[[], datetime] | None = None,
) -> FcmRegistrationResult:
    """Persist one device-bound target without returning or logging its token."""

    now = (clock or (lambda: datetime.now(UTC)))().astimezone(UTC)
    stable = hashlib.sha256(device_id.encode("utf-8")).hexdigest()[:24]
    target_ref = f"android-{stable}"
    destination_id = f"fcm-{stable}"
    fingerprint = hashlib.sha256(registration_token.encode("utf-8")).hexdigest()

    targets = _read_or_empty_private_json(targets_path, FcmTargetFile)
    destinations = _read_or_empty_private_json(destinations_path, DestinationFile)

    targets.targets[target_ref] = registration_token
    targets.registrations[target_ref] = FcmTargetMetadata(
        device_id=device_id,
        registered_at=now,
        token_fingerprint=fingerprint,
    )

    existing = next(
        (item for item in destinations.destinations if item.id == destination_id),
        None,
    )
    if existing is None:
        destination = NotificationDestination(
            id=destination_id,
            display_name=f"{display_name} notifications",
            enabled=False,
            activated_at=now,
            provider="fcm",
            provider_target_ref=target_ref,
            severity_policy=frozenset({"info", "warning", "critical"}),
        )
        destinations.destinations.append(destination)
    else:
        destination = existing.model_copy(
            update={
                "display_name": f"{display_name} notifications",
                "provider": "fcm",
                "provider_target_ref": target_ref,
                "severity_policy": frozenset({"info", "warning", "critical"}),
            }
        )
        destinations.destinations = [
            destination if item.id == destination_id else item
            for item in destinations.destinations
        ]

    # Write the private token first. If the second atomic write fails, the
    # destination remains absent/disabled and therefore cannot dispatch.
    _write_private_json(targets_path, targets)
    _write_private_json(destinations_path, destinations)
    return FcmRegistrationResult(
        device_id=device_id,
        target_ref=target_ref,
        destination_id=destination_id,
        registered_at=now,
        token_fingerprint=fingerprint,
        destination_enabled=destination.enabled,
        activated_at=destination.activated_at,
    )


class FcmNotificationProvider:
    id = "fcm"

    def __init__(
        self,
        token_source: AccessTokenSource,
        project_id: str,
        targets: Mapping[str, str],
        *,
        timeout_seconds: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token_source = token_source
        self._project_id = project_id
        self._targets = dict(targets)
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds), follow_redirects=False
        )

    @property
    def endpoint(self) -> str:
        return f"https://fcm.googleapis.com/v1/projects/{self._project_id}/messages:send"

    def _payload(
        self, notification: NotificationRecord, destination: NotificationDestination
    ) -> dict:
        target_ref = destination.provider_target_ref
        if not target_ref or target_ref not in self._targets:
            raise KeyError("notification destination is not registered")
        channel = (
            "roomos_critical"
            if notification.severity == "critical"
            else "roomos_warning"
        )
        return {
            "message": {
                "token": self._targets[target_ref],
                "notification": {
                    "title": notification.title,
                    "body": notification.body,
                },
                "data": {
                    "app": "RoomOS",
                    "severity": notification.severity,
                    "title": notification.title,
                    "body": notification.body,
                    "source": notification.source,
                    "notification_id": notification.id,
                    "channel": channel,
                },
                "android": {
                    "priority": (
                        "HIGH" if notification.severity == "critical" else "NORMAL"
                    ),
                    "notification": {
                        "channel_id": channel,
                        "tag": notification.id,
                    },
                },
            }
        }

    @staticmethod
    def _error_code(response: httpx.Response) -> str | None:
        try:
            payload = response.json()
        except ValueError:
            return None
        error = payload.get("error") if isinstance(payload, dict) else None
        if not isinstance(error, dict):
            return None
        details = error.get("details")
        if isinstance(details, list):
            for detail in details:
                if isinstance(detail, dict) and isinstance(detail.get("errorCode"), str):
                    return detail["errorCode"]
        return error.get("status") if isinstance(error.get("status"), str) else None

    async def send(
        self, notification: NotificationRecord, destination: NotificationDestination
    ) -> DeliveryResult:
        try:
            payload = self._payload(notification, destination)
        except KeyError:
            return "permanent_failure"
        try:
            access_token = await self._token_source.token()
            response = await self._client.post(
                self.endpoint,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json; UTF-8",
                },
                json=payload,
            )
        except (httpx.TimeoutException, httpx.NetworkError):
            return "temporary_failure"
        except Exception as exc:
            logger.warning("FCM provider isolated error=%s", type(exc).__name__)
            return "temporary_failure"

        if response.status_code == 200:
            try:
                result = response.json()
            except ValueError:
                return "temporary_failure"
            return (
                "delivered"
                if isinstance(result, dict) and isinstance(result.get("name"), str)
                else "temporary_failure"
            )

        code = self._error_code(response)
        if response.status_code == 401 or code == "UNAUTHENTICATED":
            self._token_source.invalidate()
            return "temporary_failure"
        if code in TEMPORARY_FCM_ERRORS or response.status_code in {429, 500, 502, 503, 504}:
            return "temporary_failure"
        if code in PERMANENT_FCM_ERRORS or 400 <= response.status_code < 500:
            return "permanent_failure"
        return "temporary_failure"

    async def close(self) -> None:
        await self._client.aclose()


def load_fcm_runtime(
    *,
    enabled: bool,
    credentials_path: str,
    destinations_path: str,
    targets_path: str,
    timeout_seconds: float,
    token_source_factory: Callable[[str], GoogleServiceAccountTokenSource] | None = None,
) -> FcmRuntime:
    if not enabled:
        return FcmRuntime()
    try:
        destinations = _private_json(destinations_path, DestinationFile).destinations
        target_file = _private_json(targets_path, FcmTargetFile)
        _private_json(credentials_path, _CredentialShape)
        source_factory = token_source_factory or GoogleServiceAccountTokenSource
        token_source = source_factory(credentials_path)
        fcm_destinations = [item for item in destinations if item.provider == "fcm"]
        for item in fcm_destinations:
            if not item.provider_target_ref or item.provider_target_ref not in target_file.targets:
                raise ValueError("FCM destination target is missing")
        if not fcm_destinations:
            raise ValueError("FCM destination registry is empty")
        provider = FcmNotificationProvider(
            token_source,
            token_source.project_id,
            target_file.targets,
            timeout_seconds=timeout_seconds,
        )
        return FcmRuntime(
            providers=(provider,),
            destinations=fcm_destinations,
            status="configured",
        )
    except (OSError, ValueError, ValidationError, json.JSONDecodeError) as exc:
        logger.error("FCM configuration unavailable error=%s", type(exc).__name__)
        return FcmRuntime(status="unavailable")


class _CredentialShape(BaseModel):
    """Validates only shape; the credential implementation validates cryptography."""

    model_config = ConfigDict(extra="allow")

    type: str
    project_id: str = Field(min_length=1)
    private_key: str = Field(min_length=1)
    client_email: str = Field(min_length=1)
