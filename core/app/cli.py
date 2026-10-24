import argparse
import logging
import asyncio
import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .auth import AuthStore
from .config import Settings
from .dns_health import DnsHealthMonitor
from .fcm import (
    DestinationFile,
    FcmTargetFile,
    _private_json,
    _write_private_json,
    load_fcm_runtime,
)
from .ha_client import HomeAssistantClient
from .notifications import NotificationEngine
from .production_notifications import ProductionNotificationPolicy
from .routines import RoutineEngine, evening_window_open
from .scenes import SceneEngine, install_all_off_scene
from .service import system_overview


def iso(timestamp: int | None) -> str | None:
    return datetime.fromtimestamp(timestamp, UTC).isoformat() if timestamp else None


def main() -> int:
    parser = argparse.ArgumentParser(prog="roomos")
    commands = parser.add_subparsers(dest="command", required=True)
    pairing = commands.add_parser("pairing")
    pairing_commands = pairing.add_subparsers(dest="pairing_command", required=True)
    create = pairing_commands.add_parser("create")
    create.add_argument("--ttl-seconds", type=int)
    create.add_argument(
        "--profile", choices=("web", "android-readonly"), default="web"
    )
    devices = commands.add_parser("devices")
    device_commands = devices.add_subparsers(dest="device_command", required=True)
    device_commands.add_parser("list")
    for action in ("revoke", "revoke-sessions", "disable"):
        command = device_commands.add_parser(action)
        command.add_argument("device_id")
    for action in ("add-scope", "remove-scope"):
        command = device_commands.add_parser(action)
        command.add_argument("device_id")
        command.add_argument("scope")
    notifications = commands.add_parser("notifications")
    notification_commands = notifications.add_subparsers(
        dest="notification_command", required=True
    )
    canary = notification_commands.add_parser("enqueue-canary")
    canary.add_argument("destination_id")
    activate = notification_commands.add_parser("activate-production")
    activate.add_argument("destination_id")
    push = commands.add_parser("push")
    push_commands = push.add_subparsers(dest="push_command", required=True)
    keygen = push_commands.add_parser("vapid-keygen")
    keygen.add_argument("path")
    push_commands.add_parser("status")
    push_canary = push_commands.add_parser("canary")
    push_canary.add_argument("device_id")
    automations = commands.add_parser("automations")
    automation_commands = automations.add_subparsers(
        dest="automation_command", required=True
    )
    automation_commands.add_parser("install-production")
    automation_commands.add_parser("dry-run")
    args = parser.parse_args()
    settings = Settings()

    if args.command == "push":
        if args.push_command == "vapid-keygen":
            from .push_providers import generate_vapid_private_key
            # Refuses to overwrite (O_EXCL); prints only the public key.
            print(json.dumps({"public_key": generate_vapid_private_key(args.path)}))
            return 0
        if args.push_command == "canary":
            # Runs inside the Core container; the live engine sends exactly one attempt.
            import os
            import secrets
            import urllib.error
            import urllib.request
            token = secrets.token_urlsafe(32)
            path = "/tmp/roomos-push-canary.token"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(token)
            request = urllib.request.Request(
                "http://127.0.0.1:5501/internal/push/canary", method="POST",
                data=json.dumps({"device_id": args.device_id}).encode(),
                headers={"Content-Type": "application/json", "X-RoomOS-Canary-Token": token})
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    print(response.read().decode())
                    return 0
            except urllib.error.HTTPError as exc:
                print(json.dumps({"status": exc.code, "detail": exc.read().decode()[:200]}))
                return 1
            finally:
                if os.path.exists(path):
                    os.unlink(path)
        from .push import PushEngine
        engine = PushEngine(settings.push_registry_path, settings.push_outbox_path)
        devices = [
            {"device_id": item.device_id, "enabled": item.preferences.enabled,
             "fcm": item.fcm.status if item.fcm else None, "webpush": item.webpush.status if item.webpush else None}
            for item in engine.registry.devices.values()
        ]
        counts: dict[str, int] = {}
        for message in engine.outbox.messages:
            counts[message.status] = counts.get(message.status, 0) + 1
        print(json.dumps({"mode": settings.push_mode, "storage": engine.storage_status, "devices": devices,
                          "routes": counts, "accepted_total": engine.outbox.accepted_total}, indent=2))
        return 0

    if args.command == "automations":
        if args.automation_command == "dry-run":
            timezone = ZoneInfo("Europe/Istanbul")
            cases = {
                "after_sunset": evening_window_open(
                    "below_horizon",
                    datetime(2026, 10, 7, 4, 0, tzinfo=UTC),
                    datetime(2026, 10, 6, 17, 0, tzinfo=UTC),
                    timezone,
                ),
                "before_cutoff": evening_window_open(
                    "below_horizon",
                    datetime(2026, 10, 7, 4, 0, tzinfo=UTC),
                    datetime(2026, 10, 6, 22, 45, tzinfo=UTC),
                    timezone,
                ),
                "after_cutoff": evening_window_open(
                    "below_horizon",
                    datetime(2026, 10, 7, 4, 0, tzinfo=UTC),
                    datetime(2026, 10, 6, 23, 15, tzinfo=UTC),
                    timezone,
                ),
                "before_sunset": evening_window_open(
                    "above_horizon",
                    datetime(2026, 10, 7, 4, 0, tzinfo=UTC),
                    datetime(2026, 10, 6, 12, 0, tzinfo=UTC),
                    timezone,
                ),
            }
            print(json.dumps({"dry_run": cases, "ha_writes": 0}, sort_keys=True))
            return 0

        try:
            scenes = install_all_off_scene(settings.scenes_config_path)
            scene_engine = SceneEngine(settings)
            routine_engine = RoutineEngine(settings, scene_engine, object())
            routines = asyncio.run(routine_engine.install_production_automations())
        except (OSError, RuntimeError, ValueError) as exc:
            parser.error(str(exc))
        print(
            json.dumps(
                {
                    "scenes": len(scenes.scenes),
                    "routines": [
                        {"id": routine.id, "name": routine.name, "enabled": routine.enabled}
                        for routine in routines
                    ],
                    "status": "installed",
                },
                sort_keys=True,
            )
        )
        return 0

    if args.command == "notifications":
        runtime = load_fcm_runtime(
            enabled=settings.fcm_enabled,
            credentials_path=settings.fcm_credentials_path,
            destinations_path=settings.notification_destinations_path,
            targets_path=settings.fcm_targets_path,
            timeout_seconds=settings.notifications_provider_timeout_seconds,
        )
        if runtime.status != "configured":
            parser.error("notification delivery is not safely configured")
        if args.notification_command == "enqueue-canary":
            engine = NotificationEngine(
                settings.notifications_path,
                providers=runtime.providers,
                destinations=runtime.destinations,
                queue_limit=settings.notifications_queue_limit,
                max_attempts=settings.notifications_max_attempts,
                retry_base_seconds=settings.notifications_retry_base_seconds,
                provider_timeout_seconds=settings.notifications_provider_timeout_seconds,
                configuration_status=runtime.status,
            )
            try:
                record = asyncio.run(engine.create_canary(args.destination_id))
            except (RuntimeError, ValueError) as exc:
                parser.error(str(exc))
            finally:
                asyncio.run(runtime.close())
            print(
                json.dumps(
                    {
                        "destination_id": args.destination_id,
                        "notification_id": record.id,
                        "status": "enqueued",
                    },
                    sort_keys=True,
                )
            )
            return 0

        async def activate_production() -> dict:
            destinations = _private_json(
                settings.notification_destinations_path, DestinationFile
            )
            targets = _private_json(settings.fcm_targets_path, FcmTargetFile)
            destination = next(
                (
                    item
                    for item in destinations.destinations
                    if item.id == args.destination_id
                ),
                None,
            )
            if destination is None or destination.provider != "fcm":
                raise ValueError("destination is unavailable")
            if destination.enabled:
                raise ValueError("destination is already enabled")
            target_ref = destination.provider_target_ref
            registration = targets.registrations.get(target_ref or "")
            if (
                not target_ref
                or target_ref not in targets.targets
                or registration is None
                or registration.status != "active"
            ):
                raise ValueError("destination registration is invalid")
            if not settings.ha_connect_enabled or not settings.has_ha_credentials:
                raise ValueError("canonical system source is unavailable")

            async def discard(_event):
                return None

            async def reject_intent(_intent):
                raise RuntimeError("rollout initialization cannot emit notifications")

            client = HomeAssistantClient(settings)
            dns = DnsHealthMonitor(settings, discard)
            policy = ProductionNotificationPolicy(
                settings.notification_incidents_path,
                reject_intent,
            )
            try:
                await dns.probe_now()
                snapshot = await system_overview(client, dns.snapshot())
                epoch = await policy.initialize_rollout(
                    snapshot, epoch=datetime.now(UTC)
                )
                updated = destination.model_copy(
                    update={
                        "enabled": True,
                        "activated_at": epoch,
                        "severity_policy": frozenset(
                            {"info", "warning", "critical"}
                        ),
                    }
                )
                destinations.destinations = [
                    updated if item.id == destination.id else item
                    for item in destinations.destinations
                ]
                _write_private_json(
                    settings.notification_destinations_path, destinations
                )
                return {
                    "destination_id": destination.id,
                    "rollout_epoch": epoch.isoformat(),
                    "status": "activated",
                }
            finally:
                await client.close()

        try:
            result = asyncio.run(activate_production())
        except (OSError, RuntimeError, ValueError) as exc:
            parser.error(str(exc))
        finally:
            asyncio.run(runtime.close())
        print(json.dumps(result, sort_keys=True))
        return 0

    store = AuthStore(settings)
    store.initialize()

    if args.command == "pairing":
        code, expires = store.create_pairing_window(
            args.ttl_seconds, profile=args.profile
        )
        print(f"Pairing profile: {args.profile}")
        print(f"Pairing code: {code}")
        print(f"Expires at: {iso(expires)}")
        print("This code is single-use and will not be shown again.")
        return 0

    if args.device_command in ("add-scope", "remove-scope"):
        logging.basicConfig(level=logging.INFO, format="%(message)s")  # security audit line to stderr
        try:
            changed = store.change_scope(args.device_id, args.scope, add=args.device_command == "add-scope")
        except ValueError as error:
            parser.error(str(error))
        if changed is None:
            parser.error("device not found, disabled or revoked")
        print(json.dumps({"device_id": args.device_id, "before": changed[0], "after": changed[1]}))
        return 0
    if args.device_command == "list":
        for device in store.list_devices():
            print(json.dumps({**device, "created_at": iso(device["created_at"]), "last_seen_at": iso(device["last_seen_at"]), "revoked_at": iso(device["revoked_at"])}, sort_keys=True))
        return 0
    action = {
        "revoke": store.revoke_device,
        "revoke-sessions": store.revoke_sessions,
        "disable": store.disable_device,
    }[args.device_command]
    if not action(args.device_id):
        parser.error("device not found or already revoked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
