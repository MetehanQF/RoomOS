import asyncio
import hmac
import json
import logging
import time
from collections import defaultdict, deque
from pathlib import Path
from contextlib import asynccontextmanager
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .audit import command_audit
from .alerts import (
    ActivityListResponse,
    AlertEngine,
    AlertListResponse,
    AlertRecord,
    NotificationCenterResponse,
    NotificationMuteRequest,
)
from .auth import (
    CSRF_COOKIE,
    NATIVE_READONLY_SCOPES,
    SESSION_COOKIE,
    AuthContext,
    AuthRejected,
    AuthorizationChanged,
    AuthStore,
    PairingRateLimited,
    PairingRejected,
    security_audit,
)
from .catalog import CAMERA_IMAGE_ENTITY, LIGHTS, MAIN_GROUP, MAIN_LIGHT_IDS, SUBSCRIBED_ENTITIES
from .camera import CAMERA_ID, CameraMonitor
from .config import Settings, get_settings
from .dns_health import DnsHealthMonitor
from .events import CLOSE_AUTH_REQUIRED, CLOSE_AUTHORIZATION_CHANGED, EventBroker, HAEventBridge
from .fcm import load_fcm_runtime, register_fcm_target
from .resource_policy import SNOOZE_HOURS, AlertPolicyStore
from .push import (
    PushCapabilities,
    PushEngine,
    PushPreferencesUpdate,
    PushReceiptRequest,
    PushSettingsResponse,
    WebPushRotateRequest,
    WebPushSubscriptionRequest,
    fingerprint,
    settings_view,
    shadow_intent_sink,
)
from .push_providers import allowed_endpoint, valid_subscription_keys
from .push_runtime import adopt_legacy_fcm, build_fcm_provider, build_push_runtime, extra_hosts
from .ha_client import HomeAssistantClient, HomeAssistantUnavailable
from .background import BackgroundTasks
from .backup_status import BackupStatusReader
from .health import HealthResponse, build_health, collect_inputs
from .history import HistoryQuery, HistoryResponse, HistoryService, HistoryUnavailable
from .metrics_history import FORECAST_METRICS, MetricsHistory, METRICS
from .models import (
    ClientTelemetryAccepted,
    PairedClient,
    PairedClientsResponse,
    BrightnessRequest,
    CameraCatalog,
    ColorRequest,
    CommandResult,
    EffectRequest,
    GroupCommandResult,
    LogicalGroup,
    AuthDevice,
    AuthStatus,
    PairingRequest,
    PairingResult,
    NativePairingResult,
    FcmRegistrationRequest,
    FcmRegistrationResponse,
    PowerRequest,
    ReleaseInfo,
    RoomOSEvent,
    SystemResponse,
    TemperatureRequest,
)
from .notifications import NotificationEngine
from .policy import require_policy_permission, writable_permissions
from .panel_preferences import PanelLayout, PanelLayoutResponse
from .production_notifications import ProductionNotificationPolicy
from .routine_evaluations import EvaluationStore
from .devices import CATALOG as DEVICE_CATALOG, DEVICE_SCOPES, DeviceDetail, DeviceListResponse, DeviceRegistry, DeviceTelemetryResponse
from .windows_telemetry import WindowsTelemetryAdapter, configured_catalog, parse_config
from .client_telemetry import (
    MAX_BODY_BYTES,
    TELEMETRY_SCOPE,
    ClientTelemetryIngest,
    TelemetryRejected,
    parse_bindings,
)
from .client_telemetry import configured_catalog as client_configured_catalog
from .executions import ExecutionStore
from .scenes import SceneActivationResult, SceneEngine, SceneListResponse
from .routines import (
    Routine,
    RoutineCreate,
    RoutineEngine,
    RoutineListResponse,
    RoutineRunResponse,
    RoutineUpdate,
)
from .automations import (
    AutomationCreate,
    AutomationEngine,
    AutomationUpdate,
    ConditionContext,
    LightAction as AutomationLightAction,
    RoutineAction as AutomationRoutineAction,
    SceneAction as AutomationSceneAction,
    build_condition_context,
    device_trigger_snapshot,
)
from .service import (
    command_main_lights,
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
from .system_monitor import SystemMonitor
from .smart_alerts import (
    background_observations,
    device_observations,
    health_observations,
    routine_observation,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    client = HomeAssistantClient(settings)
    broker = EventBroker()
    # Every long-running task is started through this registry so Health sees all of them.
    background = BackgroundTasks()
    app.state.background_tasks = background
    auth = AuthStore(settings)
    if settings.auth_enabled:
        auth.initialize()
    app.state.ha = client
    app.state.broker = broker
    app.state.auth = auth
    app.state.executions = ExecutionStore(settings.executions_path)
    app.state.backup_status = BackupStatusReader(settings.backup_status_path)
    await app.state.executions.initialize()
    app.state.scenes = SceneEngine(settings, executions=app.state.executions)
    app.state.fcm = load_fcm_runtime(
        enabled=settings.fcm_enabled,
        credentials_path=settings.fcm_credentials_path,
        destinations_path=settings.notification_destinations_path,
        targets_path=settings.fcm_targets_path,
        timeout_seconds=settings.notifications_provider_timeout_seconds,
    )
    app.state.notifications = NotificationEngine(
        settings.notifications_path,
        providers=app.state.fcm.providers,
        destinations=app.state.fcm.destinations,
        queue_limit=settings.notifications_queue_limit,
        max_attempts=settings.notifications_max_attempts,
        retry_base_seconds=settings.notifications_retry_base_seconds,
        provider_timeout_seconds=settings.notifications_provider_timeout_seconds,
        configuration_status=app.state.fcm.status,
        dispatcher_max_sleep_seconds=(
            settings.notifications_dispatcher_max_sleep_seconds
        ),
    )
    async def publish_activity_event(event: RoomOSEvent) -> None:
        await broker.broadcast(event, required_scope="activity:read")
    app.state.alerts = AlertEngine(
        settings.alerts_path,
        publish_activity_event,
        history_limit=settings.alerts_history_limit,
        confirmation_seconds=settings.alerts_node_confirmation_seconds,
    )
    # 7I: workload-aware resource policy, usage profiles and per-alert snooze (additive state file).
    app.state.alert_policy = AlertPolicyStore(settings.alert_policy_path)
    app.state.alerts.policy = app.state.alert_policy
    async def observe_background_task(name: str, state: str) -> None:
        await app.state.alerts.evaluate_observations(
            background_observations(((name, state),))
        )
    background.listeners.append(observe_background_task)
    # 7G: in smart/off mode the 7F incident policy keeps evaluating (rollback continuity) but
    # queues nothing; AlertEngine lifecycles are the only push source.
    app.state.notification_policy = ProductionNotificationPolicy(
        settings.notification_incidents_path,
        app.state.notifications.handle_production_intent
        if settings.push_mode == "legacy" else shadow_intent_sink,
    )
    app.state.scenes.set_activity_sink(app.state.alerts.record_scene_activation)
    for record in app.state.executions.recovered:
        await app.state.scenes.publish_execution(record)
    async def publish_system_event(event: RoomOSEvent) -> None:
        await broker.broadcast(event, required_scope="system:read")
        monitor = getattr(app.state, "system_monitor", None)
        if monitor is not None:
            monitor.notify_source_changed()
    app.state.dns_health = DnsHealthMonitor(settings, publish_system_event)
    dns_task = (
        background.start("dns_health", app.state.dns_health.run())
        if settings.dns_monitor_enabled
        else background.disabled("dns_health")
    )
    app.state.system_monitor = SystemMonitor(
        client,
        app.state.dns_health,
        app.state.alerts,
        publish_system_event,
        app.state.notification_policy,
        interval_seconds=settings.system_monitor_interval_seconds,
        startup_delay_seconds=settings.system_monitor_startup_delay_seconds,
    )
    app.state.history = HistoryService(snapshot=app.state.system_monitor.snapshot)
    async def publish_device_event(event: RoomOSEvent, required_scope: str) -> None:
        await broker.broadcast(event, required_scope=required_scope)
    windows_config = parse_config(
        settings.devices_workstation_agent_id, settings.devices_workstation_entities,
        settings.devices_windows_heartbeat_seconds, reserved_entities=SUBSCRIBED_ENTITIES,
    )
    catalog, bindings = configured_catalog(windows_config)
    client_bindings = parse_bindings(settings.devices_client_telemetry, catalog)
    catalog, phone_bindings = client_configured_catalog(catalog, client_bindings)
    app.state.devices = DeviceRegistry(settings.device_stale_thresholds(), publish_device_event,
                                       catalog=catalog, bindings={**bindings, **phone_bindings})
    app.state.metrics_history = await asyncio.to_thread(MetricsHistory, settings.metrics_history_path)
    app.state.devices.snapshot_listeners.append(app.state.metrics_history.observe)
    app.state.push = build_push_runtime(
        settings,
        access=auth.delivery_access if settings.auth_enabled else (lambda _device: None),
        alert_lookup=app.state.alerts.alert_by_id,
        bindings=client_bindings,
        suppressed=app.state.alert_policy.suppresses,
    )
    await adopt_legacy_fcm(app.state.push.engine, settings)
    if settings.push_mode == "smart":
        app.state.alerts.add_lifecycle_sink(app.state.push.engine.handle_lifecycle)
        notification_dispatcher_task = (
            background.start("notification_dispatcher", app.state.push.engine.dispatcher_loop())
            if app.state.push.engine.delivery_enabled
            else background.disabled("notification_dispatcher")
        )
    else:
        notification_dispatcher_task = (
            background.start("notification_dispatcher", app.state.notifications.dispatcher_loop())
            if settings.push_mode == "legacy" and app.state.notifications.delivery_enabled
            else background.disabled("notification_dispatcher")
        )
    def workstation_free_gb() -> float | None:
        return getattr(getattr(app.state, "windows_telemetry", None), "system_drive_free_gb", None)
    async def observe_device(detail: DeviceDetail) -> None:
        observations = device_observations(detail, app.state.alert_policy, free_gb=workstation_free_gb())
        if observations:
            await app.state.alerts.evaluate_observations(observations)
    app.state.devices.change_listeners.append(observe_device)
    app.state.client_telemetry = ClientTelemetryIngest(
        app.state.devices, client_bindings,
        min_interval_seconds=settings.devices_client_telemetry_min_interval_seconds,
    )
    app.state.windows_telemetry = (
        WindowsTelemetryAdapter(app.state.devices, windows_config) if windows_config else None
    )
    if app.state.windows_telemetry is not None:
        app.state.devices.tickers.append(app.state.windows_telemetry.tick)
    app.state.system_monitor.snapshot_listeners.append(app.state.devices.on_system_snapshot)
    devices_task = background.start("devices", app.state.devices.run())
    system_task = (
        background.start("system_monitor", app.state.system_monitor.run())
        if settings.ha_connect_enabled and settings.has_ha_credentials
        else background.disabled("system_monitor")
    )
    async def publish_camera_event(event: RoomOSEvent) -> None:
        await broker.broadcast(event, required_scope="camera:read")
    app.state.camera = CameraMonitor(settings, client, publish_camera_event)
    camera_task = (
        background.start("camera", app.state.camera.run())
        if settings.ha_connect_enabled and settings.has_ha_credentials
        else background.disabled("camera")
    )
    async def publish_routine_event(event: RoomOSEvent) -> None:
        await broker.broadcast(event, required_scope="routines:read")
    app.state.routine_evaluations = EvaluationStore(settings.routine_evaluations_path)
    async def observe_routine_evaluation(evaluation) -> None:
        await app.state.alerts.evaluate_observations([routine_observation(evaluation)])
    app.state.routine_evaluations.listeners.append(observe_routine_evaluation)
    app.state.routines = RoutineEngine(
        settings,
        app.state.scenes,
        client,
        publish_routine_event,
        evaluations=app.state.routine_evaluations,
    )
    async def execute_automation_action(action, _context):
        if isinstance(action, AutomationSceneAction):
            result = await app.state.scenes.activate(
                action.scene_id, client, source="routine", routine_name="Smart Automation",
                trigger="automation",
            )
            if result.status == "failed":
                raise HTTPException(status_code=503, detail="Scene action failed")
            return result.model_dump(mode="json")
        if isinstance(action, AutomationRoutineAction):
            result = await app.state.routines.run(action.routine_id, source="manual")
            if result.routine.last_result and result.routine.last_result.status == "failed":
                raise HTTPException(status_code=503, detail="Routine action failed")
            return result.model_dump(mode="json")
        if not isinstance(action, AutomationLightAction):
            raise HTTPException(status_code=422, detail="Unsupported automation action")
        require_policy_permission(settings, action.target_id, action.operation)
        payload: dict[str, object] = {}
        service = "turn_on"
        if action.operation == "power":
            service = "turn_on" if action.on else "turn_off"
        elif action.operation == "brightness":
            payload["brightness"] = action.brightness
        elif action.operation == "color":
            payload["rgb_color"] = list(action.rgb or ())
        elif action.operation == "temperature":
            payload["color_temp_kelvin"] = action.kelvin
        if action.target_id == "main_room_lights":
            result = await command_main_lights(client, service, payload)
            if result.failed:
                raise HTTPException(status_code=503, detail="Group action failed")
            return result.model_dump(mode="json")
        light = light_or_404(action.target_id)
        await require_live_light_capability(client, light, action.operation)
        await client.call_service("light", service, {"entity_id": light.entity_id, **payload})
        return {"status": "success", "device_id": light.id}

    def scene_action_effects(scene_id: str) -> dict[str, Literal["on", "off"]]:
        scene = app.state.scenes.configured.get(scene_id)
        if scene is None:
            return {}
        return {
            target.device_id: "off" if target.power is False else "on"
            for target in scene.targets
        }

    def automation_action_effects(action) -> dict[str, Literal["on", "off"]]:
        if isinstance(action, AutomationSceneAction):
            return scene_action_effects(action.scene_id)
        if isinstance(action, AutomationRoutineAction):
            routine = next(
                (item for item in app.state.routines.document.routines if item.id == action.routine_id),
                None,
            )
            return scene_action_effects(routine.action.scene_id) if routine is not None else {}
        if isinstance(action, AutomationLightAction):
            targets = MAIN_LIGHT_IDS if action.target_id == "main_room_lights" else (action.target_id,)
            expected: Literal["on", "off"] = (
                "off" if action.operation == "power" and action.on is False else "on"
            )
            return {target_id: expected for target_id in targets}
        return {}

    async def automation_condition_context() -> ConditionContext:
        return await build_condition_context(
            client,
            app.state.devices,
            ZoneInfo(settings.routines_timezone),
        )

    app.state.automations = AutomationEngine(
        settings,
        action_executor=execute_automation_action,
        publish=publish_routine_event,
        action_effects=automation_action_effects,
        context_provider=automation_condition_context,
    )
    async def observe_automation_device(detail: DeviceDetail) -> None:
        snapshot = device_trigger_snapshot(detail.model_dump(mode="json"))
        snapshot["event_id"] = f"{detail.id}:{detail.connection}:{detail.telemetry_freshness}:{detail.last_telemetry_at}"
        await app.state.automations.handle_device_event(snapshot)
    app.state.devices.change_listeners.append(observe_automation_device)
    automation_scheduler_task = background.start(
        "smart_automations", app.state.automations.scheduler_loop()
    )
    scheduler_task = background.start("scheduler", app.state.routines.scheduler_loop())
    diagnostics_task = background.start("routines.diagnostics", app.state.routines.diagnostics_loop())
    async def observe_runtime_health(_snapshot: SystemResponse) -> None:
        response = build_health(collect_inputs(app.state, settings))
        observations = health_observations(response)
        observations.extend(background_observations(background.snapshot()))
        # 7I: the existing 60 s System cadence also samples Workstation, so sustained windows have real
        # per-minute coverage even when Home Assistant reports no state change. No new task.
        workstation = app.state.devices.detail("workstation", frozenset({"devices:read"}))
        observations.extend(device_observations(workstation, app.state.alert_policy, free_gb=workstation_free_gb()))
        await app.state.alerts.evaluate_observations(observations)
        app.state.alert_policy.maybe_persist_windows()
    app.state.system_monitor.snapshot_listeners.append(observe_runtime_health)
    bridge_task = background.disabled("ha_bridge")
    app.state.ha_bridge = None
    if settings.ha_connect_enabled and settings.has_ha_credentials:
        app.state.ha_bridge = HAEventBridge(
            settings,
            client,
            broker,
            app.state.scenes,
            app.state.camera,
            app.state.system_monitor,
            app.state.alerts,
            app.state.routines,
            app.state.automations,
            windows=app.state.windows_telemetry,
            devices=app.state.devices,
        )
        bridge_task = background.start("ha_bridge", app.state.ha_bridge.run())
    yield
    # From here on, task exits are the expected shutdown, not failures.
    background.begin_shutdown()
    if bridge_task:
        bridge_task.cancel()
        await asyncio.gather(bridge_task, return_exceptions=True)
    if dns_task:
        app.state.dns_health.stop()
        await asyncio.gather(dns_task, return_exceptions=True)
    if system_task:
        app.state.system_monitor.stop()
        await asyncio.gather(system_task, return_exceptions=True)
    if camera_task:
        app.state.camera.stop()
        await asyncio.gather(camera_task, return_exceptions=True)
    app.state.devices.stop()
    await asyncio.gather(devices_task, return_exceptions=True)
    app.state.routines.stop()
    app.state.automations.stop()
    scheduler_task.cancel()
    automation_scheduler_task.cancel()
    diagnostics_task.cancel()
    await asyncio.gather(
        scheduler_task, automation_scheduler_task, diagnostics_task, return_exceptions=True
    )
    if notification_dispatcher_task:
        app.state.notifications.stop()
        app.state.push.engine.stop()
        await asyncio.gather(notification_dispatcher_task, return_exceptions=True)
    await app.state.push.close()
    app.state.alert_policy.persist()
    await app.state.fcm.close()
    await client.close()
    await asyncio.to_thread(app.state.metrics_history.close)
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


def get_scenes() -> SceneEngine:
    return app.state.scenes


def get_routines() -> RoutineEngine:
    return app.state.routines


def get_automations() -> AutomationEngine:
    return app.state.automations


def get_dns_health() -> DnsHealthMonitor:
    return app.state.dns_health


def get_camera() -> CameraMonitor:
    return app.state.camera


def get_alerts() -> AlertEngine:
    return app.state.alerts


def get_notifications() -> NotificationEngine:
    return app.state.notifications


def get_system_monitor() -> SystemMonitor:
    return app.state.system_monitor


def get_history() -> HistoryService:
    return app.state.history


def get_devices() -> DeviceRegistry:
    return app.state.devices


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


def _bearer_token(value: str | None) -> str | None:
    if value is None:
        return None
    parts = value.split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        raise AuthRejected
    return parts[1]


def _request_auth_token(request: Request) -> str | None:
    authorization = request.headers.get("authorization")
    if authorization is not None:
        return _bearer_token(authorization)
    return request.cookies.get(SESSION_COOKIE)


async def require_authenticated_device(
    request: Request,
    settings: Settings = Depends(get_settings),
    auth: AuthStore = Depends(get_auth),
) -> AuthContext:
    if not settings.auth_enabled:
        return AuthContext(
            "auth-disabled",
            "Auth disabled",
            frozenset({"lights:read", "lights:write", "system:read", "routines:read", "routines:write", "camera:read", "activity:read", "devices:read", "alerts:ack", "notifications:self"}),
            "auth-disabled",
        )
    _require_host(request, settings)
    _require_https(request)
    try:
        context = auth.authenticate(_request_auth_token(request))
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
require_routines_read = require_device_scope("routines:read")
require_routines_write = require_device_scope("routines:write")
require_camera_read = require_device_scope("camera:read")
require_activity_read = require_device_scope("activity:read")


class AutomationEnabledRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


async def require_devices_read(
    context: AuthContext = Depends(require_authenticated_device),
) -> AuthContext:
    """Servers need system:read and personal devices devices:read; per-device filtering follows."""
    if not (DEVICE_SCOPES & context.scopes):
        security_audit("authorization_rejected", device_id=context.device_id, result="insufficient_scope")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
    return context


async def require_devices_scope(
    context: AuthContext = Depends(require_authenticated_device),
) -> AuthContext:
    """Device Center data that is not a server view (paired clients) needs devices:read."""
    if "devices:read" not in context.scopes:
        security_audit("authorization_rejected", device_id=context.device_id, result="insufficient_scope")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
    return context


async def require_camera_request_security(
    request: Request,
    context: AuthContext = Depends(require_camera_read),
    settings: Settings = Depends(get_settings),
) -> AuthContext:
    origin = request.headers.get("origin")
    fetch_site = request.headers.get("sec-fetch-site", "").lower()
    if (origin is not None and origin != settings.auth_allowed_origin) or fetch_site == "cross-site":
        security_audit("camera_origin_rejected", device_id=context.device_id, result="rejected")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Request rejected")
    return context


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
        return AuthStatus(
            authenticated=True,
            device=AuthDevice(
                id="auth-disabled",
                friendly_name="Auth disabled",
                scopes=["lights:read", "lights:write", "system:read", "routines:read", "routines:write", "camera:read", "activity:read", "devices:read"],
            ),
        )
    _require_host(request, settings)
    _require_https(request)
    try:
        context = auth.authenticate(_request_auth_token(request))
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


@app.post("/api/v1/auth/native/pair", response_model=NativePairingResult)
async def pair_native_device(
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
    origin = request.headers.get("origin")
    if origin is not None and origin != settings.auth_allowed_origin:
        security_audit("native_origin_rejected", result="rejected")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Request rejected")
    rate_key = f"native:{request.client.host if request.client else 'unknown'}"
    try:
        issued = auth.pair(
            body.code,
            body.friendly_name,
            rate_key,
            profile="android-readonly",
            scopes=NATIVE_READONLY_SCOPES,
            session_kind="android",
        )
    except PairingRateLimited:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Pairing temporarily unavailable",
        ) from None
    except PairingRejected:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Pairing code is invalid or expired",
        ) from None
    response.headers["Cache-Control"] = "no-store, private, max-age=0"
    response.headers["Pragma"] = "no-cache"
    context = issued.context
    return NativePairingResult(
        session_token=issued.token,
        expires_at=datetime.fromtimestamp(issued.expires_at, UTC),
        device=AuthDevice(
            id=context.device_id,
            friendly_name=context.friendly_name,
            scopes=sorted(context.scopes),
        ),
    )


@app.post(
    "/api/v1/native/notifications/fcm/register",
    response_model=FcmRegistrationResponse,
)
async def register_native_fcm_target(
    body: FcmRegistrationRequest,
    request: Request,
    response: Response,
    context: AuthContext = Depends(require_authenticated_device),
    settings: Settings = Depends(get_settings),
    auth: AuthStore = Depends(get_auth),
):
    origin = request.headers.get("origin")
    if origin is not None and origin != settings.auth_allowed_origin:
        security_audit(
            "native_fcm_origin_rejected",
            device_id=context.device_id,
            result="rejected",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Request rejected"
        )
    if auth.session_kind(context.session_id) != "android":
        security_audit(
            "native_fcm_registration_rejected",
            device_id=context.device_id,
            result="non_native_session",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied"
        )
    try:
        registered = register_fcm_target(
            targets_path=settings.fcm_targets_path,
            destinations_path=settings.notification_destinations_path,
            device_id=context.device_id,
            display_name=context.friendly_name,
            registration_token=body.registration_token,
        )
    except (OSError, ValueError, ValidationError, json.JSONDecodeError):
        logger.error(
            "Native FCM registration persistence unavailable device_id=%s",
            context.device_id,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Notification registration unavailable",
        ) from None
    try:
        await app.state.push.engine.bind_fcm(
            device_id=context.device_id,
            display_name=context.friendly_name,
            target_ref=registered.target_ref,
            token_fingerprint=registered.token_fingerprint,
        )
    except ValueError:
        logger.warning("Push registry full; FCM registration kept in the 7F registry only")
    security_audit(
        "native_fcm_registered", device_id=context.device_id, result="success"
    )
    response.headers["Cache-Control"] = "no-store, private, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return FcmRegistrationResponse(
        provider=registered.provider,
        device_id=registered.device_id,
        destination_id=registered.destination_id,
        registered_at=registered.registered_at,
        token_fingerprint=registered.token_fingerprint,
        destination_enabled=registered.destination_enabled,
        activated_at=registered.activated_at,
    )


def _panel_layout_response(
    context: AuthContext, settings: Settings, auth: AuthStore, response: Response
) -> PanelLayoutResponse:
    response.headers["Cache-Control"] = "no-store, private, max-age=0"
    response.headers["Pragma"] = "no-cache"
    if not settings.auth_enabled:
        return PanelLayoutResponse.default()
    stored = auth.panel_layout(context.device_id)
    return PanelLayoutResponse.saved(*stored) if stored is not None else PanelLayoutResponse.default()


@app.get("/api/v1/panel/layout", response_model=PanelLayoutResponse)
async def panel_layout(
    response: Response,
    context: AuthContext = Depends(require_authenticated_device),
    settings: Settings = Depends(get_settings),
    auth: AuthStore = Depends(get_auth),
) -> PanelLayoutResponse:
    return _panel_layout_response(context, settings, auth, response)


@app.put("/api/v1/panel/layout", response_model=PanelLayoutResponse)
async def save_panel_layout(
    body: PanelLayout,
    response: Response,
    context: AuthContext = Depends(require_write_security),
    settings: Settings = Depends(get_settings),
    auth: AuthStore = Depends(get_auth),
) -> PanelLayoutResponse:
    if not settings.auth_enabled:
        return PanelLayoutResponse.default()
    updated_at = auth.save_panel_layout(context.device_id, body)
    return PanelLayoutResponse.saved(body, updated_at)


@app.delete("/api/v1/panel/layout", response_model=PanelLayoutResponse)
async def reset_panel_layout(
    response: Response,
    context: AuthContext = Depends(require_write_security),
    settings: Settings = Depends(get_settings),
    auth: AuthStore = Depends(get_auth),
) -> PanelLayoutResponse:
    if settings.auth_enabled:
        auth.reset_panel_layout(context.device_id)
    return _panel_layout_response(context, settings, auth, response)


@app.get("/api/v1/health", dependencies=[Depends(require_authenticated_device)])
async def health(
    settings: Settings = Depends(get_settings),
    alerts: AlertEngine = Depends(get_alerts),
    notifications: NotificationEngine = Depends(get_notifications),
):
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
        "alerts": alerts.summary().model_dump(mode="json"),
        "notifications": (app.state.push.engine.summary() if settings.push_mode == "smart" else notifications.summary()).model_dump(mode="json"),
    }


@app.get("/api/v1/lights", dependencies=[Depends(require_lights_read)])
async def get_lights(ha: HomeAssistantClient = Depends(get_ha)):
    try:
        return await list_lights(ha)
    except HomeAssistantUnavailable as exc:
        raise ha_unavailable_response(exc) from None


@app.get(
    "/api/v1/scenes",
    response_model=SceneListResponse,
)
async def get_scene_catalog(
    context: AuthContext = Depends(require_lights_read),
    ha: HomeAssistantClient = Depends(get_ha),
    scenes: SceneEngine = Depends(get_scenes),
):
    try:
        await scenes.ensure_live_snapshot(ha)
    except HomeAssistantUnavailable as exc:
        raise ha_unavailable_response(exc) from None
    return scenes.list(can_write="lights:write" in context.scopes)


@app.post(
    "/api/v1/scenes/{scene_id}/activate",
    response_model=SceneActivationResult,
    dependencies=[
        Depends(require_write_security),
        Depends(require_lights_write),
        Depends(require_control_enabled),
    ],
)
async def activate_scene(
    scene_id: str,
    request: Request,
    ha: HomeAssistantClient = Depends(get_ha),
    scenes: SceneEngine = Depends(get_scenes),
):
    if await request.body():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Scene activation does not accept an action payload",
        )
    try:
        return await scenes.activate(scene_id, ha)
    except HomeAssistantUnavailable as exc:
        raise ha_unavailable_response(exc) from None


@app.get("/api/v1/automations")
async def list_automations(
    context: AuthContext = Depends(require_routines_read),
    engine: AutomationEngine = Depends(get_automations),
):
    metric_sensors = {
        "cpu": {"cpu_usage"}, "ram": {"ram_usage"}, "disk": {"disk_usage"},
        "temperature": {"cpu_temperature", "gpu_temperature", "battery_temperature"},
    }
    return {
        "automations": engine.store.document.automations,
        "timezone": engine.settings.routines_timezone,
        "storage_status": "corrupt" if engine.store.corrupt else "ok",
        "can_write": "routines:write" in context.scopes,
        "can_enable": {"routines:write", "lights:write"}.issubset(context.scopes),
        "limits": {"condition_depth": 3, "conditions": 20, "actions": 8, "execution_seconds": 15},
        "targets": {
            "lights": [{"id": item.id, "name": item.name} for item in (*LIGHTS, MAIN_GROUP)],
            "devices": [{
                "id": item.id,
                "name": item.name,
                "metrics": ["online", *(
                    metric for metric, sensor_ids in metric_sensors.items()
                    if any(sensor.id in sensor_ids and sensor.support != "unsupported" for sensor in item.sensors)
                )],
            } for item in DEVICE_CATALOG],
        },
    }


@app.get("/api/v1/automations/history")
async def automation_history(
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None, pattern=r"^\d+$"),
    _context: AuthContext = Depends(require_routines_read),
    engine: AutomationEngine = Depends(get_automations),
):
    return engine.history.list(limit=limit, cursor=cursor)


@app.post(
    "/api/v1/automations", status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_write_security), Depends(require_routines_write)],
)
async def create_automation(body: AutomationCreate, engine: AutomationEngine = Depends(get_automations)):
    item = engine.store.create(body, now=engine.clock().astimezone(UTC))
    engine._locks[item.id] = asyncio.Lock()
    await engine._emit("automation.changed", item)
    return item


@app.patch(
    "/api/v1/automations/{automation_id}",
    dependencies=[Depends(require_write_security), Depends(require_routines_write)],
)
async def update_automation(automation_id: str, body: AutomationUpdate, engine: AutomationEngine = Depends(get_automations)):
    item = engine.store.update(automation_id, body, now=engine.clock().astimezone(UTC))
    await engine._emit("automation.changed", item)
    return item


@app.delete(
    "/api/v1/automations/{automation_id}", status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_write_security), Depends(require_routines_write)],
)
async def delete_automation(automation_id: str, engine: AutomationEngine = Depends(get_automations)):
    engine.store.delete(automation_id)
    engine._locks.pop(automation_id, None)
    await engine._emit("automation.deleted", automation_id=automation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post(
    "/api/v1/automations/{automation_id}/simulate",
    dependencies=[Depends(require_write_security), Depends(require_routines_write)],
)
async def simulate_automation(automation_id: str, engine: AutomationEngine = Depends(get_automations)):
    return await engine.simulate(automation_id)


@app.post(
    "/api/v1/automations/{automation_id}/enabled",
    dependencies=[Depends(require_write_security)],
)
async def enable_automation(
    automation_id: str,
    body: AutomationEnabledRequest,
    context: AuthContext = Depends(require_routines_write),
    settings: Settings = Depends(get_settings),
    engine: AutomationEngine = Depends(get_automations),
):
    if body.enabled:
        if "lights:write" not in context.scopes:
            security_audit(
                "authorization_rejected",
                device_id=context.device_id,
                result="insufficient_scope",
            )
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
        require_control_enabled(settings)
    item = engine.store.set_enabled(automation_id, body.enabled, now=engine.clock().astimezone(UTC))
    await engine._emit("automation.changed", item)
    return item


@app.post(
    "/api/v1/automations/{automation_id}/run",
    dependencies=[Depends(require_write_security), Depends(require_routines_write), Depends(require_lights_write), Depends(require_control_enabled)],
)
async def run_automation(automation_id: str, engine: AutomationEngine = Depends(get_automations)):
    return await engine.run_manual(automation_id)


@app.get("/api/v1/routines", response_model=RoutineListResponse)
async def list_routines(
    context: AuthContext = Depends(require_routines_read),
    routines: RoutineEngine = Depends(get_routines),
):
    return routines.list(
        can_write="routines:write" in context.scopes,
        can_run={"routines:write", "lights:write"}.issubset(context.scopes),
    )


@app.post(
    "/api/v1/routines",
    response_model=Routine,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_write_security), Depends(require_routines_write)],
)
async def create_routine(
    body: RoutineCreate,
    routines: RoutineEngine = Depends(get_routines),
):
    return await routines.create(body)


@app.patch(
    "/api/v1/routines/{routine_id}",
    response_model=Routine,
    dependencies=[Depends(require_write_security), Depends(require_routines_write)],
)
async def update_routine(
    routine_id: str,
    body: RoutineUpdate,
    routines: RoutineEngine = Depends(get_routines),
):
    return await routines.update(routine_id, body)


@app.delete(
    "/api/v1/routines/{routine_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_write_security), Depends(require_routines_write)],
)
async def delete_routine(
    routine_id: str,
    routines: RoutineEngine = Depends(get_routines),
):
    await routines.delete(routine_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post(
    "/api/v1/routines/{routine_id}/run",
    response_model=RoutineRunResponse,
    dependencies=[
        Depends(require_write_security),
        Depends(require_routines_write),
        Depends(require_lights_write),
        Depends(require_control_enabled),
    ],
)
async def run_routine(
    routine_id: str,
    routines: RoutineEngine = Depends(get_routines),
):
    try:
        return await routines.run(routine_id, source="manual")
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


async def _main_lights_command(settings: Settings, ha: HomeAssistantClient, action: str, service: str, data: dict):
    require_policy_permission(settings, "main_room_lights", action)
    try:
        result = await command_main_lights(ha, service, data)
    except HomeAssistantUnavailable as exc:
        command_audit("main_room_lights", action, "failed")
        raise ha_unavailable_response(exc) from None
    command_audit(
        "main_room_lights", action, "success" if result.accepted else "partial_failure",
        succeeded=result.succeeded, failed=[failure.device_id for failure in result.failed],
    )
    if result.failed:
        return JSONResponse(status_code=status.HTTP_207_MULTI_STATUS, content=result.model_dump(mode="json"))
    return result


# Declared before /lights/{light_id}/...: the literal group path must win route matching.
@app.post(
    "/api/v1/lights/main_room_lights/power",
    response_model=GroupCommandResult,
    dependencies=[Depends(require_write_security), Depends(require_lights_write), Depends(require_control_enabled)],
)
async def main_lights_power(body: PowerRequest, ha: HomeAssistantClient = Depends(get_ha), settings: Settings = Depends(get_settings)):
    return await _main_lights_command(settings, ha, "power", "turn_on" if body.on else "turn_off", {})


@app.post(
    "/api/v1/lights/main_room_lights/brightness",
    response_model=GroupCommandResult,
    dependencies=[Depends(require_write_security), Depends(require_lights_write), Depends(require_control_enabled)],
)
async def main_lights_brightness(body: BrightnessRequest, ha: HomeAssistantClient = Depends(get_ha), settings: Settings = Depends(get_settings)):
    return await _main_lights_command(settings, ha, "brightness", "turn_on", {"brightness": body.brightness})


@app.post(
    "/api/v1/lights/main_room_lights/temperature",
    response_model=GroupCommandResult,
    dependencies=[Depends(require_write_security), Depends(require_lights_write), Depends(require_control_enabled)],
)
async def main_lights_temperature(body: TemperatureRequest, ha: HomeAssistantClient = Depends(get_ha), settings: Settings = Depends(get_settings)):
    require_policy_permission(settings, "main_room_lights", "temperature")
    validate_kelvin(MAIN_GROUP, body.kelvin)
    return await _main_lights_command(settings, ha, "temperature", "turn_on", {"color_temp_kelvin": body.kelvin})


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
async def system(
    ha: HomeAssistantClient = Depends(get_ha),
    dns_health: DnsHealthMonitor = Depends(get_dns_health),
    monitor: SystemMonitor = Depends(get_system_monitor),
):
    try:
        snapshot = await system_overview(ha, dns_health.snapshot())
        await monitor.accept(snapshot)
        return snapshot
    except HomeAssistantUnavailable as exc:
        raise ha_unavailable_response(exc) from None


def reject_query_parameters(request: Request) -> None:
    if request.query_params:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="This endpoint does not accept query parameters",
        )


@app.get(
    "/api/v1/system/health",
    response_model=HealthResponse,
    dependencies=[Depends(require_system_read), Depends(reject_query_parameters)],
)
async def system_health(settings: Settings = Depends(get_settings)) -> HealthResponse:
    """RoomOS operational health from in-memory runtime state only (no probes, no I/O)."""
    return build_health(collect_inputs(app.state, settings))


@app.get(
    "/api/v1/devices",
    response_model=DeviceListResponse,
    dependencies=[Depends(reject_query_parameters)],
)
async def list_devices(
    context: AuthContext = Depends(require_devices_read),
    registry: DeviceRegistry = Depends(get_devices),
) -> DeviceListResponse:
    """Read-only registry view from in-memory state; no source probes or commands."""
    return registry.list(context.scopes)


CLIENT_LAST_SEEN_RESOLUTION_SECONDS = 300  # AuthStore.authenticate refresh interval


@app.get(
    "/api/v1/clients",
    response_model=PairedClientsResponse,
    dependencies=[Depends(reject_query_parameters)],
)
async def list_paired_clients(
    context: AuthContext = Depends(require_devices_scope),
    settings: Settings = Depends(get_settings),
    auth: AuthStore = Depends(get_auth),
) -> PairedClientsResponse:
    """Paired RoomOS apps for the Device Center. Read-only; no scopes, tokens or session IDs."""
    rows = auth.list_clients(context.device_id) if settings.auth_enabled else []
    epoch = lambda value: datetime.fromtimestamp(value, UTC) if value else None  # noqa: E731
    clients = [
        PairedClient(
            id=row["ref"], name=str(row["name"])[:60], paired_at=epoch(row["paired_at"]),
            last_seen_at=epoch(row["last_seen_at"]), active_session=row["kind"] is not None,
            app=row["kind"] if row["kind"] in ("web", "android") else "unknown", current=row["current"],
        )
        for row in rows
    ]
    clients.sort(key=lambda client: not client.current)  # stable: current first, then recency
    return PairedClientsResponse(clients=clients, last_seen_resolution_seconds=CLIENT_LAST_SEEN_RESOLUTION_SECONDS,
                                 generated_at=datetime.now(UTC))


async def _bounded_body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY_BYTES):
        raise TelemetryRejected(413, "too_large")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            raise TelemetryRejected(413, "too_large")
    return bytes(body)


async def _ingest_client_telemetry(
    request: Request, response: Response, context: AuthContext, auth: AuthStore, settings: Settings,
    expected_kind: str,
) -> ClientTelemetryAccepted:
    """Shared by the native (bearer) and PWA (cookie + CSRF) routes. The device written is
    derived from the authenticated session's binding only; the payload names no device."""
    ingest: ClientTelemetryIngest = app.state.client_telemetry
    response.headers["Cache-Control"] = "no-store, private, max-age=0"
    try:
        if TELEMETRY_SCOPE not in context.scopes:
            raise TelemetryRejected(403, "insufficient_scope")
        kind = auth.session_kind(context.session_id) if settings.auth_enabled else expected_kind
        if kind != expected_kind:
            raise TelemetryRejected(403, "session_kind_mismatch")
        body = await _bounded_body(request)
        device_id, stale_after = ingest.accept(context.device_id, kind, body)
    except TelemetryRejected as rejected:
        security_audit(f"client_telemetry_rejected reason={rejected.reason}", device_id=context.device_id, result="rejected")
        headers = {"Retry-After": str(rejected.retry_after)} if rejected.retry_after else None
        detail = {403: "Permission denied", 409: "Report rejected", 413: "Report too large",
                  422: "Invalid report", 429: "Too many reports"}[rejected.status]
        raise HTTPException(status_code=rejected.status, detail=detail, headers=headers) from None
    await app.state.devices.publish_changes()
    return ClientTelemetryAccepted(device_id=device_id, stale_after_seconds=stale_after,
                                   received_at=datetime.now(UTC))


@app.post(
    "/api/v1/native/devices/telemetry",
    response_model=ClientTelemetryAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(reject_query_parameters)],
)
async def native_device_telemetry(
    request: Request,
    response: Response,
    context: AuthContext = Depends(require_authenticated_device),
    settings: Settings = Depends(get_settings),
    auth: AuthStore = Depends(get_auth),
) -> ClientTelemetryAccepted:
    origin = request.headers.get("origin")
    if origin is not None and origin != settings.auth_allowed_origin:
        security_audit("client_telemetry_rejected reason=origin", device_id=context.device_id, result="rejected")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Request rejected")
    return await _ingest_client_telemetry(request, response, context, auth, settings, "android")


@app.post(
    "/api/v1/devices/self/telemetry",
    response_model=ClientTelemetryAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(reject_query_parameters)],
)
async def web_device_telemetry(
    request: Request,
    response: Response,
    context: AuthContext = Depends(require_write_security),
    settings: Settings = Depends(get_settings),
    auth: AuthStore = Depends(get_auth),
) -> ClientTelemetryAccepted:
    return await _ingest_client_telemetry(request, response, context, auth, settings, "web")


def _unknown_device() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown device ID")


@app.get(
    "/api/v1/devices/{device_id}",
    response_model=DeviceDetail,
    dependencies=[Depends(reject_query_parameters)],
)
async def device_detail(
    device_id: str,
    context: AuthContext = Depends(require_devices_read),
    registry: DeviceRegistry = Depends(get_devices),
) -> DeviceDetail:
    detail = registry.detail(device_id, context.scopes)
    if detail is None:
        raise _unknown_device()
    return detail


@app.get(
    "/api/v1/devices/{device_id}/telemetry",
    response_model=DeviceTelemetryResponse,
    dependencies=[Depends(reject_query_parameters)],
)
async def device_telemetry(
    device_id: str,
    context: AuthContext = Depends(require_devices_read),
    registry: DeviceRegistry = Depends(get_devices),
) -> DeviceTelemetryResponse:
    telemetry = registry.telemetry(device_id, context.scopes)
    if telemetry is None:
        raise _unknown_device()
    return telemetry


@app.get("/api/v1/devices/{device_id}/history")
async def device_history(
    device_id: str,
    metric: str = Query(max_length=40),
    range: Literal["1h", "24h", "7d", "30d"] = "24h",
    context: AuthContext = Depends(require_devices_read),
    registry: DeviceRegistry = Depends(get_devices),
):
    detail = registry.detail(device_id, context.scopes)
    if detail is None:
        raise _unknown_device()
    if metric not in METRICS or not any(s.id == metric and s.support == "supported" for s in detail.sensors):
        raise HTTPException(status_code=422, detail="Unsupported metric")
    result = await asyncio.to_thread(app.state.metrics_history.query, device_id, metric, range)
    if result["status"] == "unavailable":
        raise HTTPException(status_code=503, detail="Metrics history unavailable")
    return result


@app.get("/api/v1/devices/{device_id}/forecast")
async def device_forecast(
    device_id: str,
    metric: str = Query(default="disk_usage", max_length=40),
    context: AuthContext = Depends(require_devices_read),
    registry: DeviceRegistry = Depends(get_devices),
):
    detail = registry.detail(device_id, context.scopes)
    if detail is None:
        raise _unknown_device()
    if metric not in FORECAST_METRICS or not any(s.id == metric and s.support == "supported" for s in detail.sensors):
        raise HTTPException(status_code=422, detail="Unsupported metric")
    result = await asyncio.to_thread(app.state.metrics_history.forecast, device_id, metric)
    if result["status"] == "unavailable":
        raise HTTPException(status_code=503, detail="Metrics history unavailable")
    return result


@app.get(
    "/api/v1/system/history",
    response_model=HistoryResponse,
    dependencies=[Depends(require_system_read)],
)
async def system_history(
    query: Annotated[HistoryQuery, Query()],
    ha: HomeAssistantClient = Depends(get_ha),
    history: HistoryService = Depends(get_history),
) -> HistoryResponse:
    try:
        return await history.get(ha, query)
    except HistoryUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telemetry history is unavailable",
        ) from None


@app.get(
    "/api/v1/system/version",
    response_model=ReleaseInfo,
    dependencies=[Depends(require_system_read)],
)
async def system_version(settings: Settings = Depends(get_settings)) -> ReleaseInfo:
    return ReleaseInfo(
        release=settings.release_name,
        revision=settings.release_revision,
        component=settings.release_component,
    )


@app.get(
    "/api/v1/alerts",
    response_model=AlertListResponse,
    dependencies=[Depends(require_activity_read)],
)
async def alerts(alert_engine: AlertEngine = Depends(get_alerts)):
    return alert_engine.list_alerts()


@app.get(
    "/api/v1/notifications",
    response_model=NotificationCenterResponse,
    dependencies=[Depends(require_activity_read)],
)
async def notification_center(
    status: Literal["all", "active", "resolved"] = "all",
    severity: Literal["info", "warning", "critical"] | None = None,
    unread: bool | None = None,
    source: str | None = Query(default=None, min_length=1, max_length=40),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=500),
    alert_engine: AlertEngine = Depends(get_alerts),
) -> NotificationCenterResponse:
    response = alert_engine.list_notifications(
        status=status,
        severity=severity,
        unread=unread,
        source=source,
        limit=limit,
        offset=offset,
    )
    policy: AlertPolicyStore = app.state.alert_policy
    shown = {item.id for item in response.notifications}
    response.snoozes = {key: {"until": value.until, "severity": value.severity}
                        for key, value in policy.snoozes().items() if key in shown}
    response.insights = {key: value for key, value in policy.insights.items() if key in shown}
    return response


class SnoozeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hours: Literal[2, 8, 24, 168, 1, 4]  # 2 h, 8 h, 24 h, 7 days; 1/4 kept for older Android client builds


class ProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile: Literal["normal", "gaming", "development"]
    hours: int | None = Field(default=None, ge=1, le=24)


async def _publish_alert_update(alert: AlertRecord) -> None:
    await app.state.broker.broadcast(
        RoomOSEvent(type="alert.updated", device_id=alert.id, data={"alert": alert.model_dump(mode="json")}),
        required_scope="activity:read",
    )


@app.post("/api/v1/notifications/{alert_id}/snooze")
async def notification_snooze(
    alert_id: str,
    body: SnoozeRequest,
    context: AuthContext = Depends(require_write_security),
    alert_engine: AlertEngine = Depends(get_alerts),
) -> dict:
    """Silences this alert's repeat notifications only. It stays active and Health stays truthful."""
    _require_alert_scope(context, "alerts:snooze")
    return await _snooze_alert(alert_id, body, context, alert_engine)


async def _snooze_alert(alert_id: str, body: SnoozeRequest, context: AuthContext, alert_engine: AlertEngine) -> dict:
    alert = alert_engine.alert_by_id(alert_id)
    if alert is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")
    if alert.status != "active":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Only active alerts can be snoozed")
    try:
        snooze = app.state.alert_policy.set_snooze(alert_id, hours=body.hours, severity=alert.severity, actor=context.device_id)
    except OSError as exc:
        raise HTTPException(status_code=503, detail="Alert policy storage unavailable") from exc
    security_audit(f"alert_snoozed hours={body.hours}", device_id=context.device_id, result="success")
    await _publish_alert_update(alert)
    return {"alert_id": alert_id, "until": snooze.until, "severity": snooze.severity}


@app.delete("/api/v1/notifications/{alert_id}/snooze")
async def notification_unsnooze(
    alert_id: str,
    context: AuthContext = Depends(require_write_security),
    alert_engine: AlertEngine = Depends(get_alerts),
) -> dict:
    _require_alert_scope(context, "alerts:snooze")
    return await _unsnooze_alert(alert_id, context, alert_engine)


async def _unsnooze_alert(alert_id: str, context: AuthContext, alert_engine: AlertEngine) -> dict:
    alert = alert_engine.alert_by_id(alert_id)
    if alert is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")
    try:
        cleared = app.state.alert_policy.clear_snooze(alert_id)
    except OSError as exc:
        raise HTTPException(status_code=503, detail="Alert policy storage unavailable") from exc
    security_audit("alert_unsnoozed", device_id=context.device_id, result="success")
    await _publish_alert_update(alert)
    return {"alert_id": alert_id, "cleared": cleared}


@app.get("/api/v1/alerts/profiles", dependencies=[Depends(require_activity_read)])
async def alert_profiles() -> dict:
    policy: AlertPolicyStore = app.state.alert_policy
    devices = {}
    for device in ("workstation", "primary", "secondary"):
        item = policy.profile_view(device)
        devices[device] = {"profile": item.profile if item else "normal", "until": item.until if item else None}
    return {"devices": devices}


@app.put("/api/v1/alerts/profiles/{device_id}")
async def set_alert_profile(
    device_id: Literal["workstation", "primary", "secondary"],
    body: ProfileRequest,
    context: AuthContext = Depends(require_write_security),
) -> dict:
    """Changes utilization sensitivity only; temperature, disk exhaustion and services are unaffected."""
    _require_alert_scope(context, "alerts:profile")
    return _set_alert_profile(device_id, body, context)


def _set_alert_profile(device_id: str, body: ProfileRequest, context: AuthContext) -> dict:
    try:
        item = app.state.alert_policy.set_profile(device_id, body.profile, hours=body.hours, actor=context.device_id)
    except OSError as exc:
        raise HTTPException(status_code=503, detail="Alert policy storage unavailable") from exc
    security_audit(f"alert_profile_set profile={body.profile}", device_id=context.device_id, result="success")
    return {"device_id": device_id, "profile": item.profile if item else "normal", "until": item.until if item else None}


_native_alert_write_attempts: dict[str, deque[float]] = defaultdict(deque)


async def require_native_alert_write(
    request: Request,
    context: AuthContext = Depends(require_authenticated_device),
    settings: Settings = Depends(get_settings),
    auth: AuthStore = Depends(get_auth),
) -> AuthContext:
    """Native Bearer only; browser cookie and web-session Bearer never bypass CSRF."""
    _require_native_origin(request, settings, context)
    if not request.headers.get("authorization", "").lower().startswith("bearer ") or (
        settings.auth_enabled and auth.session_kind(context.session_id) != "android"
    ):
        security_audit("native_alert_write_rejected", device_id=context.device_id, result="session_kind_or_bearer")
        raise HTTPException(status_code=403, detail="Permission denied")
    now = time.monotonic()
    attempts = _native_alert_write_attempts[context.device_id]
    while attempts and attempts[0] <= now - 60:
        attempts.popleft()
    if len(attempts) >= 20:
        security_audit("native_alert_write_rejected", device_id=context.device_id, result="rate_limited")
        raise HTTPException(status_code=429, detail="Too many requests")
    attempts.append(now)
    return context


@app.post("/api/v1/native/notifications/{alert_id}/snooze")
async def native_notification_snooze(
    alert_id: str,
    body: SnoozeRequest,
    context: AuthContext = Depends(require_native_alert_write),
    alert_engine: AlertEngine = Depends(get_alerts),
) -> dict:
    _require_alert_scope(context, "alerts:snooze")
    return await _snooze_alert(alert_id, body, context, alert_engine)


@app.delete("/api/v1/native/notifications/{alert_id}/snooze")
async def native_notification_unsnooze(
    alert_id: str,
    context: AuthContext = Depends(require_native_alert_write),
    alert_engine: AlertEngine = Depends(get_alerts),
) -> dict:
    _require_alert_scope(context, "alerts:snooze")
    return await _unsnooze_alert(alert_id, context, alert_engine)


@app.put("/api/v1/native/alerts/profiles/{device_id}")
async def native_set_alert_profile(
    device_id: Literal["workstation", "primary", "secondary"],
    body: ProfileRequest,
    context: AuthContext = Depends(require_native_alert_write),
) -> dict:
    _require_alert_scope(context, "alerts:profile")
    return _set_alert_profile(device_id, body, context)


def _require_alert_scope(context: AuthContext, scope: str) -> None:
    if scope not in context.scopes:
        security_audit(
            "authorization_rejected",
            device_id=context.device_id,
            result="insufficient_scope",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied",
        )


def _notification_or_404(alert: AlertRecord | None) -> AlertRecord:
    if alert is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notification not found",
        )
    return alert


@app.post("/api/v1/notifications/{alert_id}/read", response_model=AlertRecord)
async def notification_read(
    alert_id: str,
    context: AuthContext = Depends(require_write_security),
    alert_engine: AlertEngine = Depends(get_alerts),
) -> AlertRecord:
    _require_alert_scope(context, "activity:read")
    return _notification_or_404(await alert_engine.mark_read(alert_id))


@app.post("/api/v1/notifications/{alert_id}/acknowledge", response_model=AlertRecord)
async def notification_acknowledge(
    alert_id: str,
    context: AuthContext = Depends(require_write_security),
    alert_engine: AlertEngine = Depends(get_alerts),
) -> AlertRecord:
    _require_alert_scope(context, "alerts:ack")
    return _notification_or_404(
        await alert_engine.acknowledge(alert_id, actor=context.device_id)
    )


@app.post("/api/v1/notifications/{alert_id}/mute", response_model=AlertRecord)
async def notification_mute(
    alert_id: str,
    body: NotificationMuteRequest,
    context: AuthContext = Depends(require_write_security),
    alert_engine: AlertEngine = Depends(get_alerts),
) -> AlertRecord:
    _require_alert_scope(context, "alerts:ack")
    return _notification_or_404(
        await alert_engine.mute(
            alert_id,
            until=alert_engine.clock() + timedelta(minutes=body.minutes),
            actor=context.device_id,
        )
    )


def get_push() -> PushEngine:
    return app.state.push.engine


def _push_capabilities(settings: Settings) -> PushCapabilities:
    runtime = app.state.push
    return PushCapabilities(
        mode=settings.push_mode,
        delivery_active=settings.push_mode == "smart" and runtime.engine.delivery_enabled,
        webpush_available=runtime.webpush_public_key is not None,
        webpush_public_key=runtime.webpush_public_key,
        fcm_available=runtime.fcm_available,
        timezone=settings.push_timezone,
    )


def _push_session_kind(context: AuthContext, settings: Settings, auth: AuthStore) -> str | None:
    return auth.session_kind(context.session_id) if settings.auth_enabled else "web"


def _push_settings(context: AuthContext, settings: Settings, auth: AuthStore, response: Response) -> PushSettingsResponse:
    response.headers["Cache-Control"] = "no-store, private, max-age=0"
    return settings_view(
        app.state.push.engine,
        device_id=context.device_id,
        display_name=context.friendly_name,
        session_kind=_push_session_kind(context, settings, auth),
        can_manage="notifications:self" in context.scopes,
        can_receive="activity:read" in context.scopes,
        capabilities=_push_capabilities(settings),
    )


def _require_push_manage(context: AuthContext, settings: Settings, auth: AuthStore, kind: str) -> None:
    if "notifications:self" not in context.scopes:
        security_audit("push_settings_rejected", device_id=context.device_id, result="insufficient_scope")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
    if _push_session_kind(context, settings, auth) != kind:
        security_audit("push_settings_rejected", device_id=context.device_id, result="session_kind_mismatch")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")


def _require_native_origin(request: Request, settings: Settings, context: AuthContext) -> None:
    origin = request.headers.get("origin")
    if origin is not None and origin != settings.auth_allowed_origin:
        security_audit("push_origin_rejected", device_id=context.device_id, result="rejected")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Request rejected")


def _validated_subscription(body: WebPushSubscriptionRequest, settings: Settings, context: AuthContext) -> str:
    host = allowed_endpoint(body.endpoint, extra_hosts(settings))
    if host is None or not valid_subscription_keys(body.keys.p256dh, body.keys.auth):
        security_audit("push_subscription_rejected", device_id=context.device_id, result="invalid_subscription")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Unsupported push subscription")
    if app.state.push.webpush_public_key is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Web Push is not configured")
    return host


CANARY_TOKEN_PATH = "/tmp/roomos-push-canary.token"
CANARY_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1"})
CANARY_TOKEN_MAX_AGE_SECONDS = 120


def _consume_canary_token(presented: str | None) -> bool:
    """One-time operator token written by `app.cli push canary` inside this container (tmpfs)."""
    path = Path(CANARY_TOKEN_PATH)
    try:
        info = path.stat()
        expected = path.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    finally:
        path.unlink(missing_ok=True)  # single use, even when the token is wrong
    fresh = (datetime.now(UTC).timestamp() - info.st_mtime) <= CANARY_TOKEN_MAX_AGE_SECONDS
    private = not (info.st_mode & 0o077)
    return bool(presented and expected and fresh and private and hmac.compare_digest(presented, expected))


@app.post("/internal/push/canary", include_in_schema=False)
async def internal_push_canary(request: Request, settings: Settings = Depends(get_settings)) -> dict:
    """Not proxied by nginx (only /api/* is). Requires a loopback client inside the Core container
    and a fresh one-time token file that only a process in this container can create."""
    client_host = request.client.host if request.client else ""
    if client_host not in CANARY_LOCAL_HOSTS or not _consume_canary_token(request.headers.get("x-roomos-canary-token")):
        security_audit("push_canary_rejected", result="rejected")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Request rejected")
    try:
        body = json.loads(await request.body() or b"{}")
        device_id = str(body["device_id"])[:80]
    except (ValueError, KeyError, TypeError):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="device_id required") from None
    engine: PushEngine = app.state.push.engine
    provider = engine.providers.get("fcm")
    owned = None
    if provider is None:
        if not settings.fcm_enabled:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="FCM is not enabled")
        try:
            provider = owned = build_fcm_provider(settings)
        except (OSError, ValueError):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="FCM provider unavailable") from None
    try:
        message = await engine.send_canary(device_id, provider)
    except ValueError as exc:
        security_audit("push_canary_refused", device_id=device_id, result="refused")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    finally:
        if owned is not None:
            await owned.close()
    security_audit(f"push_canary_sent status={message.status}", device_id=device_id, result="success")
    return {"message_id": message.id, "status": message.status, "reason": message.reason,
            "attempt_count": message.attempt_count, "accepted_at": message.accepted_at,
            "attempts": [{"result": a.result, "code": a.code} for a in message.attempts]}


@app.get("/api/v1/push/settings", response_model=PushSettingsResponse, dependencies=[Depends(reject_query_parameters)])
async def push_settings(
    response: Response,
    context: AuthContext = Depends(require_authenticated_device),
    settings: Settings = Depends(get_settings),
    auth: AuthStore = Depends(get_auth),
) -> PushSettingsResponse:
    return _push_settings(context, settings, auth, response)


@app.put("/api/v1/push/settings", response_model=PushSettingsResponse)
async def update_push_settings(
    body: PushPreferencesUpdate,
    response: Response,
    context: AuthContext = Depends(require_write_security),
    settings: Settings = Depends(get_settings),
    auth: AuthStore = Depends(get_auth),
    push: PushEngine = Depends(get_push),
) -> PushSettingsResponse:
    _require_push_manage(context, settings, auth, "web")
    try:
        await push.update_preferences(context.device_id, context.friendly_name, body)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Push registry is full") from None
    security_audit("push_settings_updated", device_id=context.device_id, result="success")
    return _push_settings(context, settings, auth, response)


@app.put("/api/v1/push/webpush/subscription", response_model=PushSettingsResponse)
async def put_webpush_subscription(
    body: WebPushSubscriptionRequest,
    response: Response,
    context: AuthContext = Depends(require_write_security),
    settings: Settings = Depends(get_settings),
    auth: AuthStore = Depends(get_auth),
    push: PushEngine = Depends(get_push),
) -> PushSettingsResponse:
    _require_push_manage(context, settings, auth, "web")
    host = _validated_subscription(body, settings, context)
    try:
        await push.subscribe_webpush(device_id=context.device_id, display_name=context.friendly_name,
                                     endpoint=body.endpoint, host=host, p256dh=body.keys.p256dh, auth=body.keys.auth)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Push registry is full") from None
    security_audit(f"push_webpush_subscribed fp={fingerprint(body.endpoint)[:12]}", device_id=context.device_id, result="success")
    return _push_settings(context, settings, auth, response)


@app.delete("/api/v1/push/webpush/subscription", response_model=PushSettingsResponse)
async def delete_webpush_subscription(
    response: Response,
    context: AuthContext = Depends(require_write_security),
    settings: Settings = Depends(get_settings),
    auth: AuthStore = Depends(get_auth),
    push: PushEngine = Depends(get_push),
) -> PushSettingsResponse:
    # Removing one's own subscription only reduces exposure, so it needs no manage scope.
    await push.unsubscribe_webpush(context.device_id)
    security_audit("push_webpush_unsubscribed", device_id=context.device_id, result="success")
    return _push_settings(context, settings, auth, response)


@app.post("/api/v1/push/webpush/rotate", status_code=status.HTTP_204_NO_CONTENT)
async def rotate_webpush_subscription(
    body: WebPushRotateRequest,
    request: Request,
    context: AuthContext = Depends(require_authenticated_device),
    settings: Settings = Depends(get_settings),
    auth: AuthStore = Depends(get_auth),
    push: PushEngine = Depends(get_push),
) -> Response:
    """Service-worker `pushsubscriptionchange`. The worker cannot carry CSRF, so the request must be
    same-origin and prove the currently registered endpoint; it can only replace this device's own."""
    if settings.auth_enabled:
        _require_origin(request, settings)
    _require_push_manage(context, settings, auth, "web")
    sub = body.subscription
    host = _validated_subscription(sub, settings, context)
    rotated = await push.rotate_webpush(device_id=context.device_id, old_endpoint=body.old_endpoint, endpoint=sub.endpoint,
                                        host=host, p256dh=sub.keys.p256dh, auth=sub.keys.auth)
    if rotated is None:
        security_audit("push_webpush_rotate_rejected", device_id=context.device_id, result="rejected")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Subscription changed; resubscribe")
    security_audit("push_webpush_rotated", device_id=context.device_id, result="success")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/api/v1/push/receipts", status_code=status.HTTP_204_NO_CONTENT)
async def web_push_receipt(
    body: PushReceiptRequest,
    request: Request,
    context: AuthContext = Depends(require_authenticated_device),
    settings: Settings = Depends(get_settings),
    push: PushEngine = Depends(get_push),
) -> Response:
    if settings.auth_enabled:
        _require_origin(request, settings)
    if not await push.record_receipt(context.device_id, body.message_id, body.event):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown delivery")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/v1/native/push/settings", response_model=PushSettingsResponse, dependencies=[Depends(reject_query_parameters)])
async def native_push_settings(
    request: Request,
    response: Response,
    context: AuthContext = Depends(require_authenticated_device),
    settings: Settings = Depends(get_settings),
    auth: AuthStore = Depends(get_auth),
) -> PushSettingsResponse:
    _require_native_origin(request, settings, context)
    return _push_settings(context, settings, auth, response)


@app.put("/api/v1/native/push/settings", response_model=PushSettingsResponse)
async def native_update_push_settings(
    body: PushPreferencesUpdate,
    request: Request,
    response: Response,
    context: AuthContext = Depends(require_authenticated_device),
    settings: Settings = Depends(get_settings),
    auth: AuthStore = Depends(get_auth),
    push: PushEngine = Depends(get_push),
) -> PushSettingsResponse:
    _require_native_origin(request, settings, context)
    _require_push_manage(context, settings, auth, "android")
    try:
        await push.update_preferences(context.device_id, context.friendly_name, body)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Push registry is full") from None
    security_audit("push_settings_updated", device_id=context.device_id, result="success")
    return _push_settings(context, settings, auth, response)


@app.post("/api/v1/native/push/receipts", status_code=status.HTTP_204_NO_CONTENT)
async def native_push_receipt(
    body: PushReceiptRequest,
    request: Request,
    context: AuthContext = Depends(require_authenticated_device),
    settings: Settings = Depends(get_settings),
    auth: AuthStore = Depends(get_auth),
    push: PushEngine = Depends(get_push),
) -> Response:
    _require_native_origin(request, settings, context)
    if settings.auth_enabled and auth.session_kind(context.session_id) != "android":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
    if not await push.record_receipt(context.device_id, body.message_id, body.event):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown delivery")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get(
    "/api/v1/activity",
    response_model=ActivityListResponse,
    dependencies=[Depends(require_activity_read)],
)
async def activity(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=500),
    category: str = Query(default="all", pattern="^(all|alerts|scenes)$"),
    alert_engine: AlertEngine = Depends(get_alerts),
):
    response = alert_engine.list_activity(limit=limit, offset=offset, category=category)
    store = getattr(app.state, "executions", None)
    if store is not None:
        response.execution_storage_status = store.storage_status
        for item in response.activity:
            item.execution = store.get(item.execution_id)
            if item.execution:
                from .catalog import LIGHT_BY_ID
                item.device_names = {t.device_id: LIGHT_BY_ID[t.device_id].name for t in item.execution.targets}
    return response


@app.get("/api/v1/system/primary", dependencies=[Depends(require_system_read)])
async def primary(ha: HomeAssistantClient = Depends(get_ha)):
    try:
        return await system_node(ha, "primary")
    except HomeAssistantUnavailable as exc:
        raise ha_unavailable_response(exc) from None


@app.get("/api/v1/system/secondary", dependencies=[Depends(require_system_read)])
async def secondary(ha: HomeAssistantClient = Depends(get_ha)):
    try:
        return await system_node(ha, "secondary")
    except HomeAssistantUnavailable as exc:
        raise ha_unavailable_response(exc) from None


@app.get(
    "/api/v1/cameras",
    response_model=CameraCatalog,
    dependencies=[Depends(require_camera_request_security)],
)
async def cameras(camera: CameraMonitor = Depends(get_camera)):
    return camera.catalog()


@app.get(
    "/api/v1/cameras/{camera_id}/snapshot",
    dependencies=[Depends(require_camera_request_security)],
)
async def camera_snapshot(
    camera_id: str,
    camera: CameraMonitor = Depends(get_camera),
    ha: HomeAssistantClient = Depends(get_ha),
):
    if camera_id != CAMERA_ID:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found")
    current = camera.catalog().cameras[0]
    if not current.snapshot_available:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Snapshot unavailable")
    try:
        image = await ha.get_image(CAMERA_IMAGE_ENTITY)
    except HomeAssistantUnavailable as exc:
        raise ha_unavailable_response(exc) from None
    if image is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Snapshot unavailable")
    payload, media_type = image
    return Response(
        content=payload,
        media_type=media_type,
        headers={
            "Cache-Control": "no-store, private, max-age=0",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'",
            "Referrer-Policy": "no-referrer",
        },
    )


async def _serve_events(
    socket: WebSocket,
    settings: Settings,
    auth: AuthStore,
    token: str | None,
    *,
    require_web_origin: bool,
) -> None:
    broker: EventBroker = app.state.broker
    if settings.auth_enabled:
        if socket.headers.get("host", "").lower() != settings.auth_allowed_host.lower():
            security_audit("websocket_host_rejected", result="rejected")
            await socket.close(code=4403)
            return
        if socket.headers.get("x-forwarded-proto", "").lower() != "https":
            security_audit("websocket_transport_rejected", result="rejected")
            await socket.close(code=4403)
            return
        origin = socket.headers.get("origin")
        if (require_web_origin and origin != settings.auth_allowed_origin) or (
            not require_web_origin
            and origin is not None
            and origin != settings.auth_allowed_origin
        ):
            security_audit("websocket_origin_rejected", result="rejected")
            await socket.close(code=4403)
            return
        try:
            context = auth.authenticate(token)
        except AuthRejected:
            security_audit("websocket_authentication_rejected", result="rejected")
            await socket.close(code=4401)
            return
        if not ({"lights:read", "system:read", "routines:read", "camera:read", "activity:read", "devices:read"} & context.scopes):
            security_audit("websocket_authorization_rejected", device_id=context.device_id, result="insufficient_scope")
            await socket.close(code=4403)
            return

        def validate_session() -> None:
            # Authentication already performs one indexed session lookup per eligible
            # delivery. Reuse that result; never freeze permissions for a socket's life.
            current = auth.authenticate(token)
            if (current.device_id, current.session_id, current.scopes) != (
                context.device_id, context.session_id, context.scopes
            ):
                raise AuthorizationChanged("WebSocket authorization changed; reconnect required")
    else:
        validate_session = lambda: None
        context = AuthContext(
            "auth-disabled",
            "Auth disabled",
            frozenset({"lights:read", "system:read", "routines:read", "camera:read", "activity:read", "devices:read"}),
            "auth-disabled",
        )

    await broker.connect(socket, validate_session, context.scopes)
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
                pass
            # Validate on the idle tick AND inbound traffic; traffic cannot postpone
            # revocation indefinitely. A fresh connection obtains fresh scopes.
            try:
                validate_session()
            except AuthorizationChanged:
                security_audit("websocket_authorization_changed", device_id=context.device_id, result="reconnect_required")
                await socket.close(code=CLOSE_AUTHORIZATION_CHANGED)
                break
            except AuthRejected:
                security_audit("websocket_authentication_rejected", result="revoked_or_expired")
                await socket.close(code=CLOSE_AUTH_REQUIRED)
                break
    except WebSocketDisconnect:
        pass
    finally:
        broker.disconnect(socket)


@app.websocket("/api/v1/events")
async def events(
    socket: WebSocket,
    settings: Settings = Depends(get_settings),
    auth: AuthStore = Depends(get_auth),
):
    await _serve_events(
        socket,
        settings,
        auth,
        socket.cookies.get(SESSION_COOKIE),
        require_web_origin=True,
    )


@app.websocket("/api/v1/native/events")
async def native_events(
    socket: WebSocket,
    settings: Settings = Depends(get_settings),
    auth: AuthStore = Depends(get_auth),
):
    try:
        token = _bearer_token(socket.headers.get("authorization"))
    except AuthRejected:
        token = None
    await _serve_events(
        socket,
        settings,
        auth,
        token,
        require_web_origin=False,
    )
