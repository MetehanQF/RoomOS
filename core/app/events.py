import asyncio
import logging
from collections.abc import Callable

from fastapi import WebSocket

from .catalog import (
    LIGHT_BY_ENTITY,
    SUBSCRIBED_ENTITIES,
    SYSTEM_ENTITIES,
    node_id_for_entity,
)
from .config import Settings
from .ha_client import HomeAssistantClient, HomeAssistantUnavailable
from .models import RoomOSEvent
from .service import light_from_ha

logger = logging.getLogger(__name__)


class EventBroker:
    def __init__(self) -> None:
        self._clients: dict[WebSocket, Callable[[], None] | None] = {}

    async def connect(self, socket: WebSocket, validator: Callable[[], None] | None = None) -> None:
        await socket.accept()
        self._clients[socket] = validator

    def disconnect(self, socket: WebSocket) -> None:
        self._clients.pop(socket, None)

    async def broadcast(self, event: RoomOSEvent) -> None:
        dead: list[WebSocket] = []
        for socket, validator in tuple(self._clients.items()):
            try:
                if validator:
                    validator()
                await socket.send_json(event.model_dump(mode="json"))
            except Exception:
                try:
                    await socket.close(code=4401)
                except Exception:
                    pass
                dead.append(socket)
        for socket in dead:
            self.disconnect(socket)


class HAEventBridge:
    def __init__(
        self, settings: Settings, client: HomeAssistantClient, broker: EventBroker
    ) -> None:
        self.settings = settings
        self.client = client
        self.broker = broker

    async def run(self) -> None:
        backoff = self.settings.ha_reconnect_min_seconds
        while True:
            try:
                await self.broker.broadcast(
                    RoomOSEvent(type="roomos.connection", data={"home_assistant": "connecting"})
                )
                async for message in self.client.event_stream():
                    if message.get("type") == "result" and message.get("success"):
                        backoff = self.settings.ha_reconnect_min_seconds
                        logger.info("Home Assistant state_changed subscription active")
                        await self.broker.broadcast(
                            RoomOSEvent(
                                type="roomos.connection", data={"home_assistant": "connected"}
                            )
                        )
                        continue
                    event = message.get("event", {})
                    data = event.get("data", {})
                    entity_id = data.get("entity_id")
                    if entity_id not in SUBSCRIBED_ENTITIES:
                        continue
                    new_state = data.get("new_state")
                    if entity_id in LIGHT_BY_ENTITY:
                        definition = LIGHT_BY_ENTITY[entity_id]
                        await self.broker.broadcast(
                            RoomOSEvent(
                                type="light.state_changed",
                                device_id=definition.id,
                                data={
                                    "light": light_from_ha(definition, new_state).model_dump(
                                        mode="json"
                                    )
                                },
                            )
                        )
                    elif entity_id in SYSTEM_ENTITIES:
                        await self.broker.broadcast(
                            RoomOSEvent(
                                type="system.metric_updated",
                                device_id=node_id_for_entity(entity_id),
                                data={
                                    "metric": entity_id.split(".", 1)[1],
                                    "value": new_state.get("state") if new_state else None,
                                    "unit": (new_state or {}).get("attributes", {}).get(
                                        "unit_of_measurement"
                                    ),
                                    "last_updated": (new_state or {}).get("last_updated"),
                                },
                            )
                        )
            except asyncio.CancelledError:
                raise
            except HomeAssistantUnavailable:
                await self.broker.broadcast(
                    RoomOSEvent(type="roomos.connection", data={"home_assistant": "disconnected"})
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.settings.ha_reconnect_max_seconds)
