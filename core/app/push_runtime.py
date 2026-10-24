"""Phase 7G runtime assembly: providers, live FCM token lookup and the one-time legacy adoption."""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from .client_telemetry import ClientBinding
from .config import Settings
from .fcm import DestinationFile, FcmTargetFile, GoogleServiceAccountTokenSource, _private_json
from .push import PushEngine
from .push_providers import FcmPushProvider, VapidKey, WebPushProvider

logger = logging.getLogger(__name__)


@dataclass
class PushRuntime:
    engine: PushEngine
    webpush_public_key: str | None = None
    fcm_available: bool = False
    closers: list[Callable] = field(default_factory=list)

    async def close(self) -> None:
        for close in self.closers:
            await close()


def live_fcm_token_lookup(targets_path: str) -> Callable[[str], str | None]:
    def lookup(target_ref: str) -> str | None:
        try:
            return _private_json(targets_path, FcmTargetFile).targets.get(target_ref)
        except (OSError, ValueError, ValidationError, json.JSONDecodeError):
            logger.warning("FCM target registry unreadable at send time")
            return None
    return lookup


def extra_hosts(settings: Settings) -> frozenset[str]:
    return frozenset(item.strip().lower() for item in settings.webpush_extra_endpoint_hosts.split(",") if item.strip())


def build_push_runtime(
    settings: Settings,
    *,
    access,
    alert_lookup,
    bindings: dict[str, ClientBinding],
    suppressed=None,
    token_source_factory: Callable[[str], GoogleServiceAccountTokenSource] | None = None,
) -> PushRuntime:
    providers: dict = {}
    closers: list[Callable] = []
    status = "configured"
    public_key: str | None = None
    fcm_ok = False
    if settings.push_mode == "smart" and settings.fcm_enabled:
        try:
            source = (token_source_factory or GoogleServiceAccountTokenSource)(settings.fcm_credentials_path)
            provider = FcmPushProvider(source, source.project_id, live_fcm_token_lookup(settings.fcm_targets_path),
                                       endpoint_base=settings.fcm_endpoint_base,
                                       timeout_seconds=settings.notifications_provider_timeout_seconds)
            providers["fcm"] = provider
            closers.append(provider.close)
            fcm_ok = True
        except (OSError, ValueError) as exc:
            logger.error("Push FCM provider unavailable error=%s", type(exc).__name__)
            status = "unavailable"
    if settings.push_mode == "smart":
        try:
            if not Path(settings.webpush_vapid_private_key_path).is_file():  # /dev/null default mount
                raise FileNotFoundError
            vapid = VapidKey.load(settings.webpush_vapid_private_key_path)
            provider = WebPushProvider(vapid, settings.webpush_vapid_subject,
                                       timeout_seconds=settings.notifications_provider_timeout_seconds)
            providers["webpush"] = provider
            closers.append(provider.close)
            public_key = vapid.public_key
        except FileNotFoundError:
            logger.info("Web Push not configured (no VAPID key)")
        except (OSError, ValueError) as exc:
            logger.error("Web Push VAPID key unusable error=%s", type(exc).__name__)
    if settings.push_mode != "smart":
        status = "disabled"
    elif not providers and status == "configured":
        status = "unavailable"
    physical = {auth_id: binding.device_id for auth_id, binding in bindings.items()}
    engine = PushEngine(
        settings.push_registry_path, settings.push_outbox_path, providers=providers, access=access,
        physical_id=lambda device_id: physical.get(device_id, device_id), alert_lookup=alert_lookup,
        suppressed=suppressed or (lambda _id, _severity: False),
        timezone=settings.push_timezone, provider_timeout_seconds=settings.notifications_provider_timeout_seconds,
        max_sleep_seconds=settings.notifications_dispatcher_max_sleep_seconds, configuration_status=status,
    )
    return PushRuntime(engine=engine, webpush_public_key=public_key, fcm_available=fcm_ok, closers=closers)


async def adopt_legacy_fcm(engine: PushEngine, settings: Settings) -> int:
    """Seed the registry from 7F FCM destinations once (enabled state preserved). Read-only on 7F files."""
    try:
        destinations = _private_json(settings.notification_destinations_path, DestinationFile).destinations
        targets = _private_json(settings.fcm_targets_path, FcmTargetFile)
    except FileNotFoundError:
        return 0
    except (OSError, ValueError, ValidationError, json.JSONDecodeError) as exc:
        logger.warning("Legacy FCM registry not adoptable error=%s", type(exc).__name__)
        return 0
    adopted = 0
    for destination in destinations:
        ref = destination.provider_target_ref
        meta = targets.registrations.get(ref or "")
        if destination.provider != "fcm" or meta is None:
            continue
        if engine.device(meta.device_id) is None:
            await engine.seed_legacy_fcm(
                device_id=meta.device_id, display_name=destination.display_name.removesuffix(" notifications"),
                target_ref=ref, token_fingerprint=meta.token_fingerprint, registered_at=meta.registered_at,
                enabled=destination.enabled and destination.activated_at is not None,
            )
            adopted += 1
    return adopted


def build_fcm_provider(settings: Settings, token_source_factory=None) -> FcmPushProvider:
    """On-demand FCM provider for the operator canary (works in legacy mode too)."""
    source = (token_source_factory or GoogleServiceAccountTokenSource)(settings.fcm_credentials_path)
    return FcmPushProvider(source, source.project_id, live_fcm_token_lookup(settings.fcm_targets_path),
                           endpoint_base=settings.fcm_endpoint_base,
                           timeout_seconds=settings.notifications_provider_timeout_seconds)
