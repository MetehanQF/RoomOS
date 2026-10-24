"""Installation registry: RoomOS logical IDs -> Home Assistant entities.

Loaded once at import from the YAML file named by ``ROOMOS_INSTALLATION_PATH``,
falling back to the bundled ``installation.example.yaml`` (placeholder entities,
also used by the test suite). Anything absent from the registry is unreachable
through the API, and entity IDs never leave Core.

Logical IDs (light, node, camera) are fixed by the API contract; only the entity
mapping and display names come from the file. The module-level names below are
the stable interface the rest of Core imports.
"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

EXAMPLE_PATH = Path(__file__).resolve().parent / "installation.example.yaml"

LIGHT_IDS: tuple[str, ...] = ("room_main", "room_2", "room_3", "room_4", "desk_ambient")
NODE_IDS: tuple[str, ...] = ("primary", "secondary")
NODE_SENSOR_ROLES = frozenset({"cpu", "ram", "disk", "temperature", "uptime", "nextcloud"})
NODE_BINARY_ROLES = frozenset({"online", "docker", "cloudflared", "tailscale", "throttled", "smart", "portainer"})
NODE_REQUIRED_ROLES = frozenset({"cpu", "ram", "disk", "temperature", "uptime", "online"})


class InstallationConfigError(RuntimeError):
    """The installation registry is missing, malformed or incomplete."""


@dataclass(frozen=True, slots=True)
class LightDefinition:
    id: str
    entity_id: str
    name: str
    model: str
    min_kelvin: int
    max_kelvin: int
    supports_effects: bool = True


def _registry_path() -> Path:
    configured = os.environ.get("ROOMOS_INSTALLATION_PATH", "").strip()
    return Path(configured) if configured else EXAMPLE_PATH


def _require(mapping: Any, key: str, where: str) -> Any:
    if not isinstance(mapping, dict) or key not in mapping:
        raise InstallationConfigError(f"installation registry: missing {where}.{key}")
    return mapping[key]


def _entity(value: Any, domains: tuple[str, ...], where: str) -> str:
    if not isinstance(value, str) or "." not in value or value.split(".", 1)[0] not in domains:
        raise InstallationConfigError(f"installation registry: {where} must be a {'/'.join(domains)} entity id")
    return value


def _load(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise InstallationConfigError(f"installation registry not readable: {path}") from exc
    except yaml.YAMLError as exc:
        raise InstallationConfigError(f"installation registry is not valid YAML: {path}") from exc
    if not isinstance(data, dict):
        raise InstallationConfigError(f"installation registry must be a mapping: {path}")
    return data


_PATH = _registry_path()
_DATA = _load(_PATH)
if _PATH == EXAMPLE_PATH:
    logger.warning(
        "Using the bundled example installation registry; set ROOMOS_INSTALLATION_PATH to map your own entities."
    )

# --- Lights -----------------------------------------------------------------------------

def _light(light_id: str) -> LightDefinition:
    spec = _require(_require(_DATA, "lights", "root"), light_id, "lights")
    where = f"lights.{light_id}"
    return LightDefinition(
        light_id,
        _entity(_require(spec, "entity_id", where), ("light",), f"{where}.entity_id"),
        str(_require(spec, "name", where)),
        str(spec.get("model", "light")),
        int(_require(spec, "min_kelvin", where)),
        int(_require(spec, "max_kelvin", where)),
        bool(spec.get("supports_effects", True)),
    )


LIGHTS: tuple[LightDefinition, ...] = tuple(_light(light_id) for light_id in LIGHT_IDS)
LIGHT_BY_ID = {light.id: light for light in LIGHTS}
LIGHT_BY_ENTITY = {light.entity_id: light for light in LIGHTS}
if len(LIGHT_BY_ENTITY) != len(LIGHTS):
    raise InstallationConfigError("installation registry: two lights map to the same entity")

_GROUP = _require(_DATA, "light_group", "root")
# Exposed for read/control compatibility, but RoomOS all-light actions deliberately
# expand PHYSICAL_LIGHT_ENTITY_IDS instead of using this HA group.
HA_GROUP = LightDefinition(
    "ha_light_group",
    _entity(_require(_GROUP, "entity_id", "light_group"), ("light",), "light_group.entity_id"),
    str(_GROUP.get("name", "HA light group")),
    "HA group", 2500, 6500, False,
)
ALLOWED_HA_LIGHT_ENTITIES = frozenset((*LIGHT_BY_ENTITY, HA_GROUP.entity_id))
PHYSICAL_LIGHT_ENTITY_IDS = tuple(light.entity_id for light in LIGHTS)

# Logical group "main_room_lights". Commands expand to the member entities so each
# member is live-checked and reported on its own, like all_room_lights.
MAIN_LIGHT_IDS: tuple[str, ...] = tuple(_require(_GROUP, "main_members", "light_group"))
if not MAIN_LIGHT_IDS or any(light_id not in LIGHT_BY_ID for light_id in MAIN_LIGHT_IDS):
    raise InstallationConfigError("installation registry: light_group.main_members must name configured lights")
MAIN_LIGHTS: tuple[LightDefinition, ...] = tuple(LIGHT_BY_ID[light_id] for light_id in MAIN_LIGHT_IDS)
# Common Kelvin range of every member; a group value must be valid for all of them.
MAIN_GROUP = LightDefinition(
    "main_room_lights", HA_GROUP.entity_id, str(_GROUP.get("main_name", "Main room lights")), "light group",
    max(light.min_kelvin for light in MAIN_LIGHTS), min(light.max_kelvin for light in MAIN_LIGHTS), False,
)

# --- Monitored hosts ----------------------------------------------------------------------

def _node_entities(node_id: str) -> dict[str, str]:
    spec = _require(_require(_DATA, "nodes", "root"), node_id, "nodes")
    entities = _require(spec, "entities", f"nodes.{node_id}")
    if not isinstance(entities, dict):
        raise InstallationConfigError(f"installation registry: nodes.{node_id}.entities must be a mapping")
    missing = NODE_REQUIRED_ROLES - set(entities)
    if missing:
        raise InstallationConfigError(f"installation registry: nodes.{node_id} is missing {sorted(missing)}")
    result: dict[str, str] = {}
    for role, entity_id in entities.items():
        where = f"nodes.{node_id}.entities.{role}"
        if role in NODE_SENSOR_ROLES:
            result[role] = _entity(entity_id, ("sensor",), where)
        elif role in NODE_BINARY_ROLES:
            result[role] = _entity(entity_id, ("binary_sensor",), where)
        else:
            raise InstallationConfigError(f"installation registry: unknown role {where}")
    return result


# node id -> role -> entity id
NODE_ENTITIES: dict[str, dict[str, str]] = {node_id: _node_entities(node_id) for node_id in NODE_IDS}
NODE_NAMES: dict[str, str] = {
    node_id: str(_DATA["nodes"][node_id].get("name", node_id.title())) for node_id in NODE_IDS
}
PRIMARY_ENTITIES: tuple[str, ...] = tuple(NODE_ENTITIES["primary"].values())
SECONDARY_ENTITIES: tuple[str, ...] = tuple(NODE_ENTITIES["secondary"].values())
# entity id -> (node id, API metric id "<node>_<role>")
NODE_METRIC_BY_ENTITY: dict[str, tuple[str, str]] = {
    entity_id: (node_id, f"{node_id}_{role}")
    for node_id, roles in NODE_ENTITIES.items()
    for role, entity_id in roles.items()
}
if len(NODE_METRIC_BY_ENTITY) != len(PRIMARY_ENTITIES) + len(SECONDARY_ENTITIES):
    raise InstallationConfigError("installation registry: an entity is mapped to more than one node role")

_FRIGATE = _require(_DATA, "frigate", "root")
FRIGATE_STATUS_ENTITY = _entity(_require(_FRIGATE, "status", "frigate"), ("sensor",), "frigate.status")
FRIGATE_ENTITIES = (
    FRIGATE_STATUS_ENTITY,
    _entity(_require(_FRIGATE, "uptime", "frigate"), ("sensor",), "frigate.uptime"),
)
SYSTEM_ENTITIES = frozenset((*PRIMARY_ENTITIES, *SECONDARY_ENTITIES, *FRIGATE_ENTITIES))

# --- Camera -------------------------------------------------------------------------------
# Camera infrastructure identifiers remain server-side. API clients receive only
# the stable logical id ``room_camera`` and bounded status/event metadata.
_CAMERA = _require(_DATA, "camera", "root")
CAMERA_STATUS_ENTITY = _entity(_require(_CAMERA, "status", "camera"), ("sensor",), "camera.status")
CAMERA_LAST_PERSON_ENTITY = _entity(
    _require(_CAMERA, "last_person", "camera"), ("input_datetime", "sensor"), "camera.last_person"
)
CAMERA_LAST_PERSON_ID_ENTITY = _entity(
    _require(_CAMERA, "last_person_id", "camera"), ("input_text", "sensor"), "camera.last_person_id"
)
CAMERA_IMAGE_ENTITY = _entity(_require(_CAMERA, "image", "camera"), ("image",), "camera.image")
CAMERA_ENTITIES = (
    CAMERA_STATUS_ENTITY,
    FRIGATE_STATUS_ENTITY,
    CAMERA_LAST_PERSON_ENTITY,
    CAMERA_LAST_PERSON_ID_ENTITY,
    CAMERA_IMAGE_ENTITY,
)

# --- Routines -----------------------------------------------------------------------------
# These Home Assistant identifiers are private server mappings. Routine clients
# see only the bounded ``presence_home`` and solar transition abstractions.
_PRESENCE = _require(_DATA, "presence", "root")
PRESENCE_ENTITY_ID = _entity(_require(_PRESENCE, "person", "presence"), ("person",), "presence.person")
PRESENCE_TRACKER_ENTITY_ID = _entity(
    _require(_PRESENCE, "tracker", "presence"), ("device_tracker",), "presence.tracker"
)
SOLAR_ENTITY_ID = _entity(_PRESENCE.get("solar", "sun.sun"), ("sun",), "presence.solar")
AUTOMATION_ENTITIES = (PRESENCE_ENTITY_ID, SOLAR_ENTITY_ID)

SUBSCRIBED_ENTITIES = frozenset(
    (*LIGHT_BY_ENTITY, *SYSTEM_ENTITIES, *CAMERA_ENTITIES, *AUTOMATION_ENTITIES)
)
