import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.executions import Execution, ExecutionDocument, ExecutionStore, TargetExecution
from app.scenes import SceneEngine, SceneConfigEntry
from app.config import Settings
from app.catalog import LIGHT_BY_ID
from app.ha_client import HomeAssistantUnavailable
from app.alerts import AlertEngine
from app.routines import RoutineEngine, RoutineDocument, Routine, ActivateSceneAction, ManualTrigger

NOW = datetime(2026, 10, 7, tzinfo=UTC)
CTX = dict(scene_id='normal', scene_name='Normal', source='manual', trigger='manual')

@pytest.mark.asyncio
async def test_missing_atomic_mode_valid_load(tmp_path):
    path = tmp_path / 'executions.json'
    store = ExecutionStore(str(path))
    assert not store.document.executions and store.storage_status == 'ok'
    await store.initialize()
    original_replace = os.replace
    observed = []
    def replace(src, dst):
        observed.append(json.loads(Path(src).read_text()))
        assert Path(src).stat().st_mode & 0o777 == 0o600
        original_replace(src, dst)
    with patch('app.executions.os.replace', side_effect=replace):
        record = await store.begin(**CTX)
        assert json.loads(path.read_text())['executions'][0]['result'] == 'running'
        await store.finish(record, result='success')
    assert len(observed) == 2 and not list(tmp_path.glob('.*.tmp'))
    assert path.stat().st_mode & 0o777 == 0o600
    assert ExecutionStore(str(path)).get(record.id).result == 'success'

@pytest.mark.parametrize('payload', ['bad', '{"version":2,"executions":[]}', '{"version":1,"executions":[{}]}', '{"version":1,"executions":[],"secret":"x"}'])
@pytest.mark.asyncio
async def test_corruption_preserved(tmp_path, payload):
    path = tmp_path / 'executions.json'; path.write_text(payload)
    store = ExecutionStore(str(path)); await store.initialize()
    record = await store.begin(**CTX); await store.finish(record, result='failed', reason='internal_error')
    assert store.storage_status == 'corrupt' and path.read_text() == payload
    assert store.get(record.id).result == 'failed'

@pytest.mark.asyncio
async def test_retention_and_serialization(tmp_path):
    store = ExecutionStore(str(tmp_path / 'executions.json'))
    async def run():
        record = await store.begin(**CTX)
        await asyncio.sleep(0)
        await store.finish(record, result='success')
    await asyncio.gather(*(run() for _ in range(260)))
    loaded = ExecutionStore(str(store.path))
    assert len(loaded.document.executions) == 250
    assert all(r.result == 'success' for r in loaded.document.executions)
    assert not store._started

@pytest.mark.asyncio
async def test_interrupted_deterministic_and_active_not_recovered(tmp_path):
    path = tmp_path / 'executions.json'
    store = ExecutionStore(str(path), clock=lambda: NOW, monotonic=lambda: 10)
    old = await store.begin(**CTX)
    restarted = ExecutionStore(str(path), clock=lambda: NOW+timedelta(seconds=2))
    await restarted.initialize()
    interrupted = restarted.get(old.id)
    assert (interrupted.result, interrupted.reason, interrupted.duration_ms) == ('interrupted', 'interrupted', 2000)
    active = await restarted.begin(**CTX)
    assert restarted.get(active.id).result == 'running'
    assert len(restarted.recovered) == 1

@pytest.mark.asyncio
async def test_clock_rollback_saturates(tmp_path):
    path = tmp_path/'executions.json'
    store = ExecutionStore(str(path), clock=lambda: NOW)
    record = await store.begin(**CTX)
    loaded = ExecutionStore(str(path), clock=lambda: NOW-timedelta(seconds=30))
    assert loaded.get(record.id).duration_ms == 0
    assert loaded.get(record.id).finished_at == NOW

@pytest.mark.asyncio
async def test_write_failure_nonblocking_and_sanitized(tmp_path, caplog):
    store = ExecutionStore(str(tmp_path/'executions.json'))
    with patch('app.executions.os.replace', side_effect=OSError('SECRET http://192.0.2.1 light.private')):
        record = await store.begin(**CTX)
        await store.finish(record, result='success')
    assert store.storage_status == 'write_failed' and store.get(record.id).result == 'success'
    assert 'SECRET' not in caplog.text and '192.168' not in caplog.text
    assert not list(tmp_path.glob('.*.tmp'))

class HA:
    def __init__(self, outcomes=None, *, off=False, wake_confirm=True):
        self.outcomes = outcomes or {}; self.calls=[]; self.reads=[]; self.off=off; self.wake_confirm=wake_confirm
    async def get_state(self, entity):
        self.reads.append(entity)
        device = next(d.id for d in LIGHT_BY_ID.values() if d.entity_id==entity)
        outcome = self.outcomes.get(device)
        if outcome == 'read_error': raise HomeAssistantUnavailable('SECRET http://192.0.2.1')
        return {'state': 'unavailable' if outcome=='unavailable' else 'off' if self.off else 'on', 'attributes': {'supported_color_modes': ['onoff'] if outcome=='capability' else ['color_temp','hs'], 'effect_list': [] if outcome=='capability' else ['Off'], 'min_color_temp_kelvin':2500, 'max_color_temp_kelvin':6500}}
    async def call_service(self, domain, service, payload):
        self.calls.append((domain,service,payload))
        device = next(d.id for d in LIGHT_BY_ID.values() if d.entity_id==payload['entity_id'])
        if self.outcomes.get(device) == 'service_error': raise HomeAssistantUnavailable('SECRET light.raw')
        if self.off and self.wake_confirm: self.off=False


def engine(tmp_path, targets=None):
    scene=SceneConfigEntry(id='normal', targets=targets or [dict(device_id='room_main',power=True),dict(device_id='desk_ambient',power=False)])
    settings=Settings(ROOMOS_CONTROL_ENABLED=True, ROOMOS_CONTROL_POLICY='production', ROOMOS_SCENE_DUPLICATE_WINDOW_SECONDS=0.5)
    store=ExecutionStore(str(tmp_path/'executions.json'))
    scenes=SceneEngine(settings,{'normal':scene},executions=store)
    alerts=AlertEngine(str(tmp_path/'alerts.json')); scenes.set_activity_sink(alerts.record_scene_activation)
    return scenes,store,alerts

@pytest.mark.parametrize('outcomes,status,reasons,calls',[
    ({},'success',[],2),
    ({'room_main':'unavailable'},'partial',['unavailable'],1),
    ({'room_main':'unavailable','desk_ambient':'unavailable'},'partial',['unavailable','unavailable'],0),
    ({'room_main':'service_error','desk_ambient':'service_error'},'failed',['ha_error','ha_error'],2),
    ({'room_main':'read_error'},'partial',['ha_unavailable'],1),
])
@pytest.mark.asyncio
async def test_outcomes_and_activity(tmp_path,outcomes,status,reasons,calls):
    scenes,store,alerts=engine(tmp_path); ha=HA(outcomes)
    result=await scenes.activate('normal',ha)
    record=store.get(result.execution_id)
    assert result.status==record.result==status
    assert [t.reason for t in record.targets if t.reason]==reasons
    assert len(ha.calls)==calls
    assert alerts.list_activity(limit=10,offset=0,category='scenes').activity[0].execution_id==record.id
    payload=store.path.read_text()
    for secret in ['SECRET','192.168','http://','light.']: assert secret not in payload
    assert len(ha.reads)==(3 if 'read_error' in outcomes.values() else 7)  # Existing reconciliation stops on a failed read.

@pytest.mark.asyncio
async def test_capability_and_wake_stage(tmp_path,monkeypatch):
    scenes,store,_=engine(tmp_path,[dict(device_id='room_main',power=True,brightness=191,kelvin=4000)])
    ha=HA(off=True,wake_confirm=False)
    async def no_sleep(_): pass
    monkeypatch.setattr('app.scenes.asyncio.sleep',no_sleep)
    result=await scenes.activate('normal',ha)
    target=store.get(result.execution_id).targets[0]
    assert target.stage=='wake' and target.reason=='power_on_not_confirmed' and len(ha.calls)==1
    scenes._last_started['normal']=0
    scenes.configured['normal']=SceneConfigEntry(id='normal',targets=[dict(device_id='room_main',effect='Off')])
    result=await scenes.activate('normal',HA({'room_main':'capability'}))
    target=store.get(result.execution_id).targets[0]
    assert target.reason=='capability_rejected' and target.stage=='read'

@pytest.mark.asyncio
async def test_wake_success_apply_failure(tmp_path,monkeypatch):
    scenes,store,_=engine(tmp_path,[dict(device_id='room_main',power=True,brightness=191)])
    ha=HA(off=True)
    original=ha.call_service
    async def call(*args):
        if len(ha.calls): ha.outcomes['room_main']='service_error'
        await original(*args)
    ha.call_service=call
    result=await scenes.activate('normal',ha)
    assert len(ha.calls)==2
    target=store.get(result.execution_id).targets[0]
    assert (target.result,target.stage,target.reason)==('failed','apply','ha_error')

@pytest.mark.asyncio
async def test_storage_failure_does_not_block_engine(tmp_path):
    scenes,store,_=engine(tmp_path); ha=HA()
    with patch('app.executions.os.replace',side_effect=OSError('disk full')):
        result=await scenes.activate('normal',ha)
    assert result.status=='success' and len(ha.calls)==2 and store.storage_status=='write_failed'

@pytest.mark.asyncio
async def test_manual_duplicate_not_persisted_routine_duplicate_is(tmp_path):
    scenes,store,_=engine(tmp_path);ha=HA();scenes.settings.scene_duplicate_window_seconds=10
    await scenes.activate('normal',ha)
    with pytest.raises(HTTPException) as error: await scenes.activate('normal',ha)
    assert error.value.status_code==429 and len(store.document.executions)==1
    with pytest.raises(HTTPException):
        await scenes.activate('normal',ha,source='routine',routine_id='rt_'+'1'*32,routine_name='Evening',trigger='ha_state')
    assert len(store.document.executions)==2
    assert store.document.executions[-1].reason=='duplicate' and len(ha.calls)==2

@pytest.mark.asyncio
async def test_routine_correlation_and_rejection(tmp_path):
    scenes,store,_=engine(tmp_path);ha=HA()
    settings=scenes.settings.model_copy(update={'routines_path':str(tmp_path/'routines.json')})
    routine=Routine(id='rt_'+'1'*32,name='Evening',enabled=True,trigger=ManualTrigger(),action=ActivateSceneAction(scene_id='normal'),created_at=NOW,updated_at=NOW)
    routines=RoutineEngine(settings,scenes,ha)
    routines.document=RoutineDocument(routines=[routine]);routines._locks[routine.id]=asyncio.Lock()
    result=await routines.run(routine.id,source='ha_state')
    record=store.get(result.scene.execution_id)
    assert record.source=='routine' and record.trigger=='ha_state' and record.routine_name=='Evening'
    assert json.loads(Path(settings.routines_path).read_text())['routines'][0]['last_result']['execution_id']==record.id
    settings.control_enabled=False
    with pytest.raises(HTTPException): await routines.run(routine.id,source='schedule')
    assert store.document.executions[-1].reason=='control_disabled'
    assert routines.document.routines[0].last_result.execution_id==store.document.executions[-1].id

@pytest.mark.parametrize('field,value',[('device_id','light.secret'),('actions',['http://private']),('reason','raw exception'),('stage','secret')])
def test_target_schema_leak_rejection(field,value):
    data=dict(device_id='room_main',actions=['power'],result='failed',reason='ha_error',stage='apply',duration_ms=1)
    data[field]=value
    with pytest.raises(ValidationError): TargetExecution(**data)

@pytest.mark.asyncio
async def test_activity_join_and_legacy(client,tmp_path):
    from app.main import app
    store=app.state.executions
    record=await store.begin(**CTX);await store.finish(record,result='success')
    await app.state.alerts.record_scene_activation(scene_id='normal',scene_name='Normal',status='success',occurred_at=NOW,source='manual',execution_id=record.id)
    await app.state.alerts.record_scene_activation(scene_id='normal',scene_name='Normal',status='success',occurred_at=NOW,source='manual')
    response=client.get('/api/v1/activity').json()
    items=response['activity']
    assert any(i['execution'] and i['execution']['id']==record.id for i in items)
    assert any(i['execution_id'] is None and i['execution'] is None for i in items)
    assert 'execution' not in json.loads(Path(app.state.alerts.path).read_text())['activity'][0] if hasattr(app.state.alerts,'path') else True

@pytest.mark.asyncio
async def test_cancelled_execution_left_running_for_restart(tmp_path):
    scenes,store,_=engine(tmp_path); entered=asyncio.Event(); ha=HA()
    async def blocked(*args):
        entered.set()
        await asyncio.Event().wait()
    ha.call_service=blocked
    task=asyncio.create_task(scenes.activate('normal',ha))
    await entered.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert store.document.executions[0].result=='running'
    assert ExecutionStore(str(store.path)).document.executions[0].result=='interrupted'
    assert not ha.calls

@pytest.mark.asyncio
async def test_interrupted_activity_recovery_idempotent(tmp_path):
    scenes,store,alerts=engine(tmp_path);record=await store.begin(**CTX)
    restarted=ExecutionStore(str(store.path))
    for _ in range(2): await scenes.publish_execution(restarted.get(record.id))
    activity=alerts.list_activity(limit=10,offset=0,category='scenes')
    assert activity.total==1 and activity.activity[0].type=='scene_failed'

@pytest.mark.parametrize('case,expected,http', [('policy','policy_rejected',403),('unconfigured','not_configured',409),('kelvin','invalid_action',422)])
@pytest.mark.asyncio
async def test_preflight_rejections_preserve_http(tmp_path,case,expected,http):
    scenes,store,_=engine(tmp_path);ha=HA()
    if case=='policy': scenes.settings.control_policy='read_only'
    if case=='unconfigured': scenes.configured={}
    if case=='kelvin': scenes.configured['normal']=SceneConfigEntry(id='normal',targets=[dict(device_id='room_main',kelvin=9999)])
    with pytest.raises(HTTPException) as error: await scenes.activate('normal',ha)
    assert error.value.status_code==http
    record=store.document.executions[0]
    assert (record.result,record.reason,record.targets)==('rejected',expected,[])
    assert not ha.reads and not ha.calls

@pytest.mark.asyncio
async def test_unexpected_error_sanitized(tmp_path):
    scenes,store,_=engine(tmp_path);ha=HA()
    async def fail(*args): raise RuntimeError('http://192.0.2.1 SECRET light.entity')
    ha.get_state=fail
    with pytest.raises(RuntimeError): await scenes.activate('normal',ha)
    record=store.document.executions[0]
    assert record.result=='failed' and record.reason=='internal_error'
    assert 'SECRET' not in store.path.read_text()

@pytest.mark.asyncio
async def test_concurrent_scenes_keep_target_concurrency_bound(tmp_path):
    scenes,store,_=engine(tmp_path)
    scenes.configured['night']=SceneConfigEntry(id='night',targets=[dict(device_id='room_2',power=False)])
    ha=HA();active=0;maximum=0;original=ha.call_service
    async def call(*args):
        nonlocal active,maximum
        active+=1;maximum=max(maximum,active)
        await asyncio.sleep(0);await original(*args);active-=1
    ha.call_service=call
    results=await asyncio.gather(scenes.activate('normal',ha),scenes.activate('night',ha))
    assert len({r.execution_id for r in results})==2
    assert len(ha.calls)==3 and maximum<=scenes.settings.scene_max_concurrency*2
    assert len(store.document.executions)==2


def test_oversize_valid_records_rejected():
    records=[Execution(id=f'ex_{i:032x}',started_at=NOW,**CTX) for i in range(251)]
    with pytest.raises(ValidationError): ExecutionDocument(version=1,executions=records)
