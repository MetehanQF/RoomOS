import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.alerts import AlertEngine, AlertRecord
from app.notifications import (
    NotificationDestination,
    NotificationEngine,
)
from test_alerts import immediate_service_rules, system


# These tests exercise lifecycle/recovery/notification semantics, not the 7I service debounce.
pytestmark = pytest.mark.usefixtures("immediate_service_rules")


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 4, 10, 0, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, seconds: int):
        self.now += timedelta(seconds=seconds)


class FakeProvider:
    id = "fake"

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    async def send(self, notification, destination):
        self.calls.append((notification.id, destination.id))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class HangingProvider:
    id = "hanging"

    async def send(self, notification, destination):
        del notification, destination
        await asyncio.sleep(60)
        return "delivered"


def destination(*, id="tablet", activated_at=None, **values):
    fields = {
        "id": id,
        "display_name": id.replace("-", " ").title(),
        "activated_at": activated_at
        or datetime(2026, 10, 4, 9, 0, tzinfo=UTC),
    }
    fields.update(values)
    return NotificationDestination(**fields)


def alert(*, severity="warning", started_at=None, status="active"):
    now = started_at or datetime(2026, 10, 4, 10, 0, tzinfo=UTC)
    return AlertRecord(
        id="service.dns.degraded",
        type="service.degraded",
        severity=severity,
        title="DNS redundancy degraded",
        message="Only part of the DNS cluster is currently functional.",
        source="dns",
        started_at=now,
        updated_at=now,
        status=status,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("severity", ["warning", "critical"])
async def test_warning_and_critical_started_create_one_candidate(tmp_path, severity):
    engine = NotificationEngine(str(tmp_path / "notifications.json"))
    record = alert(severity=severity)
    first = await engine.handle_alert_started(record)
    second = await engine.handle_alert_started(record)
    assert first is not None
    assert second is not None
    assert first.id == second.id
    assert engine.summary().pending_count == 1
    serialized = first.model_dump_json()
    assert "entity_id" not in serialized
    assert "192.168" not in serialized


@pytest.mark.asyncio
async def test_info_and_resolved_are_ignored(tmp_path):
    engine = NotificationEngine(str(tmp_path / "notifications.json"))
    assert await engine.handle_alert_started(alert(severity="info")) is None
    assert await engine.handle_alert_started(alert(status="resolved")) is None
    assert engine.notifications() == []
    assert not (tmp_path / "notifications.json").exists()


@pytest.mark.asyncio
async def test_alert_engine_integration_deduplicates_and_new_lifecycle_notifies(tmp_path):
    clock = Clock()
    notifications = NotificationEngine(str(tmp_path / "notifications.json"), clock=clock)
    alerts = AlertEngine(str(tmp_path / "alerts.json"), clock=clock)
    alerts.set_notification_sink(notifications.handle_alert_started)
    await alerts.evaluate(system(dns="degraded"))
    await alerts.evaluate(system(dns="degraded"))
    assert len(notifications.notifications()) == 1
    await alerts.evaluate(system(dns="healthy"))
    clock.advance(1)
    await alerts.evaluate(system(dns="degraded"))
    assert len(notifications.notifications()) == 2
    assert len({item.dedupe_key for item in notifications.notifications()}) == 2


@pytest.mark.asyncio
async def test_notification_sink_failure_never_breaks_alert_lifecycle(tmp_path):
    alerts = AlertEngine(str(tmp_path / "alerts.json"))

    async def broken(_alert):
        raise RuntimeError("provider detail must stay isolated")

    alerts.set_notification_sink(broken)
    summary = await alerts.evaluate(system(dns="degraded"))
    assert summary.active_count == 1


@pytest.mark.asyncio
async def test_restart_preserves_outbox_and_mode_0600(tmp_path):
    path = tmp_path / "notifications.json"
    first = NotificationEngine(str(path))
    created = await first.handle_alert_started(alert())
    second = NotificationEngine(str(path))
    assert second.notifications()[0].id == created.id
    assert second.summary().pending_count == 1
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_corrupt_persistence_disables_candidates_and_delivery(tmp_path):
    path = tmp_path / "notifications.json"
    path.write_text('{"not":"valid"}', encoding="utf-8")
    original = path.read_bytes()
    provider = FakeProvider("delivered")
    engine = NotificationEngine(
        str(path),
        providers=[provider],
        destinations=[destination()],
    )
    assert engine.summary().status == "corrupt"
    assert await engine.handle_alert_started(alert()) is None
    assert await engine.dispatch_pending() == 0
    assert provider.calls == []
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_outbox_growth_is_bounded(tmp_path):
    clock = Clock()
    engine = NotificationEngine(
        str(tmp_path / "notifications.json"), queue_limit=3, clock=clock
    )
    for index in range(7):
        record = alert(started_at=clock())
        record.id = f"service.fixture.{index}"
        await engine.handle_alert_started(record)
        clock.advance(1)
    assert len(engine.notifications()) == 3


@pytest.mark.asyncio
async def test_temporary_failure_uses_bounded_retry_then_delivers(tmp_path):
    clock = Clock()
    provider = FakeProvider("temporary_failure", "delivered")
    engine = NotificationEngine(
        str(tmp_path / "notifications.json"),
        providers=[provider],
        destinations=[destination()],
        retry_base_seconds=10,
        max_attempts=3,
        clock=clock,
    )
    await engine.handle_alert_started(alert())
    assert await engine.dispatch_pending() == 1
    assert await engine.dispatch_pending() == 0
    clock.advance(10)
    assert await engine.dispatch_pending() == 1
    summary = engine.summary()
    assert summary.pending_count == 0
    assert summary.external_attempts == 2
    assert summary.external_deliveries == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "results",
    [
        ("permanent_failure",),
        (RuntimeError("temporary"), RuntimeError("temporary")),
    ],
)
async def test_permanent_or_exhausted_failure_never_retries_forever(tmp_path, results):
    clock = Clock()
    provider = FakeProvider(*results)
    engine = NotificationEngine(
        str(tmp_path / "notifications.json"),
        providers=[provider],
        destinations=[destination(id="phone")],
        retry_base_seconds=1,
        max_attempts=len(results),
        clock=clock,
    )
    await engine.handle_alert_started(alert())
    for _ in results:
        assert await engine.dispatch_pending() == 1
        clock.advance(2)
    assert await engine.dispatch_pending() == 0
    summary = engine.summary()
    assert summary.failed_count == 1
    assert summary.external_deliveries == 0


@pytest.mark.asyncio
async def test_provider_timeout_is_isolated_as_temporary_failure(tmp_path):
    engine = NotificationEngine(
        str(tmp_path / "notifications.json"),
        providers=[HangingProvider()],
        destinations=[destination(id="wear", provider="hanging")],
        provider_timeout_seconds=0.01,
        retry_base_seconds=10,
    )
    await engine.handle_alert_started(alert())
    assert await engine.dispatch_pending() == 1
    summary = engine.summary()
    assert summary.pending_count == 1
    assert summary.external_attempts == 1
    assert summary.external_deliveries == 0


@pytest.mark.asyncio
async def test_single_dispatcher_wakes_delivers_and_stops_cleanly(tmp_path):
    provider = FakeProvider("delivered")
    engine = NotificationEngine(
        str(tmp_path / "notifications.json"),
        providers=[provider],
        destinations=[
            destination(
                id="tablet",
                provider="fake",
                provider_target_ref="tablet-main",
            )
        ],
        dispatcher_max_sleep_seconds=60,
    )
    task = asyncio.create_task(engine.dispatcher_loop())
    await engine.handle_alert_started(alert())
    for _ in range(20):
        if engine.summary().external_deliveries == 1:
            break
        await asyncio.sleep(0.01)
    engine.stop()
    await asyncio.wait_for(task, timeout=1)
    assert provider.calls
    assert engine.summary().external_deliveries == 1


@pytest.mark.asyncio
async def test_disabled_dispatcher_makes_zero_provider_calls(tmp_path):
    provider = FakeProvider("delivered")
    engine = NotificationEngine(
        str(tmp_path / "notifications.json"),
        providers=[provider],
        destinations=[destination(id="disabled", enabled=False)],
    )
    await engine.dispatcher_loop()
    assert provider.calls == []


def test_production_shape_is_explicitly_disabled_without_provider(tmp_path):
    engine = NotificationEngine(str(tmp_path / "notifications.json"))
    summary = engine.summary()
    assert summary.model_dump(mode="json") == {
        "status": "disabled",
        "configured_providers": 0,
        "configured_destinations": 0,
        "pending_count": 0,
        "failed_count": 0,
        "external_attempts": 0,
        "external_deliveries": 0,
        "last_delivery": None,
    }


def test_invalid_enabled_configuration_is_unavailable_without_network(tmp_path):
    engine = NotificationEngine(
        str(tmp_path / "notifications.json"), configuration_status="unavailable"
    )
    assert engine.summary().status == "unavailable"
    assert engine.delivery_enabled is False


@pytest.mark.asyncio
async def test_destination_without_activation_is_fail_closed(tmp_path):
    provider = FakeProvider("delivered")
    engine = NotificationEngine(
        str(tmp_path / "notifications.json"),
        providers=[provider],
        destinations=[
            NotificationDestination(id="new-phone", display_name="New phone")
        ],
    )
    await engine.handle_alert_started(alert())
    assert engine.summary().configured_destinations == 0
    assert engine.delivery_enabled is False
    assert await engine.dispatch_pending() == 0
    assert provider.calls == []


@pytest.mark.asyncio
async def test_candidate_before_activation_is_never_retroactively_routed(tmp_path):
    path = tmp_path / "notifications.json"
    clock = Clock()
    before = NotificationEngine(str(path), clock=clock)
    await before.handle_alert_started(alert(started_at=clock()))
    clock.advance(60)

    provider = FakeProvider("delivered")
    after = NotificationEngine(
        str(path),
        providers=[provider],
        destinations=[destination(activated_at=clock())],
        clock=clock,
    )
    assert await after.dispatch_pending() == 0
    assert provider.calls == []
    assert after.notifications()[0].deliveries == []


@pytest.mark.asyncio
async def test_resolved_historical_lifecycle_is_not_delivered_to_new_destination(
    tmp_path,
):
    notification_path = tmp_path / "notifications.json"
    clock = Clock()
    notifications = NotificationEngine(str(notification_path), clock=clock)
    alerts = AlertEngine(str(tmp_path / "alerts.json"), clock=clock)
    alerts.set_notification_sink(notifications.handle_alert_started)
    await alerts.evaluate(system(dns="degraded"))
    await alerts.evaluate(system(dns="healthy"))
    clock.advance(60)

    provider = FakeProvider("delivered")
    restarted = NotificationEngine(
        str(notification_path),
        providers=[provider],
        destinations=[destination(activated_at=clock())],
        clock=clock,
    )
    assert await restarted.dispatch_pending() == 0
    assert provider.calls == []


@pytest.mark.asyncio
async def test_candidate_at_or_after_activation_is_eligible(tmp_path):
    clock = Clock()
    provider = FakeProvider("delivered", "delivered")
    engine = NotificationEngine(
        str(tmp_path / "notifications.json"),
        providers=[provider],
        destinations=[destination(activated_at=clock())],
        clock=clock,
    )
    at_boundary = await engine.handle_alert_started(alert(started_at=clock()))
    clock.advance(1)
    later = alert(started_at=clock())
    later.id = "service.dns.degraded.second-lifecycle"
    after_boundary = await engine.handle_alert_started(later)

    assert at_boundary.deliveries[0].destination_id == "tablet"
    assert after_boundary.deliveries[0].destination_id == "tablet"
    assert await engine.dispatch_pending() == 2
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_destinations_apply_independent_activation_boundaries(tmp_path):
    clock = Clock()
    provider = FakeProvider("delivered", "delivered", "delivered")
    engine = NotificationEngine(
        str(tmp_path / "notifications.json"),
        providers=[provider],
        destinations=[
            destination(id="already-active", activated_at=clock() - timedelta(seconds=1)),
            destination(id="future-active", activated_at=clock() + timedelta(seconds=1)),
        ],
        clock=clock,
    )
    first = await engine.handle_alert_started(alert(started_at=clock()))
    assert [item.destination_id for item in first.deliveries] == ["already-active"]

    clock.advance(2)
    second_alert = alert(started_at=clock())
    second_alert.id = "service.dns.degraded.next"
    second = await engine.handle_alert_started(second_alert)
    assert [item.destination_id for item in second.deliveries] == [
        "already-active",
        "future-active",
    ]


def test_activation_boundary_round_trips_and_requires_timezone():
    original = destination()
    restored = NotificationDestination.model_validate_json(original.model_dump_json())
    assert restored.activated_at == original.activated_at
    with pytest.raises(ValueError):
        destination(activated_at=datetime(2026, 10, 4, 10, 0))


@pytest.mark.asyncio
async def test_canary_has_fixed_payload_and_exactly_one_destination(tmp_path):
    clock = Clock()
    provider = FakeProvider("delivered")
    engine = NotificationEngine(
        str(tmp_path / "notifications.json"),
        providers=[provider],
        destinations=[
            destination(id="handset", provider="fake", activated_at=clock()),
            destination(id="tablet", provider="fake", activated_at=clock()),
        ],
        clock=clock,
    )
    canary = await engine.create_canary("handset")
    assert canary.event_type == "system.canary"
    assert canary.title == "RoomOS notification test"
    assert canary.body == "This is a one-time RoomOS delivery canary."
    assert [(item.provider_id, item.destination_id) for item in canary.deliveries] == [
        ("fake", "handset")
    ]
    assert await engine.dispatch_pending() == 1
    assert provider.calls == [(canary.id, "handset")]
    with pytest.raises(TypeError):
        await engine.create_canary("handset", title="arbitrary")
