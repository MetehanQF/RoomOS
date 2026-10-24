from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import stat
import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[1]


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


status_mod = load("roomos_backup_status")
retention = load("roomos_backup_retention")
NOW = datetime(2026, 10, 7, 1, 30, tzinfo=UTC)
SCHEDULE = "Daily 04:30 Europe/Istanbul"


# --- status writer --------------------------------------------------------------


def test_status_lifecycle_and_schema(tmp_path):
    path = tmp_path / "status.json"
    status_mod.update(path, event="start", schedule=SCHEDULE, cadence_seconds=86400, now=NOW)
    doc = json.loads(path.read_text())
    assert doc["status"] == "running" and doc["restore_tested"] is False and doc["last_success_at"] is None
    status_mod.update(path, event="success", schedule=SCHEDULE, cadence_seconds=86400, now=NOW,
                      success_at="2026-10-07T01:30:05Z", restore_test_at="2026-10-07T01:30:20Z")
    doc = json.loads(path.read_text())
    assert (doc["status"], doc["restore_tested"], doc["last_success_at"]) == ("success", True, "2026-10-07T01:30:05Z")
    status_mod.update(path, event="start", schedule=SCHEDULE, cadence_seconds=86400, now=NOW + timedelta(days=1))
    status_mod.update(path, event="failure", schedule=SCHEDULE, cadence_seconds=86400, reason="restore_failed",
                      now=NOW + timedelta(days=1))
    doc = json.loads(path.read_text())
    assert doc["status"] == "failed" and doc["reason"] == "restore_failed"
    assert doc["last_success_at"] == "2026-10-07T01:30:05Z"  # last good backup is preserved
    assert set(doc) == set(status_mod.FIELDS)
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert len(path.read_bytes()) <= status_mod.MAX_BYTES


def test_status_never_contains_paths_or_free_text(tmp_path):
    path = tmp_path / "status.json"
    status_mod.update(path, event="start", schedule=SCHEDULE, cadence_seconds=86400, now=NOW)
    with pytest.raises(status_mod.StatusError):
        status_mod.update(path, event="failure", schedule=SCHEDULE, cadence_seconds=86400,
                          reason="Traceback: /home/user/key")  # privacy-scan:allow fake path in a redaction test
    with pytest.raises(status_mod.StatusError):
        status_mod.update(path, event="success", schedule=SCHEDULE, cadence_seconds=86400)
    assert str(tmp_path) not in path.read_text()


def test_skip_records_lock_without_touching_running_status(tmp_path):
    path = tmp_path / "status.json"
    status_mod.update(path, event="start", schedule=SCHEDULE, cadence_seconds=86400, now=NOW)
    status_mod.update(path, event="skip", schedule=SCHEDULE, cadence_seconds=86400, reason="locked", now=NOW)
    doc = json.loads(path.read_text())
    assert doc["status"] == "running" and doc["last_skip_reason"] == "locked"


def test_corrupt_status_is_detected_and_replaced_by_next_authoritative_write(tmp_path):
    path = tmp_path / "status.json"
    path.write_text("{broken")
    with pytest.raises(status_mod.StatusError):
        status_mod.load(path)
    status_mod.update(path, event="start", schedule=SCHEDULE, cadence_seconds=86400, now=NOW)
    assert status_mod.load(path)["status"] == "running"


def test_atomic_write_leaves_previous_file_on_failure(tmp_path, monkeypatch):
    path = tmp_path / "status.json"
    status_mod.update(path, event="start", schedule=SCHEDULE, cadence_seconds=86400, now=NOW)
    before = path.read_bytes()

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(status_mod.os, "replace", boom)
    with pytest.raises(OSError):
        status_mod.update(path, event="failure", schedule=SCHEDULE, cadence_seconds=86400, reason="backup_failed")
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".status.json.*.tmp"))


# --- retention ------------------------------------------------------------------


def make_artifact(directory: Path, created: datetime, tested: bool | None, suffix: str = "abc1234") -> Path:
    name = f"roomos-state-{created.strftime('%Y%m%dT%H%M%SZ')}-{suffix}.roomos-backup"
    artifact = directory / name
    artifact.write_bytes(b"x")
    if tested is not None:
        (directory / f"{name}.metadata.json").write_text(json.dumps({"restore_tested": tested}))
        if tested:
            (directory / f"{name}.restore-report.json").write_text("{}")
    return artifact


def test_retention_keeps_newest_tested_and_only_touches_roomos_artifacts(tmp_path):
    for day in range(20):
        make_artifact(tmp_path, NOW - timedelta(days=day), tested=True)
    old_untested = make_artifact(tmp_path, NOW - timedelta(days=9), tested=False, suffix="pre6b2")
    fresh_untested = make_artifact(tmp_path, NOW - timedelta(days=1), tested=False, suffix="fresh")
    unrelated = [tmp_path / "pi-backup-20261001.tar.gz", tmp_path / "roomos-state-notes.txt",
                 tmp_path / "roomos-state-20261001T000000Z-x.roomos-backup.bak"]
    for path in unrelated:
        path.write_text("keep")
    (tmp_path / "subdir.roomos-backup").mkdir()
    victims = retention.select(retention.scan(tmp_path), NOW)
    names = {item.path.name for item in victims}
    assert len([item for item in victims if item.tested]) == 6  # 20 tested - keep 14
    assert old_untested.name in names and fresh_untested.name not in names
    newest = max(retention.scan(tmp_path), key=lambda item: item.created if item.tested else NOW - timedelta(days=99))
    assert newest.path.name not in names
    assert retention.main(["--directory", str(tmp_path), "--apply"]) == 0
    assert all(path.exists() for path in unrelated)
    remaining = [item for item in retention.scan(tmp_path) if item.tested]
    assert len(remaining) == 14
    assert not list(tmp_path.glob(f"{old_untested.name}*"))


def test_retention_never_deletes_without_a_known_good_artifact(tmp_path):
    for day in range(10, 20):
        make_artifact(tmp_path, NOW - timedelta(days=day), tested=False)
    make_artifact(tmp_path, NOW - timedelta(days=30), tested=None)
    assert retention.select(retention.scan(tmp_path), NOW) == []


def test_retention_dry_run_deletes_nothing(tmp_path):
    for day in range(16):
        make_artifact(tmp_path, NOW - timedelta(days=day), tested=True)
    before = sorted(path.name for path in tmp_path.iterdir())
    assert retention.main(["--directory", str(tmp_path)]) == 0
    assert sorted(path.name for path in tmp_path.iterdir()) == before


def test_retention_handles_legacy_report_name(tmp_path):
    artifact = make_artifact(tmp_path, NOW - timedelta(days=40), tested=True, suffix="649abf5")
    legacy = tmp_path / artifact.name.replace(".roomos-backup", ".roomos-restore-report.json")
    legacy.write_text("{}")
    for day in range(14):
        make_artifact(tmp_path, NOW - timedelta(days=day), tested=True)
    retention.main(["--directory", str(tmp_path), "--apply"])
    assert not legacy.exists() and not artifact.exists()


# --- scheduled wrapper end-to-end with fake docker / create wrapper ----------------

FAKE_DOCKER = """#!/usr/bin/env bash
set -u
case "$1" in
  inspect)
    case "$*" in
      *Config.Image*) echo "roomos-core:sha-abc1234" ;;
      *Health*) echo "${FAKE_HEALTH:-healthy}" ;;
    esac ;;
  run)
    [[ "${FAKE_RESTORE:-ok}" == fail ]] && exit 1
    args=("$@"); name=""; owner=""
    name="${args[-2]}"; owner="${args[-1]}"
    meta="$FAKE_BACKUP_DIR/$name.metadata.json"
    python3 - "$meta" <<'PY'
import json, sys
meta = json.load(open(sys.argv[1]))
meta.update(restore_tested=True, restore_tested_at="2026-10-07T01:30:20Z")
json.dump(meta, open(sys.argv[1], "w"))
PY
    echo '{}' > "$FAKE_BACKUP_DIR/$name.restore-report.json" ;;
  ps) [[ "${FAKE_LEFTOVER:-no}" == yes ]] && echo deadbeef ;;
  rm) ;;
esac
exit 0
"""

FAKE_CREATE = """#!/usr/bin/env bash
set -eu
[[ "${FAKE_CREATE:-ok}" == fail ]] && exit 1
output="$2"
printf 'encrypted-bytes' > "$output"
digest=$(sha256sum "$output" | cut -d' ' -f1)
[[ "${FAKE_CREATE:-ok}" == badhash ]] && digest=0000
printf '{"encrypted_sha256":"%s","restore_tested":false,"created_at":"2026-10-07T01:30:05Z","restore_tested_at":null}' "$digest" > "$output.metadata.json"
"""


@pytest.fixture
def harness(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER)
    (bin_dir / "docker").chmod(0o755)
    create = tmp_path / "fake-create.sh"
    create.write_text(FAKE_CREATE)
    create.chmod(0o755)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    key = tmp_path / "backup.key"
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)
    status_dir = tmp_path / "status"
    status_dir.mkdir()
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "ROOMOS_BACKUP_DIR": str(backup_dir), "ROOMOS_BACKUP_KEY": str(key),
        "ROOMOS_BACKUP_STATUS_FILE": str(status_dir / "status.json"),
        "ROOMOS_BACKUP_LOCK": str(tmp_path / "lock" / "run.lock"),
        "ROOMOS_BACKUP_CREATE": str(create), "FAKE_BACKUP_DIR": str(backup_dir),
    }

    def run(**overrides):
        result = subprocess.run(["bash", str(SCRIPTS / "roomos-backup-run.sh")], env={**env, **overrides},
                                capture_output=True, text=True, timeout=60)
        status_file = status_dir / "status.json"
        doc = json.loads(status_file.read_text()) if status_file.exists() else None
        return result, doc

    return run, tmp_path, backup_dir, key


def test_wrapper_success_records_restore_tested_status(harness):
    run, _, backup_dir, _ = harness
    result, doc = run()
    assert result.returncode == 0, result.stderr
    assert doc["status"] == "success" and doc["restore_tested"] is True
    assert doc["last_success_at"] == "2026-10-07T01:30:05Z" and doc["last_restore_test_at"] == "2026-10-07T01:30:20Z"
    assert len(list(backup_dir.glob("*.roomos-backup"))) == 1
    text = json.dumps(doc)
    assert str(backup_dir) not in text and ".roomos-backup" not in text and "key" not in text


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"FAKE_HEALTH": "unhealthy"}, "source_validation_failed"),
        ({"FAKE_CREATE": "fail"}, "backup_failed"),
        ({"FAKE_CREATE": "badhash"}, "integrity_failed"),
        ({"FAKE_RESTORE": "fail"}, "restore_failed"),
        ({"FAKE_LEFTOVER": "yes"}, "cleanup_failed"),
    ],
)
def test_wrapper_failures_record_bounded_reason_and_keep_last_good(harness, overrides, reason):
    run, *_ = harness
    first, good = run()
    assert first.returncode == 0
    result, doc = run(**overrides)
    assert result.returncode != 0
    assert (doc["status"], doc["reason"]) == ("failed", reason)
    assert doc["last_success_at"] == good["last_success_at"] and doc["restore_tested"] is True


def test_wrapper_source_validation_rejects_insecure_key(harness):
    run, _, _, key = harness
    key.chmod(0o644)
    result, doc = run()
    assert result.returncode != 0 and doc["reason"] == "source_validation_failed"


def test_wrapper_lock_prevents_concurrent_run(harness):
    run, tmp_path, backup_dir, _ = harness
    lock = tmp_path / "lock" / "run.lock"
    lock.parent.mkdir(mode=0o700)
    with open(lock, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        result, doc = run()
    assert result.returncode == 75
    assert doc is not None and doc["last_skip_reason"] == "locked"
    assert not list(backup_dir.glob("*.roomos-backup"))


def test_wrapper_shell_syntax_and_hardening_contract():
    script = (SCRIPTS / "roomos-backup-run.sh").read_text()
    assert subprocess.run(["bash", "-n", str(SCRIPTS / "roomos-backup-run.sh")]).returncode == 0
    assert "--network none" in script and "flock -n" in script
    assert "dst=/tooling/scripts,readonly" in script and "src=$repo_dir," not in script
    create = (SCRIPTS / "roomos-state-backup-create.sh").read_text()
    assert "src=$repo_dir/scripts,dst=/tooling/scripts,readonly" in create and "src=$repo_dir,dst" not in create
