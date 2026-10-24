import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.alerts import AlertRecord
from app.fcm import (
    FCM_SCOPE,
    FcmNotificationProvider,
    GoogleServiceAccountTokenSource,
    load_fcm_runtime,
)
from app.notifications import NotificationDestination, NotificationEngine


class TokenSource:
    project_id = "roomos-fixture"

    def __init__(self, token="oauth-secret"):
        self.value = token
        self.calls = 0
        self.invalidations = 0

    async def token(self):
        self.calls += 1
        return self.value

    def invalidate(self):
        self.invalidations += 1


class BrokenTokenSource(TokenSource):
    async def token(self):
        raise RuntimeError("credential-secret-must-not-be-logged")


def notification_alert(severity="critical"):
    now = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
    return AlertRecord(
        id="node.pc_old.offline",
        type="node.offline",
        severity=severity,
        title="Secondary is offline",
        message="RoomOS detected that Secondary is unavailable.",
        source="pc_old",
        started_at=now,
        updated_at=now,
        status="active",
    )


async def record(tmp_path, severity="critical"):
    engine = NotificationEngine(str(tmp_path / "notifications.json"))
    return await engine.handle_alert_started(notification_alert(severity))


def destination(**values):
    fields = {
        "id": "android-tablet",
        "display_name": "Android tablet",
        "activated_at": datetime(2026, 10, 4, 11, 0, tzinfo=UTC),
        "provider": "fcm",
        "provider_target_ref": "tablet-main",
    }
    fields.update(values)
    return NotificationDestination(**fields)


@pytest.mark.asyncio
async def test_fcm_delivery_uses_http_v1_minimal_payload_and_channel(tmp_path):
    seen = {}

    async def handler(request):
        seen["request"] = request
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"name": "projects/x/messages/1"})

    source = TokenSource()
    provider = FcmNotificationProvider(
        source,
        source.project_id,
        {"tablet-main": "registration-secret"},
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    item = await record(tmp_path)
    assert await provider.send(item, destination()) == "delivered"
    request = seen["request"]
    payload = seen["payload"]
    assert request.url == (
        "https://fcm.googleapis.com/v1/projects/roomos-fixture/messages:send"
    )
    assert request.headers["authorization"] == "Bearer oauth-secret"
    assert payload["message"]["token"] == "registration-secret"
    assert payload["message"]["android"]["notification"]["channel_id"] == (
        "roomos_critical"
    )
    assert payload["message"]["android"]["priority"] == "HIGH"
    serialized = json.dumps(payload)
    assert "192.168" not in serialized
    assert "entity_id" not in serialized
    assert "session" not in serialized
    await provider.close()


@pytest.mark.asyncio
async def test_warning_maps_to_warning_channel(tmp_path):
    payloads = []

    async def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"name": "projects/x/messages/2"})

    provider = FcmNotificationProvider(
        TokenSource(),
        "roomos-fixture",
        {"tablet-main": "registration-secret"},
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert await provider.send(await record(tmp_path, "warning"), destination()) == "delivered"
    android = payloads[0]["message"]["android"]
    assert android["priority"] == "NORMAL"
    assert android["notification"]["channel_id"] == "roomos_warning"
    await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error", "expected"),
    [
        (429, "QUOTA_EXCEEDED", "temporary_failure"),
        (503, "UNAVAILABLE", "temporary_failure"),
        (500, "INTERNAL", "temporary_failure"),
        (404, "UNREGISTERED", "permanent_failure"),
        (403, "SENDER_ID_MISMATCH", "permanent_failure"),
        (400, "INVALID_ARGUMENT", "permanent_failure"),
    ],
)
async def test_fcm_error_semantics(tmp_path, status, error, expected):
    async def handler(_request):
        return httpx.Response(
            status,
            json={
                "error": {
                    "status": "ERROR",
                    "details": [{"errorCode": error}],
                }
            },
        )

    provider = FcmNotificationProvider(
        TokenSource(),
        "roomos-fixture",
        {"tablet-main": "registration-secret"},
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert await provider.send(await record(tmp_path), destination()) == expected
    await provider.close()


@pytest.mark.asyncio
async def test_unauthenticated_invalidates_oauth_for_bounded_engine_retry(tmp_path):
    source = TokenSource()

    async def handler(_request):
        return httpx.Response(401, json={"error": {"status": "UNAUTHENTICATED"}})

    provider = FcmNotificationProvider(
        source,
        source.project_id,
        {"tablet-main": "registration-secret"},
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert await provider.send(await record(tmp_path), destination()) == "temporary_failure"
    assert source.invalidations == 1
    await provider.close()


@pytest.mark.asyncio
async def test_timeout_malformed_success_and_missing_target_are_safe(tmp_path):
    async def timeout(_request):
        raise httpx.ReadTimeout("bounded")

    item = await record(tmp_path)
    provider = FcmNotificationProvider(
        TokenSource(),
        "roomos-fixture",
        {"tablet-main": "registration-secret"},
        client=httpx.AsyncClient(transport=httpx.MockTransport(timeout)),
    )
    assert await provider.send(item, destination()) == "temporary_failure"
    await provider.close()

    async def malformed(_request):
        return httpx.Response(200, json={"unexpected": True})

    provider = FcmNotificationProvider(
        TokenSource(),
        "roomos-fixture",
        {"tablet-main": "registration-secret"},
        client=httpx.AsyncClient(transport=httpx.MockTransport(malformed)),
    )
    assert await provider.send(item, destination()) == "temporary_failure"
    assert await provider.send(
        item,
        destination(provider_target_ref="missing"),
    ) == "permanent_failure"
    await provider.close()


@pytest.mark.asyncio
async def test_credential_failure_is_isolated_and_secret_is_not_logged(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    provider = FcmNotificationProvider(
        BrokenTokenSource(),
        "roomos-fixture",
        {"tablet-main": "registration-secret"},
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: None)),
    )
    assert await provider.send(await record(tmp_path), destination()) == "temporary_failure"
    assert "credential-secret-must-not-be-logged" not in caplog.text
    assert "registration-secret" not in caplog.text
    await provider.close()


class FakeCredentials:
    project_id = "roomos-fixture"

    def __init__(self, clock):
        self.clock = clock
        self.token = None
        self.expiry = None
        self.refreshes = 0

    def refresh(self, _request):
        self.refreshes += 1
        self.token = f"oauth-{self.refreshes}"
        self.expiry = self.clock[0] + timedelta(hours=1)


@pytest.mark.asyncio
async def test_oauth_token_acquisition_reuse_expiry_and_refresh():
    clock = [datetime(2026, 10, 4, 12, 0, tzinfo=UTC)]
    credentials = FakeCredentials(clock)
    seen = {}

    def factory(path, *, scopes):
        seen.update(path=path, scopes=scopes)
        return credentials

    source = GoogleServiceAccountTokenSource(
        "/private/fcm.json",
        credentials_factory=factory,
        request_factory=lambda: object(),
        clock=lambda: clock[0],
    )
    assert await source.token() == "oauth-1"
    assert await source.token() == "oauth-1"
    assert credentials.refreshes == 1
    assert seen == {"path": "/private/fcm.json", "scopes": [FCM_SCOPE]}
    clock[0] += timedelta(hours=1)
    assert await source.token() == "oauth-2"
    source.invalidate()
    assert await source.token() == "oauth-3"


class RuntimeTokenSource(TokenSource):
    def __init__(self, _path):
        super().__init__()


def private_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


@pytest.mark.asyncio
async def test_runtime_loader_is_disabled_or_fail_closed_and_never_logs_secrets(
    tmp_path, caplog
):
    assert load_fcm_runtime(
        enabled=False,
        credentials_path=str(tmp_path / "missing-credential"),
        destinations_path=str(tmp_path / "missing-destinations"),
        targets_path=str(tmp_path / "missing-targets"),
        timeout_seconds=5,
    ).status == "disabled"

    caplog.set_level(logging.ERROR)
    targets = tmp_path / "targets.json"
    private_json(targets, {"version": 1, "targets": {"tablet-main": "do-not-log-token"}})
    runtime = load_fcm_runtime(
        enabled=True,
        credentials_path=str(tmp_path / "missing-credential"),
        destinations_path=str(tmp_path / "missing-destinations"),
        targets_path=str(targets),
        timeout_seconds=5,
    )
    assert runtime.status == "unavailable"
    assert runtime.providers == ()
    assert "do-not-log-token" not in caplog.text


@pytest.mark.asyncio
async def test_runtime_loader_requires_private_complete_registries(tmp_path):
    credentials = tmp_path / "credentials.json"
    destinations = tmp_path / "destinations.json"
    targets = tmp_path / "targets.json"
    private_json(
        credentials,
        {
            "type": "service_account",
            "project_id": "roomos-fixture",
            "private_key": "not-real",
            "client_email": "fixture@example.invalid",
        },
    )
    private_json(
        destinations,
        {
            "version": 1,
            "destinations": [destination().model_dump(mode="json")],
        },
    )
    private_json(
        targets,
        {"version": 1, "targets": {"tablet-main": "registration-secret"}},
    )
    runtime = load_fcm_runtime(
        enabled=True,
        credentials_path=str(credentials),
        destinations_path=str(destinations),
        targets_path=str(targets),
        timeout_seconds=5,
        token_source_factory=RuntimeTokenSource,
    )
    assert runtime.status == "configured"
    assert len(runtime.providers) == 1
    assert len(runtime.destinations) == 1
    assert runtime.destinations[0].activated_at == datetime(
        2026, 10, 4, 11, 0, tzinfo=UTC
    )
    await runtime.close()

    targets.chmod(0o644)
    unavailable = load_fcm_runtime(
        enabled=True,
        credentials_path=str(credentials),
        destinations_path=str(destinations),
        targets_path=str(targets),
        timeout_seconds=5,
        token_source_factory=RuntimeTokenSource,
    )
    assert unavailable.status == "unavailable"
    assert unavailable.providers == ()
