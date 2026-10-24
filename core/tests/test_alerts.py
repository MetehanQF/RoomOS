from datetime import UTC, datetime, timedelta
import os

import pytest

from app.alerts import AlertEngine, AlertObservation, AlertRule, rules_from_system
from app.models import (
    DnsClusterStatus,
    DnsResolverStatus,
    ServiceStatus,
    SystemMetric,
    SystemNode,
    SystemResponse,
)


@pytest.fixture
def immediate_service_rules(monkeypatch):
    """Isolate lifecycle tests from the 7I service debounce (covered by test_service_debounce_*)."""
    import app.smart_alerts as smart_alerts
    monkeypatch.setattr(smart_alerts, "SERVICE_FAILURE_SECONDS", {"degraded": 0.0, "offline": 0.0})
    monkeypatch.setattr(smart_alerts, "SERVICE_RECOVERY_SECONDS", 0.0)


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, seconds: int):
        self.now += timedelta(seconds=seconds)


def metric(id: str, value, *, available: bool = True):
    return SystemMetric(
        id=id,
        value=value if available else None,
        available=available,
        stale=False,
        freshness="fresh" if available else "unknown",
    )


def system(
    *,
    primary_online=True,
    secondary_online=True,
    throttled=False,
    dns="healthy",
    frigate="healthy",
    nextcloud="healthy",
):
    primary = SystemNode(
        id="primary",
        name="Primary",
        online=primary_online,
        metrics={
            "primary_online": metric("primary_online", primary_online),
            "primary_throttled": metric("primary_throttled", throttled),
        },
    )
    secondary = SystemNode(
        id="secondary",
        name="Secondary",
        online=secondary_online,
        metrics={"secondary_online": metric("secondary_online", secondary_online)},
    )
    checked = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    member = "healthy" if dns == "healthy" else "offline" if dns == "offline" else "unknown"
    cluster = DnsClusterStatus(
        status=dns,
        primary=DnsResolverStatus(id="primary", role="primary", status=member, last_checked=checked),
        secondary=DnsResolverStatus(id="secondary", role="secondary", status=member, last_checked=checked),
        last_checked=checked,
    )

    def service(id, health):
        return ServiceStatus(
            id=id,
            status="offline" if health == "offline" else "online" if health in {"healthy", "degraded"} else "unknown",
            health=health,
            source="canonical fixture",
            freshness="fresh" if health != "unknown" else "unknown",
        )

    return SystemResponse(
        primary=primary,
        secondary=secondary,
        services=[
            service("home_assistant", "healthy"),
            service("frigate", frigate),
            service("nextcloud", nextcloud),
            service("dns", dns),
        ],
        dns_cluster=cluster,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("node", ["primary", "secondary"])
async def test_node_offline_requires_confirmation_and_recovers(tmp_path, node):
    clock = Clock()
    events = []

    async def publish(event):
        events.append(event)

    engine = AlertEngine(
        str(tmp_path / "alerts.json"), publish, confirmation_seconds=30, clock=clock
    )
    state = system(**{f"{node}_online": False})
    await engine.evaluate(state)
    assert engine.list_alerts().active == []
    clock.advance(31)
    await engine.evaluate(state)
    assert [item.id for item in engine.list_alerts().active] == [f"node.{node}.offline"]
    assert events[-1].type == "alert.started"
    clock.advance(1)
    await engine.evaluate(system())
    assert engine.list_alerts().active == []
    assert events[-1].type == "alert.resolved"


@pytest.mark.asyncio
async def test_pi_throttling_warning_and_camera_privacy_is_not_a_rule(tmp_path):
    engine = AlertEngine(str(tmp_path / "alerts.json"))
    await engine.evaluate(system(throttled=True))
    alert = engine.list_alerts().active[0]
    assert (alert.id, alert.severity) == ("node.primary.throttled", "warning")
    serialized = str(rules_from_system(system()))
    assert "camera" not in serialized.lower()
    assert "privacy" not in serialized.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dns", "expected_id", "severity"),
    [
        ("healthy", None, None),
        ("unknown", None, None),
        ("degraded", "service.dns.degraded", "warning"),
        ("offline", "service.dns.offline", "critical"),
    ],
)
async def test_dns_alert_semantics(tmp_path, dns, expected_id, severity):
    clock = Clock()
    engine = AlertEngine(str(tmp_path / f"{dns}.json"), clock=clock)
    await engine.evaluate(system(dns=dns))
    assert engine.list_alerts().active == []  # 7I: one sample never opens a service alert
    for _ in range(3):
        clock.advance(61)
        await engine.evaluate(system(dns=dns))
    active = engine.list_alerts().active
    assert [item.id for item in active] == ([] if expected_id is None else [expected_id])
    if active:
        assert active[0].severity == severity


@pytest.mark.asyncio
@pytest.mark.parametrize("service", ["frigate", "nextcloud"])
async def test_trusted_core_service_offline_is_critical(tmp_path, service):
    clock = Clock()
    engine = AlertEngine(str(tmp_path / f"{service}.json"), clock=clock)
    await engine.evaluate(system(**{service: "offline"}))
    assert engine.list_alerts().active == []
    for _ in range(3):
        clock.advance(61)
        await engine.evaluate(system(**{service: "offline"}))
    active = engine.list_alerts().active
    assert [(item.id, item.severity) for item in active] == [
        (f"service.{service}.offline", "critical")
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("immediate_service_rules")
async def test_duplicate_refresh_does_not_duplicate_alert_or_activity(tmp_path):
    engine = AlertEngine(str(tmp_path / "alerts.json"))
    await engine.evaluate(system(dns="degraded"))
    await engine.evaluate(system(dns="degraded"))
    assert engine.list_alerts().active_count == 1
    activity = engine.list_activity(limit=100, offset=0, category="all")
    assert activity.total == 1
    assert activity.activity[0].type == "alert_started"


@pytest.mark.asyncio
@pytest.mark.usefixtures("immediate_service_rules")
async def test_flapping_history_is_bounded(tmp_path):
    clock = Clock()
    engine = AlertEngine(str(tmp_path / "alerts.json"), history_limit=5, clock=clock)
    for _ in range(8):
        await engine.evaluate(system(dns="offline"))
        clock.advance(1)
        await engine.evaluate(system(dns="healthy"))
        clock.advance(1)
    activity = engine.list_activity(limit=100, offset=0, category="all")
    assert activity.total == 5
    assert {item.type for item in activity.activity} == {"alert_started", "alert_resolved"}


@pytest.mark.asyncio
@pytest.mark.usefixtures("immediate_service_rules")
async def test_persistence_survives_restart_and_is_mode_0600(tmp_path):
    path = tmp_path / "alerts.json"
    first = AlertEngine(str(path))
    await first.evaluate(system(frigate="offline"))
    second = AlertEngine(str(path))
    assert second.list_alerts().active[0].id == "service.frigate.offline"
    assert second.list_activity(limit=10, offset=0, category="alerts").total == 1
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
@pytest.mark.usefixtures("immediate_service_rules")
async def test_corrupt_persistence_is_fail_safe_and_not_overwritten(tmp_path):
    path = tmp_path / "alerts.json"
    path.write_text('{"not":"valid state"}', encoding="utf-8")
    original = path.read_bytes()
    engine = AlertEngine(str(path))
    assert engine.storage_status == "corrupt"
    await engine.evaluate(system(dns="offline"))
    assert engine.list_alerts().active_count == 1
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_scene_activity_is_logical_bounded_and_filterable(tmp_path):
    engine = AlertEngine(str(tmp_path / "alerts.json"))
    occurred = datetime(2026, 10, 3, 12, 30, tzinfo=UTC)
    await engine.record_scene_activation(
        scene_id="night",
        scene_name="Gece",
        status="success",
        occurred_at=occurred,
        source="user",
    )
    result = engine.list_activity(limit=10, offset=0, category="scenes")
    assert result.total == 1
    assert result.activity[0].title == "Gece activated"
    serialized = result.model_dump_json()
    assert "entity_id" not in serialized
    assert "192.168" not in serialized


@pytest.mark.asyncio
async def test_home_assistant_connection_lifecycle_is_deduplicated(tmp_path):
    engine = AlertEngine(str(tmp_path / "alerts.json"))
    await engine.evaluate_ha_connection(False)
    await engine.evaluate_ha_connection(False)
    assert engine.list_alerts().active_count == 1
    assert engine.list_activity(limit=10, offset=0, category="all").total == 1
    await engine.evaluate_ha_connection(True)
    assert engine.list_alerts().active_count == 0
    assert engine.list_activity(limit=10, offset=0, category="all").total == 2


def smart_rule(*, failure_seconds=30, recovery_seconds=30):
    return AlertRule(
        key="resource.primary.cpu",
        severity="warning",
        type="resource.high",
        title="Primary CPU usage is high",
        message="CPU usage has remained above 90%.",
        source="primary",
        failure_seconds=failure_seconds,
        recovery_seconds=recovery_seconds,
    )


@pytest.mark.asyncio
async def test_observation_lifecycle_debounces_open_and_recovery(tmp_path):
    clock = Clock()
    engine = AlertEngine(str(tmp_path / "alerts.json"), clock=clock)
    failed = AlertObservation(state="failed", rule=smart_rule())
    healthy = AlertObservation(state="healthy", rule=smart_rule())

    await engine.evaluate_observations([failed])
    clock.advance(31)
    await engine.evaluate_observations([failed])
    assert [item.id for item in engine.list_alerts().active] == ["resource.primary.cpu"]

    await engine.evaluate_observations([healthy])
    clock.advance(31)
    await engine.evaluate_observations([healthy])
    assert engine.list_alerts().active == []
    assert [item.type for item in engine.list_activity(limit=10, offset=0, category="alerts").activity] == [
        "alert_resolved", "alert_started"
    ]


@pytest.mark.asyncio
async def test_unknown_observation_resets_pending_and_never_recovers_active_alert(tmp_path):
    clock = Clock()
    engine = AlertEngine(str(tmp_path / "alerts.json"), clock=clock)
    rule = smart_rule()
    failed = AlertObservation(state="failed", rule=rule)
    unknown = AlertObservation(state="unknown", rule=rule)
    healthy = AlertObservation(state="healthy", rule=rule)

    await engine.evaluate_observations([failed])
    clock.advance(31)
    await engine.evaluate_observations([unknown])
    await engine.evaluate_observations([failed])
    assert engine.list_alerts().active == []
    clock.advance(31)
    await engine.evaluate_observations([failed])
    assert engine.list_alerts().active_count == 1

    await engine.evaluate_observations([healthy])
    clock.advance(31)
    await engine.evaluate_observations([unknown])
    assert engine.list_alerts().active_count == 1


@pytest.mark.asyncio
async def test_pending_transition_survives_restart_without_bypassing_confirmation(tmp_path):
    clock = Clock()
    path = tmp_path / "alerts.json"
    rule = smart_rule(failure_seconds=60)
    failed = AlertObservation(state="failed", rule=rule)
    first = AlertEngine(str(path), clock=clock)
    await first.evaluate_observations([failed])
    clock.advance(45)

    second = AlertEngine(str(path), clock=clock)
    await second.evaluate_observations([failed])
    assert second.list_alerts().active == []
    clock.advance(16)
    await second.evaluate_observations([failed])
    assert second.list_alerts().active_count == 1


@pytest.mark.asyncio
async def test_repeated_failure_is_one_bounded_record_and_one_lifecycle_event(tmp_path):
    clock = Clock()
    engine = AlertEngine(str(tmp_path / "alerts.json"), clock=clock)
    failed = AlertObservation(state="failed", rule=smart_rule(failure_seconds=0))
    for _ in range(500):
        await engine.evaluate_observations([failed])
        clock.advance(1)

    active = engine.list_alerts().active
    assert len(active) == 1
    assert active[0].occurrence_count == 2
    assert engine.list_activity(limit=100, offset=0, category="alerts").total == 1


@pytest.mark.asyncio
async def test_service_debounce_single_lost_dns_reply_never_opens_an_alert(tmp_path):
    """Reproduces 2026-10-16: one failed cycle between healthy cycles (secondary lost one UDP reply)."""
    clock = Clock()
    engine = AlertEngine(str(tmp_path / "alerts.json"), clock=clock)
    for status in ["healthy", "degraded", "healthy", "degraded", "healthy", "degraded", "degraded", "healthy"]:
        await engine.evaluate(system(dns=status))
        clock.advance(60)
    assert engine.list_activity(limit=100, offset=0, category="all").total == 0


@pytest.mark.asyncio
async def test_service_debounce_dual_timeout_cycle_never_produces_a_critical(tmp_path):
    clock = Clock()
    seen = []
    engine = AlertEngine(str(tmp_path / "alerts.json"), clock=clock)
    async def sink(transition, alert): seen.append((transition, alert.severity))
    engine.add_lifecycle_sink(sink)
    for status in ["offline", "healthy", "offline", "healthy"]:
        await engine.evaluate(system(dns=status))
        clock.advance(60)
    assert seen == [] and engine.list_alerts().active == []


@pytest.mark.asyncio
async def test_service_debounce_sustained_outage_opens_once_and_recovery_needs_confirmation(tmp_path):
    clock = Clock()
    engine = AlertEngine(str(tmp_path / "alerts.json"), clock=clock)
    for _ in range(4):
        await engine.evaluate(system(frigate="offline"))
        clock.advance(60)
    assert [a.id for a in engine.list_alerts().active] == ["service.frigate.offline"]
    await engine.evaluate(system())  # one healthy sample is not recovery
    assert engine.list_alerts().active_count == 1
    clock.advance(61)
    await engine.evaluate(system())
    assert engine.list_alerts().active_count == 0
    assert engine.list_activity(limit=100, offset=0, category="all").total == 2


def count_replaces(monkeypatch):
    import app.alerts as alerts_module
    calls = []
    real = alerts_module.os.replace

    def replace(source, target):
        calls.append(str(target))
        return real(source, target)

    monkeypatch.setattr(alerts_module.os, "replace", replace)
    return calls


@pytest.mark.asyncio
async def test_unchanged_evaluations_do_not_rewrite_the_document(tmp_path, monkeypatch):
    clock = Clock()
    path = tmp_path / "alerts.json"
    engine = AlertEngine(str(path), clock=clock)
    replaces = count_replaces(monkeypatch)
    healthy = AlertObservation(state="healthy", rule=smart_rule())
    await engine.evaluate_observations([healthy])
    assert len(replaces) == 1  # first write of the process is always made durable
    inode = path.stat().st_ino
    for _ in range(20):
        clock.advance(15)
        await engine.evaluate_observations([healthy])
        await engine.evaluate_ha_connection(True)
    assert len(replaces) == 1 and path.stat().st_ino == inode
    assert path.read_text(encoding="utf-8") == engine._state.model_dump_json(exclude_none=True)


@pytest.mark.asyncio
async def test_every_state_change_is_still_written_before_lifecycle_sinks(tmp_path, monkeypatch):
    clock = Clock()
    path = tmp_path / "alerts.json"
    engine = AlertEngine(str(path), clock=clock)
    rule = smart_rule()
    failed = AlertObservation(state="failed", rule=rule)
    healthy = AlertObservation(state="healthy", rule=rule)
    seen_on_disk = []

    async def sink(transition, alert):
        stored = AlertEngine(str(path)).alert_by_id(alert.id)
        seen_on_disk.append((transition, stored.status, stored.updated_at == alert.updated_at))

    engine.add_lifecycle_sink(sink)
    states = []
    for observation in [failed, failed, healthy, healthy]:
        await engine.evaluate_observations([observation])
        states.append(path.read_text(encoding="utf-8"))
        assert states[-1] == engine._state.model_dump_json(exclude_none=True)
        clock.advance(31)
    assert seen_on_disk == [("started", "active", True), ("resolved", "resolved", True)]
    assert len(set(states)) == len(states)  # pending/alert/activity changes each produced a new write


@pytest.mark.asyncio
async def test_failed_write_is_retried_even_when_the_document_is_unchanged(tmp_path, monkeypatch):
    import app.alerts as alerts_module
    clock = Clock()
    path = tmp_path / "alerts.json"
    engine = AlertEngine(str(path), clock=clock)
    healthy = AlertObservation(state="healthy", rule=smart_rule())
    await engine.evaluate_observations([healthy])
    real_fsync = alerts_module.os.fsync
    failures = {"left": 1}

    def flaky_directory_fsync(fd):
        if failures["left"] and os.path.isdir(f"/proc/self/fd/{fd}"):
            failures["left"] -= 1
            raise OSError(5, "EIO")
        return real_fsync(fd)

    monkeypatch.setattr(alerts_module.os, "fsync", flaky_directory_fsync)
    await engine.evaluate_observations([AlertObservation(state="failed", rule=smart_rule(failure_seconds=0))])
    assert engine.write_failed is True
    replaces = count_replaces(monkeypatch)
    await engine.evaluate_ha_connection(True)  # no state change, but durability was never confirmed
    assert replaces == [str(path)] and engine.write_failed is False
    await engine.evaluate_ha_connection(True)
    assert replaces == [str(path)]
    assert AlertEngine(str(path)).list_alerts().active_count == 1


@pytest.mark.asyncio
async def test_restart_writes_once_then_skips_identical_documents(tmp_path, monkeypatch):
    clock = Clock()
    path = tmp_path / "alerts.json"
    first = AlertEngine(str(path), clock=clock)
    await first.evaluate_observations([AlertObservation(state="failed", rule=smart_rule(failure_seconds=0))])
    before = path.read_bytes()
    replaces = count_replaces(monkeypatch)
    second = AlertEngine(str(path), clock=clock)
    await second.evaluate_ha_connection(True)
    await second.evaluate_ha_connection(True)
    assert len(replaces) == 1 and path.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["delete", "replace", "truncate"])
async def test_externally_changed_file_is_rewritten_on_the_next_evaluation(tmp_path, monkeypatch, tamper):
    clock = Clock()
    path = tmp_path / "alerts.json"
    engine = AlertEngine(str(path), clock=clock)
    await engine.evaluate_observations([AlertObservation(state="failed", rule=smart_rule(failure_seconds=0))])
    expected = path.read_bytes()
    if tamper == "delete":
        path.unlink()
    elif tamper == "replace":
        other = tmp_path / "other.json"
        other.write_bytes(expected)
        os.replace(other, path)
    else:
        path.write_text("{}", encoding="utf-8")
    await engine.evaluate_ha_connection(True)  # no state change: only the file identity check rewrites
    assert path.read_bytes() == engine._state.model_dump_json(exclude_none=True).encode()
    assert path.stat().st_mode & 0o777 == 0o600
    assert AlertEngine(str(path)).list_alerts().active_count == 1
