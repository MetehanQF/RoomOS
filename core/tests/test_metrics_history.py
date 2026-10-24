from datetime import UTC, datetime, timedelta
import pytest

from app.metrics_history import MetricsHistory


NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)


def sample(store, at=NOW, value=42, metric="cpu_usage", **kwargs):
    return store.record("workstation", metric, value, "%", "home_assistant", at, **kwargs)


def test_duplicate_and_out_of_order_do_not_add_weight(tmp_path):
    h = MetricsHistory(tmp_path / "metrics.db")
    assert sample(h)
    assert not sample(h)
    assert not sample(h, at=NOW-timedelta(seconds=1))
    points = h.query("workstation", "cpu_usage", "1h", now=NOW+timedelta(seconds=1))["points"]
    assert sum(p["samples"] for p in points) == 1


def test_invalid_stale_unsupported_and_zero_temperature_rejected(tmp_path):
    h = MetricsHistory(tmp_path / "metrics.db")
    for options in ({"fresh": False}, {"supported": False}, {"online": False}):
        assert not sample(h, **options)
    assert not sample(h, value=float("nan"))
    assert not sample(h, value=101)
    assert not sample(h, value=0, metric="gpu_temperature")
    assert not sample(h, metric="secret_token")


def test_gaps_and_restart(tmp_path):
    path = tmp_path / "metrics.db"
    h = MetricsHistory(path)
    sample(h)
    sample(h, at=NOW+timedelta(minutes=20), value=60)
    h = MetricsHistory(path)
    result = h.query("workstation", "cpu_usage", "1h", now=NOW+timedelta(minutes=25))
    assert any(p["avg"] is None for p in result["points"])
    assert sum(p["samples"] for p in result["points"]) == 2
    assert len(result["points"]) <= 721


def test_query_bounds_and_corruption_fail_closed(tmp_path):
    path = tmp_path / "metrics.db"
    path.write_bytes(b"not sqlite")
    h = MetricsHistory(path)
    assert not sample(h)
    assert h.query("workstation", "cpu_usage", "30d", now=NOW)["status"] == "unavailable"
    assert path.read_bytes() == b"not sqlite"


def test_utc_and_retention(tmp_path):
    h = MetricsHistory(tmp_path / "metrics.db")
    sample(h, at=NOW-timedelta(days=31))
    sample(h)
    result = h.query("workstation", "cpu_usage", "30d", now=NOW)
    assert sum(p["samples"] for p in result["points"]) == 1
    assert all(p["ts"].endswith("+00:00") for p in result["points"])


def _hourly(store, values, end=NOW, metric="disk_usage", device="workstation"):
    start = end - timedelta(hours=len(values) - 1)
    for index, value in enumerate(values):
        if value is not None:
            assert store.record(device, metric, value, "%", "home_assistant", start + timedelta(hours=index))


def test_forecast_requires_multi_day_covered_consistent_growth(tmp_path):
    h = MetricsHistory(tmp_path / "metrics.db")
    _hourly(h, [50 + i * 0.05 for i in range(24)])
    assert h.forecast("workstation", now=NOW)["status"] == "insufficient"  # one day only
    h = MetricsHistory(tmp_path / "grow.db")
    _hourly(h, [50 + i * 0.05 for i in range(24 * 5)])  # 1.2 points/day
    result = h.forecast("workstation", now=NOW)
    assert result["status"] == "ok" and 36 < result["days_until_full"] < 37.5
    assert result["slope_per_day"] == pytest.approx(1.2, abs=0.01)


def test_forecast_refuses_steps_flat_noise_gaps_and_stale(tmp_path):
    cases = {
        "unreliable": [50 + i * 0.05 for i in range(60)] + [30 + i * 0.05 for i in range(60)],  # deletion
        "not_increasing": [60.0] * 120,
        "insufficient": [50 + i * 0.05 if i % 3 == 0 else None for i in range(120)],  # sparse
    }
    for expected, values in cases.items():
        h = MetricsHistory(tmp_path / f"{expected}.db")
        _hourly(h, values)
        assert h.forecast("workstation", now=NOW)["status"] == expected, expected
    h = MetricsHistory(tmp_path / "noisy.db")
    _hourly(h, [50 + (i % 2) * 4 + i * 0.002 for i in range(120)])
    assert h.forecast("workstation", now=NOW)["status"] == "unreliable"
    h = MetricsHistory(tmp_path / "stale.db")
    _hourly(h, [50 + i * 0.05 for i in range(120)], end=NOW - timedelta(days=1))
    assert h.forecast("workstation", now=NOW)["status"] == "insufficient"
    with pytest.raises(ValueError):
        h.forecast("workstation", "cpu_usage", now=NOW)


def test_locked_database_skips_without_disabling(tmp_path):
    import sqlite3
    path = tmp_path / "metrics.db"
    h = MetricsHistory(path)
    blocker = sqlite3.connect(path)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        assert not sample(h)
        assert h.query("workstation", "cpu_usage", "1h", now=NOW)["status"] == "unavailable"
    finally:
        blocker.rollback()
        blocker.close()
    assert h.status == "ok"
    assert sample(h, at=NOW + timedelta(minutes=2))


# ----- 7J.1: one transaction per device update, reused writer, crash and lock safety ------------
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace


def _sensor(metric, value, at, unit="%", **overrides):
    fields = dict(id=metric, value=value, unit=unit, source="system_monitor", support="supported",
                  freshness="fresh", availability="available", measured_at=at, observed_at=None)
    return SimpleNamespace(**{**fields, **overrides})


def _detail(at, device="primary", connection="online", extra=()):
    sensors = [_sensor("cpu_usage", 20, at), _sensor("ram_usage", 30, at), _sensor("disk_usage", 40, at),
               _sensor("cpu_temperature", 50, at, unit="°C"), *extra]
    return SimpleNamespace(id=device, connection=connection, sensors=sensors)


def _commits(store):
    statements = []
    store._writer().set_trace_callback(statements.append)
    return statements


def _rows(path):
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        return db.execute("SELECT * FROM metrics ORDER BY device,metric,step,bucket").fetchall(), \
            db.execute("SELECT * FROM cursors ORDER BY device,metric").fetchall()


async def test_device_update_is_one_transaction_with_unchanged_semantics(tmp_path):
    h = MetricsHistory(tmp_path / "metrics.db")
    statements = _commits(h)
    invalid = (_sensor("gpu_temperature", 0, NOW, unit="°C"), _sensor("gpu_usage", 5, NOW, support="unverified"),
               _sensor("ram_total", 8, NOW, freshness="stale"), _sensor("secret_token", 1, NOW))
    await h.observe(_detail(NOW, extra=invalid))
    assert statements.count("COMMIT") == 1 and statements.count("BEGIN IMMEDIATE") == 1
    await h.observe(_detail(NOW))  # duplicate snapshot: no transaction at all
    await h.observe(_detail(NOW + timedelta(seconds=30)))  # sub-minute: no transaction
    assert statements.count("COMMIT") == 1
    await h.observe(_detail(NOW + timedelta(minutes=1), connection="offline"))
    assert statements.count("COMMIT") == 1
    await h.observe(_detail(NOW + timedelta(minutes=1)))
    assert statements.count("COMMIT") == 2
    rows, cursors = _rows(h.path)
    assert [c[1] for c in cursors] == ["cpu_temperature", "cpu_usage", "disk_usage", "ram_usage"]
    assert all(r[9] == 2 for r in rows)  # two samples per bucket, invalid sensors never stored


def test_batch_matches_sequential_records_exactly(tmp_path):
    batched, single = MetricsHistory(tmp_path / "a.db"), MetricsHistory(tmp_path / "b.db")
    for minute in range(0, 400, 1):
        at = NOW + timedelta(minutes=minute, seconds=minute % 7)
        samples = [("cpu_usage", minute % 90, "%", "system_monitor", at, "measured"),
                   ("cpu_temperature", 40 + minute % 30, "°C", "system_monitor", at, "measured"),
                   ("cpu_usage", 99, "%", "system_monitor", at - timedelta(seconds=5), "measured")]  # out of order
        batched.record_many("primary", samples)
        for metric, value, unit, source, when, provenance in samples:
            single.record("primary", metric, value, unit, source, when, provenance=provenance)
    assert _rows(batched.path) == _rows(single.path)


async def test_transient_lock_skips_whole_update_and_next_snapshot_retries(tmp_path):
    path = tmp_path / "metrics.db"
    h = MetricsHistory(path)
    blocker = sqlite3.connect(path)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        await h.observe(_detail(NOW))
    finally:
        blocker.rollback()
        blocker.close()
    assert h.status == "ok" and _rows(path) == ([], [])
    await h.observe(_detail(NOW + timedelta(seconds=10)))  # same minute, retried instead of lost
    rows, cursors = _rows(path)
    assert len(cursors) == 4 and sum(r[9] for r in rows if r[2] == 300) == 4
    await h.observe(_detail(NOW + timedelta(seconds=10)))  # replay of the stored sample adds no weight
    assert _rows(path) == (rows, cursors)


def test_failure_mid_update_rolls_back_everything_and_fails_closed(tmp_path):
    path = tmp_path / "metrics.db"
    h = MetricsHistory(path)
    h.record_many("primary", [("cpu_usage", 1, "%", "system_monitor", NOW, "measured")])
    before = _rows(path)
    db = h._writer()
    db.execute("PRAGMA max_page_count=" + str(db.execute("PRAGMA page_count").fetchone()[0]))  # disk full
    samples = [(metric, 5, "%", "system_monitor", NOW + timedelta(minutes=minute), "measured")
               for minute in range(1, 400) for metric in ("cpu_usage", "ram_usage")]
    assert h.record_many("primary", samples) is None
    assert h.status == "unavailable" and h._db is None
    assert _rows(path) == before  # no partial update survives
    with sqlite3.connect(path) as check:
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_writer_connection_is_reused(tmp_path, monkeypatch):
    import app.metrics_history as module
    opened = []
    real = module.sqlite3.connect
    monkeypatch.setattr(module.sqlite3, "connect", lambda *a, **k: opened.append(a) or real(*a, **k))
    h = MetricsHistory(tmp_path / "metrics.db")
    for minute in range(20):
        assert sample(h, at=NOW + timedelta(minutes=minute))
    assert len(opened) == 1


def test_close_leaves_a_plain_rollback_journal_database(tmp_path):
    path = tmp_path / "metrics.db"
    h = MetricsHistory(path)
    sample(h)
    h.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["metrics.db"]
    assert path.read_bytes()[18:20] == b"\x01\x01"  # legacy (rollback journal) file format
    h.close()  # idempotent
    assert sample(h, at=NOW + timedelta(minutes=1))  # a late write reopens instead of failing
    h.close()


def test_closed_database_snapshots_from_a_read_only_directory(tmp_path):
    """The 04:30 backup reads the state through a read-only bind mount."""
    path = tmp_path / "state" / "metrics.db"
    h = MetricsHistory(path)
    sample(h)
    h.close()
    path.parent.chmod(0o500)
    path.chmod(0o400)
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as source, sqlite3.connect(tmp_path / "copy.db") as target:
            source.backup(target)
            assert target.execute("SELECT count(*) FROM cursors").fetchone()[0] == 1
    finally:
        path.parent.chmod(0o700)


CRASH = r'''
import os, sys
from datetime import UTC, datetime, timedelta
sys.path.insert(0, sys.argv[1])
from app.metrics_history import MetricsHistory
NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)
h = MetricsHistory(sys.argv[2])
for minute in range(int(sys.argv[3])):
    at = NOW + timedelta(minutes=minute)
    assert h.record_many("primary", [(m, minute % 90, "%", "system_monitor", at, "measured") for m in ("cpu_usage", "ram_usage")]) == 2
if sys.argv[4] == "in_transaction":
    db = h._writer()
    db.execute("BEGIN IMMEDIATE")
    # Enough uncommitted pages to spill past the 512 KiB cache: database pages change on disk
    # behind a hot journal, the worst moment for a kill.
    for index in range(4000):
        h._apply(db, f"spill_{index}", "ram_usage", 1, "%", "system_monitor", NOW + timedelta(days=1), "measured")
os._exit(9)  # no close, no rollback: like SIGKILL
'''


def _crash(tmp_path, minutes, where):
    path = tmp_path / "metrics.db"
    script = tmp_path / "crash.py"
    script.write_text(CRASH)
    core = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, script, core, path, str(minutes), where], capture_output=True)
    assert result.returncode == 9, result.stderr
    return path


def _snapshot_read_only(path, target):
    """What the 04:30 backup does: SQLite backup API through a read-only bind mount."""
    path.parent.chmod(0o500)
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as source, sqlite3.connect(target) as copy:
            source.backup(copy)
            assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            return copy.execute("SELECT count(*), sum(samples) FROM metrics").fetchone()
    finally:
        path.parent.chmod(0o700)


def test_kill_after_commits_keeps_every_committed_update(tmp_path):
    (tmp_path / "state").mkdir()
    path = _crash(tmp_path / "state", 30, "after_commit")
    journal = path.with_name(path.name + "-journal")
    assert journal.exists() and not journal.read_bytes()[:28].strip(b"\0")  # reused journal, header zeroed: not hot
    assert journal.stat().st_mode & 0o777 == 0o600 and path.stat().st_mode & 0o777 == 0o600
    rows, _ = _rows(path)
    assert _snapshot_read_only(path, tmp_path / "copy.db") == (len(rows), sum(r[9] for r in rows))
    h = MetricsHistory(path)
    rows, cursors = _rows(path)
    assert sum(r[9] for r in rows if r[2] == 300) == 60 and len(cursors) == 2
    assert not h.record("primary", "cpu_usage", 5, "%", "system_monitor", NOW + timedelta(minutes=29))  # replay
    with sqlite3.connect(path) as check:
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_kill_inside_a_transaction_recovers_without_the_partial_update(tmp_path):
    path = _crash(tmp_path, 5, "in_transaction")
    assert path.with_name(path.name + "-journal").read_bytes()[:8] == bytes.fromhex("d9d505f920a163d7")  # hot
    h = MetricsHistory(path)
    assert h.status == "ok"
    rows, cursors = _rows(path)
    assert sum(r[9] for r in rows if r[2] == 300) == 10
    assert max(c[2] for c in cursors) == (NOW + timedelta(minutes=4)).timestamp()
    assert {c[0] for c in cursors} == {"primary"} and not path.with_name(path.name + "-journal").read_bytes()[:28].strip(b"\0")
    with sqlite3.connect(path) as check:
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
