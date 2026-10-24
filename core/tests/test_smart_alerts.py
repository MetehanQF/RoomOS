from datetime import UTC, datetime

import pytest

from app.alerts import AlertEngine
from app.devices import DeviceRegistry, Reading
from app.health import HealthComponent, HealthResponse
from app.models import DnsClusterStatus, DnsResolverStatus, ServiceStatus, SystemMetric, SystemNode, SystemResponse
from app.routine_evaluations import Evaluation
from app.smart_alerts import background_observations, device_observations, health_observations, routine_observation, system_observations


NOW = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)


def metric(id, value, *, freshness="fresh", available=True, unit="%"):
    return SystemMetric(id=id, value=value if available else None, unit=unit, available=available,
                        stale=freshness == "stale", freshness=freshness)


def system(*, cpu=20, ram=30, disk=40, temperature=50, freshness="fresh"):
    def node(id):
        return SystemNode(id=id, name=id, online=True, metrics={
            f"{id}_online": metric(f"{id}_online", True, unit=None),
            f"{id}_cpu": metric(f"{id}_cpu", cpu, freshness=freshness),
            f"{id}_ram": metric(f"{id}_ram", ram, freshness=freshness),
            f"{id}_disk": metric(f"{id}_disk", disk, freshness=freshness),
            f"{id}_temperature": metric(f"{id}_temperature", temperature, freshness=freshness, unit="°C"),
            f"{id}_throttled": metric(f"{id}_throttled", False, unit=None),
        })
    resolver = DnsResolverStatus(id="primary", role="primary", status="healthy", last_checked=NOW)
    return SystemResponse(
        primary=node("primary"), secondary=node("secondary"),
        services=[ServiceStatus(id=id, status="online", health="healthy", source="fixture", freshness="fresh")
                  for id in ("home_assistant", "frigate", "nextcloud", "dns")],
        dns_cluster=DnsClusterStatus(status="healthy", primary=resolver,
                                     secondary=resolver.model_copy(update={"id": "secondary", "role": "secondary"}),
                                     last_checked=NOW),
    )


def by_key(observations):
    return {item.rule.key: item for item in observations}


def test_system_resource_thresholds_use_hysteresis_and_fresh_evidence_only():
    assert by_key(system_observations(system(cpu=91)))["resource.primary.cpu"].state == "failed"
    assert by_key(system_observations(system(cpu=85)))["resource.primary.cpu"].state == "hold"
    assert by_key(system_observations(system(cpu=79)))["resource.primary.cpu"].state == "healthy"
    assert by_key(system_observations(system(cpu=99, freshness="stale")))["resource.primary.cpu"].state == "unknown"


def test_disk_and_temperature_escalate_without_duplicate_fingerprints():
    observations = by_key(system_observations(system(disk=96, temperature=86)))
    assert observations["resource.primary.disk"].state == "failed"
    assert observations["resource.primary.disk"].rule.severity == "critical"
    assert observations["resource.primary.temperature"].rule.severity == "critical"
    assert len([key for key in observations if key == "resource.primary.disk"]) == 1


def test_only_workstation_generates_device_observations_and_stale_telemetry_is_unknown():
    registry = DeviceRegistry(
        {"system_monitor": 180, "home_assistant": 900, "mqtt": 300, "roomos_native": 120, "roomos_web": 120},
        clock=lambda: NOW, monotonic=lambda: 1,
    )
    handset = registry.detail("phone", frozenset({"devices:read"}))
    assert device_observations(handset) == []

    workstation = registry.detail("workstation", frozenset({"devices:read"}))
    assert by_key(device_observations(workstation))["telemetry.workstation.degraded"].state == "unknown"


def _workstation(connection):
    registry = DeviceRegistry(
        {"system_monitor": 180, "home_assistant": 900, "mqtt": 300, "roomos_native": 120, "roomos_web": 120},
        clock=lambda: NOW, monotonic=lambda: 1,
    )
    return registry.detail("workstation", frozenset({"devices:read"})).model_copy(update={"connection": connection})


@pytest.mark.parametrize("connection", ["offline", "online", "unknown"])
def test_workstation_being_switched_off_is_never_an_alert(connection):
    """Workstation is a personal computer, not a server: powering it off is normal use. The rule key stays
    managed (always healthy) so an alert opened by an older release resolves instead of staying active."""
    assert by_key(device_observations(_workstation(connection)))["device.workstation.offline"].state == "healthy"


def test_switching_workstation_off_releases_utilization_alerts_but_keeps_disk(tmp_path):
    from app.resource_policy import AlertPolicyStore
    store = AlertPolicyStore(str(tmp_path / "alert-policy.json"), clock=lambda: NOW)
    observations = by_key(device_observations(_workstation("offline"), store))
    for sensor in ("cpu_usage", "gpu_usage", "ram_usage"):
        assert observations[f"resource.workstation.{sensor}"].state == "healthy"
    assert observations["resource.workstation.disk_usage"].state == "unknown"   # the disk is still full
    assert observations["telemetry.workstation.degraded"].state == "unknown"


def test_node_offline_needs_five_minutes_so_a_reboot_is_not_critical():
    from app.config import Settings
    assert Settings.model_fields["alerts_node_confirmation_seconds"].default == 300.0
    secondary = by_key(system_observations(system(), node_failure_seconds=300))["node.secondary.offline"]
    assert secondary.rule.failure_seconds == 300 and secondary.rule.severity == "critical"


def test_failed_routine_evaluation_opens_and_success_recovers_same_fingerprint():
    failed = Evaluation(evaluation_id="ev_" + "a" * 32, routine_id="rt_" + "b" * 32,
                        evaluated_at=NOW, trigger_origin="manual", completion_state="complete",
                        outcome="failed", reason_code="execution_failed")
    success = failed.model_copy(update={"outcome": "executed", "reason_code": None, "execution_result": "success"})
    assert routine_observation(failed).state == "failed"
    assert routine_observation(success).state == "healthy"
    assert routine_observation(failed).rule.key == routine_observation(success).rule.key


def test_health_components_map_degraded_and_critical_unhealthy_without_unknown_recovery():
    response = HealthResponse(overall="unhealthy", checked_at=NOW, components=[
        HealthComponent(id="home_assistant", name="Home Assistant", status="unhealthy", summary="Disconnected", critical=True, checked_at=NOW),
        HealthComponent(id="backups", name="Backups", status="degraded", summary="Overdue", critical=False, checked_at=NOW),
        HealthComponent(id="telemetry_history", name="Telemetry", status="unknown", summary="Not checked", critical=False, checked_at=NOW),
    ])
    observations = by_key(health_observations(response))
    assert observations["health.home_assistant"].rule.severity == "critical"
    assert observations["health.backups"].rule.severity == "warning"
    assert observations["health.telemetry_history"].state == "unknown"


@pytest.mark.asyncio
async def test_system_evaluation_uses_sustained_resource_catalog(tmp_path):
    current = [NOW]
    engine = AlertEngine(str(tmp_path / "alerts.json"), clock=lambda: current[0])
    elevated = system(cpu=91)
    await engine.evaluate(elevated)
    current[0] = current[0].replace(minute=5, second=1)
    await engine.evaluate(elevated)
    assert [item.id for item in engine.list_alerts().active] == [
        "resource.primary.cpu", "resource.secondary.cpu"
    ]


def test_background_catalog_distinguishes_critical_running_and_disabled_tasks():
    observations = by_key(background_observations((
        ("scheduler", "failed"),
        ("devices", "running"),
        ("notification_dispatcher", "disabled"),
    )))
    assert observations["background.scheduler.stopped"].state == "failed"
    assert observations["background.scheduler.stopped"].rule.severity == "critical"
    assert observations["background.devices.stopped"].state == "healthy"
    assert observations["background.notification_dispatcher.stopped"].state == "unknown"
