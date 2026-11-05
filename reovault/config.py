"""Configuration: `reovault.toml` (or `$REOVAULT_CONFIG_FILE`) merged with
`REOVAULT_*` environment variables, which always win over the file.

Camera passwords live here, next to each camera's address: keep the file
private (0600). The restic password is its own file (`RESTIC_PASSWORD_FILE`),
and alert channel secrets are file paths.
"""

from __future__ import annotations

import os
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, SecretStr, model_validator
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
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    """Names the camera's restic repository (`<repository>/<id>`): never changes."""
    name: str | None = None
    timezone: str
    """IANA zone the camera's clock runs in; its search times are local."""
    host: str | None = None
    """The camera's IP address or host name."""
    user: str = "admin"
    password: SecretStr | None = None
    enabled: bool = True
    demo: bool = False
    """A pretend camera with a sliding week of tiny clips, for trying
    ReoVault without hardware. Needs no host or password."""

    @model_validator(mode="after")
    def _reachable(self) -> CameraConfig:
        if not self.demo and not self.host:
            raise ValueError(f"camera {self.id!r} needs a host (or demo = true)")
        return self


class DestinationConfig(BaseModel):
    """Where backups go. Each camera gets its own restic repository at
    `<repository>/<cam_id>`. One enabled destination is the *primary*: clips are
    backed up there first. The others receive copies of its snapshots."""

    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
    """Unique and never renamed: the index remembers snapshots by it."""
    repository: str
    """A local path, or `rclone:<remote>:<path>`."""
    primary: bool = False
    """With exactly one enabled destination, that one is the primary anyway."""
    keep_days: int = Field(default=0, ge=0)
    """Keep backups this long, counted from the newest one. 0 keeps everything."""
    max_gb: int = Field(default=0, ge=0)
    """The most this destination may hold. 0 means no limit."""
    enabled: bool = True


class BackupConfig(BaseModel):
    interval_minutes: int = Field(default=60, ge=1, le=1440)
    password_file: Path | None = None
    """The restic password, the same for every destination (copying needs two
    repositories open at once). A file of its own (`restic-password` next to
    reovault.toml in the image), never in this config; unset means
    `$RESTIC_PASSWORD_FILE`."""


class AlertRules(BaseModel):
    enabled: bool = True
    renotify_hours: int = Field(default=24, ge=1, le=720)
    notify_on_resolve: bool = True


class AlertsConfig(BaseModel):
    """Where alerts go. Credentials are read from files, e.g. next to reovault.toml."""

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
    # A daily look at GitHub's releases for a newer ReoVault; only the version
    # number comes back, nothing about this server goes out.
    update_check: bool = True
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
    destinations: list[DestinationConfig] = Field(default_factory=list)
    backup: BackupConfig = Field(default_factory=BackupConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)

    def camera(self, cam_id: str) -> CameraConfig | None:
        return next((c for c in self.cameras if c.id == cam_id), None)

    @model_validator(mode="after")
    def _destinations_make_sense(self) -> Settings:
        names = [d.name for d in self.destinations]
        if len(set(names)) != len(names):
            raise ValueError("destination names must be unique")
        enabled = [d for d in self.destinations if d.enabled]
        marked = [d for d in enabled if d.primary]
        if len(marked) > 1:
            raise ValueError("only one enabled destination can be the primary")
        if len(enabled) > 1 and not marked:
            raise ValueError("with several enabled destinations, one must have primary = true")
        return self

    def primary(self) -> DestinationConfig | None:
        """Where clips are backed up first, or None before any is connected."""
        enabled = [d for d in self.destinations if d.enabled]
        return next((d for d in enabled if d.primary), enabled[0] if len(enabled) == 1 else None)

    def copies(self) -> list[DestinationConfig]:
        """Enabled destinations that receive copies of the primary's snapshots."""
        primary = self.primary()
        return [d for d in self.destinations if d.enabled and d is not primary]

    def destination(self, name: str) -> DestinationConfig | None:
        return next((d for d in self.destinations if d.name == name), None)

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
