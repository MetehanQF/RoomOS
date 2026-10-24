import asyncio
import json
import logging
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Any

import httpx
import websockets

from .config import Settings

logger = logging.getLogger(__name__)

HISTORY_MAX_BYTES = 2 * 1024 * 1024
HISTORY_TIMEOUT_SECONDS = 5.0


class HomeAssistantError(RuntimeError):
    """Sanitized Home Assistant integration error."""


class HomeAssistantUnavailable(HomeAssistantError):
    pass


class HomeAssistantClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0))
        self.connected = False

    def _headers(self) -> dict[str, str]:
        if not self.settings.has_ha_credentials:
            raise HomeAssistantUnavailable("Home Assistant credentials are not configured")
        return {
            "Authorization": f"Bearer {self.settings.ha_token.get_secret_value()}",
            "Content-Type": "application/json",
        }

    async def close(self) -> None:
        await self._http.aclose()

    async def get_state(self, entity_id: str) -> dict[str, Any] | None:
        try:
            response = await self._http.get(
                f"{self.settings.ha_rest_url}/api/states/{entity_id}", headers=self._headers()
            )
            if response.status_code == 404:
                self.connected = True
                return None
            response.raise_for_status()
            self.connected = True
            return response.json()
        except HomeAssistantUnavailable:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            self.connected = False
            logger.warning("Home Assistant state request failed (%s)", type(exc).__name__)
            raise HomeAssistantUnavailable("Home Assistant is unavailable") from None

    async def get_states(self, entity_ids: Sequence[str]) -> dict[str, dict[str, Any] | None]:
        results = await asyncio.gather(
            *(self.get_state(entity_id) for entity_id in entity_ids), return_exceptions=True
        )
        if results and all(isinstance(result, Exception) for result in results):
            raise HomeAssistantUnavailable("Home Assistant is unavailable")
        return {
            entity_id: result if isinstance(result, dict) else None
            for entity_id, result in zip(entity_ids, results, strict=True)
        }

    async def get_history(
        self,
        entity_ids: Sequence[str],
        start: datetime,
        end: datetime,
        *,
        max_bytes: int = HISTORY_MAX_BYTES,
        timeout_seconds: float = HISTORY_TIMEOUT_SECONDS,
    ) -> list[list[dict[str, Any]]]:
        """Read-only bounded recorder history. GET only; never shares the service-call path."""

        try:
            async with asyncio.timeout(timeout_seconds):
                async with self._http.stream(
                    "GET",
                    f"{self.settings.ha_rest_url}/api/history/period/{start.isoformat()}",
                    headers=self._headers(),
                    params={
                        "filter_entity_id": ",".join(entity_ids),
                        "end_time": end.isoformat(),
                        "minimal_response": "",
                        "no_attributes": "",
                        "significant_changes_only": "0",
                    },
                    timeout=httpx.Timeout(timeout_seconds, connect=3.0),
                ) as response:
                    response.raise_for_status()
                    declared = response.headers.get("content-length")
                    if declared is not None and declared.isdigit() and int(declared) > max_bytes:
                        raise HomeAssistantUnavailable("History response is too large")
                    payload = bytearray()
                    async for chunk in response.aiter_bytes():
                        payload.extend(chunk)
                        if len(payload) > max_bytes:
                            raise HomeAssistantUnavailable("History response is too large")
            data = json.loads(payload)
            if not isinstance(data, list) or not all(isinstance(series, list) for series in data):
                raise HomeAssistantUnavailable("History response is malformed")
            self.connected = True
            return data
        except HomeAssistantUnavailable:
            raise
        except (TimeoutError, httpx.HTTPError, ValueError) as exc:
            logger.warning("Home Assistant history request failed (%s)", type(exc).__name__)
            raise HomeAssistantUnavailable("Home Assistant history is unavailable") from None

    async def get_image(self, entity_id: str, *, max_bytes: int = 5 * 1024 * 1024) -> tuple[bytes, str] | None:
        try:
            async with self._http.stream(
                "GET",
                f"{self.settings.ha_rest_url}/api/image_proxy/{entity_id}",
                headers=self._headers(),
            ) as response:
                if response.status_code in {404, 503}:
                    self.connected = True
                    return None
                response.raise_for_status()
                media_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if media_type not in {"image/jpeg", "image/png", "image/webp"}:
                    raise HomeAssistantUnavailable("Camera image is unavailable")
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > max_bytes:
                        raise HomeAssistantUnavailable("Camera image is unavailable")
                self.connected = True
                return bytes(payload), media_type
        except HomeAssistantUnavailable:
            raise
        except httpx.HTTPError as exc:
            self.connected = False
            logger.warning("Home Assistant image request failed (%s)", type(exc).__name__)
            raise HomeAssistantUnavailable("Home Assistant is unavailable") from None

    async def call_service(
        self, domain: str, service: str, payload: dict[str, Any]
    ) -> list[dict[str, Any]]:
        try:
            response = await self._http.post(
                f"{self.settings.ha_rest_url}/api/services/{domain}/{service}",
                headers=self._headers(),
                json=payload,
            )
            response.raise_for_status()
            self.connected = True
            fields = sorted(key for key in payload if key != "entity_id")
            logger.info(
                "Home Assistant service call succeeded domain=%s service=%s fields=%s",
                domain,
                service,
                fields,
            )
            return response.json()
        except HomeAssistantUnavailable:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            self.connected = False
            logger.warning("Home Assistant service request failed (%s)", type(exc).__name__)
            raise HomeAssistantUnavailable("Home Assistant is unavailable") from None

    async def event_stream(self) -> AsyncIterator[dict[str, Any]]:
        token = self._headers()["Authorization"].removeprefix("Bearer ")
        try:
            async with websockets.connect(
                self.settings.ha_ws_url, open_timeout=5, close_timeout=3, ping_interval=20
            ) as socket:
                hello = await socket.recv()
                if '"type":"auth_required"' not in hello.replace(" ", ""):
                    raise HomeAssistantUnavailable("Unexpected Home Assistant handshake")
                await socket.send(__import__("json").dumps({"type": "auth", "access_token": token}))
                auth = __import__("json").loads(await socket.recv())
                if auth.get("type") != "auth_ok":
                    raise HomeAssistantUnavailable("Home Assistant authentication failed")
                self.connected = True
                logger.info("Home Assistant WebSocket authenticated")
                await socket.send(
                    __import__("json").dumps(
                        {"id": 1, "type": "subscribe_events", "event_type": "state_changed"}
                    )
                )
                while True:
                    yield __import__("json").loads(await socket.recv())
        except HomeAssistantUnavailable:
            raise
        except Exception as exc:
            self.connected = False
            logger.warning("Home Assistant WebSocket disconnected (%s)", type(exc).__name__)
            raise HomeAssistantUnavailable("Home Assistant WebSocket is unavailable") from None
