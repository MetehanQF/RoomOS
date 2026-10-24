from __future__ import annotations

import importlib.util
import io
import json
import os
import sqlite3
import tarfile
from argparse import Namespace
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).parents[1] / "roomos_state_backup.py"
SPEC = importlib.util.spec_from_file_location("roomos_state_backup", MODULE_PATH)
backup = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(backup)


def make_state(root: Path) -> None:
    root.mkdir(mode=0o700)
    database = root / "roomos-auth.db"
    with sqlite3.connect(database) as db:
        db.executescript(
            """
            CREATE TABLE devices(id TEXT PRIMARY KEY, enabled INTEGER NOT NULL, revoked_at INTEGER);
            CREATE TABLE sessions(id TEXT PRIMARY KEY, revoked_at INTEGER, sliding_expires_at INTEGER NOT NULL, absolute_expires_at INTEGER NOT NULL);
            CREATE TABLE pairing_windows(id TEXT PRIMARY KEY, used_at INTEGER, expires_at INTEGER NOT NULL);
            CREATE TABLE panel_preferences(device_id TEXT PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE, version INTEGER NOT NULL, layout TEXT NOT NULL, updated_at INTEGER NOT NULL);
            INSERT INTO devices VALUES('device',1,NULL);
            INSERT INTO sessions VALUES('session',NULL,4102444800,4102444800);
            INSERT INTO panel_preferences VALUES('device',1,'{"version":1,"cards":["main_lights","ambient","scenes","routines","system"],"hidden":[],"density":"compact","scene_order":["normal","work_1","work_2","gaming_1","gaming_2","movie","night","all_off"],"header":{"clock":true,"date":true,"connection":true,"health":true}}',1792173600);
            """
        )
    documents = {
        "scenes.json": {"scenes": []},
        "scene-state.json": {"last_applied": None},
        "alerts.json": {"version": 1, "alerts": {}, "pending": {}, "activity": []},
        "notifications.json": {"version": 1, "notifications": [], "external_attempts": 0, "external_deliveries": 0, "last_delivery": None},
        "notification-destinations.json": {"version": 1, "destinations": []},
        "fcm-targets.json": {"version": 1, "targets": {}, "registrations": {}},
        "notification-incidents.json": {"version": 1, "incidents": {}, "rollout_epoch": None},
        "automations.json": {"version": 1, "automations": [], "claimed_slots": {}, "cooldown_until": {}},
    }
    for name, value in documents.items():
        (root / name).write_text(json.dumps(value))
    (root / "server-secret").write_bytes(os.urandom(32))
    (root / "automation-history.jsonl").write_text("")
    for path in root.iterdir():
        os.chmod(path, 0o600)


def create_fixture(tmp_path: Path):
    state = tmp_path / "state"
    make_state(state)
    key = tmp_path / "key"
    backup.keygen(key)
    artifact = tmp_path / "fixture.roomos-backup"
    args = Namespace(
        state_root=str(state), output=str(artifact), key_file=str(key), release="test",
        revision="a" * 40, core_image="roomos-core:sha-aaaaaaa", temp_root=str(tmp_path),
        output_uid=None, output_gid=None,
    )
    metadata = backup.create_backup(args)
    return state, key, artifact, Path(str(artifact) + ".metadata.json"), metadata


def test_round_trip_and_failure_modes(tmp_path):
    _state, key, artifact, metadata_path, metadata = create_fixture(tmp_path)
    restore = tmp_path / "restore"
    report_path = tmp_path / "report.json"
    report = backup.restore_backup(Namespace(
        artifact=str(artifact), metadata=str(metadata_path), key_file=str(key),
        restore_root=str(restore), report=str(report_path),
    ))
    assert report["passed"] is True
    assert report["semantic_summary"] == metadata["semantic_summary"]
    assert backup.verify_restore_root(restore)["sqlite_integrity"] == "ok"
    assert (restore / "state" / "automations.json").is_file()
    assert (restore / "state" / "automation-history.jsonl").is_file()
    assert report["semantic_summary"]["automations"] == 0
    assert report["semantic_summary"]["automation_history"] == 0
    with sqlite3.connect(restore / "state" / "roomos-auth.db") as db:
        panel = db.execute("SELECT version,layout,updated_at FROM panel_preferences WHERE device_id='device'").fetchone()
    assert panel is not None
    assert panel[0] == 1 and json.loads(panel[1])["density"] == "compact" and panel[2] == 1792173600
    report_document = json.loads(report_path.read_text())
    report_document["runtime_gate"] = {"runtime_schema_gate": "ok", "external_network_used": False}
    backup.atomic_write(report_path, json.dumps(report_document).encode())
    attested = backup.attest_restore(metadata_path, report_path)
    assert attested["restore_tested"] is True

    wrong_key = tmp_path / "wrong-key"
    backup.keygen(wrong_key)
    with pytest.raises(backup.BackupError, match="authentication failed"):
        backup.read_envelope(artifact, wrong_key)

    corrupted = tmp_path / "corrupt.roomos-backup"
    payload = bytearray(artifact.read_bytes())
    payload[-20] ^= 1
    corrupted.write_bytes(payload)
    with pytest.raises(backup.BackupError, match="authentication failed"):
        backup.read_envelope(corrupted, key)

    scene = restore / "state" / "scenes.json"
    original = scene.read_bytes()
    scene.write_bytes(original + b" ")
    with pytest.raises(backup.BackupError, match="checksum mismatch"):
        backup.verify_restore_root(restore)
    scene.write_bytes(original)
    (restore / "state" / "alerts.json").unlink()
    with pytest.raises(backup.BackupError, match="do not match"):
        backup.verify_restore_root(restore)


def test_automation_state_and_history_are_schema_checked(tmp_path):
    state = tmp_path / "state"
    make_state(state)
    (state / "automations.json").write_text('{"version":1,"automations":[{"raw_entity":"light.secret"}],"claimed_slots":{},"cooldown_until":{}}')
    with pytest.raises(backup.BackupError, match="automation schema"):
        backup.validate_source_root(state)
    (state / "automations.json").write_text('{"version":1,"automations":[],"claimed_slots":{},"cooldown_until":{}}')
    (state / "automation-history.jsonl").write_text('{"timestamp":"bad"}\n')
    with pytest.raises(backup.BackupError, match="automation history"):
        backup.validate_source_root(state)


def test_modified_manifest_and_incompatible_format_fail_closed(tmp_path):
    _state, key, artifact, _metadata_path, _metadata = create_fixture(tmp_path)
    plaintext, _ = backup.read_envelope(artifact, key)
    manifest, files = backup.parse_archive(plaintext)
    manifest["included"][0]["sha256"] = "0" * 64
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        backup.add_tar_bytes(archive, "manifest.json", backup.canonical_json(manifest), mode=0o600, uid=os.getuid(), gid=os.getgid(), mtime=0)
        for path, (payload, member) in files.items():
            backup.add_tar_bytes(archive, path, payload, mode=member.mode, uid=member.uid, gid=member.gid, mtime=0)
    modified = backup.decrypt_archive(backup.encrypt_archive(buffer.getvalue(), backup.load_key(key)), backup.load_key(key))
    changed_manifest, changed_files = backup.parse_archive(modified)
    with pytest.raises(backup.BackupError, match="checksum mismatch"):
        backup.validate_manifest_and_files(changed_manifest, changed_files)

    manifest["backup_format_version"] = 99
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        backup.add_tar_bytes(archive, "manifest.json", backup.canonical_json(manifest), mode=0o600, uid=os.getuid(), gid=os.getgid(), mtime=0)
        for path, (payload, member) in files.items():
            backup.add_tar_bytes(archive, path, payload, mode=member.mode, uid=member.uid, gid=member.gid, mtime=0)
    with pytest.raises(backup.BackupError, match="incompatible backup format"):
        backup.parse_archive(buffer.getvalue())


def test_unknown_and_missing_state_are_rejected(tmp_path):
    root = tmp_path / "state"
    make_state(root)
    (root / "unknown.json").write_text("{}")
    with pytest.raises(backup.BackupError, match="unknown persistent"):
        backup.validate_source_root(root)
    (root / "unknown.json").unlink()
    (root / "alerts.json").unlink()
    with pytest.raises(backup.BackupError, match="missing required"):
        backup.validate_source_root(root)

@pytest.mark.parametrize('payload', [
    {'version': 2, 'executions': []}, {'version': 1, 'executions': [{}]},
    {'version': 1, 'executions': [{}]*251}, {'version': 1, 'executions': [], 'raw': 'secret'},
])
def test_execution_schema_invalid(payload):
    with pytest.raises(backup.BackupError):
        backup.validate_json_document('executions.json', json.dumps(payload).encode())


def test_execution_present_encrypted_restore(tmp_path):
    state,key,artifact,metadata_path,_ = create_fixture(tmp_path)
    (state/'executions.json').write_text('{"version":1,"executions":[]}')
    os.chmod(state/'executions.json',0o600)
    args=Namespace(state_root=str(state),output=str(tmp_path/'new.roomos-backup'),key_file=str(key),release='test',revision='b'*40,core_image='roomos-core:sha-bbbbbbb',temp_root=str(tmp_path),output_uid=None,output_gid=None)
    metadata=backup.create_backup(args)
    assert metadata['semantic_summary']['execution_count']==0
    result=backup.restore_backup(Namespace(artifact=args.output,key_file=str(key),metadata=args.output+'.metadata.json',restore_root=str(tmp_path/'restored'),report=None))
    assert result['semantic_summary']['execution_count']==0
    assert (tmp_path/'restored/state/executions.json').stat().st_mode & 0o777 == 0o600


def test_old_tool_v1_encrypted_artifact_compatibility(tmp_path,monkeypatch):
    # Generate the exact v1 shape (including its old semantic digest), then
    # authenticate/decrypt/restore with v2. Absence is not a migration error.
    summary=backup.semantic_summary
    def old_summary(*args):
        value=summary(*args);value.pop('execution_count');value.pop('evaluation_count');return value
    with monkeypatch.context() as old:
        old.setattr(backup,'TOOL_VERSION','1')
        old.setattr(backup,'semantic_summary',old_summary)
        _,key,artifact,metadata_path,_=create_fixture(tmp_path)
    result=backup.restore_backup(Namespace(artifact=str(artifact),key_file=str(key),metadata=str(metadata_path),restore_root=str(tmp_path/'restored'),report=None))
    assert 'execution_count' not in result['semantic_summary']
    assert not (tmp_path/'restored/state/executions.json').exists()


def test_evaluation_encrypted_roundtrip_and_schema(tmp_path):
    state,key,_,_,_=create_fixture(tmp_path)
    document={'version':1,'evaluations':[{'evaluation_id':'ev_'+'a'*32,'routine_id':'rt_'+'b'*32,
        'evaluated_at':'2026-10-11T11:00:00Z','trigger_origin':'sunset','completion_state':'complete',
        'outcome':'skipped','reason_code':'presence_away','execution_id':None,'execution_result':None}]}
    member=state/'routine-evaluations.json';member.write_text(json.dumps(document));member.chmod(0o600)
    args=Namespace(state_root=str(state),output=str(tmp_path/'eval.roomos-backup'),key_file=str(key),release='test',revision='b'*40,core_image='roomos-core:sha-bbbbbbb',temp_root=str(tmp_path),output_uid=None,output_gid=None)
    metadata=backup.create_backup(args)
    result=backup.restore_backup(Namespace(artifact=args.output,key_file=str(key),metadata=args.output+'.metadata.json',restore_root=str(tmp_path/'restored'),report=None))
    assert result['semantic_summary']['evaluation_count']==1
    assert (tmp_path/'restored/state/routine-evaluations.json').read_bytes()==member.read_bytes()
    assert b'presence_away' not in Path(args.output).read_bytes()
    document['evaluations'][0]['latitude']=1
    with pytest.raises(backup.BackupError):backup.validate_json_document(member.name,json.dumps(document).encode())
    plain,_=backup.read_envelope(Path(args.output),key)
    manifest,_=backup.parse_archive(plain)
    assert manifest['tool_version']=='5'


def test_old_tool_v2_without_evaluations(tmp_path,monkeypatch):
    summary=backup.semantic_summary
    def old_summary(*args):
        value=summary(*args);value.pop('evaluation_count');return value
    with monkeypatch.context() as old:
        old.setattr(backup,'TOOL_VERSION','2');old.setattr(backup,'semantic_summary',old_summary)
        _,key,artifact,metadata_path,_=create_fixture(tmp_path)
    result=backup.restore_backup(Namespace(artifact=str(artifact),key_file=str(key),metadata=str(metadata_path),restore_root=str(tmp_path/'restored'),report=None))
    assert 'evaluation_count' not in result['semantic_summary']
    assert not (tmp_path/'restored/state/routine-evaluations.json').exists()


def test_push_state_is_optional_encrypted_and_schema_checked(tmp_path):
    state,key,_,_,_=create_fixture(tmp_path)
    registry={'version':1,'devices':{'dev':{'device_id':'dev','display_name':'Tab','created_at':'2026-10-16T09:00:00Z',
        'preferences':{'enabled':True},'webpush':{'endpoint':'https://fcm.googleapis.com/fcm/send/secret-endpoint',
        'p256dh':'x'*87,'auth':'y'*22,'endpoint_fingerprint':'0'*64,'endpoint_host':'fcm.googleapis.com',
        'subscribed_at':'2026-10-16T09:00:00Z'}}}}
    outbox={'version':1,'messages':[],'attempts_total':0,'accepted_total':0,'failed_total':0}
    for name,value in (('push-registry.json',registry),('push-outbox.json',outbox)):
        member=state/name;member.write_text(json.dumps(value));member.chmod(0o600)
    args=Namespace(state_root=str(state),output=str(tmp_path/'push.roomos-backup'),key_file=str(key),release='test',revision='c'*40,core_image='roomos-core:sha-ccccccc',temp_root=str(tmp_path),output_uid=None,output_gid=None)
    backup.create_backup(args)
    assert b'secret-endpoint' not in Path(args.output).read_bytes()
    backup.restore_backup(Namespace(artifact=args.output,key_file=str(key),metadata=args.output+'.metadata.json',restore_root=str(tmp_path/'restored'),report=None))
    assert (tmp_path/'restored/state/push-registry.json').read_bytes()==(state/'push-registry.json').read_bytes()
    outbox['last_accepted_at']='2026-10-16T09:00:00Z'
    backup.validate_json_document('push-outbox.json',json.dumps(outbox).encode())  # optional key
    with pytest.raises(backup.BackupError):
        backup.validate_json_document('push-outbox.json',json.dumps({**outbox,'surprise':1}).encode())
    registry['devices']['dev']['preferences']['enabled']='maybe'
    with pytest.raises(backup.BackupError):
        backup.validate_json_document('push-registry.json',json.dumps(registry).encode())


def test_vapid_private_key_is_optional_and_only_stored_encrypted(tmp_path):
    state,key,_,_,_=create_fixture(tmp_path)
    pem=b'-----BEGIN PRIVATE KEY-----\nVAPIDFIXTURESECRET\n-----END PRIVATE KEY-----\n'  # privacy-scan:allow fake key fixture
    member=state/'webpush-vapid-private.pem';member.write_bytes(pem);member.chmod(0o600)
    args=Namespace(state_root=str(state),output=str(tmp_path/'vapid.roomos-backup'),key_file=str(key),release='test',revision='d'*40,core_image='roomos-core:sha-ddddddd',temp_root=str(tmp_path),output_uid=None,output_gid=None)
    backup.create_backup(args)
    assert b'VAPIDFIXTURESECRET' not in Path(args.output).read_bytes()
    assert b'VAPIDFIXTURESECRET' not in Path(args.output+'.metadata.json').read_bytes()
    backup.restore_backup(Namespace(artifact=args.output,key_file=str(key),metadata=args.output+'.metadata.json',restore_root=str(tmp_path/'restored'),report=None))
    assert (tmp_path/'restored/state/webpush-vapid-private.pem').read_bytes()==pem


def test_backup_containers_mount_the_push_validator_for_rolled_back_cores():
    root=Path(__file__).parents[1]
    for script in ('roomos-backup-run.sh','roomos-state-backup-create.sh'):
        assert 'core/app/push.py,dst=/tooling/core/app/push.py,readonly' in (root/script).read_text()


def test_alert_policy_is_optional_encrypted_and_schema_checked(tmp_path):
    state,key,_,_,_=create_fixture(tmp_path)
    doc={'version':1,'profiles':{'workstation':{'profile':'gaming','until':'2026-10-17T00:00:00Z','set_at':'2026-10-16T20:00:00Z','set_by':'dev'}},
         'snoozes':{'resource.workstation.disk_usage':{'until':'2026-10-16T22:00:00Z','severity':'warning','set_at':'2026-10-16T20:00:00Z','set_by':'dev'}},
         'windows':{'resource.workstation.cpu_usage':[[29364000.0,91.0,96.0,2.0]]}}
    member=state/'alert-policy.json';member.write_text(json.dumps(doc));member.chmod(0o600)
    args=Namespace(state_root=str(state),output=str(tmp_path/'pol.roomos-backup'),key_file=str(key),release='test',revision='e'*40,core_image='roomos-core:sha-eeeeeee',temp_root=str(tmp_path),output_uid=None,output_gid=None)
    backup.create_backup(args)
    backup.restore_backup(Namespace(artifact=args.output,key_file=str(key),metadata=args.output+'.metadata.json',restore_root=str(tmp_path/'restored'),report=None))
    assert (tmp_path/'restored/state/alert-policy.json').read_bytes()==member.read_bytes()
    doc['profiles']['workstation']['profile']='turbo'
    with pytest.raises(backup.BackupError):
        backup.validate_json_document('alert-policy.json',json.dumps(doc).encode())


def test_backup_containers_mount_the_alert_policy_validator():
    root=Path(__file__).parents[1]
    for script in ('roomos-backup-run.sh','roomos-state-backup-create.sh'):
        assert 'core/app/resource_policy.py,dst=/tooling/core/app/resource_policy.py,readonly' in (root/script).read_text()


def test_backup_containers_mount_the_automation_model_for_rolled_back_cores():
    root = Path(__file__).parents[1]
    for script in ('roomos-backup-run.sh', 'roomos-state-backup-create.sh'):
        assert 'core/app/automations.py,dst=/usr/local/lib/python3.12/site-packages/app/automations.py,readonly' in (root / script).read_text()


def _core_metrics_history():
    import sys
    sys.path.insert(0, str(Path(__file__).parents[2] / 'core'))
    from app import metrics_history
    return metrics_history


def _metrics_db(path: Path):
    from datetime import UTC, datetime, timedelta
    history = _core_metrics_history().MetricsHistory(path)
    start = datetime(2026, 10, 17, 9, tzinfo=UTC)
    for minute in range(0, 120, 2):
        assert history.record('workstation', 'disk_usage', 60 + minute / 100, '%', 'home_assistant', start + timedelta(minutes=minute))
    assert history.record('primary', 'cpu_temperature', 55.5, '°C', 'system_monitor', start, provenance='measured')
    history.close()  # what Core shutdown does before the backup quiesce reads the state
    return history


def test_metrics_history_allowlists_match_core():
    core = _core_metrics_history()
    assert backup.METRICS_HISTORY_METRICS == core.METRICS
    assert backup.METRICS_HISTORY_SOURCES == core.SOURCES


def test_metrics_history_is_optional_encrypted_round_trip(tmp_path):
    state, key, _, _, first = create_fixture(tmp_path)
    assert 'metrics_history' not in first['semantic_summary']  # absent: earlier-tool summary shape unchanged
    _metrics_db(state / 'metrics-history.db')
    args = Namespace(state_root=str(state), output=str(tmp_path / 'm.roomos-backup'), key_file=str(key), release='test', revision='f' * 40, core_image='roomos-core:sha-fffffff', temp_root=str(tmp_path), output_uid=None, output_gid=None)
    metadata = backup.create_backup(args)
    assert metadata['semantic_summary']['metrics_history'] == {'rows': 28, 'series': 2}
    assert b'workstation' not in Path(args.output).read_bytes()
    restored = tmp_path / 'restored'
    report = backup.restore_backup(Namespace(artifact=args.output, key_file=str(key), metadata=args.output + '.metadata.json', restore_root=str(restored), report=None))
    assert report['semantic_summary']['metrics_history'] == {'rows': 28, 'series': 2}
    assert sorted(path.name for path in (restored / 'state').iterdir()) == sorted(path.name for path in state.iterdir())
    # The restored database is the same history to the 7J Core reader.
    from datetime import UTC, datetime
    reader = _core_metrics_history().MetricsHistory(restored / 'state' / 'metrics-history.db')
    points = reader.query('workstation', 'disk_usage', '24h', now=datetime(2026, 10, 17, 11, 30, tzinfo=UTC))['points']
    assert sum(point['samples'] for point in points) == 60


def test_metrics_history_reused_journal_of_a_killed_core_is_not_state(tmp_path):
    """7J.1 Core reuses its rollback journal; if Core was killed instead of stopped, it is left zeroed."""
    state, key, _, _, _ = create_fixture(tmp_path)
    history = _metrics_db(state / 'metrics-history.db')
    from datetime import UTC, datetime
    assert history.record('primary', 'cpu_usage', 5, '%', 'system_monitor', datetime(2026, 10, 17, 12, tzinfo=UTC))
    assert (state / 'metrics-history.db-journal').is_file()  # writer not closed: like SIGKILL
    args = Namespace(state_root=str(state), output=str(tmp_path / 'k.roomos-backup'), key_file=str(key), release='test', revision='f' * 40, core_image='roomos-core:sha-fffffff', temp_root=str(tmp_path), output_uid=None, output_gid=None)
    assert backup.create_backup(args)['semantic_summary']['metrics_history'] == {'rows': 30, 'series': 3}
    restored = tmp_path / 'restored'
    backup.restore_backup(Namespace(artifact=args.output, key_file=str(key), metadata=args.output + '.metadata.json', restore_root=str(restored), report=None))
    assert sorted(path.name for path in (restored / 'state').iterdir()) == sorted(path.name for path in state.iterdir() if not path.name.endswith('-journal'))


@pytest.mark.parametrize('tamper', [
    "CREATE TABLE extra(x)",
    "INSERT INTO cursors VALUES('workstation','ha_token_value',1)",
    "INSERT INTO cursors VALUES('192.0.2.20','cpu_usage',1)",
    "UPDATE metrics SET source='http://example' WHERE rowid=1",
    "UPDATE metrics SET total='secret' WHERE rowid=1",
    "UPDATE metrics SET step=60 WHERE rowid=1",
    "CREATE VIEW leak AS SELECT 1",
    "PRAGMA user_version=2",
])
def test_metrics_history_validator_fails_closed(tmp_path, tamper):
    path = tmp_path / 'metrics-history.db'
    _metrics_db(path)
    with sqlite3.connect(path) as db:
        db.executescript(tamper)
    with pytest.raises(backup.BackupError):
        backup.metrics_history_summary(path)


def test_metrics_history_corrupt_file_blocks_backup(tmp_path):
    state, key, _, _, _ = create_fixture(tmp_path)
    member = state / 'metrics-history.db'
    member.write_bytes(b'not sqlite')
    member.chmod(0o600)
    args = Namespace(state_root=str(state), output=str(tmp_path / 'bad.roomos-backup'), key_file=str(key), release='test', revision='f' * 40, core_image='roomos-core:sha-fffffff', temp_root=str(tmp_path), output_uid=None, output_gid=None)
    with pytest.raises((backup.BackupError, sqlite3.Error)):
        backup.create_backup(args)
    assert not Path(args.output).exists()


def test_backup_tmpfs_fits_the_metrics_history_limit():
    import re
    root = Path(__file__).parents[1]
    limit_mib = backup.METRICS_HISTORY_MAX_BYTES // (1024 * 1024)
    for script, mount in (('roomos-state-backup-create.sh', '/dev/shm'), ('roomos-backup-run.sh', '/restore')):
        size = int(re.search(rf'--tmpfs {re.escape(mount)}:[^ ]*size=(\d+)m', (root / script).read_text()).group(1))
        assert size >= 2 * limit_mib + 16, script  # snapshot + semantic copy + auth DB
