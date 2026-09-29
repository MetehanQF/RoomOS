import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from collections.abc import Callable

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.responses import JSONResponse

from .audit import command_audit
from .auth import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    AuthContext,
    AuthRejected,
    AuthStore,
    PairingRateLimited,
    PairingRejected,
    security_audit,
)
from .catalog import LIGHTS
from .config import Settings, get_settings
from .events import EventBroker, HAEventBridge
from .ha_client import HomeAssistantClient, HomeAssistantUnavailable
from .models import (
    BrightnessRequest,
    ColorRequest,
    CommandResult,
    EffectRequest,
    GroupCommandResult,
    LogicalGroup,
    AuthDevice,
    AuthStatus,
    PairingRequest,
    PairingResult,
    PowerRequest,
    RoomOSEvent,
    TemperatureRequest,
)
from .policy import require_policy_permission, writable_permissions
from .service import (
    ha_unavailable_response,
    light_from_ha,
    light_or_404,
    list_lights,
    require_live_light_capability,
    set_all_power,
    set_power,
    system_node,
    system_overview,
    validate_kelvin,
    validate_live_effect,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    client = HomeAssistantClient(settings)
    broker = EventBroker()
    auth = AuthStore(settings)
    if settings.auth_enabled:
        auth.initialize()
    app.state.ha = client
    app.state.broker = broker
    app.state.auth = auth
    bridge_task = None
    if settings.ha_connect_enabled and settings.has_ha_credentials:
        bridge_task = asyncio.create_task(HAEventBridge(settings, client, broker).run())
    yield
    if bridge_task:
        bridge_task.cancel()
        await asyncio.gather(bridge_task, return_exceptions=True)
    await client.close()
    auth.close()


app = FastAPI(
    title="RoomOS Core API",
    version="0.1.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)


def get_ha() -> HomeAssistantClient:
    return app.state.ha


def get_auth() -> AuthStore:
    return app.state.auth


def _reject_security(event: str, detail: str = "Authentication required") -> None:
    security_audit(event, result="rejected")
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def _require_host(request: Request, settings: Settings) -> None:
    if request.headers.get("host", "").lower() != settings.auth_allowed_host.lower():
        security_audit("host_rejected", result="rejected")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Request rejected")


def _require_https(request: Request) -> None:
    if request.headers.get("x-forwarded-proto", request.url.scheme).lower() != "https":
        security_audit("authentication_rejected", result="insecure_transport")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="HTTPS is required")


def _require_origin(request: Request, settings: Settings) -> None:
    if request.headers.get("origin") != settings.auth_allowed_origin:
        security_audit("origin_rejected", result="rejected")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Request rejected")


async def require_authenticated_device(
    request: Request,
    settings: Settings = Depends(get_settings),
    auth: AuthStore = Depends(get_auth),
) -> AuthContext:
    if not settings.auth_enabled:
        return AuthContext("auth-disabled", "Auth disabled", frozenset({"lights:read", "lights:write", "system:read"}), "auth-disabled")
    _require_host(request, settings)
    _require_https(request)
    try:
        context = auth.authenticate(request.cookies.get(SESSION_COOKIE))
    except AuthRejected:
        _reject_security("authentication_rejected")
    request.state.device = context
    return context


def require_device_scope(scope: str) -> Callable:
    async def dependency(
        context: AuthContext = Depends(require_authenticated_device),
    ) -> AuthContext:
        if scope not in context.scopes:
            security_audit("authorization_rejected", device_id=context.device_id, result="insufficient_scope")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
        return context
    return dependency


require_lights_read = require_device_scope("lights:read")
require_lights_write = require_device_scope("lights:write")
require_system_read = require_device_scope("system:read")


async def require_write_security(
    request: Request,
    context: AuthContext = Depends(require_authenticated_device),
    settings: Settings = Depends(get_settings),
    auth: AuthStore = Depends(get_auth),
) -> AuthContext:
    if not settings.auth_enabled:
        return context
    _require_origin(request, settings)
    csrf_header = request.headers.get("x-csrf-token")
    csrf_cookie = request.cookies.get(CSRF_COOKIE)
    if not csrf_header or not csrf_cookie or not hmac.compare_digest(csrf_header, csrf_cookie) or not auth.validate_csrf(context, csrf_header):
        security_audit("csrf_rejected", device_id=context.device_id, result="rejected")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Request rejected")
    return context


def require_control_enabled(settings: Settings = Depends(get_settings)) -> None:
    if not settings.control_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="RoomOS control is disabled",
        )


@app.get("/healthz")
async def container_health():
    return {"status": "ok"}


@app.get("/api/v1/auth/status", response_model=AuthStatus)
async def auth_status(
    request: Request,
    settings: Settings = Depends(get_settings),
    auth: AuthStore = Depends(get_auth),
):
    if not settings.auth_enabled:
        return AuthStatus(authenticated=True, device=AuthDevice(id="auth-disabled", friendly_name="Auth disabled", scopes=["lights:read", "lights:write", "system:read"]))
    _require_host(request, settings)
    _require_https(request)
    try:
        context = auth.authenticate(request.cookies.get(SESSION_COOKIE))
    except AuthRejected:
        return AuthStatus(authenticated=False)
    return AuthStatus(
        authenticated=True,
        device=AuthDevice(id=context.device_id, friendly_name=context.friendly_name, scopes=sorted(context.scopes)),
    )


@app.post("/api/v1/auth/pair", response_model=PairingResult)
async def pair_device(
    body: PairingRequest,
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings),
    auth: AuthStore = Depends(get_auth),
):
    if not settings.auth_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    _require_host(request, settings)
    _require_https(request)
    _require_origin(request, settings)
    try:
        issued = auth.pair(body.code, body.friendly_name, "pairing")
    except PairingRateLimited:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Pairing temporarily unavailable") from None
    except PairingRejected:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Pairing code is invalid or expired") from None
    max_age = settings.auth_session_absolute_days * 86400
    response.set_cookie(
        SESSION_COOKIE,
        issued.token,
        max_age=max_age,
        secure=True,
        httponly=True,
        samesite="strict",
        path="/api",
    )
    response.set_cookie(
        CSRF_COOKIE,
        issued.csrf_token,
        max_age=max_age,
        secure=True,
        httponly=False,
        samesite="strict",
        path="/",
    )
    context = issued.context
    return PairingResult(device=AuthDevice(id=context.device_id, friendly_name=context.friendly_name, scopes=sorted(context.scopes)))


@app.get("/api/v1/health", dependencies=[Depends(require_authenticated_device)])
async def health(settings: Settings = Depends(get_settings)):
    ha_client = getattr(app.state, "ha", None)
    permissions = writable_permissions(settings)
    actions = sorted({action for allowed in permissions.values() for action in allowed})
    return {
        "status": "ok",
        "service": "roomos-core",
        "version": settings.app_version,
        "home_assistant": (
            "not_configured"
            if not settings.has_ha_credentials
            else "connected"
            if ha_client and ha_client.connected
            else "disconnected"
        ),
        "live_connection_enabled": settings.ha_connect_enabled,
        "control_enabled": settings.control_enabled,
        "control_policy": settings.control_policy,
        "writable_device_ids": sorted(permissions),
        "writable_actions": actions,
        "writable_permissions": {
            device_id: sorted(allowed) for device_id, allowed in sorted(permissions.items())
        },
    }


@app.get("/api/v1/lights", dependencies=[Depends(require_lights_read)])
async def get_lights(ha: HomeAssistantClient = Depends(get_ha)):
    try:
        return await list_lights(ha)
    except HomeAssistantUnavailable as exc:
        raise ha_unavailable_response(exc) from None


@app.post(
    "/api/v1/lights/all/power",
    response_model=GroupCommandResult,
    dependencies=[Depends(require_write_security), Depends(require_lights_write), Depends(require_control_enabled)],
)
async def all_power(
    body: PowerRequest,
    ha: HomeAssistantClient = Depends(get_ha),
    settings: Settings = Depends(get_settings),
):
    require_policy_permission(settings, "all_room_lights", "power")
    try:
        result = await set_all_power(ha, body.on)
        command_audit(
            "all_room_lights",
            "power",
            "success" if result.accepted else "partial_failure",
            succeeded=result.succeeded,
            failed=[failure.device_id for failure in result.failed],
        )
        if result.failed:
            return JSONResponse(
                status_code=status.HTTP_207_MULTI_STATUS,
                content=result.model_dump(mode="json"),
            )
        return result
    except HomeAssistantUnavailable as exc:
        command_audit("all_room_lights", "power", "failed")
        raise ha_unavailable_response(exc) from None


@app.get("/api/v1/groups/all_room_lights", response_model=LogicalGroup, dependencies=[Depends(require_lights_read)])
async def all_room_lights_group():
    return LogicalGroup(member_ids=[light.id for light in LIGHTS])


@app.get("/api/v1/lights/{light_id}", dependencies=[Depends(require_lights_read)])
async def get_light(light_id: str, ha: HomeAssistantClient = Depends(get_ha)):
    light = light_or_404(light_id)
    try:
        return light_from_ha(light, await ha.get_state(light.entity_id))
    except HomeAssistantUnavailable as exc:
        raise ha_unavailable_response(exc) from None


@app.post(
    "/api/v1/lights/{light_id}/power",
    response_model=CommandResult,
    dependencies=[Depends(require_write_security), Depends(require_lights_write), Depends(require_control_enabled)],
)
async def power(
    light_id: str,
    body: PowerRequest,
    ha: HomeAssistantClient = Depends(get_ha),
    settings: Settings = Depends(get_settings),
):
    require_policy_permission(settings, light_id, "power")
    light = light_or_404(light_id)
    try:
        await require_live_light_capability(ha, light, "power")
        await set_power(ha, light.entity_id, body.on)
        command_audit(light.id, "power", "success")
        return CommandResult(device_id=light.id)
    except HomeAssistantUnavailable as exc:
        command_audit(light.id, "power", "failed")
        raise ha_unavailable_response(exc) from None


@app.post(
    "/api/v1/lights/{light_id}/brightness",
    response_model=CommandResult,
    dependencies=[Depends(require_write_security), Depends(require_lights_write), Depends(require_control_enabled)],
)
async def brightness(
    light_id: str,
    body: BrightnessRequest,
    ha: HomeAssistantClient = Depends(get_ha),
    settings: Settings = Depends(get_settings),
):
    require_policy_permission(settings, light_id, "brightness")
    light = light_or_404(light_id)
    try:
        await require_live_light_capability(ha, light, "brightness")
        await ha.call_service(
            "light", "turn_on", {"entity_id": light.entity_id, "brightness": body.brightness}
        )
        command_audit(light.id, "brightness", "success")
        return CommandResult(device_id=light.id)
    except HomeAssistantUnavailable as exc:
        command_audit(light.id, "brightness", "failed")
        raise ha_unavailable_response(exc) from None


@app.post(
    "/api/v1/lights/{light_id}/color",
    response_model=CommandResult,
    dependencies=[Depends(require_write_security), Depends(require_lights_write), Depends(require_control_enabled)],
)
async def color(
    light_id: str,
    body: ColorRequest,
    ha: HomeAssistantClient = Depends(get_ha),
    settings: Settings = Depends(get_settings),
):
    require_policy_permission(settings, light_id, "color")
    light = light_or_404(light_id)
    payload = {"entity_id": light.entity_id}
    payload.update({"rgb_color": list(body.rgb)} if body.rgb else {"hs_color": [body.hue, body.saturation]})
    try:
        await require_live_light_capability(ha, light, "color")
        await ha.call_service("light", "turn_on", payload)
        command_audit(light.id, "color", "success")
        return CommandResult(device_id=light.id)
    except HomeAssistantUnavailable as exc:
        command_audit(light.id, "color", "failed")
        raise ha_unavailable_response(exc) from None


@app.post(
    "/api/v1/lights/{light_id}/temperature",
    response_model=CommandResult,
    dependencies=[Depends(require_write_security), Depends(require_lights_write), Depends(require_control_enabled)],
)
async def temperature(
    light_id: str,
    body: TemperatureRequest,
    ha: HomeAssistantClient = Depends(get_ha),
    settings: Settings = Depends(get_settings),
):
    require_policy_permission(settings, light_id, "temperature")
    light = light_or_404(light_id)
    validate_kelvin(light, body.kelvin)
    try:
        await require_live_light_capability(ha, light, "temperature")
        await ha.call_service(
            "light", "turn_on", {"entity_id": light.entity_id, "color_temp_kelvin": body.kelvin}
        )
        command_audit(light.id, "temperature", "success")
        return CommandResult(device_id=light.id)
    except HomeAssistantUnavailable as exc:
        command_audit(light.id, "temperature", "failed")
        raise ha_unavailable_response(exc) from None


@app.post(
    "/api/v1/lights/{light_id}/effect",
    response_model=CommandResult,
    dependencies=[Depends(require_write_security), Depends(require_lights_write), Depends(require_control_enabled)],
)
async def effect(
    light_id: str,
    body: EffectRequest,
    ha: HomeAssistantClient = Depends(get_ha),
    settings: Settings = Depends(get_settings),
):
    require_policy_permission(settings, light_id, "effect")
    light = light_or_404(light_id)
    try:
        current = await require_live_light_capability(ha, light, "effect")
        validate_live_effect(current, body.effect)
        await ha.call_service(
            "light", "turn_on", {"entity_id": light.entity_id, "effect": body.effect}
        )
        command_audit(light.id, "effect", "success")
        return CommandResult(device_id=light.id)
    except HomeAssistantUnavailable as exc:
        command_audit(light.id, "effect", "failed")
        raise ha_unavailable_response(exc) from None


@app.get("/api/v1/system", dependencies=[Depends(require_system_read)])
async def system(ha: HomeAssistantClient = Depends(get_ha)):
    try:
        return await system_overview(ha)
    except HomeAssistantUnavailable as exc:
        raise ha_unavailable_response(exc) from None


@app.get("/api/v1/system/{node_id}", dependencies=[Depends(require_system_read)])
async def system_node_detail(node_id: str, ha: HomeAssistantClient = Depends(get_ha)):
    try:
        return await system_node(ha, node_id)
    except HomeAssistantUnavailable as exc:
        raise ha_unavailable_response(exc) from None


@app.websocket("/api/v1/events")
async def events(
    socket: WebSocket,
    settings: Settings = Depends(get_settings),
    auth: AuthStore = Depends(get_auth),
):
    broker: EventBroker = app.state.broker
    token = socket.cookies.get(SESSION_COOKIE)
    if settings.auth_enabled:
        if socket.headers.get("host", "").lower() != settings.auth_allowed_host.lower():
            security_audit("websocket_host_rejected", result="rejected")
            await socket.close(code=4403)
            return
        if socket.headers.get("origin") != settings.auth_allowed_origin:
            security_audit("websocket_origin_rejected", result="rejected")
            await socket.close(code=4403)
            return
        if socket.headers.get("x-forwarded-proto", "").lower() != "https":
            security_audit("websocket_authentication_rejected", result="insecure_transport")
            await socket.close(code=4403)
            return
        try:
            context = auth.authenticate(token)
        except AuthRejected:
            security_audit("websocket_authentication_rejected", result="rejected")
            await socket.close(code=4401)
            return
        if not ({"lights:read", "system:read"} & context.scopes):
            security_audit("websocket_authorization_rejected", device_id=context.device_id, result="insufficient_scope")
            await socket.close(code=4403)
            return

        def validate_session() -> None:
            auth.authenticate(token)
    else:
        validate_session = lambda: None

    await broker.connect(socket, validate_session)
    await socket.send_json(
        RoomOSEvent(
            type="roomos.connection",
            data={
                "core": "connected",
                "home_assistant": (
                    "connected"
                    if app.state.ha.connected
                    else "connecting"
                    if settings.ha_connect_enabled and settings.has_ha_credentials
                    else "not_configured"
                ),
            },
        ).model_dump(mode="json")
    )
    try:
        while True:
            try:
                await asyncio.wait_for(socket.receive_text(), timeout=10)
            except TimeoutError:
                try:
                    validate_session()
                except AuthRejected:
                    security_audit("websocket_authentication_rejected", result="revoked_or_expired")
                    await socket.close(code=4401)
                    break
    except WebSocketDisconnect:
        pass
    finally:
        broker.disconnect(socket)
