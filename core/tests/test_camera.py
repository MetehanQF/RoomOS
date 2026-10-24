import asyncio
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
import httpx

import app.camera as camera_module
from app.camera import CameraMonitor, camera_status_from_states
from app.config import Settings
from app.ha_client import HomeAssistantClient, HomeAssistantUnavailable
from app.main import app, get_camera
from app.models import CameraCatalog, CameraStatus


NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


def state(value):
    return {"state": value, "attributes": {}, "last_updated": NOW.isoformat()}


def sources(*, watch="OK", frigate="running", event="2026-10-03 14:30:00", image="ready"):
    return {
        "sensor.camera_status": state(watch),
        "sensor.frigate_status": state(frigate),
        "input_datetime.camera_last_person": state(event),
        "input_text.camera_last_person_id": state("opaque-server-only"),
        "image.camera_person": state(image),
    }


def test_camera_online_and_recent_person_snapshot_are_derived_without_raw_ids(monkeypatch):
    # The HA input_datetime is naive installation-local time; +03:00 maps 14:30 to 11:30 UTC.
    monkeypatch.setattr(camera_module, "INSTALLATION_TZ", ZoneInfo("Europe/Istanbul"))
    camera = camera_status_from_states(sources(), now=NOW, recent_hours=24)
    assert camera.status == "online"
    assert camera.detection_status == "active"
    assert camera.frigate_health == "healthy"
    assert camera.last_person_event == datetime(2026, 10, 3, 11, 30, tzinfo=UTC)
    assert camera.last_person_event_freshness == "recent"
    assert camera.snapshot_available is True
    assert camera.live_view_available is False
    assert "camera_person" not in camera.model_dump_json().lower()


def test_camera_privacy_pause_is_offline_without_marking_frigate_offline():
    camera = camera_status_from_states(sources(watch="PAUSED"), now=NOW, recent_hours=24)
    assert camera.status == "offline"
    assert camera.detection_status == "paused"
    assert camera.frigate_health == "healthy"


def test_frigate_offline_is_separate_from_camera_health():
    camera = camera_status_from_states(sources(frigate="stopped"), now=NOW, recent_hours=24)
    assert camera.status == "online"
    assert camera.frigate_health == "offline"


@pytest.mark.parametrize("event", ["unknown", "unavailable", "malformed", ""])
def test_no_or_malformed_person_event_is_safe(event):
    camera = camera_status_from_states(sources(event=event), now=NOW, recent_hours=24)
    assert camera.last_person_event is None
    assert camera.last_person_event_freshness == "none"
    assert camera.snapshot_available is False


def test_stale_event_never_exposes_an_indefinitely_current_snapshot():
    camera = camera_status_from_states(
        sources(event="2026-09-29 11:36:29"), now=NOW, recent_hours=24
    )
    assert camera.last_person_event_freshness == "stale"
    assert camera.snapshot_available is False


def test_snapshot_missing_is_explicit_even_for_recent_event():
    camera = camera_status_from_states(
        sources(image="unavailable"), now=NOW, recent_hours=24
    )
    assert camera.last_person_event_freshness == "recent"
    assert camera.snapshot_available is False


@pytest.mark.asyncio
async def test_refresh_failure_is_unknown_and_does_not_crash_monitor(tmp_path):
    class FailedHA:
        async def get_states(self, _ids):
            raise HomeAssistantUnavailable("offline")

    monitor = CameraMonitor(Settings(), FailedHA(), clock=lambda: NOW)
    camera = await monitor.refresh()
    assert camera.status == "unknown"
    assert camera.frigate_health == "unknown"
    assert camera.last_checked == NOW


@pytest.mark.asyncio
async def test_refresh_is_locked_and_publishes_only_logical_camera_data():
    class TrackingHA:
        def __init__(self):
            self.active = 0
            self.peak = 0

        async def get_states(self, _ids):
            self.active += 1
            self.peak = max(self.peak, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            return sources()

    events = []
    ha = TrackingHA()
    async def publish(event):
        events.append(event)
    monitor = CameraMonitor(Settings(), ha, publish, clock=lambda: NOW)
    await asyncio.gather(monitor.refresh(), monitor.refresh())
    assert ha.peak == 1
    assert len(events) == 1
    assert events[0].type == "camera.updated"
    assert events[0].device_id == "room_camera"
    assert "entity" not in events[0].model_dump_json().lower()


class AvailableCamera:
    def catalog(self):
        return CameraCatalog(cameras=[CameraStatus(
            status="online", detection_status="active", frigate_health="healthy",
            last_person_event=NOW, last_person_event_freshness="recent",
            snapshot_available=True, live_view_available=False, last_checked=NOW,
        )])


def test_camera_catalog_and_snapshot_are_bounded_and_no_store(client):
    app.dependency_overrides[get_camera] = AvailableCamera
    try:
        catalog = client.get("/api/v1/cameras")
        assert catalog.status_code == 200
        payload = catalog.json()
        assert payload["cameras"][0]["id"] == "room_camera"
        assert payload["cameras"][0]["live_view_available"] is False
        assert "camera_person" not in catalog.text.lower()
        assert "entity_id" not in catalog.text

        snapshot = client.get("/api/v1/cameras/room_camera/snapshot")
        assert snapshot.status_code == 200
        assert snapshot.content == b"\xff\xd8\xff\xd9"
        assert snapshot.headers["cache-control"] == "no-store, private, max-age=0"
        assert snapshot.headers["pragma"] == "no-cache"
        assert snapshot.headers["x-content-type-options"] == "nosniff"

        assert client.get("/api/v1/cameras/camera.example_camera/snapshot").status_code == 404
        assert client.get("/api/v1/cameras/room_camera/snapshot/arbitrary-event").status_code == 404
    finally:
        app.dependency_overrides.pop(get_camera, None)


def test_cross_origin_camera_reads_are_rejected(client):
    assert client.get(
        "/api/v1/cameras", headers={"Origin": "https://evil.example"}
    ).status_code == 403
    assert client.get(
        "/api/v1/cameras", headers={"Sec-Fetch-Site": "cross-site"}
    ).status_code == 403


@pytest.mark.asyncio
async def test_ha_image_adapter_accepts_only_bounded_image_media():
    async def jpeg(_request):
        return httpx.Response(200, content=b"\xff\xd8\xff\xd9", headers={"Content-Type": "image/jpeg"})

    ha = HomeAssistantClient(Settings(HA_TOKEN="not-a-real-token"))  # privacy-scan:allow fake test credential
    await ha._http.aclose()
    ha._http = httpx.AsyncClient(transport=httpx.MockTransport(jpeg))
    assert await ha.get_image("image.approved") == (b"\xff\xd8\xff\xd9", "image/jpeg")
    await ha.close()


@pytest.mark.asyncio
async def test_ha_image_adapter_rejects_missing_malformed_and_oversized_media():
    for response, expected in (
        (httpx.Response(404), None),
        (httpx.Response(200, content=b"not-image", headers={"Content-Type": "text/html"}), "error"),
        (httpx.Response(200, content=b"12345", headers={"Content-Type": "image/jpeg"}), "oversized"),
    ):
        async def handler(_request, response=response):
            return response
        ha = HomeAssistantClient(Settings(HA_TOKEN="not-a-real-token"))  # privacy-scan:allow fake test credential
        await ha._http.aclose()
        ha._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        if expected is None:
            assert await ha.get_image("image.approved") is None
        else:
            with pytest.raises(HomeAssistantUnavailable):
                await ha.get_image("image.approved", max_bytes=4)
        await ha.close()
