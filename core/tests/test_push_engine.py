import asyncio
import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from app.alerts import AlertEngine, AlertObservation, AlertRecord, AlertRule
from app.push import (
    COOLDOWN_SECONDS, MAX_ATTEMPTS, OUTBOX_LIMIT, RATE_LIMIT_PER_WINDOW, PushEngine,
    PushMessage, PushPreferences, PushPreferencesUpdate, QuietHours, decide, render, render_body,
)
from app.push_providers import client_payload
from zoneinfo import ZoneInfo

T0 = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)  # 12:00 Istanbul
IST = ZoneInfo("Europe/Istanbul")
FULL = (frozenset({"activity:read"}), frozenset({"android", "web"}))


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class FakeProvider:
    def __init__(self, kind, results=None, delay=0.0):
        self.kind = kind
        self.results = list(results or [])
        self.sent = []
        self.delay = delay

    async def send(self, message, device, ttl):
        self.sent.append((message.id, device.device_id, ttl, message.transition))
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.results.pop(0) if self.results else ("accepted", None)


def alert(id="resource.workstation.disk_usage", severity="warning", started=T0, status="active",
          type="resource.high", source="workstation", message="disk usage is 87%; sustained high usage requires attention.",
          title="Workstation disk usage is high"):
    return AlertRecord(id=id, type=type, severity=severity, title=title, message=message, source=source,
                       started_at=started, updated_at=started, status=status)


async def engine_with(tmp_path, *, providers=None, access=lambda d: FULL, clock=None, alerts=None,
                      physical=lambda d: d, devices=(("dev-a", "fcm"),), enabled=True, **prefs):
    alerts = alerts if alerts is not None else {}
    engine = PushEngine(str(tmp_path / "push-registry.json"), str(tmp_path / "push-outbox.json"),
                        providers=providers if providers is not None else {"fcm": FakeProvider("fcm"), "webpush": FakeProvider("webpush")},
                        access=access, physical_id=physical, alert_lookup=lambda i: alerts.get(i),
                        clock=clock or Clock(), provider_timeout_seconds=0.2)
    for device_id, channel in devices:
        if channel == "fcm":
            await engine.bind_fcm(device_id=device_id, display_name=device_id, target_ref=f"android-{device_id}",
                                  token_fingerprint="a" * 64)
        else:
            await engine.subscribe_webpush(device_id=device_id, display_name=device_id,
                                           endpoint=f"https://fcm.googleapis.com/fcm/send/{device_id}",
                                           host="fcm.googleapis.com", p256dh="x" * 87, auth="y" * 22)
        await engine.update_preferences(device_id, device_id, PushPreferencesUpdate(enabled=enabled, **prefs))
    return engine


def statuses(engine):
    return [(m.device_id, m.transition, m.status, m.reason) for m in engine.outbox.messages]


@pytest.mark.asyncio
async def test_500_duplicate_observations_create_one_route_per_device(tmp_path):
    a = alert()
    engine = await engine_with(tmp_path, alerts={a.id: a}, devices=(("dev-a", "fcm"), ("dev-b", "webpush")))
    for _ in range(500):
        await engine.handle_lifecycle("started", a)
    assert sorted((m.device_id, m.channel) for m in engine.outbox.messages) == [("dev-a", "fcm"), ("dev-b", "webpush")]
    assert await engine.dispatch_pending() == 2
    assert {m.status for m in engine.outbox.messages} == {"accepted"}
    assert engine.summary().external_deliveries == 2


@pytest.mark.asyncio
async def test_disabled_device_and_info_default_are_skipped_not_sent(tmp_path):
    info = alert(id="health.x", severity="info", type="health.degraded", source="roomos", title="X needs attention", message="m")
    engine = await engine_with(tmp_path, alerts={info.id: info}, devices=(("dev-a", "fcm"),))
    await engine.handle_lifecycle("started", info)
    assert statuses(engine) == [("dev-a", "started", "skipped", "severity_filtered")]
    await engine.update_preferences("dev-a", "dev-a", PushPreferencesUpdate(enabled=False))
    w = alert()
    await engine.handle_lifecycle("started", w)
    assert statuses(engine)[-1] == ("dev-a", "started", "skipped", "disabled")
    assert await engine.dispatch_pending() == 0


@pytest.mark.asyncio
async def test_temporary_failures_back_off_exponentially_then_fail_bounded(tmp_path):
    clock = Clock()
    a = alert(severity="critical", type="node.offline", id="node.primary.offline", source="primary", title="Primary is offline", message="m")
    provider = FakeProvider("fcm", [("temporary_failure", "UNAVAILABLE")] * 20)
    engine = await engine_with(tmp_path, providers={"fcm": provider}, clock=clock, alerts={a.id: a})
    await engine.handle_lifecycle("started", a)
    delays = []
    for _ in range(MAX_ATTEMPTS):
        await engine.dispatch_pending()
        message = engine.outbox.messages[0]
        if message.next_attempt_at:
            delays.append((message.next_attempt_at - clock.now).total_seconds())
            clock.now = message.next_attempt_at
    assert delays[:4] == [30, 60, 120, 240] and max(delays) <= 1800
    message = engine.outbox.messages[0]
    assert message.status == "failed" and message.attempt_count == MAX_ATTEMPTS
    assert len(message.attempts) <= 8 and len(provider.sent) == MAX_ATTEMPTS
    assert await engine.dispatch_pending() == 0  # no unbounded retry


@pytest.mark.asyncio
async def test_retry_never_runs_past_ttl(tmp_path):
    clock = Clock()
    a = alert()
    engine = await engine_with(tmp_path, providers={"fcm": FakeProvider("fcm", [("temporary_failure", "x")] * 50)},
                               clock=clock, alerts={a.id: a})
    await engine.handle_lifecycle("started", a)
    message = engine.outbox.messages[0]
    message.expires_at = clock.now + timedelta(seconds=45)
    await engine.dispatch_pending()      # attempt 1 -> retry at +30 s
    clock.advance(30)
    await engine.dispatch_pending()      # attempt 2 -> retry at +60 s would pass TTL
    assert engine.outbox.messages[0].status == "expired"


@pytest.mark.asyncio
async def test_invalid_target_disables_only_that_channel_and_wipes_key_material(tmp_path):
    a = alert()
    b = alert(id="resource.primary.disk", source="primary", title="Primary disk usage is high")
    web = FakeProvider("webpush", [("invalid_target", "http_410")])
    engine = await engine_with(tmp_path, providers={"fcm": FakeProvider("fcm"), "webpush": web},
                               alerts={a.id: a, b.id: b}, devices=(("dev-a", "fcm"), ("dev-b", "webpush")))
    await engine.handle_lifecycle("started", a)
    await engine.dispatch_pending()
    device = engine.device("dev-b")
    assert device.webpush.status == "invalid"
    assert device.webpush.endpoint is None and device.webpush.p256dh is None and device.webpush.auth is None
    await engine.handle_lifecycle("started", b)  # invalid channel gets no new routes
    assert [m.device_id for m in engine.outbox.messages if m.alert_id == b.id] == ["dev-a"]
    assert engine.device("dev-a").fcm.status == "active"


@pytest.mark.asyncio
async def test_one_hanging_device_does_not_block_another(tmp_path):
    a = alert()
    slow = FakeProvider("webpush", delay=5)
    fast = FakeProvider("fcm")
    engine = await engine_with(tmp_path, providers={"fcm": fast, "webpush": slow}, alerts={a.id: a},
                               devices=(("dev-a", "fcm"), ("dev-b", "webpush")))
    await engine.handle_lifecycle("started", a)
    started = asyncio.get_running_loop().time()
    await engine.dispatch_pending()
    assert asyncio.get_running_loop().time() - started < 1.5
    by_device = {m.device_id: m for m in engine.outbox.messages}
    assert by_device["dev-a"].status == "accepted"
    assert by_device["dev-b"].status == "pending" and by_device["dev-b"].attempts[-1].code == "timeout"


@pytest.mark.asyncio
async def test_recovery_only_after_accepted_opening_and_per_preference(tmp_path):
    clock = Clock()
    a = alert(severity="critical", type="service.offline", id="service.home_assistant.offline",
              source="home_assistant", title="Home Assistant is offline", message="m")
    alerts = {a.id: a}
    engine = await engine_with(tmp_path, clock=clock, alerts=alerts)
    await engine.handle_lifecycle("started", a)
    await engine.dispatch_pending()
    alerts[a.id] = a.model_copy(update={"status": "resolved"})
    await engine.handle_lifecycle("resolved", alerts[a.id])
    await engine.dispatch_pending()
    sent = [m for m in engine.outbox.messages if m.transition == "resolved"][0]
    assert sent.status == "accepted"
    assert (sent.title, sent.body) == ("RoomOS — Düzeldi", "Home Assistant bağlantısı yeniden sağlandı.")

    w = alert(started=T0 + timedelta(minutes=1))  # warning + default recovery=critical
    alerts[w.id] = w
    await engine.handle_lifecycle("started", w)
    await engine.dispatch_pending()
    alerts[w.id] = w.model_copy(update={"status": "resolved"})
    await engine.handle_lifecycle("resolved", alerts[w.id])
    assert statuses(engine)[-1] == ("dev-a", "resolved", "skipped", "recovery_disabled")


@pytest.mark.asyncio
async def test_resolved_before_send_supersedes_opening_and_sends_no_recovery(tmp_path):
    a = alert(severity="critical", type="node.offline", id="node.secondary.offline", source="secondary", title="Secondary is offline", message="m")
    alerts = {a.id: a}
    engine = await engine_with(tmp_path, alerts=alerts)
    await engine.handle_lifecycle("started", a)
    alerts[a.id] = a.model_copy(update={"status": "resolved"})
    await engine.handle_lifecycle("resolved", alerts[a.id])
    await engine.dispatch_pending()
    assert statuses(engine) == [("dev-a", "started", "superseded", "lifecycle_changed"),
                                ("dev-a", "resolved", "skipped", "opening_not_delivered")]


@pytest.mark.asyncio
async def test_quiet_hours_defer_warning_to_window_end_and_critical_bypasses(tmp_path):
    night = datetime(2026, 10, 16, 23, 30, tzinfo=UTC)  # 02:30 Istanbul
    clock = Clock(night)
    w = alert(started=night)
    c = alert(id="node.primary.offline", severity="critical", type="node.offline", source="primary", started=night,
              title="Primary is offline", message="m")
    alerts = {w.id: w, c.id: c}
    engine = await engine_with(tmp_path, clock=clock, alerts=alerts, quiet_hours=QuietHours(enabled=True))
    await engine.handle_lifecycle("started", w)
    await engine.handle_lifecycle("started", c)
    await engine.dispatch_pending()
    by_alert = {m.alert_id: m for m in engine.outbox.messages}
    assert by_alert[c.id].status == "accepted"
    assert by_alert[w.id].status == "deferred"
    assert by_alert[w.id].next_attempt_at == datetime(2026, 10, 17, 5, 0, tzinfo=UTC)  # 08:00 Istanbul
    clock.now = datetime(2026, 10, 17, 5, 0, tzinfo=UTC)
    await engine.dispatch_pending()
    assert by_alert[w.id].id in {m.id for m in engine.outbox.messages if m.status == "accepted"}


@pytest.mark.asyncio
async def test_deferred_warning_resolved_during_quiet_hours_is_never_sent(tmp_path):
    night = datetime(2026, 10, 16, 22, 0, tzinfo=UTC)  # 01:00 Istanbul
    clock = Clock(night)
    w = alert(started=night)
    alerts = {w.id: w}
    provider = FakeProvider("fcm")
    engine = await engine_with(tmp_path, providers={"fcm": provider}, clock=clock, alerts=alerts,
                               quiet_hours=QuietHours(enabled=True))
    await engine.handle_lifecycle("started", w)
    alerts[w.id] = w.model_copy(update={"status": "resolved"})
    await engine.handle_lifecycle("resolved", alerts[w.id])
    clock.now = datetime(2026, 10, 17, 5, 1, tzinfo=UTC)
    await engine.dispatch_pending()
    assert provider.sent == []
    assert [m.status for m in engine.outbox.messages] == ["superseded", "skipped"]


def test_quiet_hours_and_critical_toggle_are_istanbul_wall_clock():
    prefs = PushPreferences(enabled=True, quiet_hours=QuietHours(enabled=True, start="22:30", end="07:15"))
    at = lambda h, m: datetime(2026, 10, 16, h, m, tzinfo=IST).astimezone(UTC)
    assert decide(prefs, transition="started", severity="warning", now=at(22, 29), zone=IST) == ("send", None)
    verdict, until = decide(prefs, transition="started", severity="warning", now=at(23, 0), zone=IST)
    assert verdict == "defer" and until == datetime(2026, 10, 17, 7, 15, tzinfo=IST).astimezone(UTC)
    verdict, until = decide(prefs, transition="started", severity="warning", now=at(3, 0), zone=IST)
    assert until == datetime(2026, 10, 16, 7, 15, tzinfo=IST).astimezone(UTC)
    assert decide(prefs, transition="started", severity="critical", now=at(3, 0), zone=IST) == ("send", None)
    strict = prefs.model_copy(update={"quiet_hours": prefs.quiet_hours.model_copy(update={"allow_critical": False})})
    assert decide(strict, transition="started", severity="critical", now=at(3, 0), zone=IST)[0] == "defer"
    assert decide(prefs, transition="resolved", severity="critical", now=at(3, 0), zone=IST) == ("skip", "quiet_hours")


@pytest.mark.asyncio
async def test_snooze_suppresses_non_critical(tmp_path):
    w = alert()
    engine = await engine_with(tmp_path, alerts={w.id: w}, snooze_minutes=60)
    await engine.handle_lifecycle("started", w)
    assert statuses(engine) == [("dev-a", "started", "skipped", "snoozed")]


@pytest.mark.asyncio
async def test_alarm_storm_is_rate_limited_and_aggregated(tmp_path):
    clock = Clock()
    alerts = {}
    provider = FakeProvider("fcm")
    engine = await engine_with(tmp_path, providers={"fcm": provider}, clock=clock, alerts=alerts)
    for index in range(15):
        item = alert(id=f"resource.n{index}.disk", source=f"n{index}", title=f"N{index} disk usage is high")
        alerts[item.id] = item
        await engine.handle_lifecycle("started", item)
        await engine.dispatch_pending()
    accepted = [m for m in engine.outbox.messages if m.status == "accepted"]
    aggregated = [m for m in engine.outbox.messages if m.status == "aggregated"]
    summaries = [m for m in engine.outbox.messages if m.transition == "summary"]
    assert len(accepted) == RATE_LIMIT_PER_WINDOW and len(aggregated) == 15 - RATE_LIMIT_PER_WINDOW
    assert len(summaries) == 1 and summaries[0].aggregated_count == len(aggregated)
    assert summaries[0].body == f"{len(aggregated)} yeni RoomOS bildirimi. Ayrıntılar için dokunun."
    clock.now = summaries[0].next_attempt_at
    await engine.dispatch_pending()
    assert summaries[0].status == "accepted" and len(provider.sent) == RATE_LIMIT_PER_WINDOW + 1


@pytest.mark.asyncio
async def test_queue_survives_restart_and_in_flight_routes_are_retried(tmp_path):
    a = alert()
    engine = await engine_with(tmp_path, providers={"fcm": FakeProvider("fcm")}, alerts={a.id: a})
    await engine.handle_lifecycle("started", a)
    engine.outbox.messages[0].in_flight = True  # simulate crash during the provider call
    engine._persist_outbox()
    path = tmp_path / "push-outbox.json"
    assert os.stat(path).st_mode & 0o777 == 0o600
    provider = FakeProvider("fcm")
    restored = PushEngine(str(tmp_path / "push-registry.json"), str(path), providers={"fcm": provider},
                          access=lambda d: FULL, alert_lookup=lambda i: a, clock=Clock())
    assert restored.outbox.messages[0].in_flight is False
    assert restored.device("dev-a").preferences.enabled is True
    assert await restored.dispatch_pending() == 1 and restored.outbox.messages[0].status == "accepted"


@pytest.mark.asyncio
async def test_revoked_or_logged_out_device_receives_nothing(tmp_path):
    a = alert()
    access = {"dev-a": FULL}
    provider = FakeProvider("fcm")
    engine = await engine_with(tmp_path, providers={"fcm": provider}, alerts={a.id: a}, access=lambda d: access.get(d))
    await engine.handle_lifecycle("started", a)
    access.clear()  # revoke / last session logged out
    await engine.dispatch_pending()
    assert provider.sent == [] and statuses(engine) == [("dev-a", "started", "cancelled", "unauthorized")]
    assert engine.device("dev-a").fcm.status == "suspended"


@pytest.mark.asyncio
async def test_scope_or_session_kind_mismatch_blocks_delivery(tmp_path):
    a = alert()
    provider = FakeProvider("fcm")
    engine = await engine_with(tmp_path, providers={"fcm": provider}, alerts={a.id: a},
                               access=lambda d: (frozenset({"activity:read"}), frozenset({"web"})))
    await engine.handle_lifecycle("started", a)
    await engine.dispatch_pending()
    assert provider.sent == []
    engine2 = await engine_with(tmp_path / "b", providers={"fcm": provider}, alerts={a.id: a},
                                access=lambda d: (frozenset({"system:read"}), frozenset({"android"})))
    await engine2.handle_lifecycle("started", a)
    await engine2.dispatch_pending()
    assert provider.sent == []


@pytest.mark.asyncio
async def test_native_and_web_push_on_one_physical_device_do_not_double_notify(tmp_path):
    a = alert()
    engine = await engine_with(tmp_path, alerts={a.id: a}, devices=(("native", "fcm"), ("pwa", "webpush")),
                               physical=lambda d: "phone")
    await engine.handle_lifecycle("started", a)
    assert [(m.device_id, m.channel) for m in engine.outbox.messages] == [("native", "fcm")]


@pytest.mark.asyncio
async def test_cooldown_suppresses_flapping_reopen(tmp_path):
    clock = Clock()
    a = alert()
    alerts = {a.id: a}
    engine = await engine_with(tmp_path, clock=clock, alerts=alerts)
    await engine.handle_lifecycle("started", a)
    await engine.dispatch_pending()
    clock.advance(600)
    reopened = a.model_copy(update={"started_at": clock.now})
    alerts[a.id] = reopened
    await engine.handle_lifecycle("started", reopened)
    assert statuses(engine)[-1] == ("dev-a", "started", "skipped", "cooldown")
    clock.advance(COOLDOWN_SECONDS)
    later = a.model_copy(update={"started_at": clock.now})
    alerts[a.id] = later
    await engine.handle_lifecycle("started", later)
    assert statuses(engine)[-1][2] == "pending"


@pytest.mark.asyncio
async def test_receipts_are_device_bound_and_require_acceptance(tmp_path):
    a = alert()
    engine = await engine_with(tmp_path, alerts={a.id: a}, devices=(("dev-a", "fcm"), ("dev-b", "webpush")))
    await engine.handle_lifecycle("started", a)
    mine = next(m for m in engine.outbox.messages if m.device_id == "dev-a")
    assert await engine.record_receipt("dev-a", mine.id, "displayed") is False  # not accepted yet
    await engine.dispatch_pending()
    assert await engine.record_receipt("dev-b", mine.id, "opened") is False     # someone else's route
    assert await engine.record_receipt("dev-a", mine.id, "opened") is True
    stored = next(m for m in engine.outbox.messages if m.id == mine.id)
    assert stored.status == "opened" and stored.displayed_at and stored.opened_at


@pytest.mark.asyncio
async def test_corrupt_outbox_fails_closed_and_is_not_overwritten(tmp_path):
    (tmp_path / "push-outbox.json").write_text("{not json")
    engine = PushEngine(str(tmp_path / "push-registry.json"), str(tmp_path / "push-outbox.json"),
                        providers={"fcm": FakeProvider("fcm")}, access=lambda d: FULL)
    assert engine.storage_status == "corrupt" and engine.summary().status == "corrupt"
    assert await engine.handle_lifecycle("started", alert()) == 0
    assert await engine.dispatch_pending() == 0
    assert (tmp_path / "push-outbox.json").read_text() == "{not json"


@pytest.mark.asyncio
async def test_outbox_is_bounded_and_evicts_terminal_routes_first(tmp_path):
    engine = await engine_with(tmp_path)
    live = None
    for index in range(OUTBOX_LIMIT + 40):
        item = alert(id=f"resource.n{index}.disk", source=f"n{index}", started=T0 + timedelta(seconds=index))
        await engine.handle_lifecycle("started", item)
        if index == 0:
            live = engine.outbox.messages[0].id
        else:
            engine.outbox.messages[-1].status = "accepted"
    engine._persist_outbox()
    assert len(engine.outbox.messages) <= OUTBOX_LIMIT
    assert live in {m.id for m in engine.outbox.messages}
    stored = json.loads((tmp_path / "push-outbox.json").read_text())
    assert len(stored["messages"]) <= OUTBOX_LIMIT


@pytest.mark.asyncio
async def test_alert_engine_lifecycle_sink_reports_started_escalated_resolved_once(tmp_path):
    seen = []
    async def sink(transition, record):
        seen.append((transition, record.severity, record.status))
    async def broken(_transition, _record):
        raise RuntimeError("isolated")
    engine = AlertEngine(str(tmp_path / "alerts.json"), clock=lambda: T0)
    engine.add_lifecycle_sink(broken)
    engine.add_lifecycle_sink(sink)
    rule = lambda severity: AlertRule(key="resource.primary.disk", severity=severity, type="resource.high",
                                      title="Primary disk usage is high", message=f"disk usage is {90 if severity == 'warning' else 96}%; x",
                                      source="primary", failure_seconds=0, recovery_seconds=0)
    for _ in range(3):
        await engine.evaluate_observations([AlertObservation(state="failed", rule=rule("warning"))])
    await engine.evaluate_observations([AlertObservation(state="failed", rule=rule("critical"))])
    await engine.evaluate_observations([AlertObservation(state="failed", rule=rule("critical"))])
    await engine.evaluate_observations([AlertObservation(state="healthy", rule=rule("critical"))])
    assert seen == [("started", "warning", "active"), ("escalated", "critical", "active"),
                    ("resolved", "critical", "resolved")]


def test_turkish_copy_uses_real_values_and_hides_details_on_lock_screen():
    workstation = alert()
    title, body, public_title, public_body = render(workstation, "started")
    assert (title, body) == ("RoomOS — Uyarı", "Workstation: C: sürücüsü %87 dolu.")
    assert (public_title, public_body) == ("RoomOS — Uyarı", "Ayrıntılar için RoomOS'u açın.")
    ha = alert(id="service.home_assistant.offline", severity="critical", type="service.offline", source="home_assistant",
               title="Home Assistant is offline", message="x")
    assert render(ha, "started")[:2] == ("RoomOS — Kritik", "Home Assistant bağlantısı kesildi.")
    assert render(ha, "resolved")[:2] == ("RoomOS — Düzeldi", "Home Assistant bağlantısı yeniden sağlandı.")
    temp = alert(id="resource.primary.temperature", type="resource.high", source="primary", title="Primary temperature is high",
                 message="temperature is 78.5°C; sustained high usage requires attention.")
    assert render_body(temp, recovered=False) == "Primary sıcaklığı yüksek: 78.5°C."
    assert render_body(alert(id="node.secondary.offline", type="node.offline", source="secondary", title="Secondary is offline", message="m"),
                       recovered=False) == "Secondary çevrimdışı."
    unknown = alert(id="x.y", type="future.kind", source="roomos", title="Something new", message="m")
    assert render_body(unknown, recovered=False) == "Something new"


def test_client_payload_contains_no_secrets_and_respects_lock_screen_preference():
    message = PushMessage(
        id="psh_" + "0" * 32, dedupe_key="0" * 64, device_id="dev", channel="webpush", transition="started",
        alert_id="resource.workstation.disk_usage", alert_started_at=T0, severity="warning",
        title="RoomOS — Uyarı", body="Workstation: C: sürücüsü %87 dolu.", public_title="RoomOS — Uyarı",
        public_body="Ayrıntılar için RoomOS'u açın.", path="/notifications?alert=resource.workstation.disk_usage",
        created_at=T0, expires_at=T0 + timedelta(hours=1),
    )
    hidden = client_payload(message, details=False)
    assert hidden["body"] == "Ayrıntılar için RoomOS'u açın." and "Workstation" not in json.dumps(hidden)
    shown = client_payload(message, details=True)
    assert shown["body"] == "Workstation: C: sürücüsü %87 dolu."
    text = json.dumps(shown)
    for forbidden in ("token", "endpoint", "p256dh", "auth", "192.168", "device_id", "mqtt", "http"):
        assert forbidden not in text.lower()
