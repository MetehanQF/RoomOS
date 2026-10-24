import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.alerts import AlertRecord
from app.models import SystemMetric
from app.main import app
from app.notifications import (
    NotificationDestination,
    NotificationEngine,
    ProductionNotificationIntent,
)
from app.production_notifications import (
    ProductionNotificationPolicy,
    SERVICE_ALLOWLIST,
    observations_from_system,
)
from app.system_monitor import SystemMonitor
from test_alerts import system


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, seconds: int):
        self.now += timedelta(seconds=seconds)

    def rewind(self, seconds: int):
        self.now -= timedelta(seconds=seconds)


class RecordingSink:
    def __init__(self):
        self.intents = []

    async def __call__(self, intent):
        self.intents.append(intent)
        return intent


class RejectOnceSink(RecordingSink):
    def __init__(self):
        super().__init__()
        self.rejections = 1

    async def __call__(self, intent):
        if self.rejections:
            self.rejections -= 1
            return None
        return await super().__call__(intent)


class FakeProvider:
    id = "fcm"

    def __init__(self):
        self.calls = []

    async def send(self, notification, destination):
        self.calls.append((notification.id, destination.id))
        return "delivered"


def snapshot(
    *,
    primary_online=True,
    secondary_online=True,
    primary_temperature=50.0,
    secondary_temperature=45.0,
    frigate="healthy",
    nextcloud="healthy",
    dns="healthy",
):
    result = system(
        primary_online=primary_online,
        secondary_online=secondary_online,
        frigate=frigate,
        nextcloud=nextcloud,
        dns=dns,
    )
    for node, value in ((result.primary, primary_temperature), (result.secondary, secondary_temperature)):
        node.metrics[f"{node.id}_temperature"] = SystemMetric(
            id=f"{node.id}_temperature",
            value=value,
            unit="°C",
            available=True,
            stale=False,
            freshness="fresh",
            last_updated=datetime(2026, 10, 5, 12, 0, tzinfo=UTC),
        )
    return result


ROLLOUT_CASES = (
    ("node.primary", {"primary_online": False}, 60, "node.offline", "node.recovered"),
    ("node.secondary", {"secondary_online": False}, 60, "node.offline", "node.recovered"),
    ("service.frigate", {"frigate": "offline"}, 60, "service.down", "service.recovered"),
    ("service.nextcloud", {"nextcloud": "offline"}, 60, "service.down", "service.recovered"),
    ("service.dns", {"dns": "offline"}, 60, "service.down", "service.recovered"),
    ("temperature.primary", {"primary_temperature": 78.0}, 120, "temperature.high", "temperature.recovered"),
    ("temperature.secondary", {"secondary_temperature": 78.0}, 120, "temperature.high", "temperature.recovered"),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("subject", "failure_values", "failure_seconds", "failure_event", "recovery_event"),
    ROLLOUT_CASES,
)
async def test_full_rollout_allowlist_routes_failure_and_recovery_only_after_cutoff(
    tmp_path,
    subject,
    failure_values,
    failure_seconds,
    failure_event,
    recovery_event,
):
    clock = Clock()
    cutoff = clock()
    provider = FakeProvider()
    destination = NotificationDestination(
        id="handset",
        display_name="Phone",
        enabled=True,
        activated_at=cutoff,
        provider="fcm",
        provider_target_ref="android-handset",
        severity_policy=frozenset({"info", "warning", "critical"}),
    )
    outbox = NotificationEngine(
        tmp_path / "notifications.json",
        providers=[provider],
        destinations=[destination],
        clock=clock,
    )
    policy = ProductionNotificationPolicy(
        tmp_path / "incidents.json",
        outbox.handle_production_intent,
        clock=clock,
    )
    await policy.initialize_rollout(snapshot(), epoch=cutoff)
    assert outbox.notifications() == []

    clock.advance(1)
    await policy.evaluate(snapshot(**failure_values))
    clock.advance(failure_seconds)
    await policy.evaluate(snapshot(**failure_values))
    clock.advance(1)
    await policy.evaluate(snapshot())
    recovery_seconds = 120 if subject.startswith("temperature.") else 30
    clock.advance(recovery_seconds)
    await policy.evaluate(snapshot())

    records = outbox.notifications()
    assert [(item.event_type, len(item.deliveries)) for item in records] == [
        (failure_event, 1),
        (recovery_event, 1),
    ]
    assert all(item.created_at > cutoff for item in records)
    assert all(item.deliveries[0].destination_id == "handset" for item in records)
    assert all(item.deliveries[0].attempt_count == 0 for item in records)
    assert provider.calls == []


@pytest.mark.asyncio
async def test_rollout_refuses_nonhealthy_or_unknown_baseline(tmp_path):
    clock = Clock()
    sink = RecordingSink()
    policy = ProductionNotificationPolicy(tmp_path / "incidents.json", sink, clock=clock)
    with pytest.raises(ValueError, match="node.primary"):
        await policy.initialize_rollout(snapshot(primary_online=False), epoch=clock())
    current = snapshot()
    current.secondary.metrics["secondary_temperature"].freshness = "stale"
    current.secondary.metrics["secondary_temperature"].stale = True
    with pytest.raises(ValueError, match="temperature.secondary"):
        await policy.initialize_rollout(current, epoch=clock())
    assert sink.intents == []


@pytest.mark.asyncio
async def test_rollout_rebaselines_pre_cutoff_open_incident_without_replay(tmp_path):
    clock = Clock()
    sink = RecordingSink()
    path = tmp_path / "incidents.json"
    policy = ProductionNotificationPolicy(path, sink, clock=clock)
    await policy.evaluate(snapshot())
    await policy.evaluate(snapshot(frigate="offline"))
    clock.advance(60)
    await policy.evaluate(snapshot(frigate="offline"))
    assert [item.event_type for item in sink.intents] == ["service.down"]

    cutoff = clock() + timedelta(seconds=1)
    await policy.initialize_rollout(snapshot(), epoch=cutoff)
    before = len(sink.intents)
    restarted = ProductionNotificationPolicy(path, sink, clock=clock)
    clock.advance(2)
    await restarted.evaluate(snapshot())
    clock.advance(300)
    await restarted.evaluate(snapshot())
    assert len(sink.intents) == before
    persisted = json.loads(path.read_text())
    assert datetime.fromisoformat(
        persisted["rollout_epoch"].replace("Z", "+00:00")
    ) == cutoff


@pytest.mark.asyncio
async def test_pre_cutoff_outbox_never_routes_and_dispatch_restart_is_idempotent(tmp_path):
    clock = Clock()
    path = tmp_path / "notifications.json"
    disabled = NotificationEngine(path, clock=clock)
    for index in range(18):
        at = clock() + timedelta(seconds=index)
        await disabled.handle_alert_started(
            AlertRecord(
                id=f"service.fixture-{index}.offline",
                type="service.offline",
                severity="warning",
                title="Fixture unavailable",
                message="Synthetic historical fixture.",
                source="fixture",
                started_at=at,
                updated_at=at,
                status="active",
            )
        )
    historical = [item.model_dump(mode="json") for item in disabled.notifications()]
    cutoff = clock() + timedelta(seconds=60)
    provider = FakeProvider()
    destination = NotificationDestination(
        id="handset",
        display_name="Phone",
        enabled=True,
        activated_at=cutoff,
        provider="fcm",
        provider_target_ref="android-handset",
        severity_policy=frozenset({"info", "warning", "critical"}),
    )
    active = NotificationEngine(
        path,
        providers=[provider],
        destinations=[destination],
        clock=clock,
    )
    assert await active.dispatch_pending() == 0
    assert [item.model_dump(mode="json") for item in active.notifications()] == historical
    assert provider.calls == []

    clock.now = cutoff + timedelta(seconds=1)
    await active.handle_production_intent(
        ProductionNotificationIntent(
            event_type="node.offline",
            severity="critical",
            subject="node.primary",
            incident_id="inc_" + "b" * 32,
            body="Primary çevrimdışı.",
            source="primary",
            occurred_at=clock(),
        )
    )
    assert await active.dispatch_pending() == 1
    restarted = NotificationEngine(
        path,
        providers=[provider],
        destinations=[destination],
        clock=clock,
    )
    assert await restarted.dispatch_pending() == 0
    assert len(provider.calls) == 1
    assert all(item.deliveries == [] for item in restarted.notifications()[:18])


@pytest.mark.asyncio
async def test_transient_node_failure_below_debounce_creates_no_intent(tmp_path):
    clock = Clock()
    sink = RecordingSink()
    policy = ProductionNotificationPolicy(tmp_path / "incidents.json", sink, clock=clock)
    await policy.evaluate(snapshot())
    await policy.evaluate(snapshot(primary_online=False))
    clock.advance(59)
    await policy.evaluate(snapshot())
    assert sink.intents == []


@pytest.mark.asyncio
async def test_persistent_node_failure_and_recovery_each_emit_exactly_once(tmp_path):
    clock = Clock()
    sink = RecordingSink()
    policy = ProductionNotificationPolicy(tmp_path / "incidents.json", sink, clock=clock)
    await policy.evaluate(snapshot())
    await policy.evaluate(snapshot(primary_online=False))
    clock.advance(60)
    await policy.evaluate(snapshot(primary_online=False))
    await policy.evaluate(snapshot(primary_online=False))
    assert [(item.event_type, item.severity, item.body) for item in sink.intents] == [
        ("node.offline", "critical", "Primary çevrimdışı.")
    ]

    clock.advance(1)
    await policy.evaluate(snapshot())
    clock.advance(29)
    await policy.evaluate(snapshot(primary_online=False))
    assert len(sink.intents) == 1
    await policy.evaluate(snapshot())
    clock.advance(30)
    await policy.evaluate(snapshot())
    await policy.evaluate(snapshot())
    assert [(item.event_type, item.severity, item.body) for item in sink.intents] == [
        ("node.offline", "critical", "Primary çevrimdışı."),
        ("node.recovered", "info", "Primary tekrar çevrimiçi."),
    ]
    assert sink.intents[0].incident_id == sink.intents[1].incident_id


@pytest.mark.asyncio
async def test_service_catalog_is_fixed_and_debounced(tmp_path):
    assert SERVICE_ALLOWLIST == ("frigate", "nextcloud", "dns")
    clock = Clock()
    sink = RecordingSink()
    policy = ProductionNotificationPolicy(tmp_path / "incidents.json", sink, clock=clock)
    await policy.evaluate(snapshot())
    await policy.evaluate(snapshot(frigate="offline"))
    clock.advance(60)
    await policy.evaluate(snapshot(frigate="offline"))
    assert [(item.event_type, item.severity, item.body) for item in sink.intents] == [
        ("service.down", "warning", "Frigate servisi kullanılamıyor.")
    ]
    await policy.evaluate(snapshot())
    clock.advance(30)
    await policy.evaluate(snapshot())
    assert sink.intents[-1].event_type == "service.recovered"
    assert sink.intents[-1].body == "Frigate servisi tekrar kullanılabilir."


@pytest.mark.asyncio
async def test_temperature_hysteresis_noise_high_hold_and_recovery(tmp_path):
    clock = Clock()
    sink = RecordingSink()
    policy = ProductionNotificationPolicy(tmp_path / "incidents.json", sink, clock=clock)
    await policy.evaluate(snapshot())

    for value in (75.0, 74.0, 75.0, 74.0):
        clock.advance(60)
        await policy.evaluate(snapshot(secondary_temperature=value))
    assert sink.intents == []

    clock.advance(1)
    await policy.evaluate(snapshot(secondary_temperature=78.0))
    clock.advance(120)
    await policy.evaluate(snapshot(secondary_temperature=78.0))
    assert [(item.event_type, item.severity, item.body) for item in sink.intents] == [
        ("temperature.high", "warning", "Secondary sıcaklığı yüksek: 78°C.")
    ]

    clock.advance(60)
    await policy.evaluate(snapshot(secondary_temperature=72.0))
    clock.advance(300)
    await policy.evaluate(snapshot(secondary_temperature=72.0))
    assert len(sink.intents) == 1

    await policy.evaluate(snapshot(secondary_temperature=70.0))
    clock.advance(120)
    await policy.evaluate(snapshot(secondary_temperature=69.0))
    assert [(item.event_type, item.severity) for item in sink.intents] == [
        ("temperature.high", "warning"),
        ("temperature.recovered", "info"),
    ]
    assert sink.intents[-1].body == "Secondary sıcaklığı normale döndü: 69°C."


@pytest.mark.asyncio
async def test_restart_and_clock_rewind_do_not_duplicate_open_incident(tmp_path):
    clock = Clock()
    sink = RecordingSink()
    path = tmp_path / "incidents.json"
    first = ProductionNotificationPolicy(path, sink, clock=clock)
    await first.evaluate(snapshot())
    await first.evaluate(snapshot(primary_online=False))
    clock.advance(60)
    await first.evaluate(snapshot(primary_online=False))
    assert len(sink.intents) == 1

    clock.rewind(3600)
    restarted = ProductionNotificationPolicy(path, sink, clock=clock)
    await restarted.evaluate(snapshot(primary_online=False))
    await restarted.evaluate(snapshot(primary_online=False))
    assert len(sink.intents) == 1
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_outbox_rejection_retries_same_server_incident_id(tmp_path):
    clock = Clock()
    sink = RejectOnceSink()
    path = tmp_path / "incidents.json"
    policy = ProductionNotificationPolicy(path, sink, clock=clock)
    await policy.evaluate(snapshot())
    await policy.evaluate(snapshot(secondary_online=False))
    clock.advance(60)
    await policy.evaluate(snapshot(secondary_online=False))
    assert sink.intents == []
    persisted_incident = json.loads(path.read_text())["incidents"]["node.secondary"][
        "incident_id"
    ]
    await policy.evaluate(snapshot(secondary_online=False))
    assert len(sink.intents) == 1
    assert sink.intents[0].incident_id == persisted_incident


@pytest.mark.asyncio
async def test_repeated_snapshot_and_ha_reconnect_shape_create_no_storm(tmp_path):
    clock = Clock()
    sink = RecordingSink()
    policy = ProductionNotificationPolicy(tmp_path / "incidents.json", sink, clock=clock)
    for _ in range(10):
        await policy.evaluate(snapshot())
        clock.advance(1)
    assert sink.intents == []


@pytest.mark.asyncio
async def test_system_monitor_reaccept_after_reconnect_creates_no_notification(tmp_path):
    clock = Clock()
    sink = RecordingSink()
    policy = ProductionNotificationPolicy(tmp_path / "incidents.json", sink, clock=clock)

    async def publish(_event):
        pass

    from app.alerts import AlertEngine

    monitor = SystemMonitor(
        object(),
        object(),
        AlertEngine(tmp_path / "alerts.json", clock=clock),
        publish,
        policy,
    )
    await monitor.accept(snapshot())
    clock.advance(1)
    await monitor.accept(snapshot())
    assert sink.intents == []


@pytest.mark.asyncio
async def test_unknown_unavailable_and_stale_are_fail_closed(tmp_path):
    current = snapshot()
    current.primary.metrics["primary_online"].freshness = "stale"
    current.primary.metrics["primary_online"].stale = True
    current.secondary.metrics["secondary_temperature"].freshness = "unavailable"
    current.secondary.metrics["secondary_temperature"].available = False
    frigate = next(item for item in current.services if item.id == "frigate")
    frigate.status = "unknown"
    frigate.health = "unknown"
    frigate.freshness = "unknown"
    observations = observations_from_system(current)
    assert observations["node.primary"].state == "unknown"
    assert observations["temperature.secondary"].state == "unknown"
    assert observations["service.frigate"].state == "unknown"

    clock = Clock()
    sink = RecordingSink()
    policy = ProductionNotificationPolicy(tmp_path / "incidents.json", sink, clock=clock)
    await policy.evaluate(current)
    clock.advance(600)
    await policy.evaluate(current)
    assert sink.intents == []


@pytest.mark.asyncio
async def test_disabled_destination_and_dispatcher_preserve_historical_records(tmp_path):
    clock = Clock()
    provider = FakeProvider()
    destination = NotificationDestination(
        id="handset",
        display_name="Phone",
        enabled=False,
        activated_at=clock(),
        provider="fcm",
        provider_target_ref="android-handset",
        severity_policy=frozenset({"info", "warning", "critical"}),
    )
    outbox = NotificationEngine(
        tmp_path / "notifications.json",
        providers=[provider],
        destinations=[destination],
        clock=clock,
    )
    for index in range(16):
        at = clock.now + timedelta(seconds=index)
        await outbox.handle_alert_started(
            AlertRecord(
                id=f"service.fixture-{index}.offline",
                type="service.offline",
                severity="warning",
                title="Fixture unavailable",
                message="Synthetic historical fixture.",
                source="fixture",
                started_at=at,
                updated_at=at,
                status="active",
            )
        )
    historical_before = [item.model_dump(mode="json") for item in outbox.notifications()]
    policy = ProductionNotificationPolicy(
        tmp_path / "incidents.json", outbox.handle_production_intent, clock=clock
    )
    await policy.evaluate(snapshot())
    await policy.evaluate(snapshot(primary_online=False))
    clock.advance(60)
    await policy.evaluate(snapshot(primary_online=False))
    records = outbox.notifications()
    assert [item.model_dump(mode="json") for item in records[:16]] == historical_before
    assert records[-1].event_type == "node.offline"
    assert records[-1].deliveries == []
    assert outbox.delivery_enabled is False
    assert await outbox.dispatch_pending() == 0
    assert provider.calls == []
    assert outbox.summary().external_attempts == 0
    assert outbox.summary().external_deliveries == 0


def test_incident_state_rejects_corrupt_file_without_overwrite(tmp_path):
    path = tmp_path / "incidents.json"
    path.write_text(json.dumps({"version": 1, "incidents": {"bad": {"phase": "boom"}}}))
    original = path.read_bytes()
    policy = ProductionNotificationPolicy(path, RecordingSink())
    assert policy.storage_status == "corrupt"
    assert path.read_bytes() == original


def test_production_intent_rejects_client_selected_severity():
    with pytest.raises(ValidationError):
        ProductionNotificationIntent(
            event_type="node.offline",
            severity="info",
            subject="node.primary",
            incident_id="inc_" + "a" * 32,
            body="Primary çevrimdışı.",
            source="primary",
            occurred_at=datetime(2026, 10, 5, 12, 0, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    "values",
    [
        {"event_type": "unknown.event"},
        {"subject": "node.unknown", "source": "primary"},
        {"body": "Arbitrary payload"},
    ],
)
def test_production_intent_rejects_unknown_or_arbitrary_fields(values):
    fields = {
        "event_type": "node.offline",
        "severity": "critical",
        "subject": "node.primary",
        "incident_id": "inc_" + "a" * 32,
        "body": "Primary çevrimdışı.",
        "source": "primary",
        "occurred_at": datetime(2026, 10, 5, 12, 0, tzinfo=UTC),
    }
    fields.update(values)
    with pytest.raises(ValidationError):
        ProductionNotificationIntent(**fields)


def test_no_public_arbitrary_notification_send_api_exists():
    notification_paths = {
        path for path in app.openapi()["paths"] if "notification" in path
    }
    assert notification_paths == {
        "/api/v1/native/notifications/fcm/register",
        "/api/v1/notifications",
        "/api/v1/notifications/{alert_id}/read",
        "/api/v1/notifications/{alert_id}/acknowledge",
        "/api/v1/notifications/{alert_id}/mute",
        # 7I: silences repeat notifications of one alert; it can never create or send a notification.
        "/api/v1/notifications/{alert_id}/snooze",
        # 7I.2: Native (Bearer + android session) twin of the snooze route; it cannot send anything.
        "/api/v1/native/notifications/{alert_id}/snooze",
    }
    assert all("send" not in path and "test" not in path for path in notification_paths)
    assert "ProductionNotificationIntent" not in json.dumps(app.openapi())
