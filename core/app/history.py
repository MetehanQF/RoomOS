"""Bounded, read-only historical telemetry for the System trends view.

Home Assistant identifiers stay in this module and ``catalog``; responses expose only the
logical ``node``/``metric``/``range`` contract. This module only ever reads history through
``HomeAssistantClient.get_history`` and has no access to service calls.
"""

import asyncio
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from .catalog import NODE_ENTITIES
from .ha_client import HomeAssistantUnavailable
from .models import SystemResponse

HistoryNode = Literal["primary", "secondary"]
HistoryMetric = Literal["cpu", "ram", "temperature"]
HistoryRange = Literal["1h", "6h", "24h"]
Freshness = Literal["fresh", "stale", "unknown", "unavailable"]
GapReason = Literal["no_data", "unavailable", "offline"]


@dataclass(frozen=True)
class RangeSpec:
    bucket_seconds: int
    buckets: int
    ttl_seconds: float


RANGES: dict[str, RangeSpec] = {
    "1h": RangeSpec(bucket_seconds=60, buckets=60, ttl_seconds=30.0),
    "6h": RangeSpec(bucket_seconds=180, buckets=120, ttl_seconds=90.0),
    "24h": RangeSpec(bucket_seconds=600, buckets=144, ttl_seconds=180.0),
}
UNITS: dict[str, str] = {"cpu": "%", "ram": "%", "temperature": "°C"}
STALE_FALLBACK_FACTOR = 2.0
FRESH_SAMPLE_MAX_AGE = timedelta(minutes=3)
CARRIED_IN_TOLERANCE = timedelta(seconds=1)


def _node_entity(node: str, role: str) -> str:
    entity_id = NODE_ENTITIES[node].get(role)
    if entity_id is None:
        raise RuntimeError(f"history mapping missing for {node}/{role}")
    return entity_id


def _build_mapping() -> dict[tuple[str, str], tuple[str, str]]:
    mapping: dict[tuple[str, str], tuple[str, str]] = {}
    for node in ("primary", "secondary"):
        online = _node_entity(node, "online")
        for metric in UNITS:
            mapping[(node, metric)] = (_node_entity(node, metric), online)
    return mapping


# Server-owned (node, metric) -> (metric entity, node connectivity entity).
HISTORY_MAP = _build_mapping()


class HistoryQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node: HistoryNode
    metric: HistoryMetric
    range: HistoryRange


class HistoryPoint(BaseModel):
    ts: datetime
    avg: float | None
    min: float | None
    max: float | None
    samples: int


class HistoryGap(BaseModel):
    start: datetime
    end: datetime
    reason: GapReason


class HistoryResponse(BaseModel):
    node: HistoryNode
    metric: HistoryMetric
    unit: str
    range: HistoryRange
    bucket_seconds: int
    points: list[HistoryPoint]
    gaps: list[HistoryGap]
    source: Literal["home_assistant"] = "home_assistant"
    freshness: Freshness
    generated_at: datetime
    cached: bool = False


class HistoryUnavailable(RuntimeError):
    """Sanitized: history could not be produced and no bounded fallback exists."""


class HistoryReader(Protocol):
    async def get_history(
        self, entity_ids: list[str], start: datetime, end: datetime
    ) -> list[list[dict[str, Any]]]: ...


@dataclass(frozen=True)
class Observation:
    ts: datetime
    value: float | None  # None = invalid (unknown/unavailable/malformed/non-finite)
    carried_in: bool


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _numeric(state: object) -> float | None:
    if isinstance(state, bool) or not isinstance(state, (str, int, float)):
        return None
    try:
        value = float(state)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def normalize_series(raw: list[dict[str, Any]], start: datetime) -> list[Observation]:
    """Finite numbers are samples; everything else is an explicit invalid marker.

    Duplicate timestamps keep the last value. The first point HA moves onto the window start
    is flagged ``carried_in``: it describes the state entering the window, not a new reading.
    """

    by_ts: dict[datetime, float | None] = {}
    for point in raw:
        if not isinstance(point, dict):
            continue
        ts = _parse_ts(point.get("last_changed") or point.get("last_updated"))
        if ts is None:
            continue
        by_ts[ts] = _numeric(point.get("state"))
    ordered = sorted(by_ts.items())
    return [
        Observation(
            ts=ts,
            value=value,
            carried_in=index == 0 and abs(ts - start) <= CARRIED_IN_TOLERANCE,
        )
        for index, (ts, value) in enumerate(ordered)
    ]


def offline_intervals(raw: list[dict[str, Any]], end: datetime) -> list[tuple[datetime, datetime]]:
    """Intervals where the node connectivity sensor reported ``off``."""

    changes: dict[datetime, str] = {}
    for point in raw:
        if not isinstance(point, dict):
            continue
        ts = _parse_ts(point.get("last_changed") or point.get("last_updated"))
        if ts is not None:
            changes[ts] = str(point.get("state", "")).lower()
    intervals: list[tuple[datetime, datetime]] = []
    ordered = sorted(changes.items())
    for index, (ts, state) in enumerate(ordered):
        if state != "off":
            continue
        until = ordered[index + 1][0] if index + 1 < len(ordered) else end
        intervals.append((ts, until))
    return intervals


def _segments(
    observations: list[Observation],
    offline: list[tuple[datetime, datetime]],
    end: datetime,
) -> list[tuple[datetime, datetime, float | None, GapReason | None]]:
    """Step-hold segments: HA records changes, so each value holds until the next change."""

    segments: list[tuple[datetime, datetime, float | None, GapReason | None]] = []
    for index, observation in enumerate(observations):
        until = observations[index + 1].ts if index + 1 < len(observations) else end
        if until <= observation.ts:
            continue
        reason: GapReason | None = None if observation.value is not None else "unavailable"
        segments.append((observation.ts, until, observation.value, reason))
    for off_start, off_end in offline:
        split: list[tuple[datetime, datetime, float | None, GapReason | None]] = []
        for seg_start, seg_end, value, reason in segments:
            if seg_end <= off_start or seg_start >= off_end:
                split.append((seg_start, seg_end, value, reason))
                continue
            if seg_start < off_start:
                split.append((seg_start, off_start, value, reason))
            split.append((max(seg_start, off_start), min(seg_end, off_end), None, "offline"))
            if seg_end > off_end:
                split.append((off_end, seg_end, value, reason))
        segments = split
    return segments


def bucketize(
    observations: list[Observation],
    offline: list[tuple[datetime, datetime]],
    *,
    window_start: datetime,
    now: datetime,
    spec: RangeSpec,
) -> tuple[list[HistoryPoint], list[HistoryGap]]:
    """Fixed buckets with min/avg/max over every value that held inside the bucket."""

    segments = _segments(observations, offline, now)
    width = timedelta(seconds=spec.bucket_seconds)
    points: list[HistoryPoint] = []
    reasons: list[GapReason | None] = []
    for index in range(spec.buckets):
        bucket_start = window_start + index * width
        bucket_end = min(bucket_start + width, now)
        values: list[float] = []
        reason: GapReason | None = "no_data"
        if bucket_end > bucket_start:
            for seg_start, seg_end, value, seg_reason in segments:
                if seg_end <= bucket_start or seg_start >= bucket_end:
                    continue
                if value is not None:
                    values.append(value)
                elif seg_reason is not None and not values:
                    reason = seg_reason
        if values:
            points.append(
                HistoryPoint(
                    ts=bucket_start,
                    avg=round(sum(values) / len(values), 2),
                    min=round(min(values), 2),
                    max=round(max(values), 2),
                    samples=len(values),
                )
            )
            reasons.append(None)
        else:
            points.append(HistoryPoint(ts=bucket_start, avg=None, min=None, max=None, samples=0))
            reasons.append(reason)
    gaps: list[HistoryGap] = []
    for index, reason in enumerate(reasons):
        if reason is None:
            continue
        bucket_start = window_start + index * width
        bucket_end = bucket_start + width
        if gaps and gaps[-1].reason == reason and gaps[-1].end == bucket_start:
            gaps[-1].end = bucket_end
        else:
            gaps.append(HistoryGap(start=bucket_start, end=bucket_end, reason=reason))
    return points, gaps


def window_for(now: datetime, spec: RangeSpec) -> datetime:
    """Bucket-aligned window start; the final bucket contains ``now``."""

    epoch = int(now.timestamp())
    aligned_end = (epoch // spec.bucket_seconds + 1) * spec.bucket_seconds
    return datetime.fromtimestamp(aligned_end - spec.buckets * spec.bucket_seconds, UTC)


def _fallback_freshness(observations: list[Observation], now: datetime) -> Freshness:
    real = [observation for observation in observations if not observation.carried_in]
    if observations and observations[-1].value is None:
        return "unavailable"
    if not real:
        return "unknown"
    return "fresh" if now - real[-1].ts <= FRESH_SAMPLE_MAX_AGE else "stale"


def live_freshness(snapshot: SystemResponse | None, node: str, metric: str) -> Freshness | None:
    if snapshot is None:
        return None
    system_node = snapshot.primary if node == "primary" else snapshot.secondary
    live = system_node.metrics.get(f"{node}_{metric}")
    return live.freshness if live is not None else None


@dataclass
class _CacheEntry:
    response: HistoryResponse
    stored_at: float


class HistoryService:
    """Process-local TTL cache with per-key single-flight. Never persisted."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        snapshot: Callable[[], SystemResponse | None] = lambda: None,
    ) -> None:
        self.clock = clock
        self.now = now
        self.snapshot = snapshot
        self._cache: dict[tuple[str, str, str], _CacheEntry] = {}
        self._inflight: dict[tuple[str, str, str], asyncio.Future[HistoryResponse]] = {}
        self.fetches = 0
        # Observability only: outcome of the most recent HA history fetch.
        self.last_success_at: datetime | None = None
        self.last_failure_at: datetime | None = None
        self.last_outcome: Literal["none", "ok", "stale_fallback", "failed"] = "none"

    async def get(self, reader: HistoryReader, query: HistoryQuery) -> HistoryResponse:
        key = (query.node, query.metric, query.range)
        spec = RANGES[query.range]
        entry = self._cache.get(key)
        if entry is not None and self.clock() - entry.stored_at <= spec.ttl_seconds:
            return entry.response.model_copy(update={"cached": True}, deep=True)
        inflight = self._inflight.get(key)
        if inflight is not None:
            return (await asyncio.shield(inflight)).model_copy(deep=True)
        future: asyncio.Future[HistoryResponse] = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            response = await self._fetch(reader, query, spec)
        except HomeAssistantUnavailable:
            self.last_failure_at = self.now()
            fallback = self._stale_fallback(key, spec)
            self.last_outcome = "failed" if fallback is None else "stale_fallback"
            if fallback is None:
                error = HistoryUnavailable("Telemetry history is unavailable")
                future.set_exception(error)
                future.exception()  # mark retrieved for waiters that never arrive
                raise error from None
            future.set_result(fallback)
            return fallback.model_copy(deep=True)
        except BaseException as exc:
            if isinstance(exc, Exception):
                self.last_failure_at = self.now()
                self.last_outcome = "failed"
            future.set_exception(HistoryUnavailable("Telemetry history is unavailable"))
            future.exception()
            if isinstance(exc, Exception):
                raise HistoryUnavailable("Telemetry history is unavailable") from None
            raise
        finally:
            self._inflight.pop(key, None)
        self._cache[key] = _CacheEntry(response=response, stored_at=self.clock())
        self.last_success_at = self.now()
        self.last_outcome = "ok"
        future.set_result(response)
        return response.model_copy(deep=True)

    def _stale_fallback(self, key: tuple[str, str, str], spec: RangeSpec) -> HistoryResponse | None:
        entry = self._cache.get(key)
        if entry is None or self.clock() - entry.stored_at > spec.ttl_seconds * STALE_FALLBACK_FACTOR:
            return None
        return entry.response.model_copy(update={"cached": True, "freshness": "stale"}, deep=True)

    async def _fetch(
        self, reader: HistoryReader, query: HistoryQuery, spec: RangeSpec
    ) -> HistoryResponse:
        metric_entity, online_entity = HISTORY_MAP[(query.node, query.metric)]
        now = self.now()
        window_start = window_for(now, spec)
        self.fetches += 1
        raw = await reader.get_history([metric_entity, online_entity], window_start, now)
        by_entity: dict[str, list[dict[str, Any]]] = {}
        for series in raw:
            if series and isinstance(series[0], dict) and isinstance(series[0].get("entity_id"), str):
                by_entity[series[0]["entity_id"]] = series
        observations = normalize_series(by_entity.get(metric_entity, []), window_start)
        offline = offline_intervals(by_entity.get(online_entity, []), now)
        points, gaps = bucketize(
            observations, offline, window_start=window_start, now=now, spec=spec
        )
        freshness = live_freshness(self.snapshot(), query.node, query.metric)
        if freshness is None:
            freshness = _fallback_freshness(observations, now)
        return HistoryResponse(
            node=query.node,
            metric=query.metric,
            unit=UNITS[query.metric],
            range=query.range,
            bucket_seconds=spec.bucket_seconds,
            points=points,
            gaps=gaps,
            freshness=freshness,
            generated_at=now,
            cached=False,
        )
