"""Event contract parity: what Core emits == EventType == shared/api/event.schema.json.

The Web union is compared with the same schema in web/src/event-contract.test.ts.
"""

import ast
import importlib.util
import json
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from app.models import EventType, RoomOSEvent

CORE = Path(__file__).resolve().parents[1]
SCHEMA = CORE.parent / "shared" / "api" / "event.schema.json"
# Calls whose first positional argument is an event type (RoutineEngine._emit).
TYPE_FORWARDERS = {"_emit"}


def emitted_types() -> tuple[set[str], list[str]]:
    literals: set[str] = set()
    dynamic: list[str] = []
    for path in sorted((CORE / "app").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
            if name == "RoomOSEvent":
                for keyword in node.keywords:
                    if keyword.arg != "type":
                        continue
                    if isinstance(keyword.value, ast.Constant):
                        literals.add(keyword.value.value)
                    else:
                        dynamic.append(f"{path.name}:{node.lineno}")
            elif name in TYPE_FORWARDERS and node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str) and "." in node.args[0].value:
                literals.add(node.args[0].value)
    return literals, dynamic


def test_every_emitted_event_type_is_declared_and_every_declared_type_is_emitted():
    literals, dynamic = emitted_types()
    assert literals == set(get_args(EventType))
    # The only non-literal construction forwards a parameter typed as EventType.
    assert [site.split(":")[0] for site in dynamic] == ["automations.py", "routines.py"]


def test_undeclared_event_type_is_rejected_at_construction():
    with pytest.raises(ValidationError):
        RoomOSEvent(type="system.node_online")


@pytest.mark.skipif(not SCHEMA.exists(), reason="shared/ is not part of the Core image; runs in the repository")
def test_shared_schema_is_generated_from_event_type():
    spec = importlib.util.spec_from_file_location("export_event_schema", CORE / "tools" / "export_event_schema.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert SCHEMA.read_text(encoding="utf-8") == module.render()
    assert json.loads(SCHEMA.read_text(encoding="utf-8"))["properties"]["type"]["enum"] == list(get_args(EventType))
