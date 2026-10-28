"""Configuration: `reovault.toml` (or `$REOVAULT_CONFIG_FILE`) merged with
`REOVAULT_*` environment variables, which always win over the file.

No secrets live here. The restic repository password comes from
`RESTIC_PASSWORD_FILE` (a Docker secret) and camera passwords live in
reolink-cli's own encrypted registry; alert channel secrets are file paths.
"""

from __future__ import annotations

import os
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

# pydantic-settings resolves `toml_file` once, at class definition time; this
# lets `load_settings()` point at a different file on every call (tests).
_config_file_override: ContextVar[str | None] = ContextVar("_config_file_override", default=None)


class CameraConfig(BaseModel):
    alias: str
    """The `reolink-cli device add <alias>` name; also the restic repo name."""
    name: str | None = None
    timezone: str
    """IANA zone the camera's clock runs in; its search times are local."""
    enabled: bool = True
    demo: bool = False
    """A pretend camera with a sliding week of tiny clips, for trying
    ReoVault without hardware. Needs no reolink-cli registry entry."""


class BackupConfig(BaseModel):
    repository: str | None = None
    """restic repository base: a local path or e.g. `rclone:nextcloud:reovault`.
    Each camera gets its own repository at `<repository>/<alias>`."""
    password_file: Path | None = None
    """The restic password file; unset means `$RESTIC_PASSWORD_FILE`."""
    interval_minutes: int = Field(default=60, ge=1, le=1440)
    keep_days: int = Field(default=0, ge=0)
    """0 keeps backups forever."""


class AlertRules(BaseModel):
    enabled: bool = True
    renotify_hours: int = Field(default=24, ge=1, le=720)
    notify_on_resolve: bool = True


class AlertsConfig(BaseModel):
    """Where alerts go. Credentials are read from files (Docker secrets)."""

    ntfy_url: str | None = None
    ntfy_token_file: Path | None = None
    webhook_url_file: Path | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_starttls: bool = True
    smtp_user: str | None = None
    smtp_password_file: Path | None = None
    smtp_from: str | None = None
    smtp_to: list[str] = Field(default_factory=list)
    rules: AlertRules = Field(default_factory=AlertRules)


class WebConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8080
    # REOVAULT_WEB__PASSWORD_HASH is the Docker-secret path; the file is the
    # default. Without either, the API answers 503 until `web set-password`.
    password_hash: str | None = None
    # Login identity, only so password managers autofill reliably.
    email: str | None = None
    password_file: Path = Path("./data/web_password")
    session_key_path: Path = Path("./data/web_session.key")
    session_max_age_days: int = 30
    cookie_secure: bool = True
    cookie_samesite: str = "lax"
    check_origin: bool = True
    login_max_attempts: int = 5
    login_window_secs: int = 900
    login_lockout_secs: int = 300
    # uvicorn trusts X-Forwarded-* only from these addresses: the reverse
    # proxy's own IP/subnet, never "*".
    forwarded_allow_ips: str = "127.0.0.1"


class StorageConfig(BaseModel):
    data_dir: Path = Path("./data")
    staging_dir: Path = Path("./data/staging")


class ToolsConfig(BaseModel):
    reolink_cli: str = "reolink-cli"
    reolink_cli_version: str = "0.20.0"
    restic: str = "restic"
    restic_version: str = "0.19.1"
    rclone: str = "rclone"
    rclone_version: str = "1.75.2"
    # reolink-cli needs its gateway for search, storage and download.
    gateway_addr: str = "127.0.0.1:9210"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="REOVAULT_", env_nested_delimiter="__", extra="ignore"
    )

    cameras: list[CameraConfig] = Field(default_factory=list)
    backup: BackupConfig = Field(default_factory=BackupConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)

    def camera(self, alias: str) -> CameraConfig | None:
        return next((c for c in self.cameras if c.alias == alias), None)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        config_file = _config_file_override.get() or os.environ.get(
            "REOVAULT_CONFIG_FILE", "reovault.toml"
        )
        return (
            init_settings,
            env_settings,
            TomlConfigSettingsSource(settings_cls, toml_file=config_file),
            file_secret_settings,
        )


def load_settings(config_file: str | Path | None = None, **overrides: Any) -> Settings:
    """Settings from `config_file` (default `$REOVAULT_CONFIG_FILE` or
    `./reovault.toml`) merged with `REOVAULT_*` env vars, which always win."""
    token = _config_file_override.set(str(config_file) if config_file else None)
    try:
        return Settings(**overrides)
    finally:
        _config_file_override.reset(token)
