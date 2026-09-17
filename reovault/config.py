"""Configuration loading: `reovault.toml` (or `$REOVAULT_CONFIG_FILE`) merged with
`REOVAULT_*` environment variables / Docker secrets. Env always wins over the file.

Nothing here is a secret by itself. `REOVAULT_MASTER_PASSPHRASE` and the camera
password are deliberately not modeled as config fields; the former is read directly
from the environment by `crypto.keyring`, the latter is never touched by ReoVault at
all (see CLAUDE.md: "never store secrets in source control" and the plan's Camera
credentials section).
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
    capable even though only one doorbell is tested (see plan: Fleet)."""

    alias: str
    """The `reolink-cli device add <alias>` name, never the camera password."""
    channel: int = 0
    timezone: str
    """IANA tz name the camera's clock runs in, e.g. 'Europe/Brussels'."""


class ReolinkCliConfig(BaseModel):
    binary: str = "reolink-cli"
    pinned_version: str = "0.19.0"
    gateway_addr: str = "127.0.0.1:9000"
    search_timeout_secs: int = 30
    download_timeout_secs: int = 300


class StorageConfig(BaseModel):
    config_dir: Path = Path("./data/config")
    vault_dir: Path = Path("./data/vault")
    staging_dir: Path = Path("./data/staging")
    db_path: Path = Path("./data/config/reovault.db")
    master_key_path: Path = Path("./data/config/master.key")


class ScheduleConfig(BaseModel):
    archive_cron: str = "0 5 * * *"
    archive_overlap_hours: int = 48
    backfill_cron: str = "0 4 * * 0"
    backfill_days: int = 30
    reconcile_interval_hours: int = 6
    integrity_scan_sample_pct: float = 5.0


class WebConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8080


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
