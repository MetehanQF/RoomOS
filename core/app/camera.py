import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .catalog import (
    CAMERA_ENTITIES,
    CAMERA_IMAGE_ENTITY,
    CAMERA_LAST_PERSON_ENTITY,
    CAMERA_STATUS_ENTITY,
    FRIGATE_STATUS_ENTITY,
)
from .config import Settings
from .ha_client import HomeAssistantClient, HomeAssistantUnavailable
from .models import CameraCatalog, CameraStatus, RoomOSEvent

logger = logging.getLogger(__name__)
CAMERA_ID = "room_camera"
# Home Assistant input_datetime values are naive local times of the installation.
INSTALLATION_TZ = ZoneInfo(os.environ.get("ROOMOS_ROUTINES_TIMEZONE", "").strip() or "UTC")


def _state(states: Mapping[str, dict | None], entity_id: str) -> str | None:
    value = states.get(entity_id)
    if not value:
        return None
    raw = str(value.get("state", "")).strip()
    return None if raw.lower() in {"", "unknown", "unavailable", "none"} else raw


def _last_person(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return parsed.replace(tzinfo=INSTALLATION_TZ).astimezone(UTC)


def camera_status_from_states(
    states: Mapping[str, dict | None],
    *,
    now: datetime,
    recent_hours: int,
) -> CameraStatus:
    watch = (_state(states, CAMERA_STATUS_ENTITY) or "").upper()
    frigate = (_state(states, FRIGATE_STATUS_ENTITY) or "").lower()
    event_time = _last_person(_state(states, CAMERA_LAST_PERSON_ENTITY))
    image_ready = _state(states, CAMERA_IMAGE_ENTITY) is not None

    if watch == "OK":
        camera_status, detection = "online", "active"
    elif watch == "PAUSED":
        camera_status, detection = "offline", "paused"
    elif watch in {"WARNING", "CRITICAL", "ERROR", "OFFLINE", "FAILED"}:
        camera_status, detection = "offline", "offline"
    else:
        camera_status, detection = "unknown", "unknown"

    if frigate in {"running", "online", "healthy", "ok", "up"}:
        frigate_health = "healthy"
    elif frigate in {"degraded", "warning"}:
        frigate_health = "degraded"
    elif frigate in {"stopped", "offline", "failed", "down", "error"}:
        frigate_health = "offline"
    else:
        frigate_health = "unknown"

    recent_cutoff = now - timedelta(hours=recent_hours)
    event_freshness = (
        "none" if event_time is None else "recent" if event_time >= recent_cutoff else "stale"
    )
    return CameraStatus(
        status=camera_status,
        detection_status=detection,
        frigate_health=frigate_health,
        last_person_event=event_time,
        last_person_event_freshness=event_freshness,
        # Old image entities may keep the last frame indefinitely. Only a recent,
        # trusted person timestamp may make the bounded snapshot endpoint available.
        snapshot_available=bool(image_ready and event_freshness == "recent"),
        live_view_available=False,
        last_checked=now,
    )


def unknown_camera() -> CameraStatus:
    return CameraStatus(
        status="unknown",
        detection_status="unknown",
        frigate_health="unknown",
    )


class CameraMonitor:
    def __init__(
        self,
        settings: Settings,
        client: HomeAssistantClient,
        publish: Callable[[RoomOSEvent], Awaitable[None]] | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.publish = publish
        self.clock = clock or (lambda: datetime.now(UTC))
        self._camera = unknown_camera()
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()

    def catalog(self) -> CameraCatalog:
        return CameraCatalog(cameras=[self._camera.model_copy(deep=True)])

    async def refresh(self) -> CameraStatus:
        async with self._lock:
            previous = self._camera.model_dump(exclude={"last_checked"})
            try:
                states = await self.client.get_states(CAMERA_ENTITIES)
                current = camera_status_from_states(
                    states,
                    now=self.clock(),
                    recent_hours=self.settings.camera_event_recent_hours,
                )
            except HomeAssistantUnavailable:
                current = unknown_camera().model_copy(update={"last_checked": self.clock()})
            self._camera = current
            changed = previous != current.model_dump(exclude={"last_checked"})
            if changed and self.publish is not None:
                await self.publish(
                    RoomOSEvent(
                        type="camera.updated",
                        device_id=CAMERA_ID,
                        data={"camera": current.model_dump(mode="json")},
                    )
                )
            return current.model_copy(deep=True)

    async def run(self) -> None:
        try:
            await asyncio.wait_for(
                self._stop.wait(), timeout=self.settings.camera_refresh_startup_delay_seconds
            )
            return
        except TimeoutError:
            pass
        while not self._stop.is_set():
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Camera refresh failed (%s)", type(exc).__name__)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.settings.camera_refresh_interval_seconds
                )
            except TimeoutError:
                continue

    def stop(self) -> None:
        self._stop.set()
