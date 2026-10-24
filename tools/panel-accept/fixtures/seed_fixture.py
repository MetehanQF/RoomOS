# Synthetic, non-production acceptance state written with the candidate image's own models.
import json, sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
sys.path.insert(0, '/src/tests')
from test_routines import managed_evening, managed_all_off  # noqa: E402
from app.routines import RoutineDocument  # noqa: E402
from app.routine_evaluations import EvaluationDocument  # noqa: E402
from app.alerts import AlertRecord, AlertStateFile  # noqa: E402

root = Path('/var/lib/roomos-auth')
now = datetime.now(UTC)
ev, off = managed_evening(), managed_all_off()
(root / 'routines.json').write_text(RoutineDocument(version=1, routines=[ev, off], claimed_slots={}).model_dump_json())


def rec(n, routine, minutes, origin, state, outcome=None, reason=None, ex=None, result=None, reporting=None, legacy=False):
    r = {'evaluation_id': f'ev_{n:032x}', 'routine_id': routine.id, 'evaluated_at': (now - timedelta(minutes=minutes)).isoformat(),
         'trigger_origin': origin, 'completion_state': state, 'outcome': outcome, 'reason_code': reason,
         'execution_id': ex, 'execution_result': result}
    if not legacy:
        r['presence_reporting'] = reporting
    return r


records = [  # oldest first, as stored
    rec(1, ev, 7200, None, 'complete', 'skipped', 'presence_unknown', legacy=True),
    rec(2, ev, 2880, 'sunset', 'pending', reporting='unknown'),
    rec(3, ev, 1500, 'arrival', 'complete', 'failed', 'ha_unavailable', reporting='unknown'),
    rec(4, off, 960, 'schedule', 'complete', 'executed', None, 'ex_' + 'c' * 32, 'success'),
    rec(5, ev, 180, 'sunset', 'complete', 'skipped', 'presence_away', reporting='stale'),
    rec(6, ev, 12, 'arrival', 'complete', 'executed', None, 'ex_' + 'd' * 32, 'partial', reporting='fresh'),
]
(root / 'routine-evaluations.json').write_text(EvaluationDocument.model_validate({'version': 1, 'evaluations': records}).model_dump_json())
alerts = AlertStateFile(alerts=[
    AlertRecord(id='fixture.acceptance.warning', type='fixture.warning', severity='warning',
                title='Secondary disk usage is high', message='Synthetic acceptance signal.', source='secondary',
                started_at=now - timedelta(minutes=18), updated_at=now - timedelta(minutes=3), status='active',
                occurrence_count=3, last_observed_at=now - timedelta(minutes=3), last_occurrence_at=now - timedelta(minutes=3)),
    AlertRecord(id='fixture.acceptance.resolved', type='fixture.recovered', severity='info',
                title='Home Assistant connection recovered', message='Synthetic recovery signal.', source='home_assistant',
                started_at=now - timedelta(hours=2), updated_at=now - timedelta(hours=1), status='resolved',
                read_at=now - timedelta(minutes=55)),
])
(root / 'alerts.json').write_text(alerts.model_dump_json(exclude_none=True))
for p in root.iterdir():
    p.chmod(0o600)
print(json.dumps({'routines': 2, 'evaluations': len(records), 'alerts': len(alerts.alerts)}))
