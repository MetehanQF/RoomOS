"""Trusted observation adapters for the canonical AlertEngine.

This module owns no tasks, persistence, sockets, or delivery. It only translates
existing RoomOS state into conservative lifecycle evidence.
"""

from .alerts import AlertObservation, AlertRule
from .devices import DeviceDetail, SensorView
from .health import HealthResponse
from .models import ServiceStatus, SystemMetric, SystemNode, SystemResponse
from .routine_evaluations import Evaluation
from .resource_policy import (
    DISK_FREE_CRITICAL_GB, DISK_FREE_RECOVER_GB, TEMPERATURE, UTILIZATION, AlertPolicyStore, MetricPolicy,
    valid_temperature,
)


def _rule(
    key: str,
    *,
    severity: str,
    type: str,
    title: str,
    message: str,
    source: str,
    failure: float,
    recovery: float,
) -> AlertRule:
    return AlertRule(
        key=key,
        severity=severity,
        type=type,
        title=title,
        message=message,
        source=source,
        failure_seconds=failure,
        recovery_seconds=recovery,
    )


def _fresh_number(metric: SystemMetric | None, units: set[str | None]) -> float | None:
    if (
        metric is None
        or not metric.available
        or metric.stale
        or metric.freshness != "fresh"
        or metric.unit not in units
        or isinstance(metric.value, bool)
        or not isinstance(metric.value, int | float)
    ):
        return None
    value = float(metric.value)
    return value if value == value and abs(value) != float("inf") else None


def _resource_observation(
    *,
    key: str,
    label: str,
    resource: str,
    value: float | None,
    unit: str,
    warning: float,
    critical: float,
    warning_recover: float,
    critical_recover: float,
    failure: float,
    recovery: float,
    source: str,
) -> AlertObservation:
    severity = "critical" if value is not None and value >= critical else "warning"
    threshold = critical if severity == "critical" else warning
    recover = critical_recover if severity == "critical" else warning_recover
    state = "unknown" if value is None else "failed" if value >= threshold else "healthy" if value <= recover else "hold"
    display = "unknown" if value is None else f"{value:g}{unit}"
    return AlertObservation(
        state=state,
        rule=_rule(
            key,
            severity=severity,
            type="resource.high",
            title=f"{label} {resource} is high",
            message=f"{resource} is {display}; sustained high usage requires attention.",
            source=source,
            failure=failure,
            recovery=recovery,
        ),
    )


def policy_observation(
    store: AlertPolicyStore,
    *,
    key: str,
    label: str,
    policy: MetricPolicy,
    value: float | None,
    source: str,
    profile_device: str,
    free_gb: float | None = None,
) -> AlertObservation:
    """7I: sustained, coverage-proven evaluation; recovery by hysteresis, confirmed over 2 minutes."""
    store.record(key, value)
    profile = store.profile(profile_device) if policy.profile_sensitive else "normal"
    display = "unknown" if value is None else f"{value:g}{policy.unit}"
    if profile != "normal":
        # The user declared heavy use: utilization is expected, and an alert opened before resolves.
        store.insights[key] = {
            "value": value, "unit": policy.unit, "profile": profile, "severity_basis": None, "threshold": None,
            "minutes": None, "coverage": None, "since": None, "free_gb": None,
            "reason": f"paused by the {profile} profile",
        }
        return AlertObservation(state="healthy", rule=_rule(
            key, severity="warning", type="resource.high", title=f"{label} {policy.name} is high",
            message=f"{policy.name} is {display}; paused by the {profile} profile.", source=source,
            failure=0, recovery=120,
        ))
    severity, met_level, evidence = "warning", None, None
    if policy.critical is not None:
        crit = store.sustained(key, policy.critical.threshold, policy.critical.minutes[profile] * 60)
        if crit.met:
            severity, met_level, evidence = "critical", policy.critical, crit
    free_critical = False
    if free_gb is not None and policy.critical is not None:
        # Real exhaustion: free space below the floor for the critical duration, whatever the percentage.
        store.record(f"{key}.free_low", 1.0 if free_gb < DISK_FREE_CRITICAL_GB else 0.0)
        free = store.sustained(f"{key}.free_low", 1.0, policy.critical.minutes[profile] * 60)
        if met_level is None and free.met:
            free_critical = True
            severity, met_level, evidence = "critical", policy.critical, free
    if met_level is None and policy.warning is not None:
        warn = store.sustained(key, policy.warning.threshold, policy.warning.minutes[profile] * 60)
        if warn.met:
            met_level, evidence = policy.warning, warn
    if met_level is not None and value is not None:
        state = "failed"
    elif value is None:
        state = "unknown"
    else:
        recover = (policy.warning or policy.critical).recover
        free_ok = free_gb is None or free_gb >= DISK_FREE_RECOVER_GB
        state = "healthy" if value <= recover and free_ok else "hold"
    minutes = met_level.minutes[profile] if met_level is not None else None
    reason = (f"free space {free_gb:g} GB is below {DISK_FREE_CRITICAL_GB:g} GB" if free_critical and severity == "critical"
              else f"above {met_level.threshold:g}{policy.unit} for at least {minutes} min" if met_level is not None
              else "within policy")
    store.insights[key] = {
        "value": value, "unit": policy.unit, "profile": profile, "severity_basis": severity if met_level else None,
        "threshold": met_level.threshold if met_level else None, "minutes": minutes,
        "coverage": evidence.coverage if evidence else None,
        "since": evidence.since.isoformat() if evidence and evidence.since else None,
        "free_gb": free_gb, "reason": reason,
    }
    return AlertObservation(
        state=state,
        rule=_rule(
            key, severity=severity, type="resource.high", title=f"{label} {policy.name} is high",
            # " is <value>;" stays first: the Turkish push renderer reads the real value from it.
            message=f"{policy.name} is {display}; {reason}.", source=source, failure=0, recovery=120,
        ),
    )


def _node_observations(node: SystemNode, node_failure_seconds: float, store: AlertPolicyStore | None = None) -> list[AlertObservation]:
    label = "Primary" if node.id == "primary" else "Secondary"
    online = node.metrics.get(f"{node.id}_online")
    online_fresh = bool(online and online.available and not online.stale and online.freshness == "fresh" and isinstance(online.value, bool))
    connection = "failed" if online_fresh and online.value is False else "healthy" if online_fresh and online.value is True else "unknown"
    observations = [AlertObservation(
        state=connection,
        rule=_rule(f"node.{node.id}.offline", severity="critical", type="node.offline",
                   title=f"{label} is offline", message=f"{label} has not passed its canonical online check.",
                   source=node.id, failure=node_failure_seconds, recovery=0),
    )]
    resources = (
        ("cpu", "CPU usage", "%", 90, 98, 80, 90, 300, 180),
        ("ram", "RAM usage", "%", 90, 97, 80, 90, 300, 180),
        ("disk", "disk usage", "%", 85, 95, 80, 90, 120, 120),
        ("temperature", "temperature", "°C", 75, 85, 70, 80, 120, 120),
    )
    for suffix, name, unit, warning, critical, warning_recover, critical_recover, failure, recovery in resources:
        metric = node.metrics.get(f"{node.id}_{suffix}")
        units = {"%"} if unit == "%" else {"°C", "C", "°c"}
        value = _fresh_number(metric, units)
        if value is not None and not (0 <= value <= (100 if unit == "%" else 125)):
            value = None
        if store is not None:
            policy = TEMPERATURE[node.id] if suffix == "temperature" else UTILIZATION[suffix]
            if suffix == "temperature":
                value = valid_temperature(value)
            observations.append(policy_observation(
                store, key=f"resource.{node.id}.{suffix}", label=label, policy=policy, value=value,
                source=node.id, profile_device=node.id,
            ))
            continue
        observations.append(_resource_observation(
            key=f"resource.{node.id}.{suffix}", label=label, resource=name, value=value, unit=unit,
            warning=warning, critical=critical, warning_recover=warning_recover,
            critical_recover=critical_recover, failure=failure, recovery=recovery, source=node.id,
        ))
    throttled = node.metrics.get(f"{node.id}_throttled")
    throttled_state = "unknown"
    if throttled and throttled.available and not throttled.stale and throttled.freshness == "fresh" and isinstance(throttled.value, bool):
        throttled_state = "failed" if throttled.value else "healthy"
    observations.append(AlertObservation(
        state=throttled_state,
        rule=_rule(f"node.{node.id}.throttled", severity="warning", type="node.throttled",
                   title=f"{label} is throttled", message=f"{label} reports an active throttling condition.",
                   source=node.id, failure=0, recovery=0),
    ))
    return observations


def _service_state(service: ServiceStatus | None, target: str) -> str:
    if service is None or service.stale or service.freshness != "fresh" or service.health == "unknown":
        return "unknown"
    if target == "offline":
        return "failed" if service.health == "offline" else "healthy"
    return "failed" if service.health == "degraded" else "healthy"


# 7I: a single sample must never open a service alert (one lost DNS reply opened ~5 warnings/day, and a
# single dual timeout could have produced a critical push). Pending transitions need >= 2 consecutive failed
# observations spanning these durations; any healthy sample resets them (AlertEngine semantics).
SERVICE_FAILURE_SECONDS = {"degraded": 150.0, "offline": 120.0}
SERVICE_RECOVERY_SECONDS = 60.0


def system_observations(system: SystemResponse, *, node_failure_seconds: float = 60,
                        store: AlertPolicyStore | None = None) -> list[AlertObservation]:
    observations = (_node_observations(system.primary, node_failure_seconds, store)
                    + _node_observations(system.secondary, node_failure_seconds, store))
    services = {item.id: item for item in system.services}
    labels = {"frigate": "Frigate", "nextcloud": "Nextcloud", "dns": "DNS"}
    for service_id, label in labels.items():
        service = services.get(service_id)
        for target, severity in (("degraded", "warning"), ("offline", "critical")):
            state = _service_state(service, target)
            if service_id == "dns":
                dns = system.dns_cluster.status
                state = "unknown" if dns == "unknown" else "failed" if dns == target else "healthy"
            observations.append(AlertObservation(
                state=state,
                rule=_rule(f"service.{service_id}.{target}", severity=severity, type=f"service.{target}",
                           title=f"{label} is {target}",
                           message=f"{label} reports a trusted {target} health state.", source=service_id,
                           failure=SERVICE_FAILURE_SECONDS[target], recovery=SERVICE_RECOVERY_SECONDS),
            ))
    return observations


def _sensor_value(sensor: SensorView | None) -> float | None:
    if (
        sensor is None
        or sensor.support != "supported"
        or sensor.availability != "available"
        or sensor.freshness != "fresh"
        or isinstance(sensor.value, bool)
        or not isinstance(sensor.value, int | float)
    ):
        return None
    value = float(sensor.value)
    return value if 0 <= value <= 125 else None


def device_observations(device: DeviceDetail | None, store: AlertPolicyStore | None = None,
                        free_gb: float | None = None) -> list[AlertObservation]:
    # Mobile browser absence and Android WorkManager cadence are intentionally excluded.
    if device is None or device.id != "workstation":
        return []
    # Workstation is a personal computer, not a server: switching it off is normal use and never an alert.
    # The key stays managed (always healthy) so an alert opened by an older release resolves.
    observations = [AlertObservation(
        state="healthy",
        rule=_rule("device.workstation.offline", severity="critical", type="device.offline",
                   title="Workstation is offline", message="Workstation is unavailable according to its trusted source.",
                   source="workstation", failure=60, recovery=60),
    )]
    telemetry = "unknown"
    if device.connection == "online":
        telemetry = "failed" if device.health == "degraded" else "healthy" if device.health == "healthy" else "unknown"
    observations.append(AlertObservation(
        state=telemetry,
        rule=_rule("telemetry.workstation.degraded", severity="warning", type="telemetry.degraded",
                   title="Workstation telemetry is degraded", message="One or more supported Workstation readings are unavailable or stale.",
                   source="workstation", failure=120, recovery=120),
    ))
    sensors = {item.id: item for item in device.sensors}
    switched_off = device.connection == "offline"
    if store is not None:
        # 7I: utilization is workload-aware; Workstation CPU/GPU temperature has no validated hardware limit,
        # so it never alerts. Unverified sensors (gpu_usage) yield no value and therefore no alert.
        for sensor_id, kind in (("cpu_usage", "cpu"), ("gpu_usage", "gpu"), ("ram_usage", "ram"), ("disk_usage", "disk")):
            observation = policy_observation(
                store, key=f"resource.workstation.{sensor_id}", label="Workstation", policy=UTILIZATION[kind],
                value=_sensor_value(sensors.get(sensor_id)), source="workstation", profile_device="workstation",
                free_gb=free_gb if kind == "disk" else None,
            )
            if switched_off and kind != "disk":
                # A switched-off computer is not under load; a full disk stays full (held as unknown).
                observation = observation.model_copy(update={"state": "healthy"})
            observations.append(observation)
        return observations
    for sensor_id, name, unit, warning, critical, warning_recover, critical_recover, failure, recovery in (
        ("cpu_usage", "CPU usage", "%", 90, 98, 80, 90, 300, 180),
        ("ram_usage", "RAM usage", "%", 90, 97, 80, 90, 300, 180),
        ("disk_usage", "disk usage", "%", 85, 95, 80, 90, 120, 120),
        ("cpu_temperature", "temperature", "°C", 75, 85, 70, 80, 120, 120),
    ):
        observations.append(_resource_observation(
            key=f"resource.workstation.{sensor_id}", label="Workstation", resource=name,
            value=_sensor_value(sensors.get(sensor_id)), unit=unit, warning=warning, critical=critical,
            warning_recover=warning_recover, critical_recover=critical_recover,
            failure=failure, recovery=recovery, source="workstation",
        ))
    return observations


def routine_observation(evaluation: Evaluation) -> AlertObservation:
    state = "unknown" if evaluation.completion_state != "complete" else "failed" if evaluation.outcome == "failed" else "healthy"
    return AlertObservation(
        state=state,
        rule=_rule(f"routine.{evaluation.routine_id}.failed", severity="warning", type="routine.failed",
                   title="Routine evaluation failed", message="A routine evaluation ended in failure; no automatic retry was attempted.",
                   source="routines", failure=0, recovery=0),
    )


def health_observations(health: HealthResponse) -> list[AlertObservation]:
    observations = []
    for component in health.components:
        state = "failed" if component.status in {"degraded", "unhealthy"} else "healthy" if component.status == "healthy" else "unknown"
        severity = "critical" if component.critical and component.status == "unhealthy" else "warning"
        observations.append(AlertObservation(
            state=state,
            rule=_rule(f"health.{component.id}", severity=severity, type="health.degraded",
                       title=f"{component.name} needs attention", message=component.summary,
                       source="health", failure=60, recovery=60),
        ))
    return observations


def background_observations(
    states: tuple[tuple[str, str], ...]
) -> list[AlertObservation]:
    critical = {"scheduler", "ha_bridge", "notification_dispatcher", "system_monitor"}
    labels = {
        "scheduler": "Routine scheduler",
        "ha_bridge": "Home Assistant bridge",
        "notification_dispatcher": "Notification dispatcher",
        "devices": "Device registry",
        "system_monitor": "System monitor",
        "camera": "Camera monitor",
        "dns_health": "DNS health monitor",
        "routines.diagnostics": "Routine diagnostics",
    }
    observations = []
    for name, status in states:
        state = "unknown" if status == "disabled" else "healthy" if status == "running" else "failed"
        label = labels.get(name, "Background service")
        observations.append(AlertObservation(
            state=state,
            rule=_rule(f"background.{name}.stopped", severity="critical" if name in critical else "warning",
                       type="background.stopped", title=f"{label} stopped",
                       message=f"{label} ended unexpectedly; RoomOS did not start an uncontrolled restart loop.",
                       source="runtime", failure=0, recovery=0),
        ))
    return observations
