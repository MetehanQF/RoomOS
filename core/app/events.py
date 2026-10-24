from .auth import AuthorizationChanged, AuthRejected
from .presence import TRACKER
from .catalog import PRESENCE_ENTITY_ID
import asyncio
import logging
from datetime import UTC, datetime
from collections.abc import Callable
from typing import TYPE_CHECKING

from fastapi import WebSocket

from .catalog import (
    AUTOMATION_ENTITIES,
    CAMERA_ENTITIES,
    LIGHT_BY_ENTITY,
    NODE_METRIC_BY_ENTITY,
    SUBSCRIBED_ENTITIES,
    SYSTEM_ENTITIES,
)
from .config import Settings
from .ha_client import HomeAssistantClient, HomeAssistantUnavailable
from .models import RoomOSEvent
from .service import light_from_ha, metric_id_for

if TYPE_CHECKING:
    from .automations import AutomationEngine
    from .devices import DeviceRegistry
    from .windows_telemetry import WindowsTelemetryAdapter
    from .alerts import AlertEngine
    from .camera import CameraMonitor
    from .scenes import SceneEngine
    from .system_monitor import SystemMonitor
    from .routines import RoutineEngine

logger = logging.getLogger(__name__)


CLOSE_AUTH_REQUIRED = 4401
CLOSE_AUTHORIZATION_CHANGED = 4409
CLOSE_TRY_AGAIN = 1013
# Per-socket ceiling for waiting on the send lock plus handing one frame to the transport.
SEND_TIMEOUT_SECONDS = 5


class EventBroker:
    def __init__(self) -> None:
        self._clients: dict[
            WebSocket, tuple[Callable[[], None] | None, frozenset[str]]
        ] = {}
        self._send_locks: dict[WebSocket, asyncio.Lock] = {}

    async def connect(
        self,
        socket: WebSocket,
        validator: Callable[[], None] | None = None,
        scopes: frozenset[str] = frozenset(),
    ) -> None:
        await socket.accept()
        self._clients[socket] = (validator, scopes)
        self._send_locks[socket] = asyncio.Lock()

    def disconnect(self, socket: WebSocket) -> None:
        self._clients.pop(socket, None)
        self._send_locks.pop(socket, None)

    async def broadcast(self, event: RoomOSEvent, *, required_scope: str | None = None) -> None:
        targets = [
            (socket, validator)
            for socket, (validator, scopes) in tuple(self._clients.items())
            if required_scope is None or required_scope in scopes
        ]
        if not targets:
            return
        payload = event.model_dump(mode="json")
        # Deliveries run concurrently: one slow socket costs the caller at most one
        # send window instead of delaying every later socket. Each delivery owns its
        # failure handling, so one broken socket never aborts the others. Awaiting the
        # whole batch keeps per-socket order for successive broadcasts and leaves no
        # detached tasks behind.
        await asyncio.gather(*(self._deliver(socket, validator, payload) for socket, validator in targets))

    async def _deliver(self, socket: WebSocket, validator: Callable[[], None] | None, payload: dict) -> None:
        lock = self._send_locks.get(socket)
        if lock is None:
            return
        try:
            # Bound backpressure and serialize delivery per socket. Revalidate
            # AFTER waiting: a queued event must not inherit earlier permission.
            async with asyncio.timeout(SEND_TIMEOUT_SECONDS):
                async with lock:
                    if socket not in self._clients:
                        return
                    if validator:
                        validator()
                    # No intervening application await after authorization.
                    # Frames already handed to the transport cannot be recalled.
                    await socket.send_json(payload)
        except Exception as exc:
            self.disconnect(socket)
            # 4401 = re-pair (session revoked/expired); 4409 = reconnect with fresh
            # scopes; 1013 = transport/backpressure failure, reconnect later. Any of
            # them stops delivery before the event is sent.
            code = (CLOSE_AUTHORIZATION_CHANGED if isinstance(exc, AuthorizationChanged)
                    else CLOSE_AUTH_REQUIRED if isinstance(exc, AuthRejected) else CLOSE_TRY_AGAIN)
            try:
                async with asyncio.timeout(1):
                    await socket.close(code=code)
            except Exception:
                pass



class HAEventBridge:
    def __init__(
        self,
        settings: Settings,
        client: HomeAssistantClient,
        broker: EventBroker,
        scenes: "SceneEngine | None" = None,
        camera: "CameraMonitor | None" = None,
        system_monitor: "SystemMonitor | None" = None,
        alerts: "AlertEngine | None" = None,
        routines: "RoutineEngine | None" = None,
        automations: "AutomationEngine | None" = None,
        *,
        windows: "WindowsTelemetryAdapter | None" = None,
        devices: "DeviceRegistry | None" = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.broker = broker
        self.scenes = scenes
        self.camera = camera
        self.system_monitor = system_monitor
        self.alerts = alerts
        self.routines = routines
        self.automations = automations
        self.windows = windows
        self.devices = devices
        # Observability only: last bridge lifecycle state and when it was entered.
        self.state: str = "connecting"
        self.state_since = datetime.now(UTC)

    async def _broadcast_active_scene(self) -> None:
        if self.scenes is None:
            return
        await self.broker.broadcast(
            RoomOSEvent(
                type="scene.active_changed",
                data={"active_scene": self.scenes.active_scene},
            )
        )

    async def _publish_devices(self) -> None:
        if self.devices is not None:
            await self.devices.publish_changes()

    async def _windows_reset(self) -> None:
        if self.windows is not None:
            self.windows.reset()
            await self._publish_devices()

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.state_since = datetime.now(UTC)

    async def run(self) -> None:
        backoff = self.settings.ha_reconnect_min_seconds
        # Consecutive unexpected (non-transport) failures. Kept apart from ``backoff``
        # because a successful subscription resets that one: a bug that fails right
        # after every subscribe must still back off instead of spinning at the minimum.
        failure_streak = 0
        connected_at: float | None = None
        loop = asyncio.get_running_loop()
        while True:
            stream = None
            try:
                if self.routines is not None:
                    self.routines.presence_observer.reset()
                    await self.routines.publish_diagnostics()
                await self._windows_reset()
                self._set_state("connecting")
                await self.broker.broadcast(
                    RoomOSEvent(type="roomos.connection", data={"home_assistant": "connecting"})
                )
                stream = self.client.event_stream()
                async for message in stream:
                    if message.get("type") == "result" and message.get("success"):
                        backoff = self.settings.ha_reconnect_min_seconds
                        connected_at = loop.time()
                        logger.info("Home Assistant state_changed subscription active")
                        if self.scenes is not None and await self.scenes.refresh_active(self.client):
                            await self._broadcast_active_scene()
                        if self.alerts is not None:
                            await self.alerts.evaluate_ha_connection(True)
                        if self.system_monitor is not None:
                            self.system_monitor.notify_source_changed()
                        if self.routines is not None:
                            environment = await self.client.get_states(AUTOMATION_ENTITIES)
                            self.routines.bootstrap_environment(
                                environment.get(AUTOMATION_ENTITIES[0]),
                                environment.get(AUTOMATION_ENTITIES[1]),
                            )
                        if self.windows is not None:
                            # One bounded read per HA (re)connect; never triggered by API reads.
                            self.windows.bootstrap(await self.client.get_states(self.windows.entity_ids))
                            await self._publish_devices()
                        self._set_state("connected")
                        await self.broker.broadcast(
                            RoomOSEvent(
                                type="roomos.connection", data={"home_assistant": "connected"}
                            )
                        )
                        continue
                    event = message.get("event", {})
                    data = event.get("data", {})
                    entity_id = data.get("entity_id")
                    if self.routines is not None and entity_id == TRACKER:
                        self.routines.presence_observer.observe(data.get("new_state"), data.get("old_state"))
                        await self.routines.publish_diagnostics()
                        continue
                    if self.windows is not None and self.windows.handles(entity_id):
                        # Device registry only: never system.* events (System page refresh, 7D.1-S2).
                        self.windows.observe(entity_id, data.get("new_state"), data.get("old_state"))
                        await self._publish_devices()
                        continue
                    if self.routines is not None and entity_id == PRESENCE_ENTITY_ID:
                        self.routines.presence_observer.person(data.get("new_state"))
                    if entity_id not in SUBSCRIBED_ENTITIES:
                        continue
                    new_state = data.get("new_state")
                    if entity_id in AUTOMATION_ENTITIES:
                        if self.routines is not None:
                            await self.routines.handle_environment_event(entity_id, new_state)
                        continue
                    if entity_id in LIGHT_BY_ENTITY:
                        definition = LIGHT_BY_ENTITY[entity_id]
                        light = light_from_ha(definition, new_state)
                        if self.automations is not None:
                            old_state = data.get("old_state") or {}
                            context = event.get("context") or {}
                            await self.automations.handle_ha_event(
                                {
                                    "event_id": context.get("id") or event.get("time_fired") or (new_state or {}).get("last_updated"),
                                    "target_id": definition.id,
                                    "old_state": old_state.get("state"),
                                    "new_state": (new_state or {}).get("state"),
                                    "attributes": (new_state or {}).get("attributes") or {},
                                    "fresh": light.available,
                                },
                                origin="external",
                            )
                        await self.broker.broadcast(
                            RoomOSEvent(
                                type="light.state_changed",
                                device_id=definition.id,
                                data={
                                    "light": light.model_dump(mode="json")
                                },
                            )
                        )
                        if self.scenes is not None and self.scenes.reconcile_light(light):
                            await self._broadcast_active_scene()
                    else:
                        if entity_id in SYSTEM_ENTITIES:
                            await self.broker.broadcast(
                                RoomOSEvent(
                                    type="system.metric_updated",
                                    device_id=NODE_METRIC_BY_ENTITY.get(entity_id, ("system", ""))[0],
                                    data={
                                        "metric": metric_id_for(entity_id),
                                        "value": new_state.get("state") if new_state else None,
                                        "unit": (new_state or {}).get("attributes", {}).get(
                                            "unit_of_measurement"
                                        ),
                                        "last_updated": (new_state or {}).get("last_updated"),
                                    },
                                )
                            )
                            if self.system_monitor is not None:
                                self.system_monitor.notify_source_changed()
                        if entity_id in CAMERA_ENTITIES and self.camera is not None:
                            await self.camera.refresh()
            except asyncio.CancelledError:
                raise
            except HomeAssistantUnavailable:
                await self._disconnected()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.settings.ha_reconnect_max_seconds)
            except Exception as exc:
                # An unexpected error must not end live updates for the rest of the
                # process lifetime. Close the stream, report disconnected and reconnect
                # with exponential backoff. Only the class name is logged.
                stable = connected_at is not None and loop.time() - connected_at >= self.settings.ha_reconnect_max_seconds
                failure_streak = 1 if stable else failure_streak + 1
                delay = min(
                    max(backoff, self.settings.ha_reconnect_min_seconds * 2 ** (failure_streak - 1)),
                    self.settings.ha_reconnect_max_seconds,
                )
                logger.error(
                    "Home Assistant bridge failed unexpectedly error=%s; reconnecting in %.0f s",
                    type(exc).__name__, delay,
                )
                await self._close_stream(stream)
                stream = None
                connected_at = None
                await self._disconnected()
                await asyncio.sleep(delay)
            finally:
                await self._close_stream(stream)

    @staticmethod
    async def _close_stream(stream) -> None:
        aclose = getattr(stream, "aclose", None)
        if aclose is None:
            return
        try:
            await aclose()
        except Exception:
            pass

    async def _disconnected(self) -> None:
        if self.routines is not None:
            self.routines.presence_observer.reset()
            await self.routines.publish_diagnostics()
        await self._windows_reset()
        self._set_state("disconnected")
        if self.alerts is not None:
            await self.alerts.evaluate_ha_connection(False)
        await self.broker.broadcast(
            RoomOSEvent(type="roomos.connection", data={"home_assistant": "disconnected"})
        )
