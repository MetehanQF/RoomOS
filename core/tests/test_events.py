import asyncio

import pytest

from app.config import Settings
from app.events import EventBroker, HAEventBridge
from app.ha_client import HomeAssistantUnavailable
from app.models import RoomOSEvent


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


class CameraHA:
    async def event_stream(self):
        yield {"type": "result", "success": True}
        yield {
            "type": "event",
            "event": {"data": {"entity_id": "sensor.camera_status", "new_state": {"state": "PAUSED"}}},
        }
        raise asyncio.CancelledError


class RecordingCamera:
    def __init__(self):
        self.refreshes = 0

    async def refresh(self):
        self.refreshes += 1


class ReconcileScenes:
    def __init__(self):
        self.active_scene = "night"

    async def refresh_active(self, client):
        return False

    def reconcile_light(self, light):
        self.active_scene = None
        return True


class RecordingAutomations:
    def __init__(self):
        self.events = []

    async def handle_ha_event(self, event, *, origin="external", context=None):
        self.events.append((event, origin))
        return []


@pytest.mark.asyncio
async def test_websocket_bridge_uses_exponential_backoff(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    settings = Settings(
        HA_TOKEN="not-a-real-token",  # privacy-scan:allow fake test credential
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
        Settings(HA_TOKEN="not-a-real-token"),  # privacy-scan:allow fake test credential
        FilterHA(),
        broker,
    )
    with pytest.raises(asyncio.CancelledError):
        await bridge.run()
    light_events = [event for event in broker.events if event.type == "light.state_changed"]
    assert len(light_events) == 1
    assert light_events[0].device_id == "room_main"
    assert "sensor.not_approved" not in str([event.model_dump() for event in broker.events])
    assert "light.living_room" not in str(light_events[0].model_dump())


@pytest.mark.asyncio
async def test_light_change_broadcasts_server_reconciled_active_scene():
    broker = RecordingBroker()
    bridge = HAEventBridge(
        Settings(HA_TOKEN="not-a-real-token"),  # privacy-scan:allow fake test credential
        FilterHA(),
        broker,
        ReconcileScenes(),
    )
    with pytest.raises(asyncio.CancelledError):
        await bridge.run()
    scene_events = [event for event in broker.events if event.type == "scene.active_changed"]
    assert len(scene_events) == 1
    assert scene_events[0].data == {"active_scene": None}
    assert scene_events[0].device_id is None
    assert "light.living_room" not in str(scene_events[0].model_dump())


@pytest.mark.asyncio
async def test_light_change_reuses_bridge_for_logical_automation_trigger_without_raw_id():
    automations = RecordingAutomations()
    bridge = HAEventBridge(
        Settings(HA_TOKEN="not-a-real-token"), FilterHA(), RecordingBroker(),  # privacy-scan:allow fake test credential
        automations=automations,
    )
    with pytest.raises(asyncio.CancelledError):
        await bridge.run()
    assert len(automations.events) == 1
    event, origin = automations.events[0]
    assert event["target_id"] == "room_main" and event["new_state"] == "off"
    assert event["fresh"] is True and origin == "external"
    assert "light.living_room" not in str(event)


@pytest.mark.asyncio
async def test_camera_source_event_refreshes_canonical_model_without_raw_broadcast():
    broker = RecordingBroker()
    camera = RecordingCamera()
    bridge = HAEventBridge(Settings(HA_TOKEN="not-a-real-token"), CameraHA(), broker, camera=camera)  # privacy-scan:allow fake test credential
    with pytest.raises(asyncio.CancelledError):
        await bridge.run()
    assert camera.refreshes == 1
    assert "sensor.camera_status" not in str([event.model_dump() for event in broker.events])


def test_client_event_socket_uses_shared_connection_event(client):
    with client.websocket_connect("/api/v1/events") as socket:
        event = socket.receive_json()
    assert event["type"] == "roomos.connection"
    assert event["device_id"] is None
    assert event["data"]["core"] == "connected"
    assert "timestamp" in event


@pytest.mark.asyncio
async def test_routine_events_are_visible_only_to_routines_read_scope():
    class Socket:
        def __init__(self):
            self.messages = []

        async def accept(self):
            pass

        async def send_json(self, payload):
            self.messages.append(payload)

    broker = EventBroker()
    lights = Socket()
    routines = Socket()
    await broker.connect(lights, scopes=frozenset({"lights:read"}))
    await broker.connect(routines, scopes=frozenset({"routines:read"}))
    event = RoomOSEvent(type="routine.changed", data={"routine_id": "rt_safe"})
    await broker.broadcast(event, required_scope="routines:read")
    assert lights.messages == []
    assert routines.messages[0]["type"] == "routine.changed"


@pytest.mark.asyncio
async def test_automation_events_are_visible_only_to_routines_read_scope():
    class Socket:
        def __init__(self): self.messages = []
        async def accept(self): pass
        async def send_json(self, payload): self.messages.append(payload)

    broker = EventBroker()
    lights, routines = Socket(), Socket()
    await broker.connect(lights, scopes=frozenset({"lights:read"}))
    await broker.connect(routines, scopes=frozenset({"routines:read"}))
    event = RoomOSEvent(type="automation.changed", data={"automation_id": "au_safe"})
    await broker.broadcast(event, required_scope="routines:read")
    assert lights.messages == []
    assert routines.messages[0]["type"] == "automation.changed"


@pytest.mark.asyncio
async def test_camera_events_are_visible_only_to_camera_read_scope():
    class Socket:
        def __init__(self):
            self.messages = []

        async def accept(self):
            pass

        async def send_json(self, payload):
            self.messages.append(payload)

    broker = EventBroker()
    lights = Socket()
    camera = Socket()
    await broker.connect(lights, scopes=frozenset({"lights:read"}))
    await broker.connect(camera, scopes=frozenset({"camera:read"}))
    event = RoomOSEvent(type="camera.updated", device_id="room_camera", data={"camera": {"status": "offline"}})
    await broker.broadcast(event, required_scope="camera:read")
    assert lights.messages == []
    assert camera.messages[0]["type"] == "camera.updated"


@pytest.mark.asyncio
async def test_alert_events_are_visible_only_to_activity_read_scope():
    class Socket:
        def __init__(self):
            self.messages = []

        async def accept(self):
            pass

        async def send_json(self, payload):
            self.messages.append(payload)

    broker = EventBroker()
    system = Socket()
    activity = Socket()
    await broker.connect(system, scopes=frozenset({"system:read"}))
    await broker.connect(activity, scopes=frozenset({"activity:read"}))
    event = RoomOSEvent(
        type="alert.started",
        device_id="service.dns.degraded",
        data={"alert": {"title": "DNS redundancy degraded"}},
    )
    await broker.broadcast(event, required_scope="activity:read")
    assert system.messages == []
    assert activity.messages[0]["type"] == "alert.started"
    assert "192.168" not in str(activity.messages)
