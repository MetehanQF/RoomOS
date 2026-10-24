from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Capabilities(BaseModel):
    power: bool = True
    brightness: bool = True
    color: bool = True
    temperature: bool = True
    effects: bool = False
    min_kelvin: int
    max_kelvin: int


class LightState(BaseModel):
    id: str
    name: str
    type: Literal["light"] = "light"
    available: bool
    on: bool | None = None
    brightness: int | None = None
    color_mode: str | None = None
    rgb: tuple[int, int, int] | None = None
    hs: tuple[float, float] | None = None
    color_temperature_kelvin: int | None = None
    effect: str | None = None
    effects: list[str] = Field(default_factory=list)
    capabilities: Capabilities
    last_updated: datetime | None = None


class PowerRequest(BaseModel):
    on: bool


class BrightnessRequest(BaseModel):
    brightness: int = Field(ge=1, le=255)


class ColorRequest(BaseModel):
    rgb: tuple[int, int, int] | None = None
    hue: float | None = Field(default=None, ge=0, le=360)
    saturation: float | None = Field(default=None, ge=0, le=100)

    @field_validator("rgb")
    @classmethod
    def validate_rgb(cls, value: tuple[int, int, int] | None):
        if value is not None and any(channel < 0 or channel > 255 for channel in value):
            raise ValueError("RGB channels must be between 0 and 255")
        return value

    @model_validator(mode="after")
    def validate_color_mode(self):
        has_hs = self.hue is not None or self.saturation is not None
        if self.rgb is not None and has_hs:
            raise ValueError("Provide RGB or hue/saturation, not both")
        if self.rgb is None and (self.hue is None or self.saturation is None):
            raise ValueError("Provide RGB or both hue and saturation")
        return self


class TemperatureRequest(BaseModel):
    kelvin: int


class EffectRequest(BaseModel):
    effect: str = Field(min_length=1, max_length=100)


class CommandResult(BaseModel):
    accepted: bool = True
    device_id: str


class GroupTargetFailure(BaseModel):
    device_id: str
    reason: Literal["unavailable", "service_failed"]


class GroupCommandResult(BaseModel):
    accepted: bool
    device_id: Literal["all_room_lights", "main_room_lights"] = "all_room_lights"
    succeeded: list[str] = Field(default_factory=list)
    failed: list[GroupTargetFailure] = Field(default_factory=list)


class LogicalGroup(BaseModel):
    id: Literal["all_room_lights"] = "all_room_lights"
    member_ids: list[str]


class SystemMetric(BaseModel):
    id: str
    value: str | float | int | bool | None
    unit: str | None = None
    available: bool
    stale: bool
    freshness: Literal["fresh", "stale", "unknown", "unavailable"]
    last_updated: datetime | None = None


class SystemNode(BaseModel):
    id: Literal["primary", "secondary"]
    name: str
    online: bool | None
    metrics: dict[str, SystemMetric]


class ServiceStatus(BaseModel):
    id: str
    status: Literal["online", "offline", "unknown"]
    health: Literal["healthy", "degraded", "offline", "unknown"]
    source: str
    stale: bool = False
    freshness: Literal["fresh", "stale", "unknown", "unavailable"]


class DnsResolverStatus(BaseModel):
    id: Literal["primary", "secondary"]
    role: Literal["primary", "secondary"]
    status: Literal["healthy", "degraded", "offline", "unknown"]
    last_checked: datetime | None = None
    response_time_ms: float | None = None
    # 7I: transport evidence. attempts=2 means the first reply was lost and the retry answered.
    attempts: int | None = None
    lost_reply_rate_1h: float | None = None
    p95_ms_1h: float | None = None


class DnsClusterStatus(BaseModel):
    status: Literal["healthy", "degraded", "offline", "unknown"]
    primary: DnsResolverStatus
    secondary: DnsResolverStatus
    last_checked: datetime | None = None


class SystemResponse(BaseModel):
    primary: SystemNode
    secondary: SystemNode
    services: list[ServiceStatus]
    dns_cluster: DnsClusterStatus


class ReleaseInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    release: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    revision: str = Field(pattern=r"^(?:unknown|[0-9a-f]{40})$")
    component: Literal["core"] = "core"


class CameraStatus(BaseModel):
    id: Literal["room_camera"] = "room_camera"
    display_name: str = "Room Camera"
    status: Literal["online", "offline", "unknown"]
    detection_status: Literal["active", "paused", "offline", "unknown"]
    frigate_health: Literal["healthy", "degraded", "offline", "unknown"]
    last_person_event: datetime | None = None
    last_person_event_freshness: Literal["recent", "stale", "none"] = "none"
    snapshot_available: bool = False
    live_view_available: bool = False
    last_checked: datetime | None = None


class CameraCatalog(BaseModel):
    cameras: list[CameraStatus]


# Every event type Core publishes on /api/v1/events. This Literal is the single source of
# truth: shared/api/event.schema.json is generated from it (tools/export_event_schema.py)
# and parity tests keep the schema, the emitting code and the Web union in step.
EventType = Literal[
    "roomos.connection",
    "light.state_changed",
    "scene.active_changed",
    "system.metric_updated",
    "system.snapshot_updated",
    "system.service_updated",
    "camera.updated",
    "device.updated",
    "alert.started",
    "alert.resolved",
    "alert.updated",
    "activity.recorded",
    "routine.changed",
    "routine.deleted",
    "routine.run_completed",
    "routine.diagnostics",
    "automation.changed",
    "automation.deleted",
    "automation.executed",
]


class RoomOSEvent(BaseModel):
    type: EventType
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    device_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class PairingRequest(BaseModel):
    code: str = Field(min_length=8, max_length=32)
    friendly_name: str = Field(min_length=1, max_length=60)

    @field_validator("code")
    @classmethod
    def normalize_code_shape(cls, value: str) -> str:
        compact = value.strip().replace("-", "").replace(" ", "").upper()
        if not compact.isalnum():
            raise ValueError("Pairing code is malformed")
        return compact

    @field_validator("friendly_name")
    @classmethod
    def normalize_friendly_name(cls, value: str) -> str:
        normalized = " ".join(value.strip().split())
        if not normalized:
            raise ValueError("Device name is required")
        return normalized


class AuthDevice(BaseModel):
    id: str
    friendly_name: str
    scopes: list[str]


class AuthStatus(BaseModel):
    authenticated: bool
    device: AuthDevice | None = None


class PairedClient(BaseModel):
    """A paired RoomOS app (7D.4 Device Center). Deliberately minimal: an opaque ID, the
    operator-entered name and coarse activity; never scopes, tokens or session data."""

    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    app: Literal["web", "android", "unknown"]
    paired_at: datetime
    last_seen_at: datetime | None
    active_session: bool
    current: bool


class PairedClientsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clients: list[PairedClient]
    last_seen_resolution_seconds: int
    generated_at: datetime


class PairingResult(BaseModel):
    authenticated: bool = True
    device: AuthDevice


class NativePairingResult(BaseModel):
    authenticated: bool = True
    session_token: str
    expires_at: datetime
    device: AuthDevice


class FcmRegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    registration_token: str = Field(
        min_length=32,
        max_length=4096,
        pattern=r"^[A-Za-z0-9_:\-]+$",
    )


class FcmRegistrationResponse(BaseModel):
    provider: Literal["fcm"] = "fcm"
    device_id: str
    destination_id: str
    registered_at: datetime
    token_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    destination_enabled: bool
    activated_at: datetime


class ClientTelemetryAccepted(BaseModel):
    """7D.3 ingest acknowledgement. No scopes, tokens, bindings or auth device IDs."""

    model_config = ConfigDict(extra="forbid")
    device_id: str
    stale_after_seconds: int
    received_at: datetime
