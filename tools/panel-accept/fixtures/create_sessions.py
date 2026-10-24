# Synthetic acceptance sessions (isolated stack only), mirroring production scopes.
# Tokens live only in the disposable auth volume and the run directory; both are removed by `down`.
import json
from app.auth import AuthStore
from app.config import Settings
store = AuthStore(Settings()); store.initialize()
plan = {
    'phone2panel':  ('web', ('lights:read', 'lights:write', 'system:read', 'routines:read', 'activity:read', 'devices:read', 'telemetry:write:self')),
    'tabpwa':    ('web', ('lights:read', 'lights:write', 'system:read', 'routines:read', 'routines:write', 'camera:read', 'activity:read', 'devices:read', 'telemetry:write:self')),
    'handsetpwa':    ('web', ('lights:read', 'lights:write', 'system:read', 'routines:read', 'activity:read', 'devices:read')),
    'readonly':  ('web', ('lights:read', 'system:read')),
    'phone2view':   ('web', ('lights:read', 'lights:write', 'system:read', 'routines:read', 'activity:read', 'devices:read')),  # extra browser contexts: no telemetry
    'handsetnative': ('android', ('lights:read', 'system:read', 'devices:read', 'telemetry:write:self')),
}
out = {}
for name, (kind, scopes) in plan.items():
    profile = 'android-readonly' if kind == 'android' else 'web'
    code, _ = store.create_pairing_window(60, profile=profile)
    s = store.pair(code, 'Synthetic acceptance ' + name, 'fixture-' + name, profile=profile, scopes=scopes, session_kind=kind)
    out[name] = {'token': s.token, 'csrf': s.csrf_token, 'device_id': s.context.device_id}
print(json.dumps(out))
