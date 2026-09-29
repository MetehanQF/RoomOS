import asyncio

import pytest

from app.config import Settings
from app.events import HAEventBridge
from app.ha_client import HomeAssistantUnavailable


class OfflineHA:
    async def event_stream(self):
        raise HomeAssistantUnavailable("offline")
        yield


class RecordingBroker:
    def __init__(self):
        self.events = []

    async def broadcast(self, event):
        self.events.append(event)


class FilterHA:
    async def event_stream(self):
        yield {"type": "result", "success": True}
        yield {
            "type": "event",
            "event": {"data": {"entity_id": "sensor.not_approved", "new_state": {"state": "secret"}}},
        }
        yield {
            "type": "event",
            "event": {
                "data": {
                    "entity_id": "light.living_room",
                    "new_state": {
                        "state": "off",
                        "attributes": {},
                        "last_updated": "2026-09-27T00:00:00Z",
                    },
                }
            },
        }
        raise asyncio.CancelledError


@pytest.mark.asyncio
async def test_websocket_bridge_uses_exponential_backoff(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    settings = Settings(
        HA_TOKEN="not-a-real-token",  # privacy-scan:allow test fixture
        ROOMOS_HA_RECONNECT_MIN_SECONDS=1,
        ROOMOS_HA_RECONNECT_MAX_SECONDS=5,
    )
    bridge = HAEventBridge(settings, OfflineHA(), RecordingBroker())
    with pytest.raises(asyncio.CancelledError):
        await bridge.run()
    assert sleeps == [1, 2, 4]


@pytest.mark.asyncio
async def test_event_bridge_filters_unapproved_entities_and_hides_ha_ids():
    broker = RecordingBroker()
    bridge = HAEventBridge(
        Settings(HA_TOKEN="not-a-real-token"),  # privacy-scan:allow test fixture
        FilterHA(),
        broker,
    )
    with pytest.raises(asyncio.CancelledError):
        await bridge.run()
    light_events = [event for event in broker.events if event.type == "light.state_changed"]
    assert len(light_events) == 1
    assert light_events[0].device_id == "living_room"
    assert "sensor.not_approved" not in str([event.model_dump() for event in broker.events])
    assert "light.living_room" not in str(light_events[0].model_dump())


def test_client_event_socket_uses_shared_connection_event(client):
    with client.websocket_connect("/api/v1/events") as socket:
        event = socket.receive_json()
    assert event["type"] == "roomos.connection"
    assert event["device_id"] is None
    assert event["data"]["core"] == "connected"
    assert "timestamp" in event
