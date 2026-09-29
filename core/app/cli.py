import argparse
import json
from datetime import UTC, datetime

from .auth import AuthStore
from .config import Settings


def iso(timestamp: int | None) -> str | None:
    return datetime.fromtimestamp(timestamp, UTC).isoformat() if timestamp else None


def main() -> int:
    parser = argparse.ArgumentParser(prog="roomos")
    commands = parser.add_subparsers(dest="command", required=True)
    pairing = commands.add_parser("pairing")
    pairing_commands = pairing.add_subparsers(dest="pairing_command", required=True)
    create = pairing_commands.add_parser("create")
    create.add_argument("--ttl-seconds", type=int)
    devices = commands.add_parser("devices")
    device_commands = devices.add_subparsers(dest="device_command", required=True)
    device_commands.add_parser("list")
    for action in ("revoke", "revoke-sessions", "disable"):
        command = device_commands.add_parser(action)
        command.add_argument("device_id")
    args = parser.parse_args()
    store = AuthStore(Settings())
    store.initialize()

    if args.command == "pairing":
        code, expires = store.create_pairing_window(args.ttl_seconds)
        print(f"Pairing code: {code}")
        print(f"Expires at: {iso(expires)}")
        print("This code is single-use and will not be shown again.")
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
