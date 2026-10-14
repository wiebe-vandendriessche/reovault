"""`reovault.toml` as the single source of truth for settings, both ways.

The dashboard writes its changes into the file (comments and layout kept,
via tomlkit), and edits made to the file by hand are picked up live by a
watcher. This is the model Frigate, Sonarr and Jellyfin use: the file is what
you back up and version, and the UI is just another editor of it.

Precedence is unchanged: `REOVAULT_*` env vars beat the file, which beats the
defaults. A field set by env is locked everywhere (`env_locked`).

Infrastructure settings (storage paths, the web bind/proxy/cookie settings,
the reolink-cli binary and gateway) are read once at startup: a change in the
file is reported as "restart required" instead of half-applied, and the
dashboard shows them read-only (`INFRA`).

A write is validated in full before it replaces the file, and replaces it
atomically (temp file + `os.replace` in the same directory), so a crash or a
bad value never leaves a broken or half-written config behind. A hand edit
that doesn't validate keeps the app on its last valid settings and is
reported as `error` until fixed.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import threading
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import tomlkit
from pydantic import BaseModel, ValidationError
from tomlkit.exceptions import ParseError
from tomlkit.items import AoT, Table
from tomlkit.toml_document import TOMLDocument

from reovault.config import Settings, load_settings
from reovault.logging import get_logger

logger = get_logger(__name__)

# Fields read once at startup and shown read-only in the dashboard.
INFRA_SECTIONS = frozenset({"storage", "reolink_cli"})
# The few [web] fields that are safe to change live from the browser; every
# other [web] field (bind, port, cookies, proxy trust, login identity) can
# lock you out, so it's infrastructure.
WEB_EDITABLE = frozenset(
    {
        "session_max_age_days",
        "login_max_attempts",
        "login_window_secs",
        "login_lockout_secs",
        "page_size",
    }
)

Listener = Callable[[Settings, Settings], None]


class ConfigUnavailableError(Exception):
    """No writable config file (none configured, or mounted read-only)."""


class ConfigConflictError(Exception):
    """The file changed since the caller read it."""


class ConfigInvalidError(Exception):
    """The edit would make the config invalid; nothing was written."""


def is_infra(section: str, field: str | None = None) -> bool:
    if section in INFRA_SECTIONS:
        return True
    return section == "web" and field not in WEB_EDITABLE


def env_locked(*path: str) -> str | None:
    """The env var that sets `path` (or a parent of it), if any. Same nesting
    pydantic-settings uses: `REOVAULT_ALERTS__RULES__ENABLED`."""
    want = [p.lower() for p in path]
    for key in os.environ:
        if not key.startswith("REOVAULT_"):
            continue
        parts = key[len("REOVAULT_") :].lower().split("__")
        if parts == want[: len(parts)] or want == parts[: len(want)]:
            return key
    return None


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def describe_error(exc: Exception) -> str:
    if isinstance(exc, tomllib.TOMLDecodeError):
        return f"Not valid TOML: {exc}"
    if isinstance(exc, ValidationError):
        problems = []
        for e in exc.errors():
            where = ".".join(str(p) for p in e["loc"])
            problems.append(f"{where}: {e['msg']}" if where else e["msg"])
        return "; ".join(problems)
    return str(exc)


class ConfigStore:
    """The live settings plus the file they come from. `current` is what the
    app runs on; it only changes through `update()` or a valid hand edit."""

    def __init__(self, path: Path | None, settings: Settings) -> None:
        self.path = path.resolve() if path is not None else None
        self.startup = settings
        self._current = settings
        self.error: str | None = None
        self.restart_required: list[str] = []
        # Who caused the latest change: "dashboard" (update) or "file" (a
        # hand edit picked up by reload). Lets the UI announce only edits
        # it didn't make itself.
        self.last_origin = "file"
        self._lock = threading.RLock()
        self._listeners: list[Listener] = []
        self._applied_hash = _hash(self._read_text())
        # What the file said at startup, the baseline for "restart required":
        # infrastructure is compared file-to-file, never against `settings`,
        # which may also carry values that didn't come from the file.
        self._baseline = settings
        if self.path is not None and self.path.exists():
            with contextlib.suppress(tomllib.TOMLDecodeError, ValidationError):
                self._baseline = load_settings(self.path)

    # -- reading -----------------------------------------------------------

    @property
    def current(self) -> Settings:
        return self._current

    @property
    def version(self) -> str:
        return _hash(self._read_text())

    @property
    def writable(self) -> bool:
        if self.path is None:
            return False
        target = self.path if self.path.exists() else self.path.parent
        return os.access(target, os.W_OK) and os.access(self.path.parent, os.W_OK)

    def _read_text(self) -> str:
        if self.path is None or not self.path.exists():
            return ""
        return self.path.read_text(encoding="utf-8")

    def document(self) -> TOMLDocument:
        """The file as tomlkit sees it; an empty document while the file
        doesn't parse (that state is reported through `error`)."""
        try:
            return tomlkit.parse(self._read_text())
        except ParseError:
            return tomlkit.document()

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    # -- applying ----------------------------------------------------------

    def _effective(self, loaded: Settings) -> Settings:
        """`loaded`, with the infrastructure fields pinned to the startup
        values: those take effect only after a restart."""
        web_live = {k: getattr(loaded.web, k) for k in WEB_EDITABLE}
        return loaded.model_copy(
            update={
                "storage": self.startup.storage,
                "reolink_cli": self.startup.reolink_cli,
                "web": self.startup.web.model_copy(update=web_live),
            }
        )

    def _apply(self, loaded: Settings, text_hash: str) -> None:
        old = self._current
        self._current = self._effective(loaded)
        self._applied_hash = text_hash
        self.error = None
        base = self._baseline
        changed = [name for name in INFRA_SECTIONS if getattr(loaded, name) != getattr(base, name)]
        changed += [
            f"web.{k}"
            for k in type(loaded.web).model_fields
            if k not in WEB_EDITABLE and getattr(loaded.web, k) != getattr(base.web, k)
        ]
        self.restart_required = sorted(changed)
        self._notify(old, self._current)

    def _notify(self, old: Settings, new: Settings) -> None:
        for listener in self._listeners:
            try:
                listener(old, new)
            except Exception as exc:  # noqa: BLE001 - one listener must not stop the rest
                logger.error("config.listener_failed", error=str(exc), exc_info=exc)

    def reload(self) -> bool:
        """Re-reads the file after an outside edit. False (and `error` set)
        when it doesn't validate; the app keeps its last valid settings."""
        if self.path is None:
            return False
        with self._lock:
            text = self._read_text()
            if _hash(text) == self._applied_hash:
                return True  # our own write, or no real change
            try:
                loaded = load_settings(self.path)
            except (tomllib.TOMLDecodeError, ValidationError) as exc:
                self.error = describe_error(exc)
                self._applied_hash = _hash(text)
                self.last_origin = "file"
                logger.error("config.invalid", path=str(self.path), error=self.error)
                self._notify(self._current, self._current)
                return False
            logger.info("config.reloaded", path=str(self.path))
            self.last_origin = "file"
            self._apply(loaded, _hash(text))
            return True

    def update(
        self, mutate: Callable[[TOMLDocument], object], *, expected_version: str | None = None
    ) -> Settings:
        """Edit the file through `mutate`, validate the result in full, then
        atomically replace the file and apply it. Raises without touching
        the file when the edit is invalid or the file moved underneath."""
        if not self.writable or self.path is None:
            raise ConfigUnavailableError(
                "The config file is read-only. Mount its folder read-write to edit settings "
                "from the dashboard."
            )
        with self._lock:
            text = self._read_text()
            if expected_version is not None and _hash(text) != expected_version:
                raise ConfigConflictError("reovault.toml changed on disk; reload and try again.")
            doc = tomlkit.parse(text)
            mutate(doc)
            new_text = tomlkit.dumps(doc)
            tmp = self.path.with_name(f".{self.path.name}.tmp")
            tmp.write_text(new_text, encoding="utf-8")
            try:
                loaded = load_settings(tmp)
            except (tomllib.TOMLDecodeError, ValidationError) as exc:
                tmp.unlink(missing_ok=True)
                raise ConfigInvalidError(describe_error(exc)) from None
            with open(tmp, "rb") as fh:
                os.fsync(fh.fileno())
            if self.path.exists():
                shutil.copymode(self.path, tmp)
                shutil.copyfile(self.path, self.path.with_name(self.path.name + ".bak"))
            os.replace(tmp, self.path)
            self.last_origin = "dashboard"
            self._apply(loaded, _hash(new_text))
            return self._current

    # -- watching ----------------------------------------------------------

    def watch(self, stop: threading.Event) -> threading.Thread | None:
        """Starts a daemon thread that reloads on outside edits."""
        if self.path is None:
            return None
        import watchfiles

        path = self.path

        def run() -> None:
            for changes in watchfiles.watch(path.parent, stop_event=stop, debounce=500):
                if any(Path(p).resolve() == path for _change, p in changes):
                    try:
                        self.reload()
                    except Exception as exc:  # noqa: BLE001 - the watcher must survive
                        logger.error("config.reload_failed", error=str(exc), exc_info=exc)

        thread = threading.Thread(target=run, name="config-watch", daemon=True)
        thread.start()
        return thread


# -- editing helpers: change values, keep the file's comments and layout ----


def _tomlable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, BaseModel):
        return {k: _tomlable(v) for k, v in value.model_dump().items() if v is not None}
    if isinstance(value, list):
        return [_tomlable(v) for v in value]
    if isinstance(value, dict):
        return {k: _tomlable(v) for k, v in value.items() if v is not None}
    return value


def table_at(doc: TOMLDocument, *keys: str) -> Table:
    """The table at `keys` (e.g. "alerts", "rules"), created if missing."""
    container: Any = doc
    for key in keys:
        if key not in container:
            container[key] = tomlkit.table()
        container = container[key]
    return container  # type: ignore[no-any-return]


def set_fields(table: Any, values: dict[str, Any], defaults: BaseModel | None = None) -> None:
    """Writes each value into `table` in place. `None` removes the key (TOML
    has no null); a key that isn't in the file yet and equals its default is
    left out, so the file only ever grows by what someone actually set."""
    for key, value in values.items():
        if value is None:
            table.pop(key, None)
            continue
        if key not in table and defaults is not None and getattr(defaults, key, object()) == value:
            continue
        table[key] = _tomlable(value)


def device_entries(doc: TOMLDocument) -> AoT:
    if "devices" not in doc:
        doc["devices"] = tomlkit.aot()
    entries: AoT = doc["devices"]
    return entries


def device_entry(doc: TOMLDocument, alias: str) -> Table | None:
    for entry in device_entries(doc):
        if entry.get("alias") == alias:
            found: Table = entry
            return found
    return None


def upsert_device_entry(doc: TOMLDocument, alias: str, **values: Any) -> Table:
    entry = device_entry(doc, alias)
    if entry is None:
        entry = tomlkit.table()
        entry["alias"] = alias
        device_entries(doc).append(entry)
        entry = device_entry(doc, alias)
        assert entry is not None
    from reovault.config import DeviceConfig

    # Defaults (channel 0, enabled) aren't written unless already present.
    set_fields(entry, values, DeviceConfig.model_construct())
    return entry


def set_device_schedule(doc: TOMLDocument, alias: str, schedule: BaseModel | None) -> None:
    """A camera's own `[devices.schedule]`, or `None` to follow `[schedule]`."""
    entry = device_entry(doc, alias)
    if entry is None:
        raise ConfigInvalidError(f"No [[devices]] entry for {alias!r} in reovault.toml.")
    if schedule is None:
        entry.pop("schedule", None)
        return
    # Only what differs from the built-in defaults, so the override reads as
    # what's actually special about this camera. A blank line after it keeps
    # the next [[devices]] entry visually separate.
    defaults = type(schedule)()
    table = tomlkit.table()
    for key, value in schedule.model_dump().items():
        if value is not None and value != getattr(defaults, key):
            table[key] = value
    table.add(tomlkit.nl())
    entry["schedule"] = table


def migrate_db_settings(store: ConfigStore, repository: Any) -> list[str]:
    """One-time move of pre-0.4 dashboard state into reovault.toml: the
    schedule (global and per camera), retention and alert rules that used to
    live in `app_settings`, and cameras added from the dashboard that exist
    only in the database. The database keys are deleted only after the file
    was written. Returns what was migrated; does nothing (and keeps the old
    keys) when the file can't be written."""
    from reovault.config import AlertRules, RetentionConfig, ScheduleConfig

    keys = [
        k
        for k in repository.setting_keys()
        if k in ("schedule", "retention", "alert_rules") or k.startswith("schedule:")
    ]
    in_file = {(d.alias, d.channel) for d in store.current.devices}
    rows = repository.list_devices()
    orphans = [r for r in rows if (r.alias, r.channel) not in in_file]
    disabled = [r for r in rows if (r.alias, r.channel) in in_file and not r.enabled]
    if not (keys or orphans or disabled):
        return []
    if not store.writable:
        logger.warning(
            "config.migration_skipped",
            reason="reovault.toml is read-only; mount its folder read-write to migrate",
        )
        return []

    def mutate(doc: TOMLDocument) -> None:
        for row in orphans:
            upsert_device_entry(
                doc,
                row.alias,
                channel=row.channel,
                timezone=row.timezone,
                name=row.name,
                enabled=None if row.enabled else False,
            )
        for row in disabled:
            upsert_device_entry(doc, row.alias, enabled=False)
        for key in keys:
            raw = repository.get_setting(key)
            if raw is None:
                continue
            if key == "schedule" and not env_locked("schedule"):
                schedule = ScheduleConfig.model_validate_json(raw)
                set_fields(table_at(doc, "schedule"), schedule.model_dump(), ScheduleConfig())
            elif key.startswith("schedule:") and not env_locked("schedule"):
                row = repository.get_device(int(key.split(":", 1)[1]))
                if row is not None:
                    upsert_device_entry(doc, row.alias, channel=row.channel, timezone=row.timezone)
                    set_device_schedule(doc, row.alias, ScheduleConfig.model_validate_json(raw))
            elif key == "retention" and not env_locked("retention"):
                policy = RetentionConfig.model_validate_json(raw)
                set_fields(table_at(doc, "retention"), policy.model_dump(), RetentionConfig())
            elif key == "alert_rules" and not env_locked("alerts", "rules"):
                rules = AlertRules.model_validate_json(raw)
                set_fields(table_at(doc, "alerts", "rules"), rules.model_dump(), AlertRules())

    store.update(mutate)
    for key in keys:
        repository.delete_setting(key)
    migrated = keys + [f"device:{r.alias}" for r in orphans]
    logger.info("config.migrated_from_database", items=migrated)
    return migrated
