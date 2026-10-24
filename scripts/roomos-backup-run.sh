#!/usr/bin/env bash
# Scheduled AND manual entry point for protected RoomOS backups (Phase 6D.4).
#
#   systemctl --user start roomos-backup.service   # same unit the timer runs
#   scripts/roomos-backup-run.sh                    # direct manual run
#
# Success means ALL of: source validation, encrypted artifact, integrity check, isolated
# network-disabled restore + runtime gate + attest, and plaintext cleanup. Only then is the
# non-secret status file updated to success. Every other outcome records a bounded reason.
set -uo pipefail

repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
backup_dir=${ROOMOS_BACKUP_DIR:-$HOME/metehantech_backups/roomos}
key_file=${ROOMOS_BACKUP_KEY:-$HOME/.config/roomos-backup/backup.key}
status_file=${ROOMOS_BACKUP_STATUS_FILE:-$HOME/.local/state/roomos-backup-status/status.json}
lock_file=${ROOMOS_BACKUP_LOCK:-$HOME/.local/state/roomos-backup/run.lock}
core_container=${ROOMOS_CORE_CONTAINER:-roomos-core-1}
schedule=${ROOMOS_BACKUP_SCHEDULE:-Daily 04:30 Europe/Istanbul}
cadence=${ROOMOS_BACKUP_CADENCE_SECONDS:-86400}
create_wrapper=${ROOMOS_BACKUP_CREATE:-$repo_dir/scripts/roomos-state-backup-create.sh}
restore_container="roomos-backup-restore-$$"

status() {
  python3 "$repo_dir/scripts/roomos_backup_status.py" "$1" --status-file "$status_file" \
    --schedule "$schedule" --cadence-seconds "$cadence" "${@:2}"
}

mkdir -p -m 0700 "$(dirname "$lock_file")"
exec 9>"$lock_file"
if ! flock -n 9; then
  status skip --reason locked || true
  echo "another RoomOS backup is already running; not starting a second one" >&2
  exit 75
fi

fail() {
  trap - INT TERM
  docker rm -f "$restore_container" >/dev/null 2>&1 || true
  status failure --reason "$1" || true
  echo "RoomOS backup failed: $1" >&2
  exit 1
}
trap 'fail interrupted' INT TERM

status start || { echo "backup status file is not writable" >&2; exit 1; }

# 1. Source validation (cheap preconditions; the create wrapper re-checks them too).
[[ -f "$key_file" && ! -L "$key_file" && $(stat -c '%a' "$key_file") == 600 ]] || fail source_validation_failed
[[ -d "$backup_dir" && ! -L "$backup_dir" && $(stat -c '%a' "$backup_dir") == 700 ]] || fail source_validation_failed
image=$(docker inspect "$core_container" --format '{{.Config.Image}}' 2>/dev/null) || fail source_validation_failed
health=$(docker inspect "$core_container" --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' 2>/dev/null) || fail source_validation_failed
[[ "$image" =~ ^roomos-core:sha-[0-9a-f]{7}$ && "$health" == healthy ]] || fail source_validation_failed

# 2. Encrypted artifact through the existing Core-only quiesce wrapper.
name="roomos-state-$(date -u +%Y%m%dT%H%M%SZ)-${image##*:sha-}.roomos-backup"
artifact="$backup_dir/$name"
"$create_wrapper" --output "$artifact" --key-file "$key_file" >/dev/null || fail backup_failed
[[ -f "$artifact" && -f "$artifact.metadata.json" ]] || fail backup_failed

# 3. Integrity: the encrypted bytes on disk must match the recorded checksum.
python3 - "$artifact" <<'PY' || fail integrity_failed
import hashlib, json, sys
artifact = sys.argv[1]
meta = json.load(open(artifact + ".metadata.json"))
digest = hashlib.sha256(open(artifact, "rb").read()).hexdigest()
sys.exit(0 if meta.get("encrypted_sha256") == digest and meta.get("restore_tested") is False else 1)
PY

# 4. Isolated restore: no network, no production state, no HA/FCM secrets, tmpfs only.
docker run --rm --name "$restore_container" --network none --user 0:0 --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=96m --tmpfs /restore:rw,noexec,nosuid,size=160m \
  --mount "type=bind,src=$repo_dir/scripts,dst=/tooling/scripts,readonly" \
  --mount "type=bind,src=$repo_dir/core/app/automations.py,dst=/usr/local/lib/python3.12/site-packages/app/automations.py,readonly" \
  --mount "type=bind,src=$repo_dir/core/app/routine_evaluations.py,dst=/tooling/core/app/routine_evaluations.py,readonly" \
  --mount "type=bind,src=$repo_dir/core/app/push.py,dst=/tooling/core/app/push.py,readonly" \
  --mount "type=bind,src=$repo_dir/core/app/resource_policy.py,dst=/tooling/core/app/resource_policy.py,readonly" \
  --mount "type=bind,src=$backup_dir,dst=/backup" \
  --mount "type=bind,src=$key_file,dst=/run/backup.key,readonly" \
  --entrypoint /bin/sh "$image" -ec '
    python /tooling/scripts/roomos_state_backup.py restore --artifact "/backup/$1" --metadata "/backup/$1.metadata.json" --key-file /run/backup.key --restore-root /restore/proof --report "/backup/$1.restore-report.json"
    python /tooling/scripts/roomos_state_backup.py runtime-gate --restore-root /restore/proof --report "/backup/$1.restore-report.json"
    python /tooling/scripts/roomos_state_backup.py attest --metadata "/backup/$1.metadata.json" --report "/backup/$1.restore-report.json"
    chown "$2" "/backup/$1.metadata.json" "/backup/$1.restore-report.json"
  ' sh "$name" "$(id -u):$(id -g)" >/dev/null || fail restore_failed

# 5. Cleanup: the restore container (and its plaintext tmpfs) must be gone.
if [[ -n $(docker ps -aq --filter "name=^${restore_container}$") ]]; then
  fail cleanup_failed
fi

# 6. Success only with attested restore-test evidence.
read -r created tested < <(python3 - "$artifact" <<'PY'
import json, sys
meta = json.load(open(sys.argv[1] + ".metadata.json"))
assert meta.get("restore_tested") is True and meta.get("created_at") and meta.get("restore_tested_at")
print(meta["created_at"], meta["restore_tested_at"])
PY
) || fail restore_failed
status success --success-at "$created" --restore-test-at "$tested" || fail cleanup_failed
trap - INT TERM

# 7. Bounded retention (never removes the newest restore-tested artifact).
if ! python3 "$repo_dir/scripts/roomos_backup_retention.py" --directory "$backup_dir" --apply; then
  status skip --reason retention_failed || true
  echo "retention failed; the new backup is valid and restore-tested" >&2
  exit 1
fi
echo "RoomOS backup protected and restore-tested at $tested"
