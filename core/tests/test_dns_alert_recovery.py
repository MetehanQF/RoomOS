"""DNS observation gaps must not fabricate alert recovery or notification intents."""
import json

import pytest

from app.alerts import AlertEngine, AlertStateFile
from app.dns_health import derive_cluster_status, unknown_dns_cluster
from app.models import DnsClusterStatus, DnsResolverStatus
from app.production_notifications import ProductionNotificationPolicy
from app.system_monitor import SystemMonitor
from test_alerts import immediate_service_rules, Clock, system
from test_production_notifications import RecordingSink, snapshot


# These tests exercise lifecycle/recovery/notification semantics, not the 7I service debounce.
pytestmark = pytest.mark.usefixtures("immediate_service_rules")


def observed(primary, secondary, clock):
    members = [DnsResolverStatus(id=role, role=role, status=status, last_checked=clock())
               for role, status in [('primary', primary), ('secondary', secondary)]]
    state = system()
    state.dns_cluster = DnsClusterStatus(status=derive_cluster_status(*members),
                                        primary=members[0], secondary=members[1], last_checked=clock())
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize('initial', ['degraded', 'offline'])
async def test_repeated_unknown_preserves_exact_alert_and_history(tmp_path, initial):
    clock = Clock(); events = []
    async def publish(event): events.append(event)
    path = tmp_path / 'alerts.json'
    engine = AlertEngine(str(path), publish, clock=clock)
    await engine.evaluate(system(dns=initial))
    before = json.loads(path.read_text())
    for _ in range(5):
        clock.advance(60)
        await engine.evaluate(system(dns='unknown'))
    assert json.loads(path.read_text()) == before
    assert [e.type for e in events] == ['alert.started']
    assert engine.list_alerts().active[0].id == f'service.dns.{initial}'
    assert AlertStateFile.model_validate_json(path.read_text()).model_dump(mode='json', exclude_none=True) == before
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_initial_unknown_then_degraded_activates_once_and_healthy_recovers(tmp_path):
    clock = Clock(); engine = AlertEngine(str(tmp_path / 'alerts.json'), clock=clock)
    await engine.evaluate(system(dns='unknown'))
    assert engine.list_alerts().active_count == 0
    assert engine.list_activity(limit=100, offset=0, category="all").total == 0
    state = observed('healthy', 'offline', clock)
    assert state.dns_cluster.status == 'degraded'
    await engine.evaluate(state)
    first = engine.list_alerts().active[0].model_dump()
    clock.advance(1)
    await engine.evaluate(state)
    assert engine.list_alerts().active[0].model_dump() == first
    assert engine.list_activity(limit=100, offset=0, category="all").total == 1
    await engine.evaluate(observed('healthy', 'healthy', clock))
    assert engine.list_alerts().active_count == 0
    assert [r.type for r in engine.list_activity(limit=100, offset=0, category="all").activity] == ['alert_resolved', 'alert_started']
    clock.advance(1)
    await engine.evaluate(observed('healthy', 'healthy', clock))
    assert engine.list_activity(limit=100, offset=0, category="all").total == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('initial', ['degraded', 'offline'])
async def test_duplicate_startup_holds_persisted_dns_until_first_probe(tmp_path, initial):
    clock = Clock(); path = tmp_path / 'alerts.json'
    first = AlertEngine(str(path), clock=clock)
    await first.evaluate(system(dns=initial))
    before = json.loads(path.read_text())
    for _ in range(2):
        restarted = AlertEngine(str(path), clock=clock)
        startup = system(); startup.dns_cluster = unknown_dns_cluster()
        assert startup.dns_cluster.last_checked is None
        clock.advance(1)
        await restarted.evaluate(startup)
        assert json.loads(path.read_text()) == before
        await restarted.evaluate(system(dns=initial))
        assert json.loads(path.read_text()) == before
    await restarted.evaluate(observed('healthy', 'healthy', clock))
    assert restarted.list_alerts().active_count == 0
    assert restarted.list_activity(limit=100, offset=0, category="all").total == 2


@pytest.mark.asyncio
async def test_known_failure_severity_transitions_and_other_services_unchanged(tmp_path):
    engine = AlertEngine(str(tmp_path / 'alerts.json'), clock=Clock())
    await engine.evaluate(system(dns='degraded', frigate='offline'))
    await engine.evaluate(system(dns='unknown', frigate='healthy'))
    assert [a.id for a in engine.list_alerts().active] == ['service.dns.degraded']
    await engine.evaluate(system(dns='offline'))
    assert [a.id for a in engine.list_alerts().active] == ['service.dns.offline']
    await engine.evaluate(system(dns='degraded'))
    assert [a.id for a in engine.list_alerts().active] == ['service.dns.degraded']


@pytest.mark.asyncio
@pytest.mark.parametrize('gap', ['unknown', 'stale', 'unavailable'])
async def test_existing_production_notification_recovery_waits_for_confirmed_health(tmp_path, gap):
    clock = Clock(); sink = RecordingSink()
    policy = ProductionNotificationPolicy(tmp_path / 'incidents.json', sink, clock=clock)
    await policy.initialize_rollout(snapshot(), epoch=clock())
    alerts = AlertEngine(str(tmp_path / 'alerts.json'), clock=clock)
    async def publish(event): pass
    monitor = SystemMonitor(object(), object(), alerts, publish, policy)
    clock.advance(1)
    await monitor.accept(snapshot(dns='offline'))
    clock.advance(61)
    await monitor.accept(snapshot(dns='offline'))
    assert [i.event_type for i in sink.intents] == ['service.down']
    # A DNS observation gap must not create alert recovery or a recovery intent,
    # including if the service's previous health value remains healthy but stale.
    unknown = snapshot(dns='unknown')
    service = next(s for s in unknown.services if s.id == 'dns')
    service.freshness = gap
    if gap != 'unknown':
        service.status = 'online'; service.health = 'healthy'; service.stale = gap == 'stale'
    for _ in range(3):
        clock.advance(60)
        await monitor.accept(unknown)
    assert [i.event_type for i in sink.intents] == ['service.down']
    assert [a.id for a in alerts.list_alerts().active] == ['service.dns.offline']
    assert alerts.list_activity(limit=100, offset=0, category="all").total == 1
    clock.advance(1)
    await monitor.accept(snapshot())
    assert [i.event_type for i in sink.intents] == ['service.down']
    clock.advance(30)
    await monitor.accept(snapshot())
    assert [i.event_type for i in sink.intents] == ['service.down', 'service.recovered']
    clock.advance(60)
    await monitor.accept(snapshot())
    assert len(sink.intents) == 2


@pytest.mark.asyncio
async def test_degraded_unknown_cycle_never_generates_dns_push(tmp_path):
    clock = Clock()
    sink = RecordingSink()
    policy = ProductionNotificationPolicy(tmp_path / 'incidents.json', sink, clock=clock)
    await policy.initialize_rollout(snapshot(), epoch=clock())
    alerts = AlertEngine(str(tmp_path / 'alerts.json'), clock=clock)
    async def publish(event): pass
    monitor = SystemMonitor(object(), object(), alerts, publish, policy)
    for status in ['degraded', 'unknown', 'unknown', 'degraded']:
        clock.advance(61)
        await monitor.accept(snapshot(dns=status))
    assert alerts.list_activity(limit=100, offset=0, category='all').total == 1
    assert alerts.list_alerts().active[0].id == 'service.dns.degraded'
    for _ in range(2):
        clock.advance(61)
        await monitor.accept(snapshot())
    assert alerts.list_alerts().active_count == 0
    assert sink.intents == []  # Redundancy loss is not total service outage in push policy.
