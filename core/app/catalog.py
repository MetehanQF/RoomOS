"""RoomOS device registry.

The single place where RoomOS logical IDs are mapped onto the Home Assistant
entities of a specific installation. Clients only ever see logical IDs such as
``living_room``; entity IDs never leave Core.

The registry is loaded once at startup from the path in
``ROOMOS_DEVICE_REGISTRY``, falling back to the bundled example. Anything absent
from it is unreachable through the API.
"""

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

GROUP_ID = "all_room_lights"

_PACKAGE_ROOT = Path(__file__).resolve().parent
# Repository layout: <repo>/config/devices.example.yaml, with the package at
# <repo>/core/app. In the container the example is copied in beside the package.
_CANDIDATE_DEFAULTS = (
    _PACKAGE_ROOT / "devices.example.yaml",
    _PACKAGE_ROOT.parents[1] / "config" / "devices.example.yaml",
)


@dataclass(frozen=True, slots=True)
class LightDefinition:
    id: str
    entity_id: str
    name: str
    min_kelvin: int
    max_kelvin: int
    supports_effects: bool = True


@dataclass(frozen=True, slots=True)
class NodeDefinition:
    id: str
    name: str
    entity_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ServiceDefinition:
    id: str
    source: str
    entity_id: str | None = None


def default_registry_path() -> Path:
    for candidate in _CANDIDATE_DEFAULTS:
        if candidate.exists():
            return candidate
    return _CANDIDATE_DEFAULTS[0]


def registry_path() -> Path:
    override = os.environ.get("ROOMOS_DEVICE_REGISTRY", "").strip()
    return Path(override) if override else default_registry_path()


def load_registry(path: Path | None = None) -> dict:
    target = path or registry_path()
    with target.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"RoomOS device registry {target} must be a mapping")
    return data


def _build_lights(raw: dict) -> tuple[LightDefinition, ...]:
    return tuple(
        LightDefinition(
            id=device_id,
            entity_id=entry["entity_id"],
            name=entry.get("name", device_id.replace("_", " ").title()),
            min_kelvin=int(entry.get("min_kelvin", 2500)),
            max_kelvin=int(entry.get("max_kelvin", 6500)),
            supports_effects=bool(entry.get("supports_effects", True)),
        )
        for device_id, entry in (raw.get("devices") or {}).items()
    )


def _build_nodes(raw: dict) -> tuple[NodeDefinition, ...]:
    return tuple(
        NodeDefinition(
            id=node_id,
            name=entry.get("name", node_id.replace("_", " ").title()),
            entity_ids=tuple(entry.get("entities") or ()),
        )
        for node_id, entry in (raw.get("nodes") or {}).items()
    )


def _build_services(raw: dict) -> tuple[ServiceDefinition, ...]:
    # ``source`` is a human-readable label, never an entity ID: it is sent to
    # the browser, and entity IDs must not reach a client.
    return tuple(
        ServiceDefinition(
            id=entry["id"],
            source=entry.get("source", "Home Assistant entity"),
            entity_id=entry.get("entity_id"),
        )
        for entry in (raw.get("services") or ())
    )


_REGISTRY = load_registry()

LIGHTS: tuple[LightDefinition, ...] = _build_lights(_REGISTRY)
NODES: tuple[NodeDefinition, ...] = _build_nodes(_REGISTRY)
SERVICES: tuple[ServiceDefinition, ...] = _build_services(_REGISTRY)

LIGHT_BY_ID = {light.id: light for light in LIGHTS}
LIGHT_BY_ENTITY = {light.entity_id: light for light in LIGHTS}
PHYSICAL_LIGHT_ENTITY_IDS = tuple(light.entity_id for light in LIGHTS)

NODE_BY_ID = {node.id: node for node in NODES}
NODE_IDS = tuple(node.id for node in NODES)

# Recorded only so a Home Assistant group entity stays out of the writable
# surface. RoomOS expands PHYSICAL_LIGHT_ENTITY_IDS instead, which is what makes
# per-target partial failure reporting possible.
EXCLUDED_GROUP_ENTITY_ID: str | None = (_REGISTRY.get("group") or {}).get("excluded_entity_id")

NODE_ENTITY_TO_NODE_ID = {
    entity_id: node.id for node in NODES for entity_id in node.entity_ids
}
SERVICE_ENTITY_IDS = tuple(
    service.entity_id for service in SERVICES if service.entity_id is not None
)
SYSTEM_ENTITIES = frozenset((*NODE_ENTITY_TO_NODE_ID, *SERVICE_ENTITY_IDS))
SUBSCRIBED_ENTITIES = frozenset((*LIGHT_BY_ENTITY, *SYSTEM_ENTITIES))


def node_id_for_entity(entity_id: str) -> str | None:
    return NODE_ENTITY_TO_NODE_ID.get(entity_id)
