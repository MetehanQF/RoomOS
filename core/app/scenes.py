import asyncio
import json
import logging
import os
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .executions import ExecutionStore, ExecutionId, TargetExecution, duration_ms
from .audit import command_audit
from .catalog import LIGHTS, LIGHT_BY_ID
from .config import Settings
from .ha_client import HomeAssistantClient, HomeAssistantUnavailable
from .policy import require_policy_permission, writable_permissions
from .models import LightState
from .service import light_from_ha, list_lights, validate_kelvin, validate_live_effect

SCENE_NAMES = {
    "normal": "Normal",
    "work_1": "Work 1",
    "work_2": "Work 2",
    "gaming_1": "Game 1",
    "gaming_2": "Game 2",
    "movie": "Film",
    "night": "Gece",
    "all_off": "Tümünü Kapat",
}

logger = logging.getLogger(__name__)

BRIGHTNESS_TOLERANCE = 2
KELVIN_TOLERANCE = 50
RGB_CHANNEL_TOLERANCE = 3


class SceneConfigurationError(RuntimeError):
    pass


class SceneTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str
    power: bool | None = None
    brightness: int | None = Field(default=None, ge=1, le=255)
    rgb: tuple[int, int, int] | None = None
    kelvin: int | None = Field(default=None, ge=1000, le=10000)
    effect: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("rgb")
    @classmethod
    def validate_rgb(cls, value: tuple[int, int, int] | None):
        if value is not None and any(channel < 0 or channel > 255 for channel in value):
            raise ValueError("RGB channels must be between 0 and 255")
        return value

    @model_validator(mode="after")
    def validate_action(self):
        attributes = (self.brightness, self.rgb, self.kelvin, self.effect)
        if self.power is None and all(value is None for value in attributes):
            raise ValueError("Scene target must define an action")
        if self.power is False and any(value is not None for value in attributes):
            raise ValueError("An off target cannot define light attributes")
        return self

    def actions(self) -> tuple[str, ...]:
        actions: list[str] = []
        if self.power is not None:
            actions.append("power")
        if self.brightness is not None:
            actions.append("brightness")
        if self.rgb is not None:
            actions.append("color")
        if self.kelvin is not None:
            actions.append("temperature")
        if self.effect is not None:
            actions.append("effect")
        return tuple(actions)


class SceneConfigEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Literal[
        "normal", "work_1", "work_2", "gaming_1", "gaming_2", "movie", "night", "all_off"
    ]
    targets: list[SceneTarget] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def validate_targets(self):
        ids = [target.device_id for target in self.targets]
        if len(ids) != len(set(ids)):
            raise ValueError("Scene targets must be unique")
        if any(device_id not in LIGHT_BY_ID for device_id in ids):
            raise ValueError("Scene target must be an approved logical light ID")
        return self


class SceneConfigFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenes: list[SceneConfigEntry] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def validate_scenes(self):
        ids = [scene.id for scene in self.scenes]
        if len(ids) != len(set(ids)):
            raise ValueError("Scene IDs must be unique")
        return self


class SceneSummary(BaseModel):
    id: str
    name: str
    configured: bool
    activatable: bool
    target_count: int


class SceneTargetResult(BaseModel):
    device: str
    reason: str


class SceneLastApplied(BaseModel):
    scene: str
    name: str
    status: Literal["success", "partial"]
    applied_at: datetime


class SceneListResponse(BaseModel):
    scenes: list[SceneSummary]
    last_applied: SceneLastApplied | None = None
    active_scene: str | None = None


class SceneActivationResult(BaseModel):
    execution_id: ExecutionId | None = None
    scene: str
    name: str
    status: Literal["success", "partial", "failed"]
    applied: list[str] = Field(default_factory=list)
    skipped: list[SceneTargetResult] = Field(default_factory=list)
    failed: list[SceneTargetResult] = Field(default_factory=list)
    applied_at: datetime


def load_scene_config(path: str) -> dict[str, SceneConfigEntry]:
    config_path = Path(path)
    try:
        if not config_path.exists():
            return {}
        parsed = SceneConfigFile.model_validate_json(config_path.read_text(encoding="utf-8"))
    except PermissionError:
        logger.warning("RoomOS scene configuration is unreadable; scenes remain unconfigured")
        return {}
    except (OSError, ValidationError, json.JSONDecodeError) as exc:
        raise SceneConfigurationError("RoomOS scene configuration is invalid") from exc
    return {scene.id: scene for scene in parsed.scenes}


def all_off_scene() -> SceneConfigEntry:
    return SceneConfigEntry(
        id="all_off",
        targets=[SceneTarget(device_id=light.id, power=False) for light in LIGHTS],
    )


def install_all_off_scene(path: str) -> SceneConfigFile:
    """Atomically append the fixed all-off scene while preserving the original seven."""

    destination = Path(path)
    try:
        document = SceneConfigFile.model_validate_json(destination.read_text(encoding="utf-8"))
    except (OSError, ValidationError, json.JSONDecodeError) as exc:
        raise SceneConfigurationError("RoomOS scene configuration is invalid") from exc
    expected_existing = set(SCENE_NAMES) - {"all_off"}
    current = {scene.id: scene for scene in document.scenes}
    if not expected_existing.issubset(current):
        raise SceneConfigurationError("The seven production scenes must exist before install")
    fixed = all_off_scene()
    if "all_off" in current and current["all_off"] != fixed:
        raise SceneConfigurationError("Existing all-off scene does not match the fixed contract")
    if "all_off" not in current:
        document.scenes.append(fixed)
    document = SceneConfigFile.model_validate(document)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(document.model_dump_json(exclude_none=True))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return document


def load_scene_state(path: str) -> SceneLastApplied | None:
    state_path = Path(path)
    try:
        if not state_path.exists():
            return None
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        return SceneLastApplied.model_validate(raw.get("last_applied"))
    except (PermissionError, OSError, ValidationError, json.JSONDecodeError, TypeError):
        logger.warning("RoomOS scene history is unreadable; last_applied starts empty")
        return None


def _within(actual: int | None, expected: int, tolerance: int) -> bool:
    return actual is not None and abs(actual - expected) <= tolerance


def target_matches_live(target: SceneTarget, live: LightState) -> bool:
    if not live.available:
        return False
    if target.power is not None and live.on is not target.power:
        return False
    if target.power is False:
        return True
    has_attributes = any(
        value is not None for value in (target.brightness, target.rgb, target.kelvin, target.effect)
    )
    if has_attributes and live.on is not True:
        return False
    if target.brightness is not None and not _within(
        live.brightness, target.brightness, BRIGHTNESS_TOLERANCE
    ):
        return False
    if target.kelvin is not None and not _within(
        live.color_temperature_kelvin, target.kelvin, KELVIN_TOLERANCE
    ):
        return False
    if target.rgb is not None:
        if live.rgb is None or any(
            abs(actual - expected) > RGB_CHANNEL_TOLERANCE
            for actual, expected in zip(live.rgb, target.rgb, strict=True)
        ):
            return False
    if target.effect is not None:
        if live.effect != target.effect:
            return False
        if live.color_mode in {"color_temp", "color_temp_kelvin"}:
            return False
    return True


class SceneEngine:
    def __init__(self, settings: Settings, configured: dict[str, SceneConfigEntry] | None = None, executions: ExecutionStore | None = None):
        self.settings = settings
        self.executions = executions or ExecutionStore(None)
        self.configured = load_scene_config(settings.scenes_config_path) if configured is None else configured
        self._locks = {scene_id: asyncio.Lock() for scene_id in SCENE_NAMES}
        self._last_started = {scene_id: 0.0 for scene_id in SCENE_NAMES}
        self._persist_enabled = configured is None
        self._last_applied = (
            load_scene_state(settings.scene_state_path) if self._persist_enabled else None
        )
        self._active_scene: str | None = None
        self._live_lights: dict[str, LightState] = {}
        self._activity_sink: Callable[..., Awaitable[None]] | None = None
        self.state_write_failed = False

    def set_activity_sink(self, sink: Callable[..., Awaitable[None]]) -> None:
        self._activity_sink = sink

    @property
    def active_scene(self) -> str | None:
        return self._active_scene

    def _persist_last_applied(self) -> None:
        if self._last_applied is None or not self._persist_enabled:
            return
        destination = Path(self.settings.scene_state_path)
        temporary = destination.with_name(f".{destination.name}.tmp")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                {"last_applied": self._last_applied.model_dump(mode="json")},
                separators=(",", ":"),
            )
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
            self.state_write_failed = False
            directory_fd = os.open(destination.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            self.state_write_failed = True
            logger.warning("RoomOS scene history could not be persisted", exc_info=True)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def match_active_scene(self, lights: dict[str, LightState]) -> str | None:
        candidates = [
            scene_id
            for scene_id in SCENE_NAMES
            if (scene := self.configured.get(scene_id))
            and all(
                (live := lights.get(target.device_id)) is not None
                and target_matches_live(target, live)
                for target in scene.targets
            )
        ]
        if self._last_applied and self._last_applied.scene in candidates:
            return self._last_applied.scene
        return candidates[0] if candidates else None

    def reconcile(self, lights: list[LightState]) -> bool:
        self._live_lights = {light.id: light for light in lights}
        active = self.match_active_scene(self._live_lights)
        changed = active != self._active_scene
        self._active_scene = active
        return changed

    def reconcile_light(self, light: LightState) -> bool:
        self._live_lights[light.id] = light
        active = self.match_active_scene(self._live_lights)
        changed = active != self._active_scene
        self._active_scene = active
        return changed

    async def refresh_active(self, ha: HomeAssistantClient) -> bool:
        if hasattr(ha, "get_states"):
            lights = await list_lights(ha)
        else:
            lights = [
                light_from_ha(definition, await ha.get_state(definition.entity_id))
                for definition in LIGHTS
            ]
        return self.reconcile(lights)

    async def ensure_live_snapshot(self, ha: HomeAssistantClient) -> None:
        required = {
            target.device_id for scene in self.configured.values() for target in scene.targets
        }
        if not required.issubset(self._live_lights):
            await self.refresh_active(ha)

    def list(self, *, can_write: bool) -> SceneListResponse:
        permissions = writable_permissions(self.settings)

        def activatable(scene_id: str) -> bool:
            scene = self.configured.get(scene_id)
            return bool(
                can_write
                and scene
                and all(
                    set(target.actions()).issubset(permissions.get(target.device_id, frozenset()))
                    for target in scene.targets
                )
            )

        return SceneListResponse(
            scenes=[
                SceneSummary(
                    id=scene_id,
                    name=name,
                    configured=scene_id in self.configured,
                    activatable=activatable(scene_id),
                    target_count=len(self.configured[scene_id].targets) if scene_id in self.configured else 0,
                )
                for scene_id, name in SCENE_NAMES.items()
            ],
            last_applied=self._last_applied,
            active_scene=self._active_scene,
        )

    def _preflight(self, scene: SceneConfigEntry) -> None:
        for target in scene.targets:
            definition = LIGHT_BY_ID[target.device_id]
            for action in target.actions():
                require_policy_permission(self.settings, target.device_id, action)
            if target.kelvin is not None:
                validate_kelvin(definition, target.kelvin)

    async def _apply_target(
        self, ha: HomeAssistantClient, target: SceneTarget, semaphore: asyncio.Semaphore, diagnostic: dict | None = None
    ) -> tuple[str, str, str | None]:
        diagnostic = diagnostic if diagnostic is not None else {}
        diagnostic["stage"] = "read"
        async with semaphore:
            definition = LIGHT_BY_ID[target.device_id]
            try:
                current = light_from_ha(definition, await ha.get_state(definition.entity_id))
            except HomeAssistantUnavailable:
                return ("failed", target.device_id, "state_unavailable")
            if not current.available:
                return ("skipped", target.device_id, "unavailable")
            for action in target.actions():
                capability = "effects" if action == "effect" else action
                if not getattr(current.capabilities, capability, False):
                    return ("failed", target.device_id, "capability_not_advertised")
            if target.effect is not None:
                try:
                    validate_live_effect(current, target.effect)
                except HTTPException:
                    return ("failed", target.device_id, "unsupported_effect")

            payload: dict[str, object] = {"entity_id": definition.entity_id}
            if target.brightness is not None:
                payload["brightness"] = target.brightness
            if target.rgb is not None:
                payload["rgb_color"] = list(target.rgb)
            if target.kelvin is not None:
                payload["color_temp_kelvin"] = target.kelvin
            if target.effect is not None:
                payload["effect"] = target.effect
            service = "turn_off" if target.power is False else "turn_on"
            try:
                # Some physical lights acknowledge a combined OFF -> ON + attributes
                # request in HA without actually illuminating. Wake an off target first,
                # confirm the bounded state transition, then apply its attributes.
                has_attributes = len(payload) > 1
                if target.power is True and not current.on and has_attributes:
                    diagnostic["stage"] = "wake"
                    await ha.call_service(
                        "light", "turn_on", {"entity_id": definition.entity_id}
                    )
                    confirmed = False
                    for _ in range(5):
                        await asyncio.sleep(0.15)
                        refreshed_raw = await ha.get_state(definition.entity_id)
                        if refreshed_raw is None:
                            continue
                        refreshed = light_from_ha(definition, refreshed_raw)
                        if refreshed.available and refreshed.on:
                            confirmed = True
                            break
                    if not confirmed:
                        return ("failed", target.device_id, "power_on_not_confirmed")
                diagnostic["stage"] = "apply"
                await ha.call_service("light", service, payload)
            except HomeAssistantUnavailable:
                return ("failed", target.device_id, "service_failed")
            return ("applied", target.device_id, None)

    def execution_context(self, scene_id, source="user", routine_id=None, routine_name=None, trigger="manual"):
        # Names come from the loaded server catalog, never request bodies. Names
        # outside the diagnostic alphabet fall back to a fixed safe label.
        name = routine_name if routine_name and re.fullmatch(r"[\w ()—:-]{1,60}", routine_name) else "Routine"
        return dict(scene_id=scene_id, scene_name=SCENE_NAMES[scene_id],
                    source="routine" if source == "routine" else "manual", trigger=trigger,
                    routine_id=routine_id, routine_name=name if source == "routine" else None)

    async def publish_execution(self, record):
        if self._activity_sink is not None:
            try:
                await self._activity_sink(scene_id=record.scene_id, scene_name=record.scene_name,
                    status=record.result, occurred_at=record.finished_at or record.started_at,
                    source=record.source, execution_id=record.id)
            except Exception:
                logger.warning("Execution activity write failed; control outcome unchanged")

    async def record_rejection(self, scene_id, *, reason, source="routine", routine_id=None, routine_name=None, trigger="manual"):
        record = await self.executions.begin(**self.execution_context(scene_id, source, routine_id, routine_name, trigger))
        record = await self.executions.finish(record, result="rejected", reason=reason)
        await self.publish_execution(record)
        return record.id

    async def activate(self, scene_id, ha, *, source="user", routine_id=None, routine_name=None, trigger="manual"):
        context = {}
        try:
            return await self._activate(scene_id, ha, source=source, routine_id=routine_id,
                                        routine_name=routine_name, trigger=trigger, context=context)
        except Exception as exc:
            record = context.get("record")
            if record is not None:
                record = await self.executions.finish(record, result="failed", reason="internal_error",
                                                       targets=context.get("targets", []))
                await self.publish_execution(record)
                setattr(exc, "execution_id", record.id)
            elif scene_id in SCENE_NAMES and not (isinstance(exc, HTTPException) and exc.status_code == 429 and source != "routine"):
                reason = {403: "policy_rejected", 422: "invalid_action", 409: "not_configured", 429: "duplicate"}.get(
                    getattr(exc, "status_code", None), "internal_error")
                execution_id = await self.record_rejection(scene_id, reason=reason, source=source,
                    routine_id=routine_id, routine_name=routine_name, trigger=trigger)
                setattr(exc, "execution_id", execution_id)
            raise

    async def _observed_apply(self, ha, target, semaphore, targets):
        diagnostic = {"stage": "read"}
        started = time.monotonic()
        outcome, device_id, reason = await self._apply_target(ha, target, semaphore, diagnostic)
        reasons = {"state_unavailable": "ha_unavailable", "capability_not_advertised": "capability_rejected",
                   "unsupported_effect": "capability_rejected", "service_failed": "ha_error",
                   "unavailable": "unavailable", "power_on_not_confirmed": "power_on_not_confirmed"}
        targets.append(TargetExecution(device_id=device_id, actions=list(target.actions()), result=outcome,
            reason=reasons.get(reason), stage=diagnostic["stage"], duration_ms=duration_ms(time.monotonic()-started)))
        return outcome, device_id, reason

    async def _activate(
        self,
        scene_id: str,
        ha: HomeAssistantClient,
        *,
        source: str = "user",
        routine_id: str | None = None,
        routine_name: str | None = None,
        trigger: str = "manual",
        context: dict,
    ) -> SceneActivationResult:
        if scene_id not in SCENE_NAMES:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown scene ID")
        scene = self.configured.get(scene_id)
        if scene is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Scene values have not been configured yet",
            )
        self._preflight(scene)
        lock = self._locks[scene_id]
        now = time.monotonic()
        if lock.locked() or now - self._last_started[scene_id] < self.settings.scene_duplicate_window_seconds:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Scene activation is already in progress",
            )

        async with lock:
            self._last_started[scene_id] = time.monotonic()
            context["record"] = await self.executions.begin(**self.execution_context(
                scene_id, source, routine_id, routine_name, trigger))
            context["targets"] = []
            semaphore = asyncio.Semaphore(self.settings.scene_max_concurrency)
            results = await asyncio.gather(
                *(self._observed_apply(ha, target, semaphore, context["targets"]) for target in scene.targets)
            )

        applied = [device_id for outcome, device_id, _ in results if outcome == "applied"]
        skipped = [
            SceneTargetResult(device=device_id, reason=reason or "unavailable")
            for outcome, device_id, reason in results
            if outcome == "skipped"
        ]
        failed = [
            SceneTargetResult(device=device_id, reason=reason or "service_failed")
            for outcome, device_id, reason in results
            if outcome == "failed"
        ]
        result_status: Literal["success", "partial", "failed"]
        if applied and not skipped and not failed:
            result_status = "success"
        elif applied or (skipped and not failed):
            result_status = "partial"
        else:
            result_status = "failed"
        applied_at = datetime.now(UTC)
        result = SceneActivationResult(
            execution_id=context["record"].id,
            scene=scene_id,
            name=SCENE_NAMES[scene_id],
            status=result_status,
            applied=applied,
            skipped=skipped,
            failed=failed,
            applied_at=applied_at,
        )
        if applied and result_status in {"success", "partial"}:
            self._last_applied = SceneLastApplied(
                scene=scene_id,
                name=SCENE_NAMES[scene_id],
                status=result_status,
                applied_at=applied_at,
            )
            self._persist_last_applied()
        try:
            await self.refresh_active(ha)
        except HomeAssistantUnavailable:
            logger.warning("Scene applied but live-state reconciliation is temporarily unavailable")
        command_audit(
            scene_id,
            "scene_activate",
            result_status,
            succeeded=applied,
            skipped=[item.device for item in skipped],
            failed=[item.device for item in failed],
            source=source,
            routine_id=routine_id,
        )
        ordered_targets = sorted(context["targets"], key=lambda t: [x.device_id for x in scene.targets].index(t.device_id))
        record = await self.executions.finish(context["record"], result=result_status, targets=ordered_targets)
        await self.publish_execution(record)
        return result
