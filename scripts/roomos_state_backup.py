#!/usr/bin/env python3
"""Create, restore, and verify encrypted RoomOS full-state backups.

The archive format is intentionally small and self-contained: an AES-256-GCM
envelope protects a deterministic tar stream containing manifest.json and the
allowlisted RoomOS state files. Plaintext archive bytes exist only in memory;
the transactionally copied SQLite database exists briefly under a caller-
selected tmpfs.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import secrets
import shutil
import sqlite3
import stat
import struct
import sys
import tarfile
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError as exc:  # pragma: no cover - operator environment gate
    raise SystemExit("cryptography is required; use the RoomOS Core Python environment") from exc


FORMAT_VERSION = 1
MAGIC = b"ROOMOSB1"
AAD = MAGIC + bytes([FORMAT_VERSION])
NONCE_SIZE = 12
KEY_SIZE = 32
TOOL_VERSION = "5"

STATE_SPECS: dict[str, dict[str, Any]] = {
    "roomos-auth.db": {"category": "auth_device_session", "kind": "sqlite", "required": True},
    "server-secret": {"category": "auth_server_secret", "kind": "binary", "required": True},
    "scenes.json": {"category": "scenes", "kind": "json", "required": True, "keys": {"scenes"}},
    "scene-state.json": {"category": "scene_state", "kind": "json", "required": True, "keys": {"last_applied"}},
    "executions.json": {"category": "execution_history", "kind": "json", "required": False, "keys": {"version", "executions"}, "version": 1},
    "routine-evaluations.json": {"category": "routine_evaluations", "kind": "json", "required": False, "keys": {"version", "evaluations"}, "version": 1},
    "routines.json": {"category": "routines", "kind": "json", "required": False, "keys": {"version", "routines", "claimed_slots"}, "version": 1},
    "automations.json": {"category": "smart_automations", "kind": "json", "required": False, "keys": {"version", "automations", "claimed_slots", "cooldown_until"}, "version": 1},
    "automation-history.jsonl": {"category": "automation_history", "kind": "jsonl", "required": False},
    "alerts.json": {"category": "alerts_activity", "kind": "json", "required": True, "keys": {"version", "alerts", "pending", "activity"}, "version": 1},
    "notifications.json": {"category": "notification_outbox", "kind": "json", "required": True, "keys": {"version", "notifications", "external_attempts", "external_deliveries", "last_delivery"}, "version": 1},
    "notification-destinations.json": {"category": "notification_destinations", "kind": "json", "required": True, "keys": {"version", "destinations"}, "version": 1},
    "fcm-targets.json": {"category": "fcm_registration_metadata", "kind": "json", "required": True, "keys": {"version", "targets", "registrations"}, "version": 1},
    "notification-incidents.json": {"category": "notification_incidents_rollout", "kind": "json", "required": True, "keys": {"version", "incidents", "rollout_epoch"}, "version": 1},
    # 7G push state: optional (absent until first use, ignored by a rolled-back 7F Core).
    "push-registry.json": {"category": "push_registry", "kind": "json", "required": False, "keys": {"version", "devices"}, "version": 1},
    # 7G Web Push VAPID private key (optional; encrypted like server-secret, never in plaintext artifacts).
    "webpush-vapid-private.pem": {"category": "webpush_vapid_key", "kind": "binary", "required": False},
    # 7I alert policy: usage profiles, snoozes, bounded sustained-evidence windows (optional, additive).
    "alert-policy.json": {"category": "alert_policy", "kind": "json", "required": False, "keys": {"version"}, "optional_keys": {"profiles", "snoozes", "windows"}, "version": 1},
    # 7J bounded numeric metric history (optional; separate DB so its failure never touches auth).
    "metrics-history.db": {"category": "metrics_history", "kind": "sqlite", "required": False},
    "push-outbox.json": {"category": "push_outbox", "kind": "json", "required": False, "keys": {"version", "messages", "attempts_total", "accepted_total", "failed_total"}, "optional_keys": {"last_accepted_at"}, "version": 1},
}

EXCLUDED_CREDENTIAL_CATEGORIES = [
    "home_assistant_long_lived_token",
    "fcm_service_account",
    "tls_acme_private_material",
    "deployment_environment",
]


class BackupError(RuntimeError):
    pass


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise BackupError("timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def atomic_write(path: Path, payload: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def load_key(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise BackupError("key file must be a regular non-symlink file")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise BackupError("key file must not be accessible by group or others")
    value = path.read_bytes()
    if len(value) != KEY_SIZE:
        raise BackupError("key file must contain exactly 32 raw bytes")
    return value


def keygen(path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise BackupError("refusing to overwrite an existing key")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(secrets.token_bytes(KEY_SIZE))
        handle.flush()
        os.fsync(handle.fileno())


def encrypt_archive(plaintext: bytes, key: bytes) -> bytes:
    nonce = secrets.token_bytes(NONCE_SIZE)
    return AAD + nonce + AESGCM(key).encrypt(nonce, plaintext, AAD)


def decrypt_archive(encrypted: bytes, key: bytes) -> bytes:
    if len(encrypted) < len(AAD) + NONCE_SIZE + 16 or encrypted[: len(AAD)] != AAD:
        raise BackupError("unsupported or corrupt backup envelope")
    nonce_start = len(AAD)
    nonce = encrypted[nonce_start : nonce_start + NONCE_SIZE]
    ciphertext = encrypted[nonce_start + NONCE_SIZE :]
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, AAD)
    except InvalidTag as exc:
        raise BackupError("backup authentication failed") from exc


def validate_source_root(root: Path) -> list[str]:
    if root.is_symlink() or not root.is_dir():
        raise BackupError("state root must be a regular directory")
    observed = {path.name for path in root.iterdir() if path.is_file() or path.is_symlink()}
    transient = {name for name in observed if name.startswith(".") and name.endswith(".tmp")}
    sqlite_sidecars = {name for name in observed if name.endswith((".db-wal", ".db-shm", ".db-journal"))}
    unknown = observed - set(STATE_SPECS) - transient - sqlite_sidecars
    if unknown:
        raise BackupError(f"unknown persistent state files: {', '.join(sorted(unknown))}")
    missing = [name for name, spec in STATE_SPECS.items() if spec["required"] and name not in observed]
    if missing:
        raise BackupError(f"missing required state files: {', '.join(sorted(missing))}")
    present = sorted(name for name in STATE_SPECS if name in observed)
    for name in present:
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise BackupError(f"state member must be a regular non-symlink file: {name}")
        if STATE_SPECS[name]["kind"] == "json":
            validate_json_document(name, path.read_bytes())
        elif STATE_SPECS[name]["kind"] == "jsonl":
            validate_automation_history(path.read_bytes())
    return present


def evaluation_model():
    """New backup tooling also validates sidecars while an older Core is rolled back."""
    try:
        from app.routine_evaluations import EvaluationDocument
    except ImportError:
        import importlib.util
        source = Path(__file__).resolve().parents[1] / "core/app/routine_evaluations.py"
        spec = importlib.util.spec_from_file_location("app.routine_evaluations", source)
        if spec is None or spec.loader is None:
            raise BackupError("evaluation validator unavailable")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        EvaluationDocument = module.EvaluationDocument
    return EvaluationDocument


def validate_json_document(name: str, payload: bytes) -> dict[str, Any]:
    spec = STATE_SPECS[name]
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError(f"invalid JSON state: {name}") from exc
    if not isinstance(value, dict) or not spec["keys"] <= set(value) <= spec["keys"] | spec.get("optional_keys", set()):
        raise BackupError(f"incompatible JSON schema keys: {name}")
    if "version" in spec and value.get("version") != spec["version"]:
        raise BackupError(f"incompatible JSON schema version: {name}")
    if name == "executions.json":
        try:
            # Same strict model in the standalone repo tool and Core image.
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
            from app.executions import ExecutionDocument
            ExecutionDocument.model_validate(value)
        except (ValueError, ImportError) as exc:
            raise BackupError("invalid execution history schema") from exc
    if name == "routine-evaluations.json":
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
            EvaluationDocument = evaluation_model()
            if len(payload) > 512 * 1024:
                raise ValueError("Oversized evaluation history")
            EvaluationDocument.model_validate(value)
        except (ValueError, ImportError) as exc:
            raise BackupError("invalid evaluation history schema") from exc
    if name == "automations.json":
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
            from app.automations import AutomationDocument
            if len(payload) > 2 * 1024 * 1024:
                raise ValueError("Oversized automation state")
            AutomationDocument.model_validate(value)
        except (ValueError, ImportError) as exc:
            raise BackupError("invalid automation schema") from exc
    if name in POLICY_MODELS:
        try:
            if len(payload) > 2 * 1024 * 1024:
                raise ValueError("Oversized alert policy")
            policy_model(POLICY_MODELS[name]).model_validate(value)
        except (ValueError, ImportError) as exc:
            raise BackupError(f"invalid alert policy schema: {name}") from exc
    if name in PUSH_MODELS:
        try:
            if len(payload) > 4 * 1024 * 1024:
                raise ValueError("Oversized push state")
            push_model(PUSH_MODELS[name]).model_validate(value)
        except (ValueError, ImportError) as exc:
            raise BackupError(f"invalid push state schema: {name}") from exc
    return value


def validate_automation_history(payload: bytes) -> int:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
        from app.automations import HistoryRecord
        if len(payload) > 4 * 1024 * 1024:
            raise ValueError("Oversized automation history")
        lines = payload.decode("utf-8").splitlines()
        if len(lines) > 2000:
            raise ValueError("Too many automation history records")
        for line in lines:
            HistoryRecord.model_validate_json(line)
        return len(lines)
    except (UnicodeDecodeError, ValueError, ImportError) as exc:
        raise BackupError("invalid automation history schema") from exc


PUSH_MODELS = {"push-registry.json": "PushRegistryFile", "push-outbox.json": "PushOutboxFile"}
POLICY_MODELS = {"alert-policy.json": "AlertPolicyFile"}


def policy_model(model_name: str):
    """7I alert-policy validator; loaded from the tooling checkout when the running Core predates 7I."""
    try:
        import app.resource_policy as module
    except ImportError:
        import importlib.util
        source = Path(__file__).resolve().parents[1] / "core/app/resource_policy.py"
        spec = importlib.util.spec_from_file_location("app.resource_policy", source)
        if spec is None or spec.loader is None:
            raise BackupError("alert policy validator unavailable")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return getattr(module, model_name)


def push_model(model_name: str):
    """7G push validators; loaded from the tooling checkout when the running Core predates 7G."""
    try:
        import app.push as module
    except ImportError:
        import importlib.util
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
        source = Path(__file__).resolve().parents[1] / "core/app/push.py"
        spec = importlib.util.spec_from_file_location("app.push", source)
        if spec is None or spec.loader is None:
            raise BackupError("push validator unavailable")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return getattr(module, model_name)


def sqlite_snapshot(source: Path, destination: Path) -> None:
    source_uri = f"file:{source}?mode=ro"
    with sqlite3.connect(source_uri, uri=True, timeout=5) as source_db:
        with sqlite3.connect(destination) as target_db:
            source_db.backup(target_db)
            result = target_db.execute("PRAGMA integrity_check").fetchone()[0]
            foreign = target_db.execute("PRAGMA foreign_key_check").fetchall()
            if result != "ok" or foreign:
                raise BackupError("SQLite snapshot integrity validation failed")
    os.chmod(destination, 0o600)


def sqlite_summary(path: Path, at: datetime) -> dict[str, int]:
    epoch = int(at.timestamp())
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        foreign = db.execute("PRAGMA foreign_key_check").fetchall()
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        base_tables = {"devices", "pairing_windows", "sessions"}
        if integrity != "ok" or foreign or tables not in {frozenset(base_tables), frozenset(base_tables | {"panel_preferences"})}:
            raise BackupError("SQLite schema or integrity check failed")
        if "panel_preferences" in tables:
            columns = {row[1] for row in db.execute("PRAGMA table_info(panel_preferences)")}
            if columns != {"device_id", "version", "layout", "updated_at"}:
                raise BackupError("SQLite panel preference schema is invalid")
            for version, payload in db.execute("SELECT version,layout FROM panel_preferences"):
                try:
                    document = json.loads(payload)
                except (TypeError, json.JSONDecodeError) as exc:
                    raise BackupError("SQLite panel preference is invalid") from exc
                cards = document.get("cards") if isinstance(document, dict) else None
                scenes = document.get("scene_order") if isinstance(document, dict) else None
                if version != 1 or document.get("version") != 1 or set(cards or ()) != {"main_lights", "ambient", "scenes", "routines", "system"} or len(cards or ()) != 5 or set(scenes or ()) != {"normal", "work_1", "work_2", "gaming_1", "gaming_2", "movie", "night", "all_off"} or len(scenes or ()) != 8:
                    raise BackupError("SQLite panel preference is invalid")
        return {
            "devices": db.execute("SELECT count(*) FROM devices").fetchone()[0],
            "devices_enabled": db.execute("SELECT count(*) FROM devices WHERE enabled=1 AND revoked_at IS NULL").fetchone()[0],
            "devices_revoked": db.execute("SELECT count(*) FROM devices WHERE revoked_at IS NOT NULL").fetchone()[0],
            "sessions": db.execute("SELECT count(*) FROM sessions").fetchone()[0],
            "sessions_active": db.execute("SELECT count(*) FROM sessions WHERE revoked_at IS NULL AND sliding_expires_at>? AND absolute_expires_at>?", (epoch, epoch)).fetchone()[0],
            "sessions_revoked": db.execute("SELECT count(*) FROM sessions WHERE revoked_at IS NOT NULL").fetchone()[0],
            "pairing_windows": db.execute("SELECT count(*) FROM pairing_windows").fetchone()[0],
            "pairing_windows_active": db.execute("SELECT count(*) FROM pairing_windows WHERE used_at IS NULL AND expires_at>?", (epoch,)).fetchone()[0],
        }


# Self-contained copy of the 7J Core allowlists (a test keeps them equal), so a rolled-back
# Core image needs no extra validator mount for this database.
METRICS_HISTORY_MAX_BYTES = 64 * 1024 * 1024
METRICS_HISTORY_METRICS = frozenset({"cpu_usage", "ram_usage", "gpu_usage", "disk_usage",
    "cpu_temperature", "gpu_temperature", "battery_temperature", "battery_percent",
    "storage_free", "storage_total", "storage_used", "ram_total", "ram_used", "gpu_memory"})
METRICS_HISTORY_SOURCES = frozenset({"system_monitor", "home_assistant", "mqtt", "roomos_native", "roomos_web"})
METRICS_HISTORY_UNITS = frozenset({"%", "°C", "GB", "GiB", "B", "MB", "MiB"})
METRICS_HISTORY_COLUMNS = {
    "metrics": ["device", "metric", "step", "bucket", "unit", "source", "total", "low", "high", "samples", "last", "provenance"],
    "cursors": ["device", "metric", "last"],
}


def metrics_history_summary(path: Path) -> dict[str, int]:
    """Schema, allowlist and bound check; numeric history only, never identifiers or strings."""
    if path.stat().st_size > METRICS_HISTORY_MAX_BYTES:
        raise BackupError("metrics history exceeds its storage limit")
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or db.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise BackupError("metrics history integrity or version check failed")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables != set(METRICS_HISTORY_COLUMNS) or {row[0] for row in db.execute("SELECT type FROM sqlite_master")} - {"table", "index"}:
                raise BackupError("metrics history schema is invalid")
            for table, columns in METRICS_HISTORY_COLUMNS.items():
                if [row[1] for row in db.execute(f"PRAGMA table_info({table})")] != columns:
                    raise BackupError("metrics history schema is invalid")
            device = "device GLOB '*[^a-z0-9_]*' OR length(device) NOT BETWEEN 1 AND 40"
            for table in METRICS_HISTORY_COLUMNS:
                if db.execute(f"SELECT count(*) FROM {table} WHERE {device}").fetchone()[0]:
                    raise BackupError("metrics history contains an invalid device id")
            metrics = {row[0] for row in db.execute("SELECT metric FROM metrics UNION SELECT metric FROM cursors")}
            units = {row[0] for row in db.execute("SELECT DISTINCT unit FROM metrics")}
            sources = {row[0] for row in db.execute("SELECT DISTINCT source FROM metrics")}
            provenance = {row[0] for row in db.execute("SELECT DISTINCT provenance FROM metrics")}
            if (metrics - METRICS_HISTORY_METRICS or units - METRICS_HISTORY_UNITS
                    or sources - METRICS_HISTORY_SOURCES or provenance - {"received", "measured"}):
                raise BackupError("metrics history contains values outside its allowlist")
            if db.execute("SELECT count(*) FROM metrics WHERE step NOT IN (300,3600) OR samples<1 OR typeof(total)!='real' AND typeof(total)!='integer'").fetchone()[0]:
                raise BackupError("metrics history contains invalid buckets")
            return {
                "rows": db.execute("SELECT count(*) FROM metrics").fetchone()[0],
                "series": db.execute("SELECT count(*) FROM cursors").fetchone()[0],
            }
    except sqlite3.Error as exc:
        raise BackupError("metrics history is not a valid database") from exc


def _collection_size(value: Any) -> int:
    return len(value) if isinstance(value, (list, dict)) else 0


def semantic_summary(state: dict[str, bytes], at: datetime, sqlite_path: Path) -> dict[str, Any]:
    summary = _semantic_summary(state, at, sqlite_path)
    if "metrics-history.db" in state:
        payload = state["metrics-history.db"]
        beside = sqlite_path.parent / "metrics-history.db"  # the snapshot (create) or restored file (verify)
        if beside.is_file() and not beside.is_symlink() and beside.read_bytes() == payload:
            summary["metrics_history"] = metrics_history_summary(beside)
        else:
            with tempfile.TemporaryDirectory(prefix="roomos-metrics-") as temporary:
                copy = Path(temporary) / "metrics-history.db"
                copy.write_bytes(payload)
                summary["metrics_history"] = metrics_history_summary(copy)
    return summary


def _semantic_summary(state: dict[str, bytes], at: datetime, sqlite_path: Path) -> dict[str, Any]:
    parsed = {
        name: validate_json_document(name, payload)
        for name, payload in state.items()
        if STATE_SPECS[name]["kind"] == "json"
    }
    scenes = parsed["scenes.json"]
    routines = parsed.get("routines.json", {"routines": []})
    automations = parsed.get("automations.json", {"automations": []})
    alerts = parsed["alerts.json"]
    notifications = parsed["notifications.json"]
    deliveries = [delivery for item in notifications["notifications"] for delivery in item.get("deliveries", [])]
    destinations = parsed["notification-destinations.json"]["destinations"]
    targets = parsed["fcm-targets.json"]
    incidents_document = parsed["notification-incidents.json"]
    incidents = incidents_document["incidents"]
    incident_values = list(incidents.values()) if isinstance(incidents, dict) else incidents
    phases = Counter(item.get("phase", "unknown") for item in incident_values)
    return {
        "execution_count": len(parsed.get("executions.json", {"executions": []})["executions"]),
        "evaluation_count": len(parsed.get("routine-evaluations.json", {"evaluations": []})["evaluations"]),
        "scenes": _collection_size(scenes["scenes"]),
        "routines": _collection_size(routines["routines"]),
        "automations": _collection_size(automations["automations"]),
        "automation_history": validate_automation_history(state.get("automation-history.jsonl", b"")),
        "alerts": _collection_size(alerts["alerts"]),
        "activity": _collection_size(alerts["activity"]),
        "alert_pending": _collection_size(alerts["pending"]),
        "outbox": _collection_size(notifications["notifications"]),
        "routes": len(deliveries),
        "route_attempts": sum(int(item.get("attempt_count", 0)) for item in deliveries),
        "delivered_routes": sum(item.get("status") == "delivered" for item in deliveries),
        "external_attempts": int(notifications["external_attempts"]),
        "external_deliveries": int(notifications["external_deliveries"]),
        "destinations": len(destinations),
        "enabled_destinations": sum(bool(item.get("enabled")) for item in destinations),
        "activated_destinations": sum(bool(item.get("enabled")) and item.get("activated_at") is not None for item in destinations),
        "dispatcher_policy_enabled": any(bool(item.get("enabled")) and item.get("activated_at") is not None for item in destinations),
        "fcm_targets": _collection_size(targets["targets"]),
        "fcm_registrations": _collection_size(targets["registrations"]),
        "incidents": len(incident_values),
        "incident_phases": dict(sorted(phases.items())),
        "rollout_epoch": incidents_document["rollout_epoch"],
        "auth": sqlite_summary(sqlite_path, at),
    }


def add_tar_bytes(archive: tarfile.TarFile, name: str, payload: bytes, *, mode: int, uid: int, gid: int, mtime: int) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = mode
    info.uid = uid
    info.gid = gid
    info.mtime = mtime
    archive.addfile(info, io.BytesIO(payload))


def create_backup(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.state_root).resolve()
    output = Path(args.output).resolve()
    metadata_path = Path(str(output) + ".metadata.json")
    if output.exists() or metadata_path.exists():
        raise BackupError("refusing to overwrite backup artifact or metadata")
    if not args.revision or len(args.revision) != 40 or any(char not in "0123456789abcdef" for char in args.revision):
        raise BackupError("revision must be a full lowercase Git SHA")
    present = validate_source_root(root)
    created = utc_now()
    temp_root = Path(args.temp_root)
    if not temp_root.is_dir():
        raise BackupError("temporary root does not exist")
    key = load_key(Path(args.key_file))
    state: dict[str, bytes] = {}
    entries: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="roomos-backup-", dir=temp_root) as temporary:
        temporary_root = Path(temporary)
        for name in present:
            source = root / name
            spec = STATE_SPECS[name]
            if spec["kind"] == "sqlite":
                snapshot = temporary_root / name
                sqlite_snapshot(source, snapshot)
                payload = snapshot.read_bytes()
            else:
                payload = source.read_bytes()
                if spec["kind"] == "json":
                    validate_json_document(name, payload)
                elif spec["kind"] == "jsonl":
                    validate_automation_history(payload)
            state[name] = payload
            source_stat = source.stat()
            entries.append({
                "path": f"state/{name}",
                "category": spec["category"],
                "kind": spec["kind"],
                "required": bool(spec["required"]),
                "size": len(payload),
                "sha256": sha256_bytes(payload),
                "mode": f"{stat.S_IMODE(source_stat.st_mode):04o}",
                "uid": source_stat.st_uid,
                "gid": source_stat.st_gid,
                "schema_version": spec.get("version", 0),
            })
        sqlite_file = temporary_root / "semantic.db"
        sqlite_file.write_bytes(state["roomos-auth.db"])
        summary = semantic_summary(state, created, sqlite_file)
        root_stat = root.stat()
        manifest = {
            "backup_format_version": FORMAT_VERSION,
            "tool_version": TOOL_VERSION,
            "created_at": iso_utc(created),
            "release": args.release,
            "revision": args.revision,
            "core_image": args.core_image,
            "consistency": "core_quiesced_sqlite_backup_api_atomic_json",
            "state_root_expectation": {"mode": f"{stat.S_IMODE(root_stat.st_mode):04o}", "uid": root_stat.st_uid, "gid": root_stat.st_gid},
            "included": entries,
            "optional_absent": sorted(name for name, spec in STATE_SPECS.items() if not spec["required"] and name not in present),
            "excluded_external_credentials": EXCLUDED_CREDENTIAL_CATEGORIES,
            "restore_compatibility": {"format": FORMAT_VERSION, "minimum_tool_version": TOOL_VERSION, "core_revision": args.revision},
            "semantic_summary": summary,
            "semantic_summary_sha256": sha256_bytes(canonical_json(summary)),
        }
        manifest_bytes = canonical_json(manifest)
        archive_buffer = io.BytesIO()
        with tarfile.open(fileobj=archive_buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
            timestamp = int(created.timestamp())
            add_tar_bytes(archive, "manifest.json", manifest_bytes, mode=0o600, uid=root_stat.st_uid, gid=root_stat.st_gid, mtime=timestamp)
            for entry in entries:
                name = Path(entry["path"]).name
                add_tar_bytes(archive, entry["path"], state[name], mode=int(entry["mode"], 8), uid=entry["uid"], gid=entry["gid"], mtime=timestamp)
        plaintext = archive_buffer.getvalue()
        encrypted = encrypt_archive(plaintext, key)
        atomic_write(output, encrypted)
        if args.output_uid is not None and args.output_gid is not None and os.geteuid() == 0:
            os.chown(output, args.output_uid, args.output_gid)
        metadata = {
            "backup_format_version": FORMAT_VERSION,
            "created_at": manifest["created_at"],
            "release": args.release,
            "revision": args.revision,
            "core_image": args.core_image,
            "artifact": output.name,
            "artifact_size": len(encrypted),
            "encrypted_sha256": sha256_bytes(encrypted),
            "plaintext_sha256": sha256_bytes(plaintext),
            "manifest_sha256": sha256_bytes(manifest_bytes),
            "semantic_summary": summary,
            "semantic_summary_sha256": manifest["semantic_summary_sha256"],
            "encryption": "AES-256-GCM",
            "retention": "manual-no-automatic-prune",
            "restore_tested": False,
            "restore_tested_at": None,
        }
        atomic_write(metadata_path, json.dumps(metadata, indent=2, sort_keys=True).encode() + b"\n")
        if args.output_uid is not None and args.output_gid is not None and os.geteuid() == 0:
            os.chown(metadata_path, args.output_uid, args.output_gid)
    return metadata


def read_envelope(artifact: Path, key_path: Path, metadata_path: Path | None = None) -> tuple[bytes, dict[str, Any] | None]:
    encrypted = artifact.read_bytes()
    metadata = None
    if metadata_path is not None:
        metadata = json.loads(metadata_path.read_text())
        if metadata.get("backup_format_version") != FORMAT_VERSION:
            raise BackupError("incompatible metadata format")
        if metadata.get("encrypted_sha256") != sha256_bytes(encrypted):
            raise BackupError("encrypted artifact checksum mismatch")
    plaintext = decrypt_archive(encrypted, load_key(key_path))
    if metadata is not None and metadata.get("plaintext_sha256") != sha256_bytes(plaintext):
        raise BackupError("plaintext archive checksum mismatch")
    return plaintext, metadata


def parse_archive(plaintext: bytes) -> tuple[dict[str, Any], dict[str, tuple[bytes, tarfile.TarInfo]]]:
    try:
        archive = tarfile.open(fileobj=io.BytesIO(plaintext), mode="r:")
    except tarfile.TarError as exc:
        raise BackupError("invalid plaintext archive") from exc
    with archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        if len(names) != len(set(names)) or "manifest.json" not in names:
            raise BackupError("archive manifest is missing or members are duplicated")
        if any(not member.isfile() or member.name.startswith("/") or ".." in Path(member.name).parts for member in members):
            raise BackupError("archive contains an unsafe member")
        manifest_payload = archive.extractfile("manifest.json").read()
        try:
            manifest = json.loads(manifest_payload)
        except json.JSONDecodeError as exc:
            raise BackupError("manifest is invalid JSON") from exc
        if manifest.get("backup_format_version") != FORMAT_VERSION or manifest.get("tool_version") not in {"1", "2", TOOL_VERSION}:
            raise BackupError("incompatible backup format or tool version")
        expected = {entry["path"] for entry in manifest.get("included", [])} | {"manifest.json"}
        if set(names) != expected:
            raise BackupError("archive members do not match the manifest")
        files = {}
        for member in members:
            if member.name == "manifest.json":
                continue
            files[member.name] = (archive.extractfile(member).read(), member)
        return manifest, files


def validate_manifest_and_files(manifest: dict[str, Any], files: dict[str, tuple[bytes, tarfile.TarInfo]]) -> None:
    included = manifest.get("included")
    if not isinstance(included, list):
        raise BackupError("manifest included list is invalid")
    entries = {entry.get("path"): entry for entry in included if isinstance(entry, dict)}
    required_names = {f"state/{name}" for name, spec in STATE_SPECS.items() if spec["required"]}
    if not required_names.issubset(entries):
        raise BackupError("manifest omits required state")
    allowed_names = {f"state/{name}" for name in STATE_SPECS}
    if set(entries) - allowed_names or set(files) != set(entries):
        raise BackupError("manifest contains unknown or missing state")
    for path, entry in entries.items():
        payload, member = files[path]
        name = Path(path).name
        spec = STATE_SPECS[name]
        if entry.get("category") != spec["category"] or entry.get("kind") != spec["kind"]:
            raise BackupError(f"manifest category mismatch: {name}")
        if entry.get("size") != len(payload) or entry.get("sha256") != sha256_bytes(payload):
            raise BackupError(f"state checksum mismatch: {name}")
        if entry.get("mode") != f"{member.mode:04o}" or member.uid != entry.get("uid") or member.gid != entry.get("gid"):
            raise BackupError(f"state archive ownership or mode mismatch: {name}")
        if spec["kind"] == "json":
            validate_json_document(name, payload)
        elif spec["kind"] == "jsonl":
            validate_automation_history(payload)
    summary_hash = sha256_bytes(canonical_json(manifest.get("semantic_summary")))
    if manifest.get("semantic_summary_sha256") != summary_hash:
        raise BackupError("manifest semantic summary checksum mismatch")


def restore_backup(args: argparse.Namespace) -> dict[str, Any]:
    artifact = Path(args.artifact).resolve()
    metadata_path = Path(args.metadata).resolve() if args.metadata else None
    plaintext, metadata = read_envelope(artifact, Path(args.key_file), metadata_path)
    manifest, files = parse_archive(plaintext)
    validate_manifest_and_files(manifest, files)
    if metadata is not None and metadata.get("manifest_sha256") != sha256_bytes(canonical_json(manifest)):
        raise BackupError("manifest checksum does not match metadata")
    root = Path(args.restore_root).resolve()
    if root.exists() and any(root.iterdir()):
        raise BackupError("restore root must be absent or empty")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_root = root / "state"
    expectation = manifest["state_root_expectation"]
    state_root.mkdir(mode=int(expectation["mode"], 8))
    if os.geteuid() == 0:
        os.chown(state_root, expectation["uid"], expectation["gid"])
    for entry in manifest["included"]:
        payload, _member = files[entry["path"]]
        destination = state_root / Path(entry["path"]).name
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, int(entry["mode"], 8))
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(destination, int(entry["mode"], 8))
        if os.geteuid() == 0:
            os.chown(destination, entry["uid"], entry["gid"])
    atomic_write(root / "manifest.json", canonical_json(manifest))
    report = verify_restore_root(root)
    report.update({
        "artifact": artifact.name,
        "encrypted_sha256": sha256_bytes(artifact.read_bytes()),
        "plaintext_sha256": sha256_bytes(plaintext),
        "manifest_sha256": sha256_bytes(canonical_json(manifest)),
        "restored_at": iso_utc(utc_now()),
        "passed": True,
    })
    if args.report:
        atomic_write(Path(args.report).resolve(), json.dumps(report, indent=2, sort_keys=True).encode() + b"\n")
    return report


def verify_restore_root(root: Path) -> dict[str, Any]:
    manifest_path = root / "manifest.json"
    state_root = root / "state"
    if not manifest_path.is_file() or not state_root.is_dir():
        raise BackupError("restore root is incomplete")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("backup_format_version") != FORMAT_VERSION:
        raise BackupError("incompatible restored manifest format")
    entries = {entry["path"]: entry for entry in manifest.get("included", [])}
    required = {f"state/{name}" for name, spec in STATE_SPECS.items() if spec["required"]}
    if not required.issubset(entries):
        raise BackupError("restored manifest omits required state")
    observed = {path.name for path in state_root.iterdir() if path.is_file()}
    expected = {Path(path).name for path in entries}
    if observed != expected:
        raise BackupError("restored files do not match manifest")
    state: dict[str, bytes] = {}
    for path, entry in entries.items():
        name = Path(path).name
        file_path = state_root / name
        payload = file_path.read_bytes()
        state[name] = payload
        file_stat = file_path.stat()
        if len(payload) != entry["size"] or sha256_bytes(payload) != entry["sha256"]:
            raise BackupError(f"restored file checksum mismatch: {name}")
        if stat.S_IMODE(file_stat.st_mode) != int(entry["mode"], 8):
            raise BackupError(f"restored file mode mismatch: {name}")
        if os.geteuid() == 0 and (file_stat.st_uid != entry["uid"] or file_stat.st_gid != entry["gid"]):
            raise BackupError(f"restored file ownership mismatch: {name}")
        if STATE_SPECS[name]["kind"] == "json":
            validate_json_document(name, payload)
        elif STATE_SPECS[name]["kind"] == "jsonl":
            validate_automation_history(payload)
    db_path = state_root / "roomos-auth.db"
    summary = semantic_summary(state, parse_utc(manifest["created_at"]), db_path)
    if manifest.get("tool_version") == "1" and "executions.json" not in state:
        summary.pop("execution_count", None)
    if manifest.get("tool_version") in {"1", "2"}:
        summary.pop("evaluation_count", None)
    if manifest.get("tool_version") in {"1", "2", "3", "4"} and "automations.json" not in state:
        summary.pop("automations", None)
        summary.pop("automation_history", None)
    if summary != manifest.get("semantic_summary") or sha256_bytes(canonical_json(summary)) != manifest.get("semantic_summary_sha256"):
        raise BackupError("restored semantic state does not match manifest")
    return {
        "backup_format_version": FORMAT_VERSION,
        "release": manifest["release"],
        "revision": manifest["revision"],
        "core_image": manifest["core_image"],
        "files_verified": len(entries),
        "sqlite_integrity": "ok",
        "json_schema_validation": "ok",
        "ownership_mode_validation": "ok",
        "semantic_summary": summary,
        "semantic_summary_sha256": manifest["semantic_summary_sha256"],
    }


def runtime_gate(root: Path) -> dict[str, Any]:
    state = root / "state"
    try:
        from app.executions import ExecutionDocument
        from app.alerts import AlertStateFile
        from app.auth import AuthStore
        from app.config import Settings
        from app.fcm import DestinationFile, FcmTargetFile
        from app.notifications import NotificationStateFile
        from app.production_notifications import ProductionIncidentStateFile
        from app.routines import load_routine_document
        from app.automations import AutomationDocument
        from app.scenes import load_scene_config, load_scene_state
    except ImportError as exc:
        raise BackupError("runtime gate must run in the RoomOS Core image") from exc
    AlertStateFile.model_validate_json((state / "alerts.json").read_text())
    NotificationStateFile.model_validate_json((state / "notifications.json").read_text())
    ProductionIncidentStateFile.model_validate_json((state / "notification-incidents.json").read_text())
    DestinationFile.model_validate_json((state / "notification-destinations.json").read_text())
    FcmTargetFile.model_validate_json((state / "fcm-targets.json").read_text())
    executions = ExecutionDocument(version=1, executions=[])
    if (state / "executions.json").exists():
        executions = ExecutionDocument.model_validate_json((state / "executions.json").read_bytes())
    evaluation_count = 0
    if (state / "routine-evaluations.json").exists():
        # Import from tooling checkout as well as image: rollback Core may predate this model.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
        EvaluationDocument = evaluation_model()
        evaluation_count = len(EvaluationDocument.model_validate_json(
            (state / "routine-evaluations.json").read_bytes()).evaluations)
    push_counts = {}
    for name, model_name in PUSH_MODELS.items():
        if (state / name).exists():
            document = push_model(model_name).model_validate_json((state / name).read_bytes())
            push_counts[name] = len(document.devices if name == "push-registry.json" else document.messages)
    for name, model_name in POLICY_MODELS.items():
        if (state / name).exists():
            document = policy_model(model_name).model_validate_json((state / name).read_bytes())
            push_counts[name] = len(document.snoozes) + len(document.profiles)
    scenes = load_scene_config(str(state / "scenes.json"))
    scene_state = load_scene_state(str(state / "scene-state.json"))
    routines, routines_corrupt = load_routine_document(str(state / "routines.json"))
    if routines_corrupt:
        raise BackupError("invalid routines runtime schema")
    automations = AutomationDocument()
    if (state / "automations.json").exists():
        automations = AutomationDocument.model_validate_json((state / "automations.json").read_bytes())
    automation_history = validate_automation_history((state / "automation-history.jsonl").read_bytes()) if (state / "automation-history.jsonl").exists() else 0
    metrics_history = metrics_history_summary(state / "metrics-history.db") if (state / "metrics-history.db").exists() else None
    settings = Settings().model_copy(update={"auth_database_path": str(state / "roomos-auth.db"), "auth_secret_path": str(state / "server-secret"), "auth_enabled": True})
    auth = AuthStore(settings)
    auth.initialize()
    return {
        "runtime_schema_gate": "ok",
        "execution_count": len(executions.executions),
        "evaluation_count": evaluation_count,
        "scenes": len(scenes),
        "scene_state_present": scene_state is not None,
        "routines": len(routines.routines),
        "automations": len(automations.automations),
        "automation_history": automation_history,
        "routines_corrupt": routines_corrupt,
        "push_state": push_counts,
        **({"metrics_history": metrics_history} if metrics_history is not None else {}),
        "external_network_used": False,
    }


def attest_restore(metadata_path: Path, report_path: Path) -> dict[str, Any]:
    metadata = json.loads(metadata_path.read_text())
    report = json.loads(report_path.read_text())
    if not report.get("passed") or report.get("encrypted_sha256") != metadata.get("encrypted_sha256"):
        raise BackupError("restore report does not attest this artifact")
    if report.get("semantic_summary_sha256") != metadata.get("semantic_summary_sha256"):
        raise BackupError("restore report semantic digest mismatch")
    runtime = report.get("runtime_gate", {})
    if runtime.get("runtime_schema_gate") != "ok" or runtime.get("external_network_used") is not False:
        raise BackupError("restore report lacks a successful isolated runtime gate")
    metadata["restore_tested"] = True
    metadata["restore_tested_at"] = report["restored_at"]
    metadata["restore_report_sha256"] = sha256_bytes(canonical_json(report))
    atomic_write(metadata_path, json.dumps(metadata, indent=2, sort_keys=True).encode() + b"\n")
    return metadata


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fail-closed encrypted RoomOS state backup and isolated restore tool")
    commands = parser.add_subparsers(dest="command", required=True)
    key = commands.add_parser("keygen", help="create a new 32-byte AES key; refuses overwrite")
    key.add_argument("--key-file", required=True)
    create = commands.add_parser("create", help="create an encrypted full-state artifact from a quiesced Core state root")
    create.add_argument("--state-root", required=True)
    create.add_argument("--output", required=True)
    create.add_argument("--key-file", required=True)
    create.add_argument("--release", required=True)
    create.add_argument("--revision", required=True)
    create.add_argument("--core-image", required=True)
    create.add_argument("--temp-root", default="/dev/shm")
    create.add_argument("--output-uid", type=int)
    create.add_argument("--output-gid", type=int)
    restore = commands.add_parser("restore", help="decrypt and restore into a new isolated root")
    restore.add_argument("--artifact", required=True)
    restore.add_argument("--metadata")
    restore.add_argument("--key-file", required=True)
    restore.add_argument("--restore-root", required=True)
    restore.add_argument("--report")
    verify = commands.add_parser("verify", help="re-verify an already restored root")
    verify.add_argument("--restore-root", required=True)
    gate = commands.add_parser("runtime-gate", help="validate restored schemas using the RoomOS Core runtime")
    gate.add_argument("--restore-root", required=True)
    gate.add_argument("--report", help="append the successful runtime gate to an existing restore report")
    attest = commands.add_parser("attest", help="mark metadata restore-tested using a successful restore report")
    attest.add_argument("--metadata", required=True)
    attest.add_argument("--report", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "keygen":
            keygen(Path(args.key_file))
            result = {"key_created": True}
        elif args.command == "create":
            result = create_backup(args)
        elif args.command == "restore":
            result = restore_backup(args)
        elif args.command == "verify":
            result = verify_restore_root(Path(args.restore_root).resolve())
        elif args.command == "runtime-gate":
            result = runtime_gate(Path(args.restore_root).resolve())
            if args.report:
                report_path = Path(args.report).resolve()
                report = json.loads(report_path.read_text())
                if not report.get("passed"):
                    raise BackupError("runtime gate report must reference a successful restore")
                report["runtime_gate"] = result
                atomic_write(report_path, json.dumps(report, indent=2, sort_keys=True).encode() + b"\n")
        else:
            result = attest_restore(Path(args.metadata).resolve(), Path(args.report).resolve())
        print(json.dumps(result, sort_keys=True))
        return 0
    except (BackupError, OSError, sqlite3.Error, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"roomos-state-backup: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
