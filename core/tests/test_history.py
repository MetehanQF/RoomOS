import asyncio
import inspect
import json
import math
import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app import history as history_module
from app.config import Settings
from app.ha_client import HomeAssistantClient, HomeAssistantUnavailable
from app.history import (
    HISTORY_MAP,
    RANGES,
    HistoryQuery,
    HistoryService,
    HistoryUnavailable,
    bucketize,
    normalize_series,
    offline_intervals,
    window_for,
)
from tests.test_auth import auth_client, pair  # noqa: F401  (fixture reuse)

NOW = datetime(2026, 10, 6, 12, 0, 30, tzinfo=UTC)


def point(state, at, entity_id=None):
    value = {"state": state, "last_changed": at.isoformat()}
    if entity_id is not None:
        value["entity_id"] = entity_id
    return value


class Reader:
    def __init__(self, series=None, error=None, delay=0.0):
        self.series = series
        self.error = error
        self.delay = delay
        self.calls = 0

    async def get_history(self, entity_ids, start, end):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        if self.series is not None:
            return self.series(entity_ids, start, end)
        return [
            [point("50", start, entity_ids[0]), point("60", end - timedelta(seconds=10))],
            [point("on", start, entity_ids[1])],
        ]


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


def service(clock=None):
    return HistoryService(clock=clock or Clock(), now=lambda: NOW)


# --- mapping / contract ---------------------------------------------------------------


def test_mapping_is_server_owned_and_limited_to_v1():
    assert set(HISTORY_MAP) == {
        (node, metric) for node in ("primary", "secondary") for metric in ("cpu", "ram", "temperature")
    }
    assert HISTORY_MAP[("primary", "temperature")] == ("sensor.primary_temperature", "binary_sensor.primary_online")
    assert HISTORY_MAP[("secondary", "cpu")] == ("sensor.secondary_cpu", "binary_sensor.secondary_online")


@pytest.mark.parametrize(("name", "spec_buckets", "seconds"), [("1h", 60, 60), ("6h", 120, 180), ("24h", 144, 600)])
def test_range_bounds(name, spec_buckets, seconds):
    assert (RANGES[name].buckets, RANGES[name].bucket_seconds) == (spec_buckets, seconds)
    response = asyncio.run(service().get(Reader(), HistoryQuery(node="primary", metric="cpu", range=name)))
    assert len(response.points) == spec_buckets
    assert response.bucket_seconds == seconds
    assert response.points[-1].ts <= NOW < response.points[-1].ts + timedelta(seconds=seconds)
    assert NOW - response.points[0].ts <= timedelta(seconds=spec_buckets * seconds)


def test_history_module_has_no_write_path():
    source = inspect.getsource(history_module)
    assert "call_service" not in source
    assert "post(" not in source.lower()


# --- normalization --------------------------------------------------------------------


def test_numeric_and_invalid_values_normalize():
    start = NOW - timedelta(minutes=10)
    raw = [
        point("12.5", start),
        point("unknown", start + timedelta(minutes=1)),
        point("unavailable", start + timedelta(minutes=2)),
        point("UNKNOWN", start + timedelta(minutes=3)),
        point("nan", start + timedelta(minutes=4)),
        point("inf", start + timedelta(minutes=5)),
        point("-inf", start + timedelta(minutes=6)),
        point("3d 16h", start + timedelta(minutes=7)),
        point(None, start + timedelta(minutes=8)),
        point("7", start + timedelta(minutes=9)),
        {"state": "1", "last_changed": "not-a-time"},
        "garbage",
    ]
    observations = normalize_series(raw, start)
    assert [o.value for o in observations] == [12.5, None, None, None, None, None, None, None, None, 7.0]
    assert all(o.value is None or math.isfinite(o.value) for o in observations)


def test_duplicate_timestamp_last_value_wins():
    start = NOW - timedelta(minutes=10)
    at = start + timedelta(minutes=2)
    observations = normalize_series([point("1", at), point("2", at), point("3", at)], start)
    assert [(o.ts, o.value) for o in observations] == [(at, 3.0)]


def test_carried_in_first_point_is_flagged_and_not_fresh_evidence():
    start = NOW - timedelta(hours=1)
    observations = normalize_series([point("40", start), point("41", start + timedelta(minutes=5))], start)
    assert [o.carried_in for o in observations] == [True, False]
    only_carried = normalize_series([point("40", start)], start)
    assert history_module._fallback_freshness(only_carried, NOW) == "unknown"
    recent = normalize_series([point("40", start), point("41", NOW - timedelta(seconds=30))], start)
    assert history_module._fallback_freshness(recent, NOW) == "fresh"
    old = normalize_series([point("40", start), point("41", NOW - timedelta(minutes=30))], start)
    assert history_module._fallback_freshness(old, NOW) == "stale"


# --- bucketing ------------------------------------------------------------------------


def test_bucket_min_avg_max_and_step_hold():
    spec = RANGES["1h"]
    start = window_for(NOW, spec)
    raw = [
        point("50", start),
        point("70", start + timedelta(seconds=20)),
        point("90", start + timedelta(seconds=40)),
    ]
    points, gaps = bucketize(normalize_series(raw, start), [], window_start=start, now=NOW, spec=spec)
    first = points[0]
    assert (first.min, first.max, first.avg, first.samples) == (50.0, 90.0, 70.0, 3)
    # HA records changes only: the last value holds through later buckets.
    assert all(p.avg == 90.0 and p.samples == 1 for p in points[1:])
    assert gaps == []


def test_spike_is_preserved_in_max():
    spec = RANGES["24h"]
    start = window_for(NOW, spec)
    raw = [point("45", start)]
    for minute in range(1, 10):
        raw.append(point("99" if minute == 5 else "45", start + timedelta(minutes=minute)))
    points, _ = bucketize(normalize_series(raw, start), [], window_start=start, now=NOW, spec=spec)
    assert points[0].max == 99.0
    assert points[0].min == 45.0
    assert points[0].avg < 60


def test_empty_bucket_and_unavailable_gap():
    spec = RANGES["1h"]
    start = window_for(NOW, spec)
    raw = [
        point("10", start + timedelta(minutes=5)),
        point("unavailable", start + timedelta(minutes=10)),
        point("20", start + timedelta(minutes=12)),
    ]
    points, gaps = bucketize(normalize_series(raw, start), [], window_start=start, now=NOW, spec=spec)
    assert points[0].avg is None and points[0].min is None and points[0].max is None and points[0].samples == 0
    assert points[5].avg == 10.0
    assert points[10].samples == 0 and points[11].samples == 0
    assert points[12].avg == 20.0
    assert [(g.reason, g.start, g.end) for g in gaps] == [
        ("no_data", start, start + timedelta(minutes=5)),
        ("unavailable", start + timedelta(minutes=10), start + timedelta(minutes=12)),
    ]


def test_offline_window_turns_misleading_zero_into_gap():
    spec = RANGES["1h"]
    start = window_for(NOW, spec)
    metric = [point("12", start), point("0", start + timedelta(minutes=20)), point("8", start + timedelta(minutes=30))]
    online = [point("on", start), point("off", start + timedelta(minutes=20)), point("on", start + timedelta(minutes=30))]
    offline = offline_intervals(online, NOW)
    assert offline == [(start + timedelta(minutes=20), start + timedelta(minutes=30))]
    points, gaps = bucketize(normalize_series(metric, start), offline, window_start=start, now=NOW, spec=spec)
    assert all(p.samples == 0 for p in points[20:30])
    assert all(p.avg != 0 for p in points if p.avg is not None)
    assert [(g.reason, g.start, g.end) for g in gaps] == [
        ("offline", start + timedelta(minutes=20), start + timedelta(minutes=30))
    ]


def test_response_series_matching_uses_entity_ids_not_order():
    def reversed_series(entity_ids, start, end):
        return [
            [point("on", start, entity_ids[1])],
            [point("33.5", start, entity_ids[0])],
        ]

    response = asyncio.run(
        service().get(Reader(series=reversed_series), HistoryQuery(node="secondary", metric="ram", range="1h"))
    )
    assert response.points[-1].avg == 33.5
    assert response.unit == "%"


# --- cache / single-flight / failure --------------------------------------------------


def test_cache_hit_and_expiration():
    clock = Clock()
    reader = Reader()
    svc = service(clock)
    query = HistoryQuery(node="primary", metric="temperature", range="1h")
    first = asyncio.run(svc.get(reader, query))
    second = asyncio.run(svc.get(reader, query))
    assert (first.cached, second.cached, reader.calls) == (False, True, 1)
    clock.value += 30.0
    assert asyncio.run(svc.get(reader, query)).cached is True
    clock.value += 0.5
    assert asyncio.run(svc.get(reader, query)).cached is False
    assert reader.calls == 2


def test_cache_keys_are_bounded_to_eighteen():
    svc = service()
    reader = Reader()
    for node in ("primary", "secondary"):
        for metric in ("cpu", "ram", "temperature"):
            for name in ("1h", "6h", "24h"):
                asyncio.run(svc.get(reader, HistoryQuery(node=node, metric=metric, range=name)))
    assert len(svc._cache) == 18


def test_stale_fallback_within_two_ttl_and_expiry():
    clock = Clock()
    svc = service(clock)
    query = HistoryQuery(node="primary", metric="cpu", range="6h")
    asyncio.run(svc.get(Reader(), query))
    failing = Reader(error=HomeAssistantUnavailable("boom"))
    clock.value += 150.0  # TTL 90 expired, within 180
    stale = asyncio.run(svc.get(failing, query))
    assert stale.cached is True and stale.freshness == "stale"
    clock.value += 31.0  # 181s > 2 x TTL
    with pytest.raises(HistoryUnavailable):
        asyncio.run(svc.get(failing, query))


def test_failure_without_cache_is_sanitized():
    svc = service()
    with pytest.raises(HistoryUnavailable) as raised:
        asyncio.run(svc.get(Reader(error=HomeAssistantUnavailable("secret detail")), HistoryQuery(node="primary", metric="cpu", range="1h")))
    assert "secret" not in str(raised.value)
    with pytest.raises(HistoryUnavailable):
        asyncio.run(svc.get(Reader(error=ValueError("boom")), HistoryQuery(node="primary", metric="cpu", range="1h")))


def test_single_flight_shares_one_fetch():
    reader = Reader(delay=0.05)
    svc = service()
    query = HistoryQuery(node="secondary", metric="temperature", range="24h")

    async def burst():
        return await asyncio.gather(*(svc.get(reader, query) for _ in range(8)))

    results = asyncio.run(burst())
    assert reader.calls == 1
    assert len({r.model_dump_json() for r in results}) == 1


def test_single_flight_failure_propagates_to_waiters():
    reader = Reader(delay=0.05, error=HomeAssistantUnavailable("down"))
    svc = service()
    query = HistoryQuery(node="secondary", metric="temperature", range="24h")

    async def burst():
        return await asyncio.gather(*(svc.get(reader, query) for _ in range(4)), return_exceptions=True)

    results = asyncio.run(burst())
    assert reader.calls == 1
    assert all(isinstance(r, HistoryUnavailable) for r in results)


def test_live_snapshot_freshness_is_preferred(client):
    from app.models import SystemMetric, SystemNode, SystemResponse, DnsClusterStatus, DnsResolverStatus

    def node(node_id, freshness):
        return SystemNode(
            id=node_id, name=node_id, online=True,
            metrics={f"{node_id}_cpu": SystemMetric(id=f"{node_id}_cpu", value=1, available=True, stale=freshness == "stale", freshness=freshness)},
        )

    snapshot = SystemResponse(
        primary=node("primary", "stale"), secondary=node("secondary", "fresh"), services=[],
        dns_cluster=DnsClusterStatus(
            status="unknown",
            primary=DnsResolverStatus(id="primary", role="primary", status="unknown"),
            secondary=DnsResolverStatus(id="secondary", role="secondary", status="unknown"),
        ),
    )
    svc = HistoryService(clock=Clock(), now=lambda: NOW, snapshot=lambda: snapshot)
    assert asyncio.run(svc.get(Reader(), HistoryQuery(node="primary", metric="cpu", range="1h"))).freshness == "stale"
    assert asyncio.run(svc.get(Reader(), HistoryQuery(node="secondary", metric="cpu", range="1h"))).freshness == "fresh"


# --- HA adapter -----------------------------------------------------------------------


def ha_client(handler):
    client = HomeAssistantClient(Settings(HA_URL="http://ha.test:8123", HA_TOKEN="test-token"))  # privacy-scan:allow fake test credential
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def test_adapter_uses_bounded_get_with_minimal_params():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=[[{"entity_id": "sensor.primary_cpu", "state": "1", "last_changed": NOW.isoformat()}]])

    client = ha_client(handler)
    data = asyncio.run(client.get_history(["sensor.primary_cpu", "binary_sensor.primary_online"], NOW - timedelta(hours=1), NOW))
    assert data[0][0]["state"] == "1"
    request = seen[0]
    assert request.method == "GET"
    assert request.url.path.startswith("/api/history/period/")
    assert request.url.params["filter_entity_id"] == "sensor.primary_cpu,binary_sensor.primary_online"
    assert "minimal_response" in request.url.params and "no_attributes" in request.url.params


def test_adapter_rejects_oversized_response():
    big = json.dumps([[{"state": "1", "last_changed": NOW.isoformat(), "pad": "x" * 2048}] * 1200])

    def handler(request):
        return httpx.Response(200, content=big.encode(), headers={"content-type": "application/json"})

    with pytest.raises(HomeAssistantUnavailable) as raised:
        asyncio.run(ha_client(handler).get_history(["sensor.primary_cpu"], NOW - timedelta(hours=24), NOW))
    assert "too large" in str(raised.value)
    with pytest.raises(HomeAssistantUnavailable):
        asyncio.run(ha_client(handler).get_history(["sensor.primary_cpu"], NOW, NOW, max_bytes=1024))


def test_adapter_timeout_is_bounded():
    async def handler(request):
        await asyncio.sleep(1.0)
        return httpx.Response(200, json=[])

    started = asyncio.run(_timed(ha_client(handler), 0.1))
    assert started < 0.6


async def _timed(client, timeout):
    loop = asyncio.get_running_loop()
    begin = loop.time()
    with pytest.raises(HomeAssistantUnavailable):
        await client.get_history(["sensor.primary_cpu"], NOW - timedelta(hours=1), NOW, timeout_seconds=timeout)
    return loop.time() - begin


@pytest.mark.parametrize("body", [b"not json", b'{"a": 1}', b"[1, 2]"])
def test_adapter_malformed_or_error_responses_are_sanitized(body):
    with pytest.raises(HomeAssistantUnavailable) as raised:
        asyncio.run(ha_client(lambda request: httpx.Response(200, content=body)).get_history(["sensor.primary_cpu"], NOW, NOW))
    assert "test-token" not in str(raised.value)
    with pytest.raises(HomeAssistantUnavailable):
        asyncio.run(ha_client(lambda request: httpx.Response(500, text="Traceback secret")).get_history(["sensor.primary_cpu"], NOW, NOW))


# --- public endpoint ------------------------------------------------------------------


def test_endpoint_returns_bounded_logical_contract(client, fake_ha):
    response = client.get("/api/v1/system/history", params={"node": "primary", "metric": "temperature", "range": "6h"})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"node", "metric", "unit", "range", "bucket_seconds", "points", "gaps", "source", "freshness", "generated_at", "cached"}
    assert (body["node"], body["metric"], body["unit"], body["range"], body["bucket_seconds"]) == ("primary", "temperature", "°C", "6h", 180)
    assert len(body["points"]) == 120
    assert body["source"] == "home_assistant"
    text = response.text
    assert "sensor." not in text and "binary_sensor" not in text and "entity" not in text
    assert all(math.isfinite(p["avg"]) for p in body["points"] if p["avg"] is not None)
    assert fake_ha.history_requests[0][0] == ["sensor.primary_temperature", "binary_sensor.primary_online"]
    assert client.get("/api/v1/system/history", params={"node": "primary", "metric": "temperature", "range": "6h"}).json()["cached"] is True
    assert fake_ha.calls == []


@pytest.mark.parametrize(
    "params",
    [
        {"node": "nas", "metric": "cpu", "range": "1h"},
        {"node": "primary", "metric": "disk", "range": "1h"},
        {"node": "primary", "metric": "uptime", "range": "1h"},
        {"node": "primary", "metric": "cpu", "range": "7d"},
        {"node": "primary", "metric": "cpu"},
        {"node": "primary", "metric": "cpu", "range": "1h", "entity_id": "sensor.primary_cpu"},
        {"node": "primary", "metric": "cpu", "range": "1h", "start": "2026-01-01T00:00:00Z"},
        {"node": "primary", "metric": "cpu", "range": "1h", "end": "2026-01-01T00:00:00Z"},
        {"node": "sensor.primary_cpu", "metric": "cpu", "range": "1h"},
    ],
)
def test_endpoint_rejects_invalid_or_unbounded_queries(client, fake_ha, params):
    assert client.get("/api/v1/system/history", params=params).status_code == 422
    assert getattr(fake_ha, "history_requests", []) == []


def test_endpoint_failure_is_sanitized_503(client, fake_ha):
    fake_ha.history_error = HomeAssistantUnavailable("internal http://192.0.2.20 detail")
    response = client.get("/api/v1/system/history", params={"node": "secondary", "metric": "ram", "range": "24h"})
    assert response.status_code == 503
    assert response.json() == {"detail": "Telemetry history is unavailable"}
    assert fake_ha.calls == []


def test_endpoint_requires_system_read(auth_client):  # noqa: F811
    client, fake_ha, store, settings = auth_client
    device_id = pair(client, store).json()["device"]["id"]
    params = {"node": "primary", "metric": "cpu", "range": "1h"}
    with sqlite3.connect(settings.auth_database_path) as db:
        db.execute("UPDATE devices SET scopes=? WHERE id=?", (json.dumps(["lights:read"]), device_id))
    assert client.get("/api/v1/system/history", params=params).status_code == 403
    with sqlite3.connect(settings.auth_database_path) as db:
        db.execute("UPDATE devices SET scopes=? WHERE id=?", (json.dumps(["system:read"]), device_id))
    assert client.get("/api/v1/system/history", params=params).status_code == 200
    client.cookies.clear()
    assert client.get("/api/v1/system/history", params=params).status_code == 401
    assert fake_ha.calls == []
