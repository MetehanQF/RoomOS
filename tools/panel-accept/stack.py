"""Isolated RoomOS acceptance stack: synthetic HA + candidate Core/Web on an --internal network.

usage: stack.py up <image-tag> <run-dir> [--harness]
       stack.py down <run-dir>
       stack.py leftovers

No production mounts, credentials, state or published ports. The Web container is reached
through a loopback-only forwarder on 127.0.0.1:$ROOMOS_ACCEPT_PORT (default 5551). Session
tokens, the disposable TLS key and the forwarder PID live in <run-dir> and are deleted by
`down`, together with every container, the network and the auth volume.
"""
import json
import os
import pathlib
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
PREFIX = 'roomos-accept'
NET, HA, CORE, WEB, VOL = f'{PREFIX}-net', f'{PREFIX}-fake-ha', f'{PREFIX}-core', f'{PREFIX}-web', f'{PREFIX}-auth'
PORT = int(os.environ.get('ROOMOS_ACCEPT_PORT', '5551'))
SUBNET = os.environ.get('ROOMOS_ACCEPT_SUBNET', '172.30.252.0/24')  # privacy-scan:allow isolated docker test network default
# Test helpers (core/tests) import pytest; the Core image does not ship it. Installed once
# into a host cache with the same pins as the development environment.
TEST_DEPS = ('pytest==8.4.2', 'pytest-asyncio==0.26.0')
CACHE = pathlib.Path(os.environ.get('XDG_CACHE_HOME', pathlib.Path.home() / '.cache')) / 'roomos-panel-accept' / 'testdeps-pytest-8.4.2'
COMMON = ['--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true', '--pids-limit', '128', '--network', NET]
RUN_FILES = ('fixture-key.pem', 'fixture-cert.pem', 'sessions.json', 'forward.pid')


def sh(*args, check=True, **kwargs):
    return subprocess.run(args, check=check, capture_output=True, text=True, **kwargs).stdout.strip()


def ip(name):
    return sh('docker', 'inspect', '-f', '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}', name)


def leftovers():
    containers = sh('docker', 'ps', '-a', '--filter', f'name=^{PREFIX}-', '--format', '{{.Names}}', check=False).split()
    networks = sh('docker', 'network', 'ls', '--filter', f'name=^{NET}$', '--format', '{{.Name}}', check=False).split()
    volumes = sh('docker', 'volume', 'ls', '--filter', f'name=^{VOL}$', '--format', '{{.Name}}', check=False).split()
    listening = subprocess.run(['ss', '-ltnH', f'sport = :{PORT}'], capture_output=True, text=True).stdout.strip()
    return {'containers': containers, 'networks': networks, 'volumes': volumes, 'forwarder_listening': bool(listening)}


def down(run_dir: pathlib.Path):
    pid = run_dir / 'forward.pid'
    if pid.exists():
        subprocess.run(['kill', pid.read_text().strip()], capture_output=True)
    subprocess.run(['docker', 'rm', '-f', HA, CORE, WEB], capture_output=True)
    subprocess.run(['docker', 'network', 'rm', NET], capture_output=True)
    subprocess.run(['docker', 'volume', 'rm', '-f', VOL], capture_output=True)
    for name in RUN_FILES:
        (run_dir / name).unlink(missing_ok=True)
    for _ in range(20):
        state = leftovers()
        if not any(state.values()):
            break
        time.sleep(0.25)
    return state


def ensure_test_deps(tag):
    if (CACHE / 'pytest').is_dir():
        return
    CACHE.mkdir(parents=True, exist_ok=True)
    sh('docker', 'run', '--rm', '--user', f'{os.getuid()}:{os.getgid()}', '-e', 'HOME=/tmp', '-v', f'{CACHE}:/out',
       '--entrypoint', 'python', f'roomos-core:{tag}', '-m', 'pip', 'install', '--quiet', '--no-cache-dir', '--target', '/out', *TEST_DEPS)


def up(tag, run_dir: pathlib.Path, harness):
    run_dir.mkdir(parents=True, exist_ok=True)
    run_dir.chmod(0o700)
    down(run_dir)
    for image in (f'roomos-core:{tag}', f'roomos-web:{tag}'):
        sh('docker', 'image', 'inspect', image)
    ensure_test_deps(tag)
    tests = REPO / 'core' / 'tests'
    fixtures = HERE / 'fixtures'
    key, cert = run_dir / 'fixture-key.pem', run_dir / 'fixture-cert.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key), '-out', str(cert),
                    '-days', '1', '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1'], check=True, capture_output=True)
    key.chmod(0o644)  # disposable loopback TLS fixture; nginx (uid 101) reads the bind-mounted file
    sh('docker', 'network', 'create', '--internal', '--subnet', SUBNET, NET)
    sh('docker', 'volume', 'create', VOL)
    sh('docker', 'run', '--rm', '--user', '0', '--network', 'none', '-v', f'{VOL}:/var/lib/roomos-auth', '--entrypoint', 'sh', f'roomos-core:{tag}',
       '-c', 'chown 10001:10001 /var/lib/roomos-auth && chmod 700 /var/lib/roomos-auth')
    sessions = sh('docker', 'run', '--rm', '-i', '--network', 'none', '-v', f'{VOL}:/var/lib/roomos-auth',
                  '-e', 'ROOMOS_AUTH_ENABLED=true', '--entrypoint', 'python', f'roomos-core:{tag}', '-',
                  input=(fixtures / 'create_sessions.py').read_text())
    (run_dir / 'sessions.json').write_text(sessions)
    (run_dir / 'sessions.json').chmod(0o600)
    ids = {k: v['device_id'] for k, v in json.loads(sessions).items()}
    test_mounts = ['-v', f'{tests}:/src/tests:ro', '-v', f'{CACHE}:/testdeps:ro']
    # Production-shaped scenes so scene controls are exercised.
    sh('docker', 'run', '--rm', '--network', 'none', '-v', f'{VOL}:/var/lib/roomos-auth', *test_mounts,
       '-e', 'PYTHONPATH=/testdeps:/src/tests', '--entrypoint', 'python', f'roomos-core:{tag}', '-c',
       "from test_scenes import production_scene_set; from app.scenes import SceneConfigFile; import os; "
       "p='/var/lib/roomos-auth/scenes.json'; open(p,'w').write(SceneConfigFile(scenes=list(production_scene_set().values())).model_dump_json(exclude_none=True)); os.chmod(p,0o600)")
    phones = {ids['handsetnative']: {'device': 'phone', 'app': 'android'}, ids['tabpwa']: {'device': 'tablet', 'app': 'web'},
              ids['phone2panel']: {'device': 'phone_2', 'app': 'web'}}
    sh('docker', 'run', '-d', '--name', HA, *COMMON, '--network-alias', 'homeassistant', '--memory', '128m', '--tmpfs', '/tmp:size=8m',
       '-v', f'{HERE / "fakeha"}:/accept:ro', '-e', 'PYTHONPATH=/accept', '--entrypoint', 'uvicorn', f'roomos-core:{tag}',
       'fake_ha:app', '--host', '0.0.0.0', '--port', '8123', '--log-level', 'warning')
    env = {'HA_URL': 'http://homeassistant:8123', 'HA_TOKEN': 'synthetic-acceptance-token', 'ROOMOS_HA_CONNECT_ENABLED': 'true',
           'ROOMOS_CONTROL_ENABLED': 'true', 'ROOMOS_CONTROL_POLICY': 'production', 'ROOMOS_FCM_ENABLED': 'false', 'ROOMOS_DNS_MONITOR_ENABLED': 'false',
           'ROOMOS_SYSTEM_MONITOR_INTERVAL_SECONDS': '30', 'ROOMOS_AUTH_ENABLED': 'true',
           'ROOMOS_AUTH_ALLOWED_HOST': '127.0.0.1', 'ROOMOS_AUTH_ALLOWED_ORIGIN': f'https://127.0.0.1:{PORT}',
           'ROOMOS_DEVICES_CLIENT_TELEMETRY': json.dumps(phones),
           'ROOMOS_DEVICES_WORKSTATION_AGENT_ID': 'workstation', 'ROOMOS_DEVICES_WORKSTATION_ENTITIES': (HERE / 'fakeha' / 'mapping.json').read_text(),
           'ROOMOS_DEVICES_WINDOWS_HEARTBEAT_SECONDS': '120'}
    target = 'harness:app' if harness else 'app.main:app'
    core = ['docker', 'run', '-d', '--name', CORE, *COMMON, '--network-alias', 'core', '--memory', '384m', '--cpus', '1',
            '--tmpfs', '/tmp:rw,nosuid,size=16m', '-v', f'{VOL}:/var/lib/roomos-auth', '-v', f'{fixtures}:/accept:ro', *test_mounts,
            '-e', 'PYTHONPATH=/accept:/testdeps:/src/tests']
    for k, v in env.items():
        core += ['-e', f'{k}={v}']
    core += [f'roomos-core:{tag}', 'sh', '-c', f'python /accept/seed_fixture.py && exec uvicorn {target} --host 0.0.0.0 --port 5501 --proxy-headers --forwarded-allow-ips=*']
    sh(*core)
    sh('docker', 'run', '-d', '--name', WEB, *COMMON, '--memory', '128m', '--cpus', '0.5', '--tmpfs', '/tmp:rw,nosuid,size=16m',
       '--tmpfs', '/var/cache/nginx:rw,nosuid,size=16m,uid=101,gid=101', '--tmpfs', '/var/run:rw,nosuid,size=1m,uid=101,gid=101',
       '--mount', f'type=bind,src={cert},dst=/run/roomos-tls/fullchain.pem,readonly',
       '--mount', f'type=bind,src={key},dst=/run/roomos-tls/privkey.pem,readonly', f'roomos-web:{tag}')
    for _ in range(60):
        logs = subprocess.run(['docker', 'logs', CORE], capture_output=True, text=True)
        if 'Application startup complete' in logs.stdout + logs.stderr:
            break
        time.sleep(1)
    else:
        raise SystemExit('acceptance Core did not start')
    proc = subprocess.Popen([sys.executable, str(HERE / 'forward.py'), ip(WEB), str(PORT)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    (run_dir / 'forward.pid').write_text(str(proc.pid))
    egress = subprocess.run(['docker', 'exec', CORE, 'python', '-c', "import urllib.request;urllib.request.urlopen('https://1.1.1.1',timeout=3)"],
                            capture_output=True, text=True)
    print(json.dumps({'tag': tag, 'harness': harness, 'fake_ha': ip(HA), 'core_container': CORE, 'web': f'https://127.0.0.1:{PORT}',
                      'egress_blocked': 'unreachable' in egress.stderr.lower(), 'production_mounts': 0}))


if __name__ == '__main__':
    command = sys.argv[1] if len(sys.argv) > 1 else ''
    if command == 'up':
        up(sys.argv[2], pathlib.Path(sys.argv[3]), '--harness' in sys.argv)
    elif command == 'down':
        print(json.dumps(down(pathlib.Path(sys.argv[2]))))
    elif command == 'leftovers':
        print(json.dumps(leftovers()))
    else:
        raise SystemExit(__doc__)
