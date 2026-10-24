"""7G operator canary: one informational message, one attempt, device-bound, loopback + one-time token."""

import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import app.main as main_module
from app.main import app
from app.push import CANARY_BODY, CANARY_TITLE, PushEngine, PushPreferencesUpdate
from app.push_providers import FcmPushProvider
from test_push_engine import FULL, Clock, alert

T0 = datetime(2026, 10, 16, 13, 30, tzinfo=UTC)


class CanaryProvider:
    kind = "fcm"

    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = []

    async def send(self, message, device, ttl, *, high_priority=False):
        self.calls.append((message.id, device.device_id, ttl, high_priority, message.title, message.body))
        return self.results.pop(0) if self.results else ("accepted", None)


async def engine(tmp_path, access=lambda d: FULL, clock=None):
    e = PushEngine(str(tmp_path / "r.json"), str(tmp_path / "o.json"), providers={}, access=access, clock=clock or Clock(T0))
    await e.bind_fcm(device_id="handset", display_name="Phone", target_ref="android-handset", token_fingerprint="f" * 64)
    await e.update_preferences("handset", "Phone", PushPreferencesUpdate(enabled=False))  # canary ignores prefs
    return e


@pytest.mark.asyncio
async def test_canary_is_one_high_priority_informational_attempt_with_receipts(tmp_path):
    e = await engine(tmp_path)
    provider = CanaryProvider()
    message = await e.send_canary("handset", provider)
    assert message.status == "accepted" and message.attempt_count == 1
    assert provider.calls == [(message.id, "handset", 600, True, CANARY_TITLE, CANARY_BODY)]
    assert message.transition == "summary" and message.alert_id is None and message.path == "/notifications"
    assert message.public_body == "Ayrıntılar için RoomOS'u açın."
    assert await e.record_receipt("handset", message.id, "displayed")
    assert await e.record_receipt("handset", message.id, "opened")
    stored = next(m for m in e.outbox.messages if m.id == message.id)
    assert stored.status == "opened" and stored.displayed_at and stored.opened_at


@pytest.mark.asyncio
async def test_canary_failure_is_final_and_never_retried(tmp_path):
    clock = Clock(T0)
    e = await engine(tmp_path, clock=clock)
    provider = CanaryProvider([("temporary_failure", "UNAVAILABLE")])
    message = await e.send_canary("handset", provider)
    assert message.status == "failed" and message.reason == "canary_UNAVAILABLE" and message.next_attempt_at is None
    e.providers = {"fcm": provider}
    clock.now = T0 + timedelta(minutes=5)
    assert await e.dispatch_pending() == 0
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_canary_guard_auth_and_registration_checks(tmp_path):
    e = await engine(tmp_path)
    await e.send_canary("handset", CanaryProvider())
    with pytest.raises(ValueError, match="10 minutes"):
        await e.send_canary("handset", CanaryProvider())
    with pytest.raises(ValueError, match="no active native"):
        await e.send_canary("unknown", CanaryProvider())
    revoked = await engine(tmp_path / "x", access=lambda d: None)
    with pytest.raises(ValueError, match="not authorized"):
        await revoked.send_canary("handset", CanaryProvider())
    web_only = await engine(tmp_path / "y", access=lambda d: (frozenset({"activity:read"}), frozenset({"web"})))
    with pytest.raises(ValueError, match="not authorized"):
        await web_only.send_canary("handset", CanaryProvider())


@pytest.mark.asyncio
async def test_canary_touches_no_alert_lifecycle(tmp_path):
    e = await engine(tmp_path)
    await e.send_canary("handset", CanaryProvider())
    assert all(m.alert_id is None for m in e.outbox.messages)
    assert e.summary().external_deliveries == 1


def test_fcm_payload_high_priority_only_when_requested():
    from test_push_providers import fcm_device, message
    normal = FcmPushProvider.payload(message("warning"), fcm_device(), "T", 600)
    urgent = FcmPushProvider.payload(message("warning"), fcm_device(), "T", 600, True)
    assert normal["message"]["android"]["priority"] == "NORMAL"
    assert urgent["message"]["android"]["priority"] == "HIGH"
    assert "notification" not in urgent["message"]


@pytest.fixture
def canary_app(monkeypatch, tmp_path, push_client):
    token_path = tmp_path / "canary.token"
    monkeypatch.setattr(main_module, "CANARY_TOKEN_PATH", str(token_path))
    provider = CanaryProvider()
    app.state.push.engine.providers = {"fcm": provider}
    yield push_client, token_path, provider


def write_token(path: Path, value="good-token", age=0, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "w") as handle:
        handle.write(value)
    os.chmod(path, mode)
    if age:
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))


def native_device(client, store):
    from test_auth import pair_native
    paired = pair_native(client, store).json()
    device_id = paired["device"]["id"]
    client.portal.call(lambda: app.state.push.engine.bind_fcm(device_id=device_id, display_name="Phone",
                                                              target_ref="android-x", token_fingerprint="f" * 64))
    return device_id


def test_internal_canary_requires_loopback_and_one_time_private_fresh_token(canary_app, monkeypatch):
    (client, _ha, store, _settings), token_path, provider = canary_app
    device_id = native_device(client, store)
    body = {"device_id": device_id}
    write_token(token_path)
    # TestClient is not a loopback client: refused even with a valid token, and the token is not consumed.
    assert client.post("/internal/push/canary", json=body, headers={"X-RoomOS-Canary-Token": "good-token"}).status_code == 403
    monkeypatch.setattr(main_module, "CANARY_LOCAL_HOSTS", frozenset({"testclient"}))
    assert client.post("/internal/push/canary", json=body).status_code == 403                      # no token header
    assert not token_path.exists()                                                                 # consumed
    write_token(token_path)
    assert client.post("/internal/push/canary", json=body, headers={"X-RoomOS-Canary-Token": "bad"}).status_code == 403
    write_token(token_path, age=300)
    assert client.post("/internal/push/canary", json=body, headers={"X-RoomOS-Canary-Token": "good-token"}).status_code == 403
    write_token(token_path, mode=0o644)
    assert client.post("/internal/push/canary", json=body, headers={"X-RoomOS-Canary-Token": "good-token"}).status_code == 403
    assert provider.calls == []
    write_token(token_path)
    sent = client.post("/internal/push/canary", json=body, headers={"X-RoomOS-Canary-Token": "good-token"})
    assert sent.status_code == 200 and sent.json()["status"] == "accepted" and sent.json()["attempt_count"] == 1
    assert "token" not in sent.text.lower().replace("canary", "")
    assert len(provider.calls) == 1 and provider.calls[0][3] is True
    write_token(token_path)
    again = client.post("/internal/push/canary", json=body, headers={"X-RoomOS-Canary-Token": "good-token"})
    assert again.status_code == 409 and len(provider.calls) == 1


def test_internal_path_is_never_proxied_by_nginx():
    root = Path(__file__).parents[2] / "docker"
    for name in ("roomos-common.inc", "nginx.conf"):
        text = (root / name).read_text()
        assert "/internal" not in text
from test_push_api import push_client  # noqa: F401,E402
