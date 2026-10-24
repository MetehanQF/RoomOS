#!/usr/bin/env bash
set -euo pipefail

release_name=${1:-}
if [[ ! "$release_name" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; then
  echo "usage: $0 <release-name>" >&2
  exit 2
fi

repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_dir"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "release builds require a clean Git worktree" >&2
  exit 1
fi

revision=$(git rev-parse HEAD)
short_revision=$(git rev-parse --short=7 HEAD)
image_tag="sha-${short_revision}"
build_created=$(date -u +%Y-%m-%dT%H:%M:%SZ)
source_uri=${ROOMOS_SOURCE_URI:-urn:roomos:source}

for image in "roomos-core:${image_tag}" "roomos-web:${image_tag}"; do
  if docker image inspect "$image" >/dev/null 2>&1; then
    echo "immutable image already exists: $image" >&2
    exit 1
  fi
done

common_args=(
  --build-arg "ROOMOS_RELEASE=${release_name}"
  --build-arg "ROOMOS_REVISION=${revision}"
  --build-arg "ROOMOS_BUILD_CREATED=${build_created}"
  --build-arg "ROOMOS_SOURCE_URI=${source_uri}"
)

docker build "${common_args[@]}" -t "roomos-core:${image_tag}" core
docker build "${common_args[@]}" -f web/Dockerfile -t "roomos-web:${image_tag}" .

printf 'ROOMOS_RELEASE=%s\n' "$release_name"
printf 'ROOMOS_REVISION=%s\n' "$revision"
printf 'ROOMOS_IMAGE_TAG=%s\n' "$image_tag"
printf 'ROOMOS_BUILD_CREATED=%s\n' "$build_created"
printf 'ROOMOS_SOURCE_URI=%s\n' "$source_uri"
