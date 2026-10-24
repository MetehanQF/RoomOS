import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, status

from .catalog import (
    FRIGATE_ENTITIES,
    FRIGATE_STATUS_ENTITY,
    LIGHTS,
    LIGHT_BY_ID,
    MAIN_LIGHTS,
    NODE_METRIC_BY_ENTITY,
    NODE_NAMES,
    PHYSICAL_LIGHT_ENTITY_IDS,
    PRIMARY_ENTITIES,
    SECONDARY_ENTITIES,
)
from .dns_health import unknown_dns_cluster
from .ha_client import HomeAssistantClient, HomeAssistantUnavailable
from .models import (
    Capabilities,
    DnsClusterStatus,
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


async def command_main_lights(client: HomeAssistantClient, service: str, data: dict[str, Any]) -> GroupCommandResult:
    """Send one light service to every available main-room light at the same time.

    Members are read once; unavailable members are reported and never called. Each call
    succeeds or fails on its own and is never retried. If Home Assistant cannot be read at
    all the caller receives HomeAssistantUnavailable (503), as for a single light.
    """
    states = await client.get_states([light.entity_id for light in MAIN_LIGHTS])
    targets = [light for light in MAIN_LIGHTS if (states.get(light.entity_id) or {}).get("state") not in (None, "unavailable")]
    outcomes = await asyncio.gather(
        *(client.call_service("light", service, {"entity_id": light.entity_id, **data}) for light in targets),
        return_exceptions=True,
    )
    by_id = {light.id: outcome for light, outcome in zip(targets, outcomes)}
    succeeded: list[str] = []
    failed: list[GroupTargetFailure] = []
    for light in MAIN_LIGHTS:
        if light.id not in by_id:
            failed.append(GroupTargetFailure(device_id=light.id, reason="unavailable"))
        elif isinstance(by_id[light.id], HomeAssistantUnavailable):
            failed.append(GroupTargetFailure(device_id=light.id, reason="service_failed"))
        elif isinstance(by_id[light.id], BaseException):
            raise by_id[light.id]
        else:
            succeeded.append(light.id)
    return GroupCommandResult(accepted=not failed, device_id="main_room_lights", succeeded=succeeded, failed=failed)


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


FAST_METRIC_SUFFIXES = frozenset({"cpu", "ram", "temperature"})
CADENCE_METRIC_SUFFIXES = frozenset({"uptime"})
FAST_METRIC_MAX_AGE = timedelta(minutes=3)
CADENCE_METRIC_MAX_AGE = timedelta(minutes=75)


def metric_id_for(entity_id: str) -> str:
    """API metric id: ``<node>_<role>`` for a mapped host entity, else the entity object id."""
    mapped = NODE_METRIC_BY_ENTITY.get(entity_id)
    return mapped[1] if mapped else entity_id.split(".", 1)[1]


def _metric(
    entity_id: str,
    state: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> SystemMetric:
    metric_id = metric_id_for(entity_id)
    attrs = state.get("attributes", {}) if state else {}
    updated = _parse_datetime(state.get("last_updated")) if state else None
    raw = state.get("state") if state else None
    if state is None or raw == "unknown":
        freshness = "unknown"
    elif raw == "unavailable":
        freshness = "unavailable"
    else:
        suffix = metric_id.rsplit("_", 1)[-1]
        max_age = (
            FAST_METRIC_MAX_AGE
            if suffix in FAST_METRIC_SUFFIXES
            else CADENCE_METRIC_MAX_AGE
            if suffix in CADENCE_METRIC_SUFFIXES
            else None
        )
        # HA last_updated is a state-change timestamp, not a poll timestamp. Stable
        # REST/integration values therefore use HA availability as their freshness
        # signal; only metrics expected to change on a cadence are aged here.
        freshness = (
            "stale"
            if max_age is not None
            and (updated is None or (now or datetime.now(UTC)) - updated > max_age)
            else "fresh"
        )
    available = freshness not in {"unknown", "unavailable"}
    stale = freshness == "stale"
    if entity_id.startswith("binary_sensor.") and available:
        value: Any = raw == "on"
    else:
        try:
            value = float(raw) if raw is not None and "." in str(raw) else int(raw)
        except (TypeError, ValueError):
            value = raw
    return SystemMetric(
        id=metric_id,
        value=value if available else None,
        unit=attrs.get("unit_of_measurement"),
        available=available,
        stale=stale,
        freshness=freshness,
        last_updated=updated,
    )


async def _node(
    client: HomeAssistantClient, node_id: str, name: str, entity_ids: tuple[str, ...]
) -> SystemNode:
    states = await client.get_states(entity_ids)
    metrics = {metric_id_for(entity_id): _metric(entity_id, states[entity_id]) for entity_id in entity_ids}
    online_metric = metrics.get(f"{node_id}_online")
    online = online_metric.value if online_metric and online_metric.available else None
    heartbeat_fresh = any(
        metrics.get(f"{node_id}_{suffix}")
        and metrics[f"{node_id}_{suffix}"].freshness == "fresh"
        for suffix in FAST_METRIC_SUFFIXES
    )
    if online is True and heartbeat_fresh:
        # Every node metric is rendered from one 60-second REST snapshot. A fresh
        # changing sibling proves the shared source is still being polled even
        # when an individual value (for example disk usage) has not changed.
        for metric in metrics.values():
            if metric.available:
                metric.freshness = "fresh"
                metric.stale = False
    elif online is False:
        for metric_id, metric in metrics.items():
            if metric_id != f"{node_id}_online" and metric.available:
                metric.freshness = "stale"
                metric.stale = True
    return SystemNode(id=node_id, name=name, online=online, metrics=metrics)


async def system_node(client: HomeAssistantClient, node_id: str) -> SystemNode:
    if node_id == "primary":
        return await _node(client, "primary", NODE_NAMES["primary"], PRIMARY_ENTITIES)
    return await _node(client, "secondary", NODE_NAMES["secondary"], SECONDARY_ENTITIES)


def _service_from_metric(
    service_id: str,
    source: str,
    metric: SystemMetric | None,
    *,
    healthy_values: frozenset[str],
    offline_values: frozenset[str],
) -> ServiceStatus:
    if metric is None or not metric.available or metric.value is None:
        freshness = metric.freshness if metric is not None else "unknown"
        return ServiceStatus(
            id=service_id,
            status="unknown",
            health="unknown",
            source=source,
            freshness=freshness,
        )
    normalized = str(metric.value).strip().lower()
    if normalized in healthy_values:
        status_value = "online"
    elif normalized in offline_values:
        status_value = "offline"
    else:
        status_value = "unknown"
    return ServiceStatus(
        id=service_id,
        status=status_value,
        health="healthy" if status_value == "online" else status_value,
        source=source,
        stale=metric.stale,
        freshness=metric.freshness,
    )


async def system_overview(
    client: HomeAssistantClient,
    dns_cluster: DnsClusterStatus | None = None,
) -> SystemResponse:
    dns_cluster = dns_cluster or unknown_dns_cluster()
    primary = await system_node(client, "primary")
    secondary = await system_node(client, "secondary")
    frigate_states = await client.get_states(FRIGATE_ENTITIES)
    frigate_metric = _metric(FRIGATE_STATUS_ENTITY, frigate_states[FRIGATE_STATUS_ENTITY])
    boolean_healthy = frozenset({"true", "on", "1"})
    boolean_offline = frozenset({"false", "off", "0"})
    services = [
        ServiceStatus(
            id="home_assistant",
            status="online",
            health="healthy",
            source="RoomOS HA API",
            freshness="fresh",
        ),
        _service_from_metric(
            "frigate",
            "Home Assistant Frigate integration",
            frigate_metric,
            healthy_values=frozenset({"running", "online", "healthy", "ok", "up"}),
            offline_values=frozenset({"stopped", "offline", "failed", "down", "error"}),
        ),
        _service_from_metric(
            "nextcloud",
            "Home Assistant REST monitor",
            primary.metrics.get("primary_nextcloud"),
            healthy_values=frozenset({"ok", "online", "healthy", "running", "up"}),
            offline_values=frozenset({"offline", "failed", "down", "error", "unhealthy"}),
        ),
        _service_from_metric(
            "docker",
            "Primary status monitor",
            primary.metrics.get("primary_docker"),
            healthy_values=boolean_healthy,
            offline_values=boolean_offline,
        ),
        ServiceStatus(
            id="dns",
            status=(
                "online"
                if dns_cluster.status in {"healthy", "degraded"}
                else "offline" if dns_cluster.status == "offline" else "unknown"
            ),
            health=dns_cluster.status,
            source="RoomOS functional DNS probe",
            freshness="fresh" if dns_cluster.last_checked is not None else "unknown",
        ),
        _service_from_metric(
            "tailscale",
            "Primary status monitor",
            primary.metrics.get("primary_tailscale"),
            healthy_values=boolean_healthy,
            offline_values=boolean_offline,
        ),
    ]
    return SystemResponse(
        primary=primary,
        secondary=secondary,
        services=services,
        dns_cluster=dns_cluster,
    )


def ha_unavailable_response(exc: HomeAssistantUnavailable) -> HTTPException:
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
