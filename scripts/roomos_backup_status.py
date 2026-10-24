#!/usr/bin/env python3
"""Non-secret RoomOS backup status metadata (host side).

The status file is the only backup information RoomOS Core may read. It never contains
paths, filenames, usernames, hosts, keys, hashes, commands or raw error text: only
timestamps, a bounded status and a bounded reason code.

Writes are atomic (same-directory temp file, fsync, rename, directory fsync) and serialized
with an flock on a sibling lock file, so a reader never sees partial JSON and a concurrent
"skipped" record cannot lose the running job's update.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
TOOL_VERSION = 2
MAX_BYTES = 4096
STATUSES = ("running", "success", "failed")
REASONS = (
    "source_validation_failed",
    "backup_failed",
    "integrity_failed",
    "restore_failed",
    "cleanup_failed",
    "retention_failed",
    "locked",
    "interrupted",
)
FIELDS = (
    "version", "status", "reason", "tool_version", "schedule", "cadence_seconds",
    "last_attempt_at", "last_finished_at", "last_success_at", "last_restore_test_at",
    "restore_tested", "last_skipped_at", "last_skip_reason",
)
TIMESTAMP_FIELDS = (
    "last_attempt_at", "last_finished_at", "last_success_at", "last_restore_test_at", "last_skipped_at",
)
SCHEDULE_MAX = 60


class StatusError(ValueError):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _timestamp(value: Any, name: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or len(value) > 40:
        raise StatusError(f"invalid {name}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StatusError(f"invalid {name}") from exc
    if parsed.tzinfo is None:
        raise StatusError(f"invalid {name}")


def validate(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict) or set(document) != set(FIELDS):
        raise StatusError("unexpected status fields")
    if document["version"] != SCHEMA_VERSION or document["tool_version"] != TOOL_VERSION:
        raise StatusError("unsupported status version")
    if document["status"] not in STATUSES:
        raise StatusError("invalid status")
    if document["reason"] is not None and document["reason"] not in REASONS:
        raise StatusError("invalid reason")
    if document["last_skip_reason"] is not None and document["last_skip_reason"] not in REASONS:
        raise StatusError("invalid skip reason")
    if (document["status"] == "failed") != (document["reason"] is not None):
        raise StatusError("reason must be present exactly for failed attempts")
    if not isinstance(document["restore_tested"], bool):
        raise StatusError("invalid restore_tested")
    if not isinstance(document["cadence_seconds"], int) or not 3600 <= document["cadence_seconds"] <= 7 * 86400:
        raise StatusError("invalid cadence")
    if not isinstance(document["schedule"], str) or not 0 < len(document["schedule"]) <= SCHEDULE_MAX:
        raise StatusError("invalid schedule")
    for name in TIMESTAMP_FIELDS:
        _timestamp(document[name], name)
    if document["restore_tested"] != (document["last_restore_test_at"] is not None):
        raise StatusError("restore_tested must match last_restore_test_at")
    return document


def empty(schedule: str, cadence_seconds: int) -> dict[str, Any]:
    return {
        "version": SCHEMA_VERSION, "status": "failed", "reason": "interrupted", "tool_version": TOOL_VERSION,
        "schedule": schedule, "cadence_seconds": cadence_seconds,
        "last_attempt_at": None, "last_finished_at": None, "last_success_at": None,
        "last_restore_test_at": None, "restore_tested": False,
        "last_skipped_at": None, "last_skip_reason": None,
    }


def load(path: Path) -> dict[str, Any] | None:
    """Return the validated document, None if absent; raise StatusError if invalid."""
    try:
        if path.stat().st_size > MAX_BYTES:
            raise StatusError("status file too large")
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        return validate(json.loads(raw))
    except json.JSONDecodeError as exc:
        raise StatusError("status file is not valid JSON") from exc


def write_atomic(path: Path, document: dict[str, Any]) -> None:
    payload = json.dumps(validate(document), sort_keys=True, separators=(",", ":")).encode()
    if len(payload) > MAX_BYTES:
        raise StatusError("status document too large")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def status_lock(path: Path):
    lock_path = path.with_name(f".{path.name}.lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def update(path: Path, *, event: str, schedule: str, cadence_seconds: int, now: datetime | None = None,
           reason: str | None = None, success_at: str | None = None, restore_test_at: str | None = None) -> dict[str, Any]:
    """Apply one bounded event: start, success, failure or skip."""
    now = now or utc_now()
    with status_lock(path):
        try:
            current = load(path)
        except StatusError:
            current = None  # an invalid previous file is replaced by a fresh authoritative record
        document = dict(current) if current is not None else empty(schedule, cadence_seconds)
        document["schedule"], document["cadence_seconds"] = schedule, cadence_seconds
        if event == "start":
            document.update(status="running", reason=None, last_attempt_at=iso(now), last_finished_at=None)
        elif event == "success":
            if success_at is None or restore_test_at is None:
                raise StatusError("success requires restore-tested timestamps")
            document.update(status="success", reason=None, last_finished_at=iso(now), last_success_at=success_at,
                            last_restore_test_at=restore_test_at, restore_tested=True)
        elif event == "failure":
            if reason not in REASONS:
                raise StatusError("invalid failure reason")
            document.update(status="failed", reason=reason, last_finished_at=iso(now))
        elif event == "skip":
            if reason not in REASONS:
                raise StatusError("invalid skip reason")
            document.update(last_skipped_at=iso(now), last_skip_reason=reason)
        else:
            raise StatusError("unknown event")
        write_atomic(path, document)
        return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("event", choices=("start", "success", "failure", "skip", "validate"))
    parser.add_argument("--status-file", required=True)
    parser.add_argument("--schedule", default="Daily 04:30 Europe/Istanbul")
    parser.add_argument("--cadence-seconds", type=int, default=86400)
    parser.add_argument("--reason")
    parser.add_argument("--success-at")
    parser.add_argument("--restore-test-at")
    args = parser.parse_args(argv)
    path = Path(args.status_file)
    try:
        if args.event == "validate":
            document = load(path)
            print("absent" if document is None else f"valid status={document['status']}")
            return 0
        update(path, event=args.event, schedule=args.schedule, cadence_seconds=args.cadence_seconds,
               reason=args.reason, success_at=args.success_at, restore_test_at=args.restore_test_at)
    except StatusError as exc:
        print(f"backup status error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
