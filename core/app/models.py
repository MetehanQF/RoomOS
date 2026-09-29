from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


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
    device_id: Literal["all_room_lights"] = "all_room_lights"
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
    last_updated: datetime | None = None


class SystemNode(BaseModel):
    id: str
    name: str
    online: bool | None
    metrics: dict[str, SystemMetric]


class ServiceStatus(BaseModel):
    id: str
    status: Literal["online", "offline", "unknown"]
    source: str


class SystemResponse(BaseModel):
    """Nodes are keyed by the logical node IDs declared in the device registry."""

    nodes: dict[str, SystemNode]
    services: list[ServiceStatus]


class RoomOSEvent(BaseModel):
    type: str
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


class PairingResult(BaseModel):
    authenticated: bool = True
    device: AuthDevice
