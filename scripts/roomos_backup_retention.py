#!/usr/bin/env python3
"""Bounded retention for encrypted RoomOS state backups.

Only files in the given directory whose names match the exact RoomOS artifact pattern are
considered; nothing else (other backups, unrelated files, subdirectories) can be selected.
An artifact "group" is the artifact plus its metadata/restore-report siblings.

Policy:
  * keep the newest KEEP_TESTED restore-tested artifacts (metadata restore_tested=true);
  * never delete the newest restore-tested artifact, whatever else happens;
  * an untested artifact is deleted only if it is older than UNTESTED_GRACE and a newer
    restore-tested artifact exists (it stays available for diagnosis meanwhile);
  * artifacts with unreadable metadata are treated as untested.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

ARTIFACT = re.compile(r"^roomos-state-(\d{8}T\d{6}Z)-[0-9a-z][0-9a-z-]{0,39}\.roomos-backup$")
SIBLING_SUFFIXES = (".metadata.json", ".restore-report.json", ".roomos-restore-report.json")
KEEP_TESTED = 14
UNTESTED_GRACE = timedelta(days=7)


@dataclass(frozen=True)
class Artifact:
    path: Path
    created: datetime
    tested: bool

    def group(self) -> list[Path]:
        base = self.path.name
        stem = base[: -len(".roomos-backup")]
        candidates = [self.path, *(self.path.with_name(base + suffix) for suffix in SIBLING_SUFFIXES[:2]),
                      self.path.with_name(stem + SIBLING_SUFFIXES[2])]
        return [candidate for candidate in candidates if candidate.is_file() and not candidate.is_symlink()]


def scan(directory: Path) -> list[Artifact]:
    artifacts = []
    for entry in directory.iterdir():
        match = ARTIFACT.fullmatch(entry.name)
        if not match or not entry.is_file() or entry.is_symlink():
            continue
        created = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        tested = False
        try:
            metadata = json.loads(entry.with_name(entry.name + ".metadata.json").read_text())
            tested = metadata.get("restore_tested") is True
        except (OSError, ValueError):
            tested = False
        artifacts.append(Artifact(entry, created, tested))
    return sorted(artifacts, key=lambda item: item.created, reverse=True)


def select(artifacts: list[Artifact], now: datetime, keep_tested: int = KEEP_TESTED,
           untested_grace: timedelta = UNTESTED_GRACE) -> list[Artifact]:
    tested = [item for item in artifacts if item.tested]
    if not tested:
        return []  # without a known-good artifact nothing is ever removed
    newest_tested = tested[0]
    remove = [item for item in tested[keep_tested:] if item is not newest_tested]
    for item in artifacts:
        if not item.tested and now - item.created > untested_grace and newest_tested.created > item.created:
            remove.append(item)
    return sorted(remove, key=lambda item: item.created)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--apply", action="store_true", help="delete; default is a dry run")
    parser.add_argument("--keep-tested", type=int, default=KEEP_TESTED)
    args = parser.parse_args(argv)
    if args.keep_tested < 1:
        parser.error("--keep-tested must be at least 1")
    directory = Path(args.directory)
    if directory.is_symlink() or not directory.is_dir():
        parser.error("backup directory must be a real directory")
    artifacts = scan(directory)
    victims = select(artifacts, datetime.now(UTC), keep_tested=args.keep_tested)
    removed_files = 0
    for item in victims:
        for path in item.group():
            if args.apply:
                path.unlink()
            removed_files += 1
    tested = sum(item.tested for item in artifacts)
    print(json.dumps({"artifacts": len(artifacts), "tested": tested, "selected": len(victims),
                      "files": removed_files, "applied": args.apply}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
