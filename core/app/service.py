from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, status

from .catalog import (
    LIGHTS,
    LIGHT_BY_ID,
    NODE_BY_ID,
    NODES,
    PHYSICAL_LIGHT_ENTITY_IDS,
    SERVICES,
)
from .ha_client import HomeAssistantClient, HomeAssistantUnavailable
from .models import (
    Capabilities,
    GroupCommandResult,
    GroupTargetFailure,
    LightState,
    ServiceStatus,
    SystemMetric,
    SystemNode,
    SystemResponse,
)


def light_or_404(light_id: str):
    definition = LIGHT_BY_ID.get(light_id)
    if definition is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown light ID")
    return definition


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def light_from_ha(definition, state: dict[str, Any] | None) -> LightState:
    attributes = state.get("attributes", {}) if state else {}
    available = bool(state and state.get("state") != "unavailable")
    effects = attributes.get("effect_list") or []
    return LightState(
        id=definition.id,
        name=definition.name,
        available=available,
        on=state.get("state") == "on" if available else None,
        brightness=attributes.get("brightness") if available else None,
        color_mode=attributes.get("color_mode") if available else None,
        rgb=attributes.get("rgb_color") if available else None,
        hs=attributes.get("hs_color") if available else None,
        color_temperature_kelvin=attributes.get("color_temp_kelvin") if available else None,
        effect=attributes.get("effect") if available else None,
        effects=effects,
        capabilities=Capabilities(
            effects=definition.supports_effects and bool(effects),
            min_kelvin=definition.min_kelvin,
            max_kelvin=definition.max_kelvin,
        ),
        last_updated=_parse_datetime(state.get("last_updated")) if state else None,
    )


async def require_live_light_capability(
    client: HomeAssistantClient, definition, action: str
) -> LightState:
    current = light_from_ha(definition, await client.get_state(definition.entity_id))
    if not current.available:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="RoomOS device is unavailable",
        )
    capability = "effects" if action == "effect" else action
    if not getattr(current.capabilities, capability, False):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Capability is not currently advertised for this device",
        )
    return current


async def list_lights(client: HomeAssistantClient) -> list[LightState]:
    states = await client.get_states([light.entity_id for light in LIGHTS])
    return [light_from_ha(light, states.get(light.entity_id)) for light in LIGHTS]


async def set_power(client: HomeAssistantClient, entity_ids: str | list[str], on: bool) -> None:
    await client.call_service(
        "light", "turn_on" if on else "turn_off", {"entity_id": entity_ids}
    )


async def set_all_power(client: HomeAssistantClient, on: bool) -> GroupCommandResult:
    states = await client.get_states(PHYSICAL_LIGHT_ENTITY_IDS)
    succeeded: list[str] = []
    failed: list[GroupTargetFailure] = []
    for light in LIGHTS:
        state = states.get(light.entity_id)
        if not state or state.get("state") == "unavailable":
            failed.append(GroupTargetFailure(device_id=light.id, reason="unavailable"))
            continue
        try:
            await set_power(client, light.entity_id, on)
        except HomeAssistantUnavailable:
            failed.append(GroupTargetFailure(device_id=light.id, reason="service_failed"))
        else:
            succeeded.append(light.id)
    return GroupCommandResult(accepted=not failed, succeeded=succeeded, failed=failed)


def validate_kelvin(definition, kelvin: int) -> None:
    if not definition.min_kelvin <= kelvin <= definition.max_kelvin:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Kelvin must be between {definition.min_kelvin} and {definition.max_kelvin}",
        )


def validate_live_effect(current: LightState, effect: str) -> None:
    if effect not in current.effects:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Unsupported effect for this light",
        )


def _metric(entity_id: str, state: dict[str, Any] | None) -> SystemMetric:
    attrs = state.get("attributes", {}) if state else {}
    updated = _parse_datetime(state.get("last_updated")) if state else None
    available = bool(state and state.get("state") not in {"unknown", "unavailable"})
    stale = not updated or datetime.now(UTC) - updated > timedelta(minutes=5)
    raw = state.get("state") if state else None
    if entity_id.startswith("binary_sensor.") and available:
        value: Any = raw == "on"
    else:
        try:
            value = float(raw) if raw is not None and "." in str(raw) else int(raw)
        except (TypeError, ValueError):
            value = raw
    return SystemMetric(
        id=entity_id.split(".", 1)[1],
        value=value if available else None,
        unit=attrs.get("unit_of_measurement"),
        available=available,
        stale=stale,
        last_updated=updated,
    )


async def _node(
    client: HomeAssistantClient, node_id: str, name: str, entity_ids: tuple[str, ...]
) -> SystemNode:
    states = await client.get_states(entity_ids)
    metrics = {entity_id.split(".", 1)[1]: _metric(entity_id, states[entity_id]) for entity_id in entity_ids}
    online_metric = metrics.get(f"{node_id}_online")
    online = online_metric.value if online_metric and online_metric.available else None
    return SystemNode(id=node_id, name=name, online=online, metrics=metrics)


def node_or_404(node_id: str):
    definition = NODE_BY_ID.get(node_id)
    if definition is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown node ID")
    return definition


async def system_node(client: HomeAssistantClient, node_id: str) -> SystemNode:
    definition = node_or_404(node_id)
    return await _node(client, definition.id, definition.name, definition.entity_ids)


def _service_status(state: dict[str, Any] | None) -> str:
    raw = state.get("state") if state else None
    if raw in {None, "unknown", "unavailable"}:
        return "unknown"
    return "online" if raw == "on" else "offline"


async def system_overview(client: HomeAssistantClient) -> SystemResponse:
    nodes = {node.id: await system_node(client, node.id) for node in NODES}
    backing_entity_ids = tuple(
        service.entity_id for service in SERVICES if service.entity_id is not None
    )
    states = await client.get_states(backing_entity_ids) if backing_entity_ids else {}
    services = [
        ServiceStatus(
            id=service.id,
            status="online"
            if service.entity_id is None
            else _service_status(states.get(service.entity_id)),
            source=service.source,
        )
        for service in SERVICES
    ]
    return SystemResponse(nodes=nodes, services=services)


def ha_unavailable_response(exc: HomeAssistantUnavailable) -> HTTPException:
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
