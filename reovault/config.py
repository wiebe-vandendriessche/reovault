"""Configuration loading: `reovault.toml` (or `$REOVAULT_CONFIG_FILE`) merged with
`REOVAULT_*` environment variables / Docker secrets. Env always wins over the file.

Nothing here is a secret by itself. `REOVAULT_MASTER_PASSPHRASE` and the camera
password are deliberately not modeled as config fields; the former is read directly
from the environment by `crypto.keyring`. The camera password is handled by
`providers.reolink_cli_registry.ReolinkCliRegistry` when a camera is added from the
dashboard: ReoVault never persists, logs, echoes, or places it on a command line,
but it does pass through this process once, on stdin, on its way to reolink-cli's
own encrypted registry -- a narrower invariant than "never touched" (see CLAUDE.md:
"never store secrets in source control").
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

# pydantic-settings resolves `model_config["toml_file"]` once, at class
# definition time, which would freeze `$REOVAULT_CONFIG_FILE` at import time
# and give tests no way to point at a fixture file. This contextvar lets
# `settings_customise_sources` (a classmethod) pick the path fresh on every
# `load_settings()` call instead.
_config_file_override: ContextVar[str | None] = ContextVar("_config_file_override", default=None)


class DeviceConfig(BaseModel):
    """One Reolink device. Kept as a list so the data model stays multi-device
    capable even though only one doorbell is tested."""

    alias: str
    """The `reolink-cli device add <alias>` name, never the camera password."""
    channel: int = 0
    timezone: str
    """IANA tz name the camera's clock runs in, e.g. 'Europe/Brussels'."""
    name: str | None = None
    """The camera's own configured display name (e.g. "Front door"), shown
    throughout the dashboard instead of `alias`. Optional: when unset, the
    dashboard shows `alias` until `device_info` backfills it from the camera
    itself (see reovault/web/app.py's `_backfill_device_identity`)."""


class ReolinkCliConfig(BaseModel):
    binary: str = "reolink-cli"
    pinned_version: str = "0.19.0"
    gateway_addr: str = "127.0.0.1:9000"
    search_timeout_secs: int = 30
    download_timeout_secs: int = 300
    # Where `reolink-cli`'s own aliases.toml/credentials.key pair lives.
    # `ReolinkCliRegistry` never passes `--config-file` (see its module
    # docstring), so this must match reolink-cli's actual default
    # ($HOME/.config/reolink-cli); it exists here only so `doctor` can
    # preflight the directory reolink-cli will try to write into, the
    # single most likely thing to break on first run.
    registry_dir: Path = Field(default_factory=lambda: Path.home() / ".config" / "reolink-cli")


class StorageConfig(BaseModel):
    config_dir: Path = Path("./data/config")
    vault_dir: Path = Path("./data/vault")
    staging_dir: Path = Path("./data/staging")
    db_path: Path = Path("./data/config/reovault.db")
    master_key_path: Path = Path("./data/config/master.key")


class ScheduleConfig(BaseModel):
    archive_cron: str = "0 5 * * *"
    archive_enabled: bool = True
    archive_overlap_hours: int = 48
    backfill_cron: str = "0 4 * * 0"
    backfill_enabled: bool = True
    backfill_days: int = 30
    reconcile_interval_hours: int = 6
    reconcile_enabled: bool = True
    integrity_scan_sample_pct: float = 5.0
    integrity_scan_enabled: bool = True
    # None means "no global override": each device's cron jobs are
    # evaluated in that device's own `timezone` (see
    # `scheduler.add_device_jobs`). Set this only to force every device's
    # schedule onto one timezone regardless of where each camera actually
    # is.
    timezone: str | None = None


class WebConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8080

    # Auth. `password_hash` is the Docker-secret/env path
    # (REOVAULT_WEB__PASSWORD_HASH); the file is the default. Neither being
    # set doesn't stop the app starting (so /healthz keeps working for the
    # Docker healthcheck); it just refuses every UI route with 503 until
    # `reovault web set-password` runs.
    password_hash: str | None = None
    # The login's identity field. With one account this adds no actual
    # security -- it can't prevent enumeration and doesn't raise the
    # offline attack cost -- what it buys is real but mundane: a password
    # manager reliably recognizes and autofills an email + password pair.
    # Not a secret, so it does NOT go in the 0600 credential file; putting
    # it there would also break the `REOVAULT_WEB__PASSWORD_HASH`
    # Docker-secret path, which expects a PHC string and nothing else.
    email: str | None = None
    password_file: Path = Path("./data/config/web_password")
    session_key_path: Path = Path("./data/config/web_session.key")
    session_max_age_days: int = 30
    cookie_secure: bool = True
    cookie_samesite: str = "lax"
    check_origin: bool = True
    login_max_attempts: int = 5
    login_window_secs: int = 900
    login_lockout_secs: int = 300

    # Proxy: uvicorn only trusts X-Forwarded-* from these addresses.
    # Behind a reverse proxy in Docker this must be the proxy's own
    # container IP/subnet, never "*" unless the port is genuinely
    # unreachable except through the proxy.
    forwarded_allow_ips: str = "127.0.0.1"

    # Streaming/paging.
    stream_chunk_bytes: int = 1024 * 1024
    page_size: int = 100


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="REOVAULT_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    devices: list[DeviceConfig] = Field(default_factory=list)
    reolink_cli: ReolinkCliConfig = Field(default_factory=ReolinkCliConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    web: WebConfig = Field(default_factory=WebConfig)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Env overrides the TOML file; the TOML file overrides class defaults.
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
    """Load settings from `config_file` (default: `$REOVAULT_CONFIG_FILE` or
    `./reovault.toml`) merged with `REOVAULT_*` env vars, which always win.
    Kept as a function, not a module-level singleton, so tests can point it at
    a fixture file without import-order surprises."""
    token = _config_file_override.set(str(config_file) if config_file else None)
    try:
        return Settings(**overrides)
    finally:
        _config_file_override.reset(token)
