"""Bounded local numeric history; no network, no device controls, no fabricated samples.

Call from a worker thread. One device update is one short transaction on a reused
writer connection; failure disables this optional feature, never authentication or
live telemetry.
"""
import asyncio
import math
import re
import sqlite3
import threading
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path


RANGES = {"1h": 3600, "24h": 86400, "7d": 604800, "30d": 2592000}
METRICS = frozenset({"cpu_usage", "ram_usage", "gpu_usage", "disk_usage",
    "cpu_temperature", "gpu_temperature", "battery_temperature", "battery_percent",
    "storage_free", "storage_total", "storage_used", "ram_total", "ram_used", "gpu_memory"})
SOURCES = frozenset({"system_monitor", "home_assistant", "mqtt", "roomos_native", "roomos_web"})
SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics (
 device TEXT NOT NULL, metric TEXT NOT NULL, step INTEGER NOT NULL, bucket INTEGER NOT NULL,
 unit TEXT NOT NULL, source TEXT NOT NULL, total REAL NOT NULL, low REAL NOT NULL,
 high REAL NOT NULL, samples INTEGER NOT NULL, last REAL NOT NULL, provenance TEXT NOT NULL,
 PRIMARY KEY(device,metric,step,bucket));
CREATE TABLE IF NOT EXISTS cursors (
 device TEXT NOT NULL, metric TEXT NOT NULL, last REAL NOT NULL,
 PRIMARY KEY(device,metric));
PRAGMA user_version=1;
"""


FORECAST_METRICS = frozenset({"disk_usage"})
FORECAST_MIN_SPAN_HOURS = 72
FORECAST_MIN_COVERAGE = 0.7
FORECAST_MAX_STEP = 5.0  # percentage points between adjacent hours: capacity change or deletion, not a trend
FORECAST_MIN_R2 = 0.6
FORECAST_MIN_SLOPE = 0.05  # percentage points per day
FORECAST_HORIZON_DAYS = 365
FORECAST_MAX_AGE_HOURS = 6


def _transient(error: sqlite3.Error) -> bool:
    """A briefly locked database is a skipped sample, not a broken feature."""
    return isinstance(error, sqlite3.OperationalError) and any(
        word in str(error).lower() for word in ("locked", "busy"))


def _valid(metric, value, unit, source, at, provenance) -> bool:
    if metric not in METRICS or source not in SOURCES or provenance not in {"received", "measured"}:
        return False
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return False
    if not isinstance(at, datetime) or at.tzinfo is None or unit not in {"%", "°C", "GB", "GiB", "B", "MB", "MiB"}:
        return False
    return not (value < 0 or (unit == "%" and value > 100) or ("temperature" in metric and not 0 < value <= 150))


class MetricsHistory:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.status = "ok"
        self._seen = {}
        self._lock = asyncio.Lock()
        self._write_lock = threading.Lock()  # one writer connection, used from worker threads
        self._db = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._write_lock:
                db = self._writer()
                self.path.chmod(0o600)  # before the first write: SQLite gives the reused journal this mode
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1):
                    raise sqlite3.DatabaseError("unsupported version")
                db.executescript(SCHEMA)
        except (OSError, sqlite3.Error):
            self._fail()

    async def observe(self, detail):
        if self.status != "ok" or detail.connection != "online":
            return
        samples, claimed = [], {}
        for sensor in detail.sensors:
            at = sensor.measured_at or sensor.observed_at
            if (sensor.id not in METRICS or sensor.support != "supported"
                    or sensor.freshness != "fresh" or sensor.availability != "available" or at is None):
                continue
            key = (detail.id, sensor.id)
            if at.timestamp() - self._seen.get(key, 0) < 60:
                continue
            claimed[key] = self._seen.get(key)
            self._seen[key] = at.timestamp()
            samples.append((sensor.id, sensor.value, sensor.unit, sensor.source, at,
                            "measured" if sensor.measured_at else "received"))
        if not samples:
            return
        async with self._lock:
            stored = await asyncio.to_thread(self.record_many, detail.id, samples)
        if stored is None:
            # Skipped (e.g. briefly locked): the next snapshot may retry; cursors still prevent duplicates.
            for key, previous in claimed.items():
                if previous is None:
                    self._seen.pop(key, None)
                else:
                    self._seen[key] = previous

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=1.0)
        db.execute("PRAGMA max_page_count=16384")  # 64 MiB at default 4096-byte pages
        db.execute("PRAGMA cache_size=-512")
        return db

    def _writer(self):
        if self._db is None:
            db = sqlite3.connect(self.path, timeout=1.0, isolation_level=None, check_same_thread=False)
            try:
                db.execute("PRAGMA max_page_count=16384")  # 64 MiB at default 4096-byte pages
                db.execute("PRAGMA cache_size=-512")
                # Same rollback journal and fsync durability as 7J, but the journal file is reused
                # (header zeroed) instead of created and unlinked on every commit. Per connection:
                # the database file format does not change. WAL was measured and rejected: a
                # cleanly closed WAL database cannot be opened through the backup's read-only mount.
                db.execute("PRAGMA journal_mode=PERSIST")
                db.execute("PRAGMA journal_size_limit=1048576")
            except BaseException:
                db.close()
                raise
            self._db = db
        return self._db

    def _fail(self):
        self.status = "unavailable"
        if self._db is not None:
            try:
                self._db.close()
            except sqlite3.Error:
                pass
            self._db = None

    def close(self):
        """Release the writer and remove the reused journal: on disk exactly as 7J leaves it."""
        with self._write_lock:
            if self._db is None:
                return
            try:
                self._db.execute("PRAGMA journal_mode=DELETE")
            except sqlite3.Error:
                pass
            finally:
                self._db.close()
                self._db = None

    def record(self, device, metric, value, unit, source, at, *,
               fresh=True, supported=True, online=True, provenance="received"):
        if not (fresh and supported and online):
            return False
        return self.record_many(device, [(metric, value, unit, source, at, provenance)]) == 1

    def record_many(self, device, samples):
        """Store one device update in one transaction, in the given order.

        Returns how many samples were new (invalid, repeated and sub-minute samples are not),
        or None when the update was skipped and nothing of it was written.
        """
        if self.status != "ok" or not re.fullmatch(r"[a-z0-9_]{1,40}", device):
            return 0
        samples = [sample for sample in samples if _valid(*sample)]
        if not samples:
            return 0
        with self._write_lock:
            try:
                db = self._writer()
                db.execute("BEGIN IMMEDIATE")
                try:
                    stored = sum(self._apply(db, device, *sample) for sample in samples)
                    db.execute("COMMIT")
                except BaseException:
                    if db.in_transaction:
                        db.execute("ROLLBACK")
                    raise
                return stored
            except sqlite3.Error as error:
                if not _transient(error):
                    self._fail()
                return None
            except OSError:
                self._fail()
                return None

    @staticmethod
    def _apply(db, device, metric, value, unit, source, at, provenance):
        timestamp = at.timestamp()
        previous = db.execute("SELECT last FROM cursors WHERE device=? AND metric=?", (device, metric)).fetchone()
        # Never count repeated reads of the same source sample, including after restart.
        if previous and timestamp <= previous[0]:
            return 0
        # At most one sample per minute; steady readings are not event storms.
        if previous and timestamp - previous[0] < 60:
            return 0
        db.execute("INSERT INTO cursors VALUES(?,?,?) ON CONFLICT(device,metric) DO UPDATE SET last=excluded.last", (device, metric, timestamp))
        for step in (300, 3600):
            bucket = int(timestamp) // step * step
            db.execute("""INSERT INTO metrics VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(device,metric,step,bucket) DO UPDATE SET
              total=metrics.total+excluded.total,low=min(metrics.low,excluded.low),
              high=max(metrics.high,excluded.high),samples=metrics.samples+1,last=excluded.last
              WHERE metrics.unit=excluded.unit AND metrics.source=excluded.source
              AND metrics.provenance=excluded.provenance""",
              (device, metric, step, bucket, unit, source, value, value, value, 1, timestamp, provenance))
        db.execute("DELETE FROM metrics WHERE device=? AND metric=? AND ((step=300 AND bucket<?) OR bucket<?)",
                   (device, metric, timestamp-172800, timestamp-2592000))
        return 1

    def query(self, device, metric, range_name, *, now=None):
        if range_name not in RANGES or metric not in METRICS or not re.fullmatch(r"[a-z0-9_]{1,40}", device):
            raise ValueError("unsupported history query")
        now = now or datetime.now(UTC)
        step = 300 if range_name in {"1h", "24h"} else 3600
        end = int(now.timestamp()) // step * step
        start = end - RANGES[range_name]
        result = {"device_id": device, "metric": metric, "range": range_name,
                  "step_seconds": step, "status": self.status, "unit": None,
                  "points": [], "generated_at": now.astimezone(UTC).isoformat()}
        if self.status != "ok":
            return result
        try:
            with closing(self._connect()) as db:
                rows = db.execute("""SELECT bucket,total,low,high,samples,unit,source,provenance
                  FROM metrics WHERE device=? AND metric=? AND step=? AND bucket>=? AND bucket<=?
                  ORDER BY bucket LIMIT 721""", (device, metric, step, start, end)).fetchall()
            points = {row[0]: row for row in rows}
            result["unit"] = rows[-1][5] if rows else None
            for bucket in range(start, end+1, step):
                row = points.get(bucket)
                # A unit change is a gap rather than silently mixing bytes and GB.
                valid = row is not None and row[5] == result["unit"]
                result["points"].append({"ts": datetime.fromtimestamp(bucket, UTC).isoformat(),
                    "avg": row[1]/row[4] if valid else None, "min": row[2] if valid else None,
                    "max": row[3] if valid else None, "samples": row[4] if valid else 0,
                    "source": row[6] if valid else None, "provenance": row[7] if valid else None})
            result["status"] = "ok" if rows else "insufficient"
        except sqlite3.Error as error:
            if not _transient(error):
                self.status = "unavailable"
            result["status"] = "unavailable"
            result["points"] = []
        except OSError:
            self.status = result["status"] = "unavailable"
        return result

    def forecast(self, device, metric="disk_usage", *, now=None):
        """Linear fill estimate from hourly evidence; explicit about when it cannot be trusted.

        Utilization is not a health verdict and a forecast is not a promise: deletions and capacity
        changes (large steps) make the window unreliable instead of being fitted through.
        """
        if metric not in FORECAST_METRICS or not re.fullmatch(r"[a-z0-9_]{1,40}", device):
            raise ValueError("unsupported forecast query")
        now = now or datetime.now(UTC)
        result = {"device_id": device, "metric": metric, "status": self.status, "reason": None,
                  "unit": "%", "observations": 0, "window_start": None, "window_end": None,
                  "slope_per_day": None, "latest": None, "days_until_full": None,
                  "generated_at": now.astimezone(UTC).isoformat()}
        if self.status != "ok":
            return result
        start = int(now.timestamp()) - RANGES["30d"]
        try:
            with closing(self._connect()) as db:
                rows = db.execute("""SELECT bucket,total/samples FROM metrics WHERE device=? AND metric=?
                  AND step=3600 AND unit='%' AND bucket>=? ORDER BY bucket LIMIT 721""", (device, metric, start)).fetchall()
        except sqlite3.Error as error:
            if not _transient(error):
                self.status = "unavailable"
            result["status"] = "unavailable"
            return result
        result["observations"] = len(rows)

        def verdict(status, reason):
            result["status"], result["reason"] = status, reason
            return result

        if len(rows) < 2:
            return verdict("insufficient", "Not enough hourly observations")
        result["window_start"] = datetime.fromtimestamp(rows[0][0], UTC).isoformat()
        result["window_end"] = datetime.fromtimestamp(rows[-1][0], UTC).isoformat()
        result["latest"] = round(rows[-1][1], 2)
        span_hours = (rows[-1][0] - rows[0][0]) / 3600
        if now.timestamp() - rows[-1][0] > FORECAST_MAX_AGE_HOURS * 3600:
            return verdict("insufficient", "No recent observations")
        if span_hours < FORECAST_MIN_SPAN_HOURS:
            return verdict("insufficient", "Fewer than three days of observations")
        if len(rows) < FORECAST_MIN_COVERAGE * (span_hours + 1):
            return verdict("insufficient", "Too many missing hours")
        if any(abs(b[1] - a[1]) > FORECAST_MAX_STEP for a, b in zip(rows, rows[1:])):
            return verdict("unreliable", "Large step change (deletion or capacity change)")
        xs = [(bucket - rows[0][0]) / 86400 for bucket, _ in rows]
        ys = [value for _, value in rows]
        mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
        sxx = sum((x - mean_x) ** 2 for x in xs)
        sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
        syy = sum((y - mean_y) ** 2 for y in ys)
        slope = sxy / sxx if sxx else 0.0
        result["slope_per_day"] = round(slope, 4)
        if slope < FORECAST_MIN_SLOPE:
            return verdict("not_increasing", "Usage is not consistently increasing")
        r2 = (sxy * sxy) / (sxx * syy) if sxx and syy else 0.0
        if r2 < FORECAST_MIN_R2:
            return verdict("unreliable", "Growth is not consistent")
        days = (100 - ys[-1]) / slope
        if days > FORECAST_HORIZON_DAYS:
            return verdict("beyond_horizon", "Not expected to fill within a year")
        result["days_until_full"] = round(days, 1)
        return verdict("ok", None)
