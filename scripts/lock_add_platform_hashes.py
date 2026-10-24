#!/usr/bin/env python3
"""Add PyPI wheel hashes for every supported image platform to core/requirements.lock.

lock-core-deps.sh records the hash of the wheel installed in one image, so the lock only builds on
that image's architecture. This adds, for each pinned version, the sha256 that PyPI publishes for
every CPython 3.12 glibc wheel on linux/amd64 and linux/arm64. Existing hashes are kept.
"""
import json
import re
import sys
import urllib.request
from pathlib import Path

PLATFORM = re.compile(r"^(any|manylinux[0-9a-z_]*_(x86_64|aarch64))$")
MARKER = "# Additional hashes: PyPI wheels for linux/amd64 and linux/arm64 (glibc, CPython 3.12)."


def compatible(filename: str) -> bool:
    parts = filename[: -len(".whl")].split("-")
    py_tags, abi_tags, plat_tags = (set(p.split(".")) for p in parts[-3:])
    if not any(PLATFORM.match(tag) for tag in plat_tags):
        return False
    if "cp312" in abi_tags or ({"py3", "py312", "cp312"} & py_tags and "none" in abi_tags):
        return True
    return "abi3" in abi_tags and any(
        re.fullmatch(r"cp3(\d+)", tag) and int(tag[3:]) <= 12 for tag in py_tags
    )


def pypi_hashes(name: str, version: str) -> list[str]:
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    with urllib.request.urlopen(url, timeout=30) as response:
        files = json.load(response)["urls"]
    return sorted(
        f["digests"]["sha256"]
        for f in files
        if f["packagetype"] == "bdist_wheel" and not f.get("yanked") and compatible(f["filename"])
    )


def main(lock_path: str) -> None:
    path = Path(lock_path)
    header, entries = [], []
    for block in re.split(r"\n(?=[a-z0-9])", path.read_text(encoding="utf-8").strip()):
        if block.startswith("#"):
            header.extend(line for line in block.splitlines() if line != MARKER)
            continue
        lines = [line.strip(" \\") for line in block.splitlines()]
        name, version = lines[0].split("==")
        known = [line.removeprefix("--hash=sha256:") for line in lines[1:]]
        published = pypi_hashes(name, version)
        if not set(known) <= set(published):
            sys.exit(f"{name}=={version}: locked hash is not published on PyPI for a supported wheel")
        hashes = known + [digest for digest in published if digest not in known]
        entries.append(" \\\n".join([lines[0], *(f"    --hash=sha256:{d}" for d in hashes)]))
    path.write_text("\n".join([*header, MARKER, *entries]) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1])
