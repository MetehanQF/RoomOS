import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.routine_evaluations import EvaluationStore, EvaluationDocument
from app.presence import PresenceObserver, TRACKER
from app.catalog import PRESENCE_ENTITY_ID, SOLAR_ENTITY_ID
from test_routines import engine, FakeScenes, managed_evening, managed_all_off, ha_state, solar_state

T = datetime(2026, 10, 6, 17, 45, tzinfo=UTC)
RID = 'rt_' + '1' * 32


@pytest.mark.asyncio
async def test_store_origin_retention_restart_failure(tmp_path, monkeypatch):
    now = [T]
    p = tmp_path / 'evaluations.json'
    store = EvaluationStore(str(p), clock=lambda: now[0])
    assert not p.exists()
    eid = await store.begin(RID, 'arrival')
    await store.finish(eid, outcome='executed', execution_id='ex_'+'2'*32, execution_result='partial')
    assert p.stat().st_mode & 0o777 == 0o600
    assert EvaluationStore(str(p), clock=lambda: now[0]).records()[0].trigger_origin == 'arrival'
    pending = await store.begin(RID, 'schedule')
    reloaded = EvaluationStore(str(p), clock=lambda: now[0])
    assert reloaded.records()[0].completion_state == 'incomplete'
    assert reloaded.records()[0].outcome is None
    now[0] += timedelta(days=30)
    assert len(store.records()) == 2  # inclusive boundary
    now[0] += timedelta(microseconds=1)
    assert store.records() == []
    monkeypatch.setattr('app.routine_evaluations.os.replace', lambda *a: (_ for _ in ()).throw(OSError()))
    await store.begin(RID, 'sunset')
    assert store.storage_status == 'write_failed' and len(store.records()) == 1
    assert not list(tmp_path.glob('.*.tmp'))
    assert pending


@pytest.mark.asyncio
async def test_completed_evaluation_notifies_listener_without_affecting_result(tmp_path):
    store = EvaluationStore(str(tmp_path / "evaluations.json"), clock=lambda: T)
    seen = []

    async def broken(_record):
        raise RuntimeError("private detail")

    async def capture(record):
        seen.append((record.routine_id, record.outcome))

    store.listeners.extend([broken, capture])
    eid = await store.begin(RID, "manual")
    await store.finish(eid, outcome="failed", reason_code="execution_failed")
    assert seen == [(RID, "failed")]
    assert store.records()[0].outcome == "failed"


@pytest.mark.asyncio
async def test_cap_corruption_and_legacy_optional_origin(tmp_path):
    store = EvaluationStore(None, clock=lambda: T)
    for _ in range(260):
        await store.begin(RID, 'sunset')
    assert len(store.records()) == 250
    legacy = store.document.model_dump(mode='json')
    del legacy['evaluations'][0]['trigger_origin']
    assert EvaluationDocument.model_validate(legacy).evaluations[0].trigger_origin is None
    path = tmp_path / 'bad.json'
    path.write_text('{bad')
    store = EvaluationStore(str(path), clock=lambda: T)
    await store.begin(RID, 'arrival')
    assert store.storage_status == 'corrupt' and path.read_text() == '{bad'


@pytest.mark.asyncio
@pytest.mark.parametrize('presence,reason', [('home', None), ('not_home','presence_away'), ('unknown','presence_unknown')])
async def test_sunset_decisions(tmp_path, presence, reason):
    scenes = FakeScenes(); r = engine(tmp_path, [managed_evening()], scenes)
    r.bootstrap_environment(ha_state(presence,T-timedelta(minutes=1)),solar_state('above_horizon',T-timedelta(minutes=1)))
    await r.handle_environment_event(SOLAR_ENTITY_ID,solar_state('below_horizon',T),now=T)
    row = r.evaluations.records()[0]
    assert row.trigger_origin == 'sunset' and row.reason_code == reason
    assert row.outcome == ('skipped' if reason else 'executed')
    assert len(scenes.calls) == (0 if reason else 1)
    assert (r.document.routines[0].last_run is None) == bool(reason)


@pytest.mark.asyncio
@pytest.mark.parametrize('local,reason', [('2026-10-07T01:59:00+03:00',None), ('2026-10-07T02:00:00+03:00','outside_window')])
async def test_arrival_boundary_and_duplicates(tmp_path,local,reason):
    t = datetime.fromisoformat(local);scenes=FakeScenes();r=engine(tmp_path,[managed_evening()],scenes)
    r.bootstrap_environment(ha_state('not_home',t-timedelta(minutes=1)),solar_state('below_horizon',t-timedelta(minutes=1)))
    assert r.evaluations.records()==[]
    for _ in range(2): await r.handle_environment_event(PRESENCE_ENTITY_ID,ha_state('home',t),now=t)
    assert len(r.evaluations.records())==1
    assert r.evaluations.records()[0].reason_code==reason
    assert len(scenes.calls)==(0 if reason else 1)


@pytest.mark.asyncio
async def test_repeated_arrivals_failure_and_schedule(tmp_path):
    scenes=FakeScenes(status='failed');r=engine(tmp_path,[managed_evening()],scenes)
    r.bootstrap_environment(ha_state('not_home',T-timedelta(minutes=1)),solar_state('below_horizon',T-timedelta(minutes=1)))
    for i,value in enumerate(['home','not_home','home']):
        await r.handle_environment_event(PRESENCE_ENTITY_ID,ha_state(value,T+timedelta(seconds=i)),now=T)
    assert len(r.evaluations.records())==2
    assert all(x.outcome=='failed' and x.reason_code=='execution_failed' for x in r.evaluations.records())
    r=engine(tmp_path,[managed_all_off()],FakeScenes())
    await r.tick(datetime(2026,10,6,23,0,tzinfo=UTC))
    assert r.evaluations.records()[0].trigger_origin=='schedule'
    await r.tick(datetime(2026,10,6,23,0,tzinfo=UTC))
    assert len(r.evaluations.records())==1


def test_presence_evidence_restart_restore_skew_and_no_gps():
    now=[T];mono=[0.0];p=PresenceObserver(clock=lambda:now[0],monotonic=lambda:mono[0])
    person=ha_state('home',T,source=TRACKER)
    p.bootstrap(person)
    assert p.snapshot().reporting=='unknown'
    now[0]+=timedelta(seconds=1);mono[0]+=1
    state=ha_state('home',now[0],source_type='gps')
    p.observe(state,None)
    assert p.snapshot().reporting=='unknown'
    p.observe(state,person)
    assert p.snapshot().reporting=='fresh' and p.snapshot().presence_freshness=='unknown'
    assert TRACKER not in p.snapshot().model_dump_json()
    mono[0]+=21600;now[0]+=timedelta(seconds=21600)
    assert p.snapshot().reporting=='stale'
    now[0]+=timedelta(hours=1)
    assert p.snapshot().reporting=='unknown'  # wall clock jump
    p.reset();assert p.snapshot().reporting=='unknown'
    p.bootstrap(person);p.observe(ha_state('home',now[0],source_type='gps',restored=True),person)
    assert p.snapshot().reporting=='unknown'
    p.observe({},person);assert p.snapshot().reporting=='unknown'
    p.person(ha_state('home',now[0]));assert p.snapshot().reporting=='unknown'


def test_api_diagnostics_scoped_and_no_writes(client):
    response=client.get('/api/v1/routines')
    assert response.status_code==200
    d=response.json()['diagnostics']
    assert d['evaluations']==[] and d['presence']['presence_freshness']=='unknown'
    assert 'latitude' not in json.dumps(d) and 'device_tracker' not in json.dumps(d)

from test_auth import auth_client  # noqa: F401 - shared isolated auth fixture


def test_authenticated_diagnostics_permissions_and_revocation(auth_client):
    from app.auth import SESSION_COOKIE
    client,ha,store,_=auth_client
    assert client.get('/api/v1/routines').status_code==401
    code,_=store.create_pairing_window()
    session=store.pair(code,'read-only fixture','fixture',scopes=('routines:read',))
    client.cookies.set(SESSION_COOKIE,session.token)
    response=client.get('/api/v1/routines');assert response.status_code==200
    assert response.json()['can_write'] is False and response.json()['can_run'] is False
    assert client.get('/api/v1/system/health').status_code==403
    assert client.post('/api/v1/routines/'+RID+'/run',headers={'Origin':'https://roomos.example.com','X-CSRF-Token':session.csrf_token}).status_code==403
    assert not ha.calls
    store.revoke_device(session.context.device_id)
    assert client.get('/api/v1/routines').status_code==401
    code,_=store.create_pairing_window()
    other=store.pair(code,'system only','fixture',scopes=('system:read',))
    client.cookies.set(SESSION_COOKIE,other.token)
    assert client.get('/api/v1/routines').status_code==403


@pytest.mark.asyncio
async def test_disabled_unknown_solar_and_diagnostic_failure_do_not_change_calls(tmp_path,monkeypatch):
    scenes=FakeScenes();routine=managed_evening();routine.enabled=False
    r=engine(tmp_path,[routine],scenes)
    r.bootstrap_environment(ha_state('home',T-timedelta(minutes=1)),solar_state('above_horizon',T-timedelta(minutes=1)))
    await r.handle_environment_event(SOLAR_ENTITY_ID,solar_state('below_horizon',T),now=T)
    assert r.evaluations.records()[0].reason_code=='routine_disabled' and not scenes.calls
    r=engine(tmp_path,[managed_evening()],scenes)
    r.bootstrap_environment(ha_state('not_home',T-timedelta(minutes=1)),None)
    await r.handle_environment_event(PRESENCE_ENTITY_ID,ha_state('home',T),now=T)
    assert r.evaluations.records()[0].reason_code=='solar_unknown' and not scenes.calls
    r=engine(tmp_path,[managed_evening()],scenes)
    r.evaluations=EvaluationStore(str(tmp_path/'events.json'),clock=lambda:T)
    monkeypatch.setattr('app.routine_evaluations.os.replace',lambda *a:(_ for _ in ()).throw(OSError()))
    r.bootstrap_environment(ha_state('not_home',T-timedelta(minutes=1)),solar_state('below_horizon',T-timedelta(minutes=1)))
    await r.handle_environment_event(PRESENCE_ENTITY_ID,ha_state('home',T),now=T)
    assert len(scenes.calls)==1 and r.evaluations.storage_status=='write_failed'


@pytest.mark.parametrize('state', [None, {}, {'attributes':[]}, {'last_updated':'invalid'},
    {'state':'home','last_updated':'2099-01-01T00:00:00Z','attributes':{'source_type':'gps'}}])
def test_missing_malformed_future_evidence_is_unknown(state):
    p=PresenceObserver(clock=lambda:T)
    p.bootstrap(ha_state('home',T,source=TRACKER));p.observe(state,{'state':'home'})
    assert p.snapshot().presence_freshness=='unknown' and p.snapshot().reporting=='unknown'


@pytest.mark.asyncio
async def test_ha_failure_exact_reason_and_link(tmp_path):
    from app.ha_client import HomeAssistantUnavailable
    class Failing(FakeScenes):
        async def activate(self,*args,**kwargs):
            exc=HomeAssistantUnavailable('not exposed');exc.execution_id='ex_'+'a'*32;raise exc
    r=engine(tmp_path,[managed_evening()],Failing())
    r.bootstrap_environment(ha_state('not_home',T-timedelta(minutes=1)),solar_state('below_horizon',T-timedelta(minutes=1)))
    await r.handle_environment_event(PRESENCE_ENTITY_ID,ha_state('home',T),now=T)
    row=r.evaluations.records()[0]
    assert row.reason_code=='ha_unavailable' and row.execution_id=='ex_'+'a'*32
    assert 'not exposed' not in row.model_dump_json()


@pytest.mark.asyncio
async def test_unknown_to_home_and_reconnect_no_fabricated_evaluation(tmp_path):
    scenes=FakeScenes();r=engine(tmp_path,[managed_evening()],scenes)
    r.bootstrap_environment(ha_state('unknown',T-timedelta(minutes=1)),solar_state('below_horizon',T-timedelta(minutes=1)))
    await r.handle_environment_event(PRESENCE_ENTITY_ID,ha_state('home',T),now=T)
    r.presence_observer.reset()
    r.bootstrap_environment(ha_state('home',T),solar_state('below_horizon',T))
    await r.handle_environment_event(PRESENCE_ENTITY_ID,ha_state('home',T+timedelta(seconds=1)),now=T)
    assert not scenes.calls and not r.evaluations.records()


@pytest.mark.asyncio
async def test_diagnostics_broker_scope_and_no_raw_attributes(tmp_path):
    from app.events import EventBroker
    class Socket:
        def __init__(self):self.messages=[]
        async def accept(self):pass
        async def send_json(self,payload):self.messages.append(payload)
    broker=EventBroker();allowed=Socket();denied=Socket()
    await broker.connect(allowed,scopes=frozenset({'routines:read'}))
    await broker.connect(denied,scopes=frozenset({'system:read'}))
    async def publish(event):await broker.broadcast(event,required_scope='routines:read')
    r=engine(tmp_path,[managed_evening()],FakeScenes(),publish=publish)
    await r.publish_diagnostics()
    assert not denied.messages and allowed.messages[0]['type']=='routine.diagnostics'
    assert 'device_tracker' not in json.dumps(allowed.messages)


def test_unreadable_and_symlink_store_preserved(tmp_path,monkeypatch):
    link=tmp_path/'link';target=tmp_path/'original';target.write_text('{private}')
    link.symlink_to(target)
    assert EvaluationStore(str(link)).storage_status=='corrupt'
    assert target.read_text()=='{private}'
    monkeypatch.setattr(Path,'exists',lambda *a:(_ for _ in ()).throw(PermissionError()))
    assert EvaluationStore(str(tmp_path/'blocked')).storage_status=='corrupt'


@pytest.mark.asyncio
async def test_request_errors_are_not_routine_evaluations_and_rejections_keep_origin(tmp_path):
    from fastapi import HTTPException
    scenes = FakeScenes(); r = engine(tmp_path, [managed_evening(), managed_all_off()], scenes)
    with pytest.raises(HTTPException) as unknown:
        await r.run(RID, source='manual')
    assert unknown.value.status_code == 404 and r.evaluations.records() == []
    with pytest.raises(HTTPException) as managed:
        await r.run(managed_evening().id, source='manual')
    row = r.evaluations.records()[0]
    assert managed.value.status_code == 409 and not scenes.calls
    assert (row.trigger_origin, row.outcome, row.reason_code) == ('manual', 'failed', 'execution_rejected')
    r.corrupt = True
    with pytest.raises(HTTPException) as broken:
        await r.run(managed_all_off().id, source='schedule')
    assert broken.value.status_code == 503 and len(r.evaluations.records()) == 1
    disabled = engine(tmp_path / 'off', [managed_evening()], FakeScenes(), enabled=False)
    disabled.bootstrap_environment(ha_state('not_home', T - timedelta(minutes=1)), solar_state('below_horizon', T - timedelta(minutes=1)))
    await disabled.handle_environment_event(PRESENCE_ENTITY_ID, ha_state('home', T), now=T)
    row = disabled.evaluations.records()[0]
    assert (row.trigger_origin, row.reason_code) == ('arrival', 'control_disabled')


@pytest.mark.asyncio
async def test_evaluation_records_reporting_recency_not_gps_age(tmp_path):
    now = [T]; mono = [0.0]
    r = engine(tmp_path, [managed_evening(), managed_all_off()], FakeScenes())
    r.presence_observer = PresenceObserver(clock=lambda: now[0], monotonic=lambda: mono[0])
    person = ha_state('not_home', T - timedelta(minutes=1), source=TRACKER)
    r.bootstrap_environment(person, solar_state('below_horizon', T - timedelta(minutes=1)))
    now[0] += timedelta(seconds=5); mono[0] += 5
    r.presence_observer.observe(ha_state('home', now[0], source_type='gps'), person)
    await r.handle_environment_event(PRESENCE_ENTITY_ID, ha_state('home', now[0], source=TRACKER), now=now[0])
    row = r.evaluations.records()[0]
    assert (row.trigger_origin, row.outcome, row.presence_reporting) == ('arrival', 'executed', 'fresh')
    assert 'gps' not in row.model_dump_json().lower()
    await r.tick(datetime(2026, 10, 6, 23, 0, tzinfo=UTC))
    assert r.evaluations.records()[0].trigger_origin == 'schedule' and r.evaluations.records()[0].presence_reporting is None
    legacy = {'version': 1, 'evaluations': [{k: v for k, v in row.model_dump(mode='json').items() if k != 'presence_reporting'}]}
    assert EvaluationDocument.model_validate(legacy).evaluations[0].presence_reporting is None
