"""Generate shared/api/event.schema.json from the Core EventType Literal.

Run from core/:  python tools/export_event_schema.py          (writes the schema)
                 python tools/export_event_schema.py --check  (exit 1 when out of date)
"""

import json
import sys
from pathlib import Path
from typing import get_args

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import EventType  # noqa: E402

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "shared" / "api" / "event.schema.json"


def build_schema() -> dict:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://roomos.local/schemas/event.schema.json",
        "title": "RoomOS Event",
        "$comment": "Generated from core/app/models.py EventType by core/tools/export_event_schema.py. Do not edit by hand.",
        "type": "object",
        "required": ["type", "timestamp", "data"],
        "properties": {
            "type": {"type": "string", "enum": list(get_args(EventType))},
            "timestamp": {"type": "string", "format": "date-time"},
            "device_id": {"type": ["string", "null"]},
            "data": {"type": "object"},
        },
        "additionalProperties": False,
    }


def render() -> str:
    return json.dumps(build_schema(), indent=2) + "\n"


def main(argv: list[str]) -> int:
    expected = render()
    if "--check" in argv:
        current = SCHEMA_PATH.read_text(encoding="utf-8") if SCHEMA_PATH.exists() else ""
        if current != expected:
            print(f"{SCHEMA_PATH} is out of date; run python tools/export_event_schema.py", file=sys.stderr)
            return 1
        return 0
    SCHEMA_PATH.write_text(expected, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
