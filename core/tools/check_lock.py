"""Consistency checks for core/requirements.lock.

``python tools/check_lock.py`` (default, used by the test suite) verifies that the lock is fully
pinned and hashed and satisfies every dependency range declared in pyproject.toml.

``python tools/check_lock.py --installed`` (used by the Dockerfile) verifies that the running
environment contains exactly the locked distributions and that the dependency closure of
roomos-core, including requested extras, is covered by the lock.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from importlib import metadata
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parent.parent
LOCK_PATH = CORE_DIR / "requirements.lock"
PYPROJECT_PATH = CORE_DIR / "pyproject.toml"
PROJECT_NAME = "roomos-core"
UNLOCKED = {"pip", PROJECT_NAME, "setuptools", "wheel"}

_ENTRY = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)$")
_HASH = re.compile(r"^--hash=sha256:[0-9a-f]{64}$")


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_lock(text: str) -> dict[str, str]:
    """Return {canonical name: version}; raise ValueError on any unpinned or unhashed entry."""
    pins: dict[str, str] = {}
    order: list[str] = []
    logical = re.sub(r"\\\n", " ", text)
    for number, raw in enumerate(logical.splitlines(), start=1):
        line = "" if raw.lstrip().startswith("#") else raw.strip()
        if not line:
            continue
        requirement, *options = line.split()
        match = _ENTRY.match(requirement)
        if not match:
            raise ValueError(f"lock entry {number} is not an exact '==' pin: {requirement}")
        if not options or not all(_HASH.match(option) for option in options):
            raise ValueError(f"lock entry {requirement} must carry only sha256 --hash options")
        name = canonical(match.group(1))
        if name in pins:
            raise ValueError(f"duplicate lock entry: {name}")
        if name in UNLOCKED:
            raise ValueError(f"{name} must not be locked")
        pins[name] = match.group(2)
        order.append(name)
    if order != sorted(order):
        raise ValueError("lock entries must be sorted by canonical name")
    if not pins:
        raise ValueError("lock is empty")
    return pins


def check_static(lock_text: str, pyproject_text: str) -> list[str]:
    from packaging.requirements import Requirement

    pins = parse_lock(lock_text)
    errors = []
    for spec in tomllib.loads(pyproject_text)["project"]["dependencies"]:
        requirement = Requirement(spec)
        name = canonical(requirement.name)
        version = pins.get(name)
        if version is None:
            errors.append(f"{name} is declared in pyproject.toml but missing from the lock")
        elif not requirement.specifier.contains(version, prereleases=True):
            errors.append(f"{name}=={version} violates pyproject.toml range '{requirement.specifier}'")
    return errors


def _closure(requirement_type) -> set[str]:
    """Walk installed metadata from roomos-core, honouring extras and environment markers."""
    visited: set[tuple[str, str]] = set()
    names: set[str] = set()
    pending = [(PROJECT_NAME, "")]
    while pending:
        name, extra = pending.pop()
        if (name, extra) in visited:
            continue
        visited.add((name, extra))
        names.add(name)
        try:
            specs = metadata.requires(name) or []
        except metadata.PackageNotFoundError:
            continue
        for spec in specs:
            requirement = requirement_type(spec)
            if requirement.marker and not requirement.marker.evaluate({"extra": extra}):
                continue
            dependency = canonical(requirement.name)
            pending.append((dependency, ""))
            pending.extend((dependency, canonical(item)) for item in requirement.extras)
    names.discard(PROJECT_NAME)
    return names


def check_installed(lock_text: str) -> list[str]:
    # The runtime image has no standalone packaging distribution; pip's vendored copy is enough here.
    from pip._vendor.packaging.requirements import Requirement

    pins = parse_lock(lock_text)
    installed = {
        canonical(dist.metadata["Name"]): dist.version
        for dist in metadata.distributions()
        if canonical(dist.metadata["Name"]) not in UNLOCKED
    }
    errors = []
    for name in sorted(pins.keys() | installed.keys()):
        if name not in installed:
            errors.append(f"{name} is locked but not installed")
        elif name not in pins:
            errors.append(f"{name}=={installed[name]} is installed but not locked")
        elif pins[name] != installed[name]:
            errors.append(f"{name} installed {installed[name]} but locked {pins[name]}")
    required = _closure(Requirement)
    for name in sorted(required - pins.keys()):
        errors.append(f"{name} is required by roomos-core but missing from the lock")
    for name in sorted(pins.keys() - required):
        errors.append(f"{name} is locked but no longer required by roomos-core")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lock", type=Path, default=LOCK_PATH)
    parser.add_argument("--pyproject", type=Path, default=PYPROJECT_PATH)
    parser.add_argument("--installed", action="store_true")
    args = parser.parse_args()
    lock_text = args.lock.read_text()
    try:
        errors = check_installed(lock_text) if args.installed else check_static(lock_text, args.pyproject.read_text())
    except ValueError as exc:
        errors = [str(exc)]
    for error in errors:
        print(f"requirements.lock out of sync: {error}", file=sys.stderr)
    if errors:
        print("regenerate with scripts/lock-core-deps.sh <image> (see core/requirements.lock header)", file=sys.stderr)
        return 1
    print("requirements.lock is in sync")
    return 0


if __name__ == "__main__":
    sys.exit(main())
