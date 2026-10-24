#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: roomos-state-backup-create.sh --output FILE --key-file FILE

Create one encrypted RoomOS full-state backup with a proven Core-only quiesce.
The output and key must both be outside the Git repository. Environment overrides:
  ROOMOS_CORE_CONTAINER  (default: roomos-core-1)
  ROOMOS_STATE_ROOT      (default: /var/lib/roomos-auth)
EOF
}

output=
key_file=
while (($#)); do
  case "$1" in
    --output) output=${2:-}; shift 2 ;;
    --key-file) key_file=${2:-}; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "$output" || -z "$key_file" ]]; then
  usage >&2
  exit 2
fi

repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
core_container=${ROOMOS_CORE_CONTAINER:-roomos-core-1}
state_root=${ROOMOS_STATE_ROOT:-/var/lib/roomos-auth}
output=$(realpath -m "$output")
key_file=$(realpath -m "$key_file")

case "$output" in "$repo_dir"|"$repo_dir"/*) echo "backup output must be outside the Git repository" >&2; exit 1;; esac
case "$key_file" in "$repo_dir"|"$repo_dir"/*) echo "backup key must be outside the Git repository" >&2; exit 1;; esac

if [[ ! -f "$key_file" || -L "$key_file" ]]; then
  echo "backup key must be a regular non-symlink file" >&2
  exit 1
fi
if [[ $(stat -c '%a' "$key_file") != 600 ]]; then
  echo "backup key must have mode 0600" >&2
  exit 1
fi

status=$(docker inspect "$core_container" --format '{{.State.Status}}')
health=$(docker inspect "$core_container" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}')
image=$(docker inspect "$core_container" --format '{{.Config.Image}}')
revision=$(docker inspect "$core_container" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')
release=$(docker inspect "$core_container" --format '{{index .Config.Labels "org.opencontainers.image.version"}}')

if [[ "$status" != running || "$health" != healthy ]]; then
  echo "Core must be running and healthy before the quiesce" >&2
  exit 1
fi
if [[ ! "$image" =~ ^roomos-core:sha-[0-9a-f]{7}$ || ! "$revision" =~ ^[0-9a-f]{40}$ || -z "$release" ]]; then
  echo "Core image lacks the required immutable release identity" >&2
  exit 1
fi
if [[ "${image##*:sha-}" != "${revision:0:7}" ]]; then
  echo "Core image tag and OCI revision do not match" >&2
  exit 1
fi

mkdir -p -m 0700 "$(dirname "$output")"
output_parent_mode=$(stat -c '%a' "$(dirname "$output")")
if ((8#$output_parent_mode & 8#077)); then
  echo "backup output directory must not be accessible by group or others" >&2
  exit 1
fi
owner_uid=$(id -u)
owner_gid=$(id -g)
core_stopped=0

restart_core() {
  if ((core_stopped)); then
    docker start "$core_container" >/dev/null
    for _attempt in $(seq 1 30); do
      current=$(docker inspect "$core_container" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}')
      [[ "$current" == healthy ]] && return 0
      [[ "$current" == unhealthy || "$current" == exited || "$current" == dead ]] && return 1
      sleep 2
    done
    echo "Core did not become healthy after snapshot" >&2
    return 1
  fi
}
trap restart_core EXIT
trap 'restart_core; exit 130' INT TERM

docker stop --time 15 "$core_container" >/dev/null
core_stopped=1
if [[ $(docker inspect "$core_container" --format '{{.State.Status}}') != exited ]]; then
  echo "Core did not reach the quiesced state" >&2
  exit 1
fi

docker run --rm --network none --user 0:0 --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=96m \
  --tmpfs /dev/shm:rw,noexec,nosuid,size=160m \
  --mount "type=bind,src=$repo_dir/scripts,dst=/tooling/scripts,readonly" \
  --mount "type=bind,src=$repo_dir/core/app/automations.py,dst=/usr/local/lib/python3.12/site-packages/app/automations.py,readonly" \
  --mount "type=bind,src=$repo_dir/core/app/routine_evaluations.py,dst=/tooling/core/app/routine_evaluations.py,readonly" \
  --mount "type=bind,src=$repo_dir/core/app/push.py,dst=/tooling/core/app/push.py,readonly" \
  --mount "type=bind,src=$repo_dir/core/app/resource_policy.py,dst=/tooling/core/app/resource_policy.py,readonly" \
  --mount "type=bind,src=$state_root,dst=/state,readonly" \
  --mount "type=bind,src=$key_file,dst=/run/roomos-backup.key,readonly" \
  --mount "type=bind,src=$(dirname "$output"),dst=/backup" \
  --entrypoint python "$image" \
  /tooling/scripts/roomos_state_backup.py create \
  --state-root /state \
  --output "/backup/$(basename "$output")" \
  --key-file /run/roomos-backup.key \
  --release "$release" \
  --revision "$revision" \
  --core-image "$image" \
  --temp-root /dev/shm \
  --output-uid "$owner_uid" \
  --output-gid "$owner_gid"

restart_core
core_stopped=0
trap - EXIT INT TERM
printf 'backup=%s\nmetadata=%s\ncore_health=healthy\n' "$output" "$output.metadata.json"
