"""7I workload-aware resource alerts, profiles and snooze."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from test_auth import auth_client as _auth_client  # noqa: F401

from app.alerts import AlertEngine, AlertRecord, AlertStateFile
from app.resource_policy import TEMPERATURE, UTILIZATION, AlertPolicyStore, valid_temperature
from app.smart_alerts import policy_observation

T0 = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def feed(store, clock, kind, values, *, key="resource.workstation.x", every=60, device="workstation", free_gb=None, policy=None):
    """One observation per `every` seconds; returns the last observation."""
    last = None
    for value in values:
        last = policy_observation(store, key=key, label="Workstation", policy=policy or UTILIZATION[kind], value=value,
                                  source=device, profile_device=device, free_gb=free_gb)
        clock.advance(every)
    return last


def fresh(tmp_path, clock):
    return AlertPolicyStore(str(tmp_path / "alert-policy.json"), clock=clock)


def test_gpu_95_for_20_minutes_is_not_an_alert(tmp_path):
    clock = Clock(); store = fresh(tmp_path, clock)
    assert feed(store, clock, "gpu", [95] * 20).state != "failed"


def test_cpu_90_for_hours_is_not_an_alert_and_never_critical(tmp_path):
    clock = Clock(); store = fresh(tmp_path, clock)
    assert feed(store, clock, "cpu", [92] * 350).state != "failed"   # long sessions are normal use
    obs = feed(store, clock, "cpu", [99] * 11)                       # 6 h sustained
    assert (obs.state, obs.rule.severity) == ("failed", "warning")


@pytest.mark.parametrize("kind,value", [("cpu", 95), ("gpu", 99), ("ram", 99.4)])
def test_utilization_alerts_only_after_six_sustained_hours(tmp_path, kind, value):
    clock = Clock(); store = fresh(tmp_path, clock)
    assert feed(store, clock, kind, [value] * 350).state != "failed"   # 5 h 50 min: still normal use
    obs = feed(store, clock, kind, [value] * 11)
    assert (obs.state, obs.rule.severity) == ("failed", "warning")
    assert "for at least 360 min" in obs.rule.message


def test_ram_exhaustion_spike_is_never_critical(tmp_path):
    clock = Clock(); store = fresh(tmp_path, clock)
    obs = feed(store, clock, "ram", [99.4] * 361)                       # the 2026-10-16 case, held 6 h
    assert (obs.state, obs.rule.severity) == ("failed", "warning")
    assert obs.rule.message.startswith("RAM usage is 99.4%;")


def test_one_normal_minute_restarts_the_six_hour_window(tmp_path):
    clock = Clock(); store = fresh(tmp_path, clock)
    feed(store, clock, "ram", [95] * 300)
    feed(store, clock, "ram", [60])                                     # game closed for a minute
    assert feed(store, clock, "ram", [95] * 300).state != "failed"


def test_six_hours_needs_coverage_not_just_first_and_last_sample(tmp_path):
    clock = Clock(); store = fresh(tmp_path, clock)
    feed(store, clock, "gpu", [96] * 60)        # first hour high
    clock.advance(4 * 3600)                     # four hours with no data at all
    obs = feed(store, clock, "gpu", [96] * 60)  # last hour high: first..last spans 6 h
    assert obs.state != "failed"
    assert store.insights["resource.workstation.x"]["reason"] == "within policy"


def test_sparse_samples_are_never_treated_as_high(tmp_path):
    clock = Clock(); store = fresh(tmp_path, clock)
    obs = feed(store, clock, "cpu", [99] * 13, every=600)  # one sample per 10 min for 2 h
    assert obs.state != "failed"


def test_gaming_and_development_pause_utilization_only_and_expire_to_normal(tmp_path):
    clock = Clock(); store = fresh(tmp_path, clock)
    store.set_profile("workstation", "gaming", hours=8, actor="dev")
    obs = feed(store, clock, "gpu", [99] * 370)
    assert obs.state == "healthy"                                        # paused while gaming
    assert store.insights["resource.workstation.x"]["reason"] == "paused by the gaming profile"
    primary = AlertPolicyStore(str(tmp_path / "p.json"), clock=clock)
    primary.set_profile("primary", "gaming", hours=None, actor="dev")
    obs = feed(primary, clock, None, [86] * 3, key="resource.primary.temperature", device="primary", policy=TEMPERATURE["primary"])
    assert (obs.state, obs.rule.severity) == ("failed", "critical")    # temperature ignores the profile
    disk = feed(primary, clock, "disk", [90] * 31, key="resource.primary.disk", device="primary")
    assert disk.state == "failed"                                        # so does disk
    clock.advance(8 * 3600)
    assert store.profile("workstation") == "normal"                       # silent expiry
    restored = AlertPolicyStore(str(tmp_path / "alert-policy.json"), clock=clock)
    assert restored.profile("workstation") == "normal"


def test_invalid_temperatures_are_rejected():
    assert valid_temperature(0.0) is None and valid_temperature(-5) is None and valid_temperature(130) is None
    assert valid_temperature(64.5) == 64.5


def test_disk_percentage_warning_and_real_free_space_critical(tmp_path):
    clock = Clock(); store = fresh(tmp_path, clock)
    obs = feed(store, clock, "disk", [87] * 31, free_gb=65.8)
    assert (obs.state, obs.rule.severity) == ("failed", "warning")
    obs = feed(store, clock, "disk", [90] * 11, free_gb=8.0)           # 90 % but < 10 GB free
    assert obs.rule.severity == "critical" and "free space 8 GB" in obs.rule.message


def test_existing_active_alert_is_not_replayed_after_upgrade(tmp_path):
    """7H persisted an active Workstation disk warning; 7I starts with empty windows."""
    path = tmp_path / "alerts.json"
    started = T0 - timedelta(hours=12)
    state = AlertStateFile(alerts=[AlertRecord(id="resource.workstation.disk_usage", type="resource.high", severity="warning",
                                               title="Workstation disk usage is high", message="disk usage is 87%; x",
                                               source="workstation", started_at=started, updated_at=started, status="active")])
    path.write_text(state.model_dump_json())
    clock = Clock()
    engine = AlertEngine(str(path), clock=clock)
    store = fresh(tmp_path, clock); engine.policy = store
    seen = []
    async def sink(transition, alert): seen.append(transition)
    engine.add_lifecycle_sink(sink)
    import asyncio
    for _ in range(40):
        obs = feed(store, clock, "disk", [87], key="resource.workstation.disk_usage", every=0)
        asyncio.run(engine.evaluate_observations([obs]))
        clock.advance(60)
    alert = engine.alert_by_id("resource.workstation.disk_usage")
    assert alert.status == "active" and alert.started_at == started and seen == []


@pytest.mark.asyncio
async def test_snooze_hides_repeats_never_an_escalation_survives_restart_and_expires_silently(tmp_path):
    clock = Clock(); store = fresh(tmp_path, clock)
    store.set_snooze("resource.workstation.ram_usage", hours=2, severity="warning", actor="dev")
    assert store.suppresses("resource.workstation.ram_usage", "warning")
    assert not store.suppresses("resource.workstation.ram_usage", "critical")   # escalation passes
    assert not store.suppresses("resource.primary.disk", "warning")              # another alert untouched
    restored = AlertPolicyStore(str(tmp_path / "alert-policy.json"), clock=clock)
    assert restored.snooze("resource.workstation.ram_usage").severity == "warning"
    clock.advance(2 * 3600 + 1)
    assert restored.snoozes() == {}
    with pytest.raises(ValueError):
        restored.set_snooze("x", hours=3, severity="warning", actor="dev")
    week = restored.set_snooze("x", hours=168, severity="warning", actor="dev")
    assert week.until - week.set_at == timedelta(days=7)


@pytest.mark.asyncio
async def test_escalation_clears_a_warning_snooze_and_push_still_routes_the_critical(tmp_path):
    from app.push import PushEngine, PushPreferencesUpdate
    from test_push_engine import FakeProvider, FULL
    clock = Clock(); store = fresh(tmp_path, clock)
    engine = AlertEngine(str(tmp_path / "alerts.json"), clock=clock); engine.policy = store
    push = PushEngine(str(tmp_path / "r.json"), str(tmp_path / "o.json"), providers={"fcm": FakeProvider("fcm")},
                      access=lambda d: FULL, alert_lookup=engine.alert_by_id, suppressed=store.suppresses, clock=clock)
    await push.bind_fcm(device_id="handset", display_name="Phone", target_ref="android-handset", token_fingerprint="f" * 64)
    await push.update_preferences("handset", "Phone", PushPreferencesUpdate(enabled=True, warning=True))
    engine.add_lifecycle_sink(push.handle_lifecycle)
    key = "resource.workstation.disk_usage"
    for value in [87] * 31:
        await engine.evaluate_observations([feed(store, clock, "disk", [value], key=key, every=0)]); clock.advance(60)
    assert engine.alert_by_id(key).severity == "warning"
    store.set_snooze(key, hours=2, severity="warning", actor="dev")
    for value in [98] * 11:
        await engine.evaluate_observations([feed(store, clock, "disk", [value], key=key, every=0)]); clock.advance(60)
    assert engine.alert_by_id(key).severity == "critical" and store.snooze(key) is None
    routes = [(m.transition, m.severity, m.status) for m in push.outbox.messages]
    assert routes[0][0] == "started" and ("escalated", "critical", "pending") in routes


@pytest.mark.asyncio
async def test_snoozed_alert_lifecycle_routes_are_skipped(tmp_path):
    from app.push import PushEngine, PushPreferencesUpdate
    from test_push_engine import FakeProvider, FULL, alert
    clock = Clock(); store = fresh(tmp_path, clock)
    a = alert(started=clock())
    push = PushEngine(str(tmp_path / "r.json"), str(tmp_path / "o.json"), providers={"fcm": FakeProvider("fcm")},
                      access=lambda d: FULL, alert_lookup=lambda i: a, suppressed=store.suppresses, clock=clock)
    await push.bind_fcm(device_id="handset", display_name="Phone", target_ref="android-handset", token_fingerprint="f" * 64)
    await push.update_preferences("handset", "Phone", PushPreferencesUpdate(enabled=True))
    store.set_snooze(a.id, hours=8, severity="warning", actor="dev")
    await push.handle_lifecycle("started", a)
    assert [(m.status, m.reason) for m in push.outbox.messages] == [("skipped", "snoozed_alert")]


def test_policy_file_is_private_bounded_and_corrupt_safe(tmp_path):
    clock = Clock(); store = fresh(tmp_path, clock)
    feed(store, clock, "cpu", [50] * 500)
    store.persist()
    path = tmp_path / "alert-policy.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert len(json.loads(path.read_text())["windows"]["resource.workstation.x"]) <= 6 * 60 + 11
    path.write_text("{broken")
    broken = AlertPolicyStore(str(path), clock=clock)
    assert broken.storage_status == "corrupt" and broken.profile("workstation") == "normal"
    broken.persist()
    assert path.read_text() == "{broken"


def test_snooze_and_profile_api_least_privilege(_auth_client, monkeypatch):
    from app.alerts import AlertObservation, AlertRule
    from app.main import app
    from test_push_api import ORIGIN, use, web_device
    client, _ha, store, _settings = _auth_client
    device_id, jar = web_device(client, store, "Phone 2")
    store.change_scope(device_id, "activity:read", add=True)
    headers = use(client, jar)
    rule = AlertRule(key="resource.workstation.disk_usage", severity="warning", type="resource.high", title="Workstation disk usage is high",
                     message="disk usage is 87%; x", source="workstation", failure_seconds=0, recovery_seconds=0)
    client.portal.call(app.state.alerts.evaluate_observations, [AlertObservation(state="failed", rule=rule)])
    path = "/api/v1/notifications/resource.workstation.disk_usage/snooze"
    assert client.post(path, json={"hours": 4}, headers=headers).status_code == 403          # no alerts:snooze
    assert client.put("/api/v1/alerts/profiles/workstation", json={"profile": "gaming"}, headers=headers).status_code == 403
    store.change_scope(device_id, "alerts:snooze", add=True)
    assert client.post(path, json={"hours": 4}, headers={"Origin": ORIGIN}).status_code == 403  # no CSRF
    assert client.post(path, json={"hours": 3}, headers=headers).status_code == 422             # 2/8/24/168 (+legacy 1/4)
    assert client.post(path, json={"hours": 169}, headers=headers).status_code == 422
    assert client.post("/api/v1/notifications/nope/snooze", json={"hours": 1}, headers=headers).status_code == 404
    snoozed = client.post(path, json={"hours": 168}, headers=headers)
    assert snoozed.status_code == 200 and snoozed.json()["severity"] == "warning"
    assert client.post(path, json={"hours": 4}, headers=headers).status_code == 200           # installed v10 app
    center = client.get("/api/v1/notifications").json()
    assert "resource.workstation.disk_usage" in center["snoozes"]
    assert center["notifications"][0]["status"] == "active"                                    # snooze never resolves
    assert client.delete(path, headers=headers).json()["cleared"] is True
    assert client.get("/api/v1/alerts/profiles").json()["devices"]["workstation"]["profile"] == "normal"
    store.change_scope(device_id, "alerts:profile", add=True)
    changed = client.put("/api/v1/alerts/profiles/workstation", json={"profile": "development", "hours": 8}, headers=headers)
    assert changed.status_code == 200 and changed.json()["profile"] == "development"
    assert client.put("/api/v1/alerts/profiles/router", json={"profile": "gaming"}, headers=headers).status_code == 422
    monkeypatch.setattr(app.state.alert_policy, "persist", lambda: False)
    assert client.post(path, json={"hours": 1}, headers=headers).status_code == 503
    assert app.state.alert_policy.snooze("resource.workstation.disk_usage") is None
    assert client.put("/api/v1/alerts/profiles/workstation", json={"profile": "gaming"}, headers=headers).status_code == 503
    assert app.state.alert_policy.profile("workstation") == "development"
    client.cookies.clear()
    assert client.get("/api/v1/alerts/profiles").status_code == 401


def test_failed_policy_write_does_not_confirm_or_keep_a_snooze(tmp_path, monkeypatch):
    store = fresh(tmp_path, Clock())
    monkeypatch.setattr("app.resource_policy.os.replace", lambda *_args: (_ for _ in ()).throw(OSError("disk failed")))
    with pytest.raises(OSError, match="persistence unavailable"):
        store.set_snooze("resource.workstation.disk_usage", hours=2, severity="warning", actor="device")
    assert store.snooze("resource.workstation.disk_usage") is None
    assert store.write_failed is True
