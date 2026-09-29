from functools import lru_cache

from pydantic import Field, HttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "RoomOS Core"
    app_version: str = "0.1.0"
    log_level: str = Field(default="INFO", alias="ROOMOS_LOG_LEVEL")
    ha_url: HttpUrl = Field(default="http://homeassistant.local:8123", alias="HA_URL")
    ha_token: SecretStr | None = Field(default=None, alias="HA_TOKEN")
    device_registry_path: str = Field(default="", alias="ROOMOS_DEVICE_REGISTRY")
    ha_connect_enabled: bool = Field(default=False, alias="ROOMOS_HA_CONNECT_ENABLED")
    control_enabled: bool = Field(default=False, alias="ROOMOS_CONTROL_ENABLED")
    control_policy: str = Field(default="read_only", alias="ROOMOS_CONTROL_POLICY")
    control_device_ids_csv: str = Field(default="", alias="ROOMOS_CONTROL_DEVICE_IDS")
    control_actions_csv: str = Field(default="", alias="ROOMOS_CONTROL_ACTIONS")
    auth_enabled: bool = Field(default=True, alias="ROOMOS_AUTH_ENABLED")
    auth_database_path: str = Field(
        default="data/roomos-auth.db", alias="ROOMOS_AUTH_DATABASE_PATH"
    )
    auth_secret_path: str = Field(
        default="data/server-secret", alias="ROOMOS_AUTH_SECRET_PATH"
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
