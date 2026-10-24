#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
core_root="$repo_root/core"
python="$core_root/.venv/bin/python"

if [[ ! -x "$python" ]]; then
  echo "Missing $python" >&2
  echo "Create this worktree's environment with: python3 -m venv core/.venv && core/.venv/bin/pip install -e 'core[test]'" >&2
  exit 2
fi

actual_prefix="$($python -c 'from pathlib import Path; import sys; print(Path(sys.prefix).resolve())')"
expected_prefix="$(realpath "$core_root/.venv")"
if [[ "$actual_prefix" != "$expected_prefix" ]]; then
  echo "Refusing Python environment outside this worktree: $actual_prefix" >&2
  exit 2
fi

cd "$core_root"
actual_app="$($python -c 'from pathlib import Path; import app; print(Path(app.__file__).resolve())')"
expected_app="$(realpath "$core_root/app/__init__.py")"
if [[ "$actual_app" != "$expected_app" ]]; then
  echo "Refusing foreign RoomOS import: $actual_app" >&2
  exit 2
fi

exec "$python" -m pytest "$@"
