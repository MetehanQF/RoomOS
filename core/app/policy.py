from types import MappingProxyType

from fastapi import HTTPException, status

from .catalog import LIGHT_BY_ID
from .config import Settings
from .auth import security_audit

LightAction = str
PHYSICAL_ACTIONS = frozenset({"power", "brightness", "color", "temperature", "effect"})
PRODUCTION_POLICY = MappingProxyType(
    {
        **{light_id: PHYSICAL_ACTIONS for light_id in LIGHT_BY_ID},
        "all_room_lights": frozenset({"power"}),
    }
)


def writable_permissions(settings: Settings) -> dict[str, frozenset[str]]:
    if not settings.control_enabled:
        return {}
    if settings.control_policy == "production":
        return dict(PRODUCTION_POLICY)
    if settings.control_policy == "scoped":
        known_ids = frozenset((*LIGHT_BY_ID, "all_room_lights"))
        return {
            device_id: frozenset(
                action
                for action in settings.control_actions
                if action in PRODUCTION_POLICY[device_id]
            )
            for device_id in settings.control_device_ids
            if device_id in known_ids
        }
    return {}


def require_policy_permission(settings: Settings, device_id: str, action: LightAction) -> None:
    known_ids = frozenset((*LIGHT_BY_ID, "all_room_lights"))
    if device_id not in known_ids:
        security_audit("write_rejected_by_policy", result="unknown_device")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown RoomOS device")
    if action not in writable_permissions(settings).get(device_id, frozenset()):
        security_audit("write_rejected_by_policy", device_id=device_id, result="action_denied")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Action is outside the active RoomOS control policy",
        )
