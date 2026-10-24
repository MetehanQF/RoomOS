import json
import sqlite3
from pathlib import Path
import pytest
from conftest import fake_ha, isolated_execution_store
from test_auth import auth_client
from app.auth import SESSION_COOKIE
from app.main import app
from app.models import RoomOSEvent
from app.config import Settings

@pytest.fixture(autouse=True)
def all_state_isolated(monkeypatch, tmp_path):
    for name, field in Settings.model_fields.items():
        if isinstance(field.default, str) and field.default.startswith('/var/lib/roomos-auth/'):
            monkeypatch.setenv(field.alias, str(tmp_path / Path(field.default).name))


@pytest.mark.parametrize('change', ['revoke', 'remove_scope', 'delete_device', 'expire'])
def test_open_websocket_stops_receiving_device_data_after_access_loss(auth_client, change):
    client, ha, store, settings = auth_client
    code, _ = store.create_pairing_window()
    issued = store.pair(code, 'Synthetic release reader', 'fixture', scopes=('devices:read',))
    token = issued.token
    headers = {'Host':'roomos.example.com','Origin':settings.auth_allowed_origin,
               'X-Forwarded-Proto':'https','Cookie':f'{SESSION_COOKIE}={token}'}
    with client.websocket_connect('/api/v1/events', headers=headers) as socket:
        assert socket.receive_json()['type'] == 'roomos.connection'
        if change == 'revoke':
            store.revoke_sessions(issued.context.device_id)
        elif change == 'delete_device':
            with sqlite3.connect(settings.auth_database_path) as db:
                db.execute('DELETE FROM devices WHERE id=?', (issued.context.device_id,))
        elif change == 'expire':
            with sqlite3.connect(settings.auth_database_path) as db:
                db.execute('UPDATE sessions SET absolute_expires_at=0 WHERE id=?', (issued.context.session_id,))
        else:
            with sqlite3.connect(settings.auth_database_path) as db:
                db.execute('UPDATE devices SET scopes=? WHERE id=?', (json.dumps(['lights:read']), issued.context.device_id))
        client.cookies.set(SESSION_COOKIE, token)
        assert client.get('/api/v1/devices').status_code == (403 if change == 'remove_scope' else 401)
        async def send():
            await app.state.broker.broadcast(RoomOSEvent(type='device.updated', data={'device':{'id':'synthetic_phone'}}), required_scope='devices:read')
        client.portal.call(send)
        message = socket.receive()
        assert message['type'] == 'websocket.close', 'Previously authorized socket received a devices:read event after permission loss'
    assert not ha.calls
