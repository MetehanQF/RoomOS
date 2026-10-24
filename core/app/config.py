from functools import lru_cache
from typing import Literal

from pydantic import Field, HttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "RoomOS Core"
    app_version: str = "0.2.0"
    release_name: str = Field(default="development", alias="ROOMOS_RELEASE")
    release_revision: str = Field(default="unknown", alias="ROOMOS_REVISION")
    release_component: Literal["core"] = Field(
        default="core", alias="ROOMOS_COMPONENT"
    )
    log_level: str = Field(default="INFO", alias="ROOMOS_LOG_LEVEL")
    ha_url: HttpUrl = Field(default="http://homeassistant.local:8123", alias="HA_URL")
    ha_token: SecretStr | None = Field(default=None, alias="HA_TOKEN")
    ha_connect_enabled: bool = Field(default=False, alias="ROOMOS_HA_CONNECT_ENABLED")
    control_enabled: bool = Field(default=False, alias="ROOMOS_CONTROL_ENABLED")
    control_policy: str = Field(default="read_only", alias="ROOMOS_CONTROL_POLICY")
    control_device_ids_csv: str = Field(default="", alias="ROOMOS_CONTROL_DEVICE_IDS")
    control_actions_csv: str = Field(default="", alias="ROOMOS_CONTROL_ACTIONS")
    scenes_config_path: str = Field(
        default="/var/lib/roomos-auth/scenes.json", alias="ROOMOS_SCENES_CONFIG_PATH"
    )
    executions_path: str = Field(default="/var/lib/roomos-auth/executions.json", alias="ROOMOS_EXECUTIONS_PATH")
    backup_status_path: str = Field(default="/run/roomos-backup-status/status.json", alias="ROOMOS_BACKUP_STATUS_PATH")
    scene_state_path: str = Field(
        default="/var/lib/roomos-auth/scene-state.json", alias="ROOMOS_SCENE_STATE_PATH"
    )
    scene_max_concurrency: int = Field(
        default=3, ge=1, le=5, alias="ROOMOS_SCENE_MAX_CONCURRENCY"
    )
    scene_duplicate_window_seconds: float = Field(
        default=2.0, ge=0.5, le=10.0, alias="ROOMOS_SCENE_DUPLICATE_WINDOW_SECONDS"
    )
    routines_path: str = Field(
        default="/var/lib/roomos-auth/routines.json", alias="ROOMOS_ROUTINES_PATH"
    )
    routine_evaluations_path: str = Field(
        default="/var/lib/roomos-auth/routine-evaluations.json", alias="ROOMOS_ROUTINE_EVALUATIONS_PATH"
    )
    presence_reporting_threshold_seconds: int = Field(
        default=21600, ge=3600, le=604800, alias="ROOMOS_PRESENCE_REPORTING_THRESHOLD_SECONDS"
    )
    routines_timezone: str = Field(
        default="UTC", alias="ROOMOS_ROUTINES_TIMEZONE"
    )
    routines_scheduler_interval_seconds: float = Field(
        default=10.0,
        ge=1.0,
        le=30.0,
        alias="ROOMOS_ROUTINES_SCHEDULER_INTERVAL_SECONDS",
    )
    routines_max_concurrency: int = Field(
        default=2, ge=1, le=5, alias="ROOMOS_ROUTINES_MAX_CONCURRENCY"
    )
    automations_path: str = Field(
        default="/var/lib/roomos-auth/automations.json", alias="ROOMOS_AUTOMATIONS_PATH"
    )
    automation_history_path: str = Field(
        default="/var/lib/roomos-auth/automation-history.jsonl",
        alias="ROOMOS_AUTOMATION_HISTORY_PATH",
    )
    automation_history_limit: int = Field(
        default=500, ge=3, le=2000, alias="ROOMOS_AUTOMATION_HISTORY_LIMIT"
    )
    automation_history_retention_days: int = Field(
        default=30, ge=1, le=365, alias="ROOMOS_AUTOMATION_HISTORY_RETENTION_DAYS"
    )
    dns_monitor_enabled: bool = Field(default=False, alias="ROOMOS_DNS_MONITOR_ENABLED")
    dns_primary_address: str = Field(
        default="192.0.2.53", alias="ROOMOS_DNS_PRIMARY_ADDRESS"
    )
    dns_secondary_address: str = Field(
        default="192.0.2.54", alias="ROOMOS_DNS_SECONDARY_ADDRESS"
    )
    dns_probe_name: str = Field(default="example.com.", alias="ROOMOS_DNS_PROBE_NAME")
    dns_probe_interval_seconds: float = Field(
        default=60.0, ge=30.0, le=300.0, alias="ROOMOS_DNS_PROBE_INTERVAL_SECONDS"
    )
    dns_probe_timeout_seconds: float = Field(
        default=2.0, ge=0.5, le=10.0, alias="ROOMOS_DNS_PROBE_TIMEOUT_SECONDS"
    )
    dns_probe_startup_delay_seconds: float = Field(
        default=1.0, ge=0.0, le=10.0, alias="ROOMOS_DNS_PROBE_STARTUP_DELAY_SECONDS"
    )
    camera_refresh_interval_seconds: float = Field(
        default=60.0, ge=30.0, le=300.0, alias="ROOMOS_CAMERA_REFRESH_INTERVAL_SECONDS"
    )
    camera_refresh_startup_delay_seconds: float = Field(
        default=1.0, ge=0.0, le=10.0, alias="ROOMOS_CAMERA_REFRESH_STARTUP_DELAY_SECONDS"
    )
    camera_event_recent_hours: int = Field(
        default=24, ge=1, le=168, alias="ROOMOS_CAMERA_EVENT_RECENT_HOURS"
    )
    alert_policy_path: str = Field(
        default="/var/lib/roomos-auth/alert-policy.json", alias="ROOMOS_ALERT_POLICY_PATH"
    )
    metrics_history_path: str = Field(
        default="/var/lib/roomos-auth/metrics-history.db", alias="ROOMOS_METRICS_HISTORY_PATH"
    )
    alerts_path: str = Field(
        default="/var/lib/roomos-auth/alerts.json", alias="ROOMOS_ALERTS_PATH"
    )
    alerts_history_limit: int = Field(
        default=300, ge=50, le=1000, alias="ROOMOS_ALERTS_HISTORY_LIMIT"
    )
    # A planned Secondary reboot takes ~1 min; only a node that stays down for 5 min is an incident.
    alerts_node_confirmation_seconds: float = Field(
        default=300.0,
        ge=5.0,
        le=300.0,
        alias="ROOMOS_ALERTS_NODE_CONFIRMATION_SECONDS",
    )
    notifications_path: str = Field(
        default="/var/lib/roomos-auth/notifications.json",
        alias="ROOMOS_NOTIFICATIONS_PATH",
    )
    notification_incidents_path: str = Field(
        default="/var/lib/roomos-auth/notification-incidents.json",
        alias="ROOMOS_NOTIFICATION_INCIDENTS_PATH",
    )
    notifications_queue_limit: int = Field(
        default=200, ge=50, le=1000, alias="ROOMOS_NOTIFICATIONS_QUEUE_LIMIT"
    )
    notifications_max_attempts: int = Field(
        default=3, ge=1, le=10, alias="ROOMOS_NOTIFICATIONS_MAX_ATTEMPTS"
    )
    notifications_retry_base_seconds: float = Field(
        default=30.0,
        ge=1.0,
        le=3600.0,
        alias="ROOMOS_NOTIFICATIONS_RETRY_BASE_SECONDS",
    )
    notifications_provider_timeout_seconds: float = Field(
        default=10.0,
        ge=1.0,
        le=60.0,
        alias="ROOMOS_NOTIFICATIONS_PROVIDER_TIMEOUT_SECONDS",
    )
    fcm_enabled: bool = Field(default=False, alias="ROOMOS_FCM_ENABLED")
    fcm_credentials_path: str = Field(
        default="/run/secrets/roomos-fcm-service-account.json",
        alias="ROOMOS_FCM_CREDENTIALS_PATH",
    )
    notification_destinations_path: str = Field(
        default="/var/lib/roomos-auth/notification-destinations.json",
        alias="ROOMOS_NOTIFICATION_DESTINATIONS_PATH",
    )
    fcm_targets_path: str = Field(
        default="/var/lib/roomos-auth/fcm-targets.json",
        alias="ROOMOS_FCM_TARGETS_PATH",
    )
    notifications_dispatcher_max_sleep_seconds: float = Field(
        default=60.0,
        ge=1.0,
        le=300.0,
        alias="ROOMOS_NOTIFICATIONS_DISPATCHER_MAX_SLEEP_SECONDS",
    )
    # Phase 7G. "legacy" keeps the exact 7F delivery path; "smart" routes AlertEngine lifecycles
    # through the 7G dispatcher; "off" delivers nothing. Activation is a separate approved change.
    push_mode: Literal["legacy", "smart", "off"] = Field(
        default="legacy", alias="ROOMOS_PUSH_MODE"
    )
    push_registry_path: str = Field(
        default="/var/lib/roomos-auth/push-registry.json", alias="ROOMOS_PUSH_REGISTRY_PATH"
    )
    push_outbox_path: str = Field(
        default="/var/lib/roomos-auth/push-outbox.json", alias="ROOMOS_PUSH_OUTBOX_PATH"
    )
    push_timezone: str = Field(default="UTC", alias="ROOMOS_PUSH_TIMEZONE")
    webpush_vapid_private_key_path: str = Field(
        default="/run/secrets/roomos-vapid-private.pem",
        alias="ROOMOS_WEBPUSH_VAPID_PRIVATE_KEY_PATH",
    )
    webpush_vapid_subject: str = Field(
        default="https://roomos.example.com", alias="ROOMOS_WEBPUSH_VAPID_SUBJECT"
    )
    # Test harness only: extra push-service hosts and an FCM base URL for synthetic providers.
    # Production leaves both at their defaults.
    webpush_extra_endpoint_hosts: str = Field(
        default="", alias="ROOMOS_WEBPUSH_EXTRA_ENDPOINT_HOSTS"
    )
    fcm_endpoint_base: str = Field(
        default="https://fcm.googleapis.com", alias="ROOMOS_FCM_ENDPOINT_BASE"
    )
    system_monitor_interval_seconds: float = Field(
        default=60.0,
        ge=30.0,
        le=300.0,
        alias="ROOMOS_SYSTEM_MONITOR_INTERVAL_SECONDS",
    )
    system_monitor_startup_delay_seconds: float = Field(
        default=1.0,
        ge=0.0,
        le=10.0,
        alias="ROOMOS_SYSTEM_MONITOR_STARTUP_DELAY_SECONDS",
    )
    # Device Registry (7D.1): per-source telemetry/reachability staleness thresholds.
    devices_system_monitor_stale_seconds: int = Field(
        default=180, ge=60, le=3600, alias="ROOMOS_DEVICES_SYSTEM_MONITOR_STALE_SECONDS"
    )
    devices_home_assistant_stale_seconds: int = Field(
        default=900, ge=60, le=86400, alias="ROOMOS_DEVICES_HOME_ASSISTANT_STALE_SECONDS"
    )
    devices_mqtt_stale_seconds: int = Field(
        default=300, ge=30, le=86400, alias="ROOMOS_DEVICES_MQTT_STALE_SECONDS"
    )
    devices_roomos_native_stale_seconds: int = Field(
        default=120, ge=30, le=86400, alias="ROOMOS_DEVICES_ROOMOS_NATIVE_STALE_SECONDS"
    )
    devices_roomos_web_stale_seconds: int = Field(
        default=120, ge=30, le=86400, alias="ROOMOS_DEVICES_ROOMOS_WEB_STALE_SECONDS"
    )
    # Phase 7D.3 Android telemetry. Unset = no phone reports anything (production default).
    # JSON object: RoomOS auth device id (the paired app) -> {"device": catalog id, "app":
    # "android" | "web"}. An explicit operator allowlist; nothing is inferred from names.
    devices_client_telemetry: str | None = Field(default=None, alias="ROOMOS_DEVICES_CLIENT_TELEMETRY")
    devices_client_telemetry_min_interval_seconds: int = Field(
        default=15, ge=5, le=600, alias="ROOMOS_DEVICES_CLIENT_TELEMETRY_MIN_INTERVAL_SECONDS"
    )

    # Phase 7D.2 Workstation onboarding. Unset = not configured (production default). Values are an
    # explicit, operator-verified allowlist: HASS.Agent device name and a JSON object mapping
    # RoomOS sensor ids (plus the required "heartbeat") to Home Assistant entity_ids.
    devices_workstation_agent_id: str | None = Field(default=None, alias="ROOMOS_DEVICES_WORKSTATION_AGENT_ID")
    devices_workstation_entities: str | None = Field(default=None, alias="ROOMOS_DEVICES_WORKSTATION_ENTITIES")
    devices_windows_heartbeat_seconds: int = Field(
        default=120, ge=45, le=3600, alias="ROOMOS_DEVICES_WINDOWS_HEARTBEAT_SECONDS"
    )

    def device_stale_thresholds(self) -> dict[str, int]:
        return {
            "system_monitor": self.devices_system_monitor_stale_seconds,
            "home_assistant": self.devices_home_assistant_stale_seconds,
            "mqtt": self.devices_mqtt_stale_seconds,
            "roomos_native": self.devices_roomos_native_stale_seconds,
            "roomos_web": self.devices_roomos_web_stale_seconds,
        }
    auth_enabled: bool = Field(default=True, alias="ROOMOS_AUTH_ENABLED")
    auth_database_path: str = Field(
        default="/var/lib/roomos-auth/roomos-auth.db", alias="ROOMOS_AUTH_DATABASE_PATH"
    )
    auth_secret_path: str = Field(
        default="/var/lib/roomos-auth/server-secret", alias="ROOMOS_AUTH_SECRET_PATH"
    )
    auth_allowed_origin: str = Field(
        default="https://roomos.example.com", alias="ROOMOS_AUTH_ALLOWED_ORIGIN"
    )
    auth_allowed_host: str = Field(
        default="roomos.example.com", alias="ROOMOS_AUTH_ALLOWED_HOST"
    )
    auth_session_sliding_days: int = Field(
        default=30, ge=1, le=90, alias="ROOMOS_AUTH_SESSION_SLIDING_DAYS"
    )
    auth_session_absolute_days: int = Field(
        default=180, ge=7, le=365, alias="ROOMOS_AUTH_SESSION_ABSOLUTE_DAYS"
    )
    auth_pairing_ttl_seconds: int = Field(
        default=600, ge=60, le=1800, alias="ROOMOS_AUTH_PAIRING_TTL_SECONDS"
    )
    auth_pairing_attempts: int = Field(
        default=5, ge=1, le=20, alias="ROOMOS_AUTH_PAIRING_ATTEMPTS"
    )
    auth_pairing_window_seconds: int = Field(
        default=300, ge=60, le=1800, alias="ROOMOS_AUTH_PAIRING_WINDOW_SECONDS"
    )
    ha_reconnect_min_seconds: float = Field(
        default=1.0, ge=1.0, alias="ROOMOS_HA_RECONNECT_MIN_SECONDS"
    )
    ha_reconnect_max_seconds: float = Field(
        default=60.0, ge=5.0, alias="ROOMOS_HA_RECONNECT_MAX_SECONDS"
    )

    @property
    def ha_rest_url(self) -> str:
        return str(self.ha_url).rstrip("/")

    @property
    def ha_ws_url(self) -> str:
        base = self.ha_rest_url
        scheme = "wss" if base.startswith("https://") else "ws"
        return f"{scheme}://{base.split('://', 1)[1]}/api/websocket"

    @property
    def has_ha_credentials(self) -> bool:
        return bool(self.ha_token and self.ha_token.get_secret_value())

    @property
    def control_device_ids(self) -> frozenset[str]:
        return frozenset(item.strip() for item in self.control_device_ids_csv.split(",") if item.strip())

    @property
    def control_actions(self) -> frozenset[str]:
        return frozenset(item.strip() for item in self.control_actions_csv.split(",") if item.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
