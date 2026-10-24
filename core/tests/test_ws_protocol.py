"""Real TCP/WebSocket acceptance; sessions, scopes and telemetry are synthetic."""
import asyncio
import json
import socket
import sqlite3
from pathlib import Path
import pytest
import uvicorn
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus
from app.auth import AuthRejected, AuthorizationChanged
from app.config import Settings, get_settings
from app.main import app
from app.models import RoomOSEvent
from app.events import EventBroker

@pytest.fixture
async def wire_server(monkeypatch, tmp_path):
    for field in Settings.model_fields.values():
        if isinstance(field.default, str) and field.default.startswith('/var/lib/roomos-auth/'):
            monkeypatch.setenv(field.alias, str(tmp_path / Path(field.default).name))
    for name, value in {'ROOMOS_AUTH_ENABLED':'true','ROOMOS_HA_CONNECT_ENABLED':'false',
                        'ROOMOS_DNS_MONITOR_ENABLED':'false','ROOMOS_CONTROL_ENABLED':'false',
                        'ROOMOS_AUTH_ALLOWED_HOST':'127.0.0.1',
                        'ROOMOS_AUTH_ALLOWED_ORIGIN':'https://127.0.0.1'}.items():
        monkeypatch.setenv(name,value)
    get_settings.cache_clear()
    listener=socket.socket(); listener.bind(('127.0.0.1',0)); listener.listen(8)
    monkeypatch.setenv('ROOMOS_AUTH_ALLOWED_HOST', f'127.0.0.1:{listener.getsockname()[1]}')
    get_settings.cache_clear()
    server=uvicorn.Server(uvicorn.Config(app,log_level='error',ws='websockets-sansio'))
    task=asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started: await asyncio.sleep(.01)
        yield f'ws://127.0.0.1:{listener.getsockname()[1]}',app.state.auth,get_settings()
    finally:
        server.should_exit=True
        await asyncio.wait_for(task,5)
        listener.close(); get_settings.cache_clear()

def session(store,scopes):
    code,_=store.create_pairing_window()
    return store.pair(code,'Synthetic protocol reader','fixture',scopes=scopes)

def change_scopes(settings,issued,scopes):
    with sqlite3.connect(settings.auth_database_path) as db:
        db.execute('UPDATE devices SET scopes=? WHERE id=?',(json.dumps(scopes),issued.context.device_id))

def connection(base,issued,native=False):
    headers={'X-Forwarded-Proto':'https'}
    if issued: headers.update({'Authorization':f'Bearer {issued.token}'} if native else {'Cookie':f'roomos_session={issued.token}'})
    return connect(base+('/api/v1/native/events' if native else '/api/v1/events'),origin='https://127.0.0.1',additional_headers=headers,open_timeout=3,close_timeout=1)

async def closed_code(ws, timeout=2):
    with pytest.raises(ConnectionClosed) as closed:
        await asyncio.wait_for(ws.recv(), timeout)
    return closed.value.rcvd.code if closed.value.rcvd else None

async def emit(event_type='device.updated',scope='devices:read'):
    await app.state.broker.broadcast(RoomOSEvent(type=event_type,data={'fixture':'synthetic-sensitive-telemetry'}),required_scope=scope)

@pytest.mark.asyncio
@pytest.mark.parametrize('native',[False,True])
async def test_real_protocol_downgrade_isolated_clients_and_regrant(wire_server,native):
    base,store,settings=wire_server
    first=session(store,('devices:read','routines:read')); second=session(store,('devices:read',))
    async with connection(base,first,native) as a,connection(base,second,native) as b:
        assert json.loads(await a.recv())['type']=='roomos.connection'
        await b.recv(); await emit()
        assert json.loads(await a.recv())['type']=='device.updated'
        assert json.loads(await b.recv())['type']=='device.updated'
        change_scopes(settings,first,['routines:read']); await emit()
        assert await closed_code(a)==4409  # reconnect with fresh scopes, not re-pair
        assert json.loads(await b.recv())['type']=='device.updated'
        async with connection(base,first,native) as restricted:
            await restricted.recv(); await emit(); await b.recv()
            await emit('routine.diagnostics','routines:read')
            assert json.loads(await restricted.recv())['type']=='routine.diagnostics'
            change_scopes(settings,first,['devices:read','routines:read'])
            await restricted.send('validate')
            assert await closed_code(restricted)==4409
        async with connection(base,first,native) as fresh:
            await fresh.recv(); await emit()
            assert json.loads(await fresh.recv())['type']=='device.updated'
            store.revoke_sessions(first.context.device_id); await emit()
            assert await closed_code(fresh)==4401  # revoked session must re-pair
    with pytest.raises(InvalidStatus):
        async with connection(base,first,native): pytest.fail('Revoked session reconnected')

@pytest.mark.asyncio
async def test_real_protocol_unauthenticated_and_no_scope_rejected(wire_server):
    base,store,_=wire_server
    for issued in (None,session(store,('not-a-read-scope',))):
        with pytest.raises(InvalidStatus):
            async with connection(base,issued): pytest.fail('Unauthorized socket accepted')

@pytest.mark.asyncio
async def test_real_protocol_idle_permissions_expire_without_event(wire_server):
    base,store,settings=wire_server; issued=session(store,('devices:read',))
    async with connection(base,issued) as ws:
        await ws.recv(); change_scopes(settings,issued,['lights:read'])
        assert await closed_code(ws,12)==4409

@pytest.mark.asyncio
async def test_queued_event_revalidates_after_wait_and_does_not_leak():
    started,release=asyncio.Event(),asyncio.Event(); authorized=True; checks=0
    class Socket:
        def __init__(self): self.sent=[]; self.closed=None
        async def accept(self): pass
        async def send_json(self,data):
            started.set(); await release.wait(); self.sent.append(data)
        async def close(self,code): self.closed=code
    def validator():
        nonlocal checks
        checks+=1
        if not authorized: raise AuthorizationChanged()
    broker=EventBroker(); ws=Socket()
    await broker.connect(ws,validator,frozenset({'devices:read'}))
    event=RoomOSEvent(type='device.updated',data={'fixture':'private'})
    first=asyncio.create_task(broker.broadcast(event,required_scope='devices:read'))
    await started.wait()
    queued=asyncio.create_task(broker.broadcast(event,required_scope='devices:read'))
    await asyncio.sleep(0); authorized=False; release.set()
    await asyncio.gather(first,queued)
    assert len(ws.sent)==1 and ws.closed==4409 and checks==2
    assert not broker._clients and not broker._send_locks


@pytest.mark.asyncio
async def test_backpressure_timeout_removes_only_slow_client():
    class Socket:
        def __init__(self, slow=False): self.slow=slow; self.sent=[]; self.closed=None
        async def accept(self): pass
        async def send_json(self, data):
            if self.slow: await asyncio.Event().wait()
            self.sent.append(data)
        async def close(self, code): self.closed=code
    broker=EventBroker(); slow=Socket(True); good=Socket()
    await broker.connect(slow,scopes=frozenset({'devices:read'}))
    await broker.connect(good,scopes=frozenset({'devices:read'}))
    async with asyncio.timeout(7):
        await broker.broadcast(RoomOSEvent(type='device.updated',data={}),required_scope='devices:read')
    # Backpressure is a transport problem: reconnect later, never force re-pairing.
    assert slow.closed==1013 and good.closed is None and len(good.sent)==1 and good in broker._clients


@pytest.mark.asyncio
async def test_unknown_physical_identity_cannot_create_or_publish_sensitive_record():
    from app.devices import DeviceRegistry, Reading
    sent=[]
    async def publish(event, scope): sent.append(event)
    registry=DeviceRegistry(Settings().device_stale_thresholds(),publish)
    await registry.publish_changes(); sent.clear()
    assert registry.ingest('mqtt','deleted-or-unknown',{'battery_percent':Reading(99)}) is None
    assert await registry.publish_changes()==0 and not sent


@pytest.mark.asyncio
async def test_revoked_session_on_queued_delivery_still_requires_repair():
    class Socket:
        def __init__(self): self.sent=[]; self.closed=None
        async def accept(self): pass
        async def send_json(self, data): self.sent.append(data)
        async def close(self, code): self.closed=code
    def revoked(): raise AuthRejected()
    broker=EventBroker(); ws=Socket()
    await broker.connect(ws,revoked,frozenset({'devices:read'}))
    await broker.broadcast(RoomOSEvent(type='device.updated',data={'fixture':'private'}),required_scope='devices:read')
    assert ws.sent==[] and ws.closed==4401 and not broker._clients


class _BroadcastSocket:
    def __init__(self, *, slow=False, error=None):
        self.slow=slow; self.error=error; self.sent=[]; self.closed=None; self.received_at=[]
    async def accept(self): pass
    async def send_json(self, data):
        if self.error is not None: raise self.error
        if self.slow: await asyncio.Event().wait()
        self.sent.append(data); self.received_at.append(asyncio.get_running_loop().time())
    async def close(self, code): self.closed=code


@pytest.mark.asyncio
async def test_slow_client_does_not_delay_fast_client_delivery():
    broker=EventBroker(); slow=_BroadcastSocket(slow=True); fast=_BroadcastSocket()
    # The slow socket connects first: sequential delivery would make the fast one wait 5 s.
    await broker.connect(slow,scopes=frozenset({'lights:read'}))
    await broker.connect(fast,scopes=frozenset({'lights:read'}))
    loop=asyncio.get_running_loop(); started=loop.time()
    task=asyncio.create_task(broker.broadcast(RoomOSEvent(type='light.state_changed',data={})))
    async with asyncio.timeout(1):
        while not fast.sent: await asyncio.sleep(0.005)
    assert fast.received_at[0]-started < 0.1
    assert not task.done()  # the slow socket is still inside its bounded send window
    async with asyncio.timeout(7):
        await task
    # Existing backpressure contract is unchanged for the slow socket only.
    assert slow.closed==1013 and slow not in broker._clients
    assert fast.closed is None and fast in broker._clients


@pytest.mark.asyncio
async def test_failing_client_does_not_abort_broadcast_to_others():
    broker=EventBroker(); broken=_BroadcastSocket(error=RuntimeError('transport detail')); good=_BroadcastSocket()
    await broker.connect(broken,scopes=frozenset({'lights:read'}))
    await broker.connect(good,scopes=frozenset({'lights:read'}))
    await broker.broadcast(RoomOSEvent(type='light.state_changed',data={'n':1}))
    assert broken.closed==1013 and broken not in broker._clients and broker._send_locks.get(broken) is None
    assert [m['data'] for m in good.sent]==[{'n':1}] and good.closed is None


@pytest.mark.asyncio
async def test_concurrent_broadcasts_keep_per_client_order():
    broker=EventBroker(); a=_BroadcastSocket(); b=_BroadcastSocket()
    await broker.connect(a,scopes=frozenset({'lights:read'}))
    await broker.connect(b,scopes=frozenset({'lights:read'}))
    await asyncio.gather(*(broker.broadcast(RoomOSEvent(type='light.state_changed',data={'n':n})) for n in range(20)))
    for socket in (a,b):
        assert [m['data']['n'] for m in socket.sent]==list(range(20))
