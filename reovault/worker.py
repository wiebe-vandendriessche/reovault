"""One thread per enabled camera. Everything that touches a camera's restic
repository happens in that camera's thread, one step after another: backup
runs, the daily `forget` and the weekly `check`. So a repository is never used
by two ReoVault operations at once, and no restic lock is ever waited on.
"""

from __future__ import annotations

import contextlib
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from reovault.backup import AlreadyRunning, BackupRun, CameraState, RunResult
from reovault.camera import Camera, DemoCamera, Gateway, ReolinkCamera
from reovault.config import CameraConfig, Settings
from reovault.db import Db, now_iso, parse_iso
from reovault.logging import get_logger
from reovault.restic import Restic
from reovault.tools import ToolError

logger = get_logger(__name__)

FORGET_EVERY = timedelta(days=1)
CHECK_EVERY = timedelta(days=7)
CHECK_SUBSET = "500M"  # read back this much data a week: catches bit rot cheaply


def restic_env(settings: Settings) -> dict[str, str]:
    env = {
        "RESTIC_CACHE_DIR": os.environ.get(
            "RESTIC_CACHE_DIR", str(settings.storage.data_dir / "restic-cache")
        )
    }
    if settings.backup.password_file is not None:
        env["RESTIC_PASSWORD_FILE"] = str(settings.backup.password_file)
    return env


def restic_for(settings: Settings, alias: str) -> Restic:
    if not settings.backup.repository:
        raise ToolError("No backup destination is configured ([backup] repository)")
    return Restic.for_camera(
        settings.tools.restic, settings.backup.repository, alias, restic_env(settings)
    )


def camera_for(settings: Settings, cfg: CameraConfig, gateway: Gateway) -> Camera:
    if cfg.demo:
        return DemoCamera(alias=cfg.alias, timezone=cfg.timezone)
    return ReolinkCamera(
        alias=cfg.alias, timezone=cfg.timezone, binary=settings.tools.reolink_cli, gateway=gateway
    )


@dataclass
class Health:
    """What the maintenance steps found, for status and alerts."""

    check_errors: int = 0
    index_mismatch: bool = False
    maintenance_error: str | None = None


@dataclass
class Worker:
    alias: str
    settings: Callable[[], Settings]
    db: Db
    gateway: Gateway
    publish: Callable[..., None]
    state: CameraState = field(default_factory=CameraState)
    health: Health = field(default_factory=Health)
    running: bool = False
    next_run_at: datetime | None = None
    _wake: threading.Event = field(default_factory=threading.Event)
    _quit: threading.Event = field(default_factory=threading.Event)
    _trigger: str = "schedule"
    _reschedule: bool = False
    _last_end: datetime | None = None
    _thread: threading.Thread | None = None
    _camera: Any = None  # kept across runs: a demo camera remembers its overwritten clips

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name=f"camera-{self.alias}", daemon=True)
        self._thread.start()

    def run_now(self) -> None:
        self._trigger = "manual"
        self._wake.set()

    def reschedule(self) -> None:
        """Re-read the interval without starting a run."""
        self._reschedule = True
        self._wake.set()

    def stop_run(self) -> None:
        self.state.stop.set()

    def shutdown(self) -> None:
        self._quit.set()
        self.state.stop.set()
        self._wake.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _loop(self) -> None:
        first = True
        while not self._quit.is_set():
            settings = self.settings()
            cfg = settings.camera(self.alias)
            if cfg is None or not cfg.enabled:
                return
            if first:
                first = False
                with contextlib.suppress(ToolError):
                    restic_for(settings, self.alias).unlock()  # locks a crash left behind
            self._backup(settings, cfg)
            self._maintain(settings)
            self._last_end = datetime.now(UTC)
            self._wait()

    def _wait(self) -> None:
        """Sleep until the next scheduled run; "Run now" or shutdown wake it
        early, a reschedule only moves the deadline."""
        while not self._quit.is_set():
            interval = timedelta(minutes=self.settings().backup.interval_minutes)
            assert self._last_end is not None
            self.next_run_at = self._last_end + interval
            self.publish("activity", camera=self.alias)
            left = (self.next_run_at - datetime.now(UTC)).total_seconds()
            woke = self._wake.wait(max(left, 0))
            self._wake.clear()
            if woke and self._reschedule and self._trigger != "manual":
                self._reschedule = False
                continue
            self._reschedule = False
            return

    def _backup(self, settings: Settings, cfg: CameraConfig) -> RunResult | None:
        trigger, self._trigger = self._trigger, "schedule"
        self.state.stop.clear()
        if self._camera is None or getattr(self._camera, "timezone", None) != cfg.timezone:
            self._camera = camera_for(settings, cfg, self.gateway)
        self.running = True
        self.publish("activity", camera=self.alias)
        try:
            return BackupRun(
                camera=self._camera,
                restic=restic_for(settings, self.alias),
                db=self.db,
                staging=settings.storage.staging_dir / self.alias,
                state=self.state,
                interval=timedelta(minutes=settings.backup.interval_minutes),
                on_progress=lambda: self.publish("activity", camera=self.alias),
            ).run(trigger)
        except (ToolError, AlreadyRunning) as exc:  # no destination configured
            logger.error("worker.run_not_started", camera=self.alias, error=str(exc))
            return None
        finally:
            self.running = False

    def _due(self, key: str, every: timedelta) -> bool:
        last = self.db.get_setting(f"{key}:{self.alias}")
        return last is None or datetime.now(UTC) - parse_iso(last) >= every

    def _maintain(self, settings: Settings) -> None:
        """Retention, the index's view of it, and the weekly read-back."""
        try:
            restic = restic_for(settings, self.alias)
            if restic.state() != "ok":
                return
            if self._due("forget", FORGET_EVERY):
                if settings.backup.keep_days:
                    restic.forget(settings.backup.keep_days)
                self._reconcile(restic)
                self.db.set_setting(f"forget:{self.alias}", now_iso())
            if self.db.get_setting(f"check:{self.alias}") is None:
                # First check a week after the first backup, not right away.
                self.db.set_setting(f"check:{self.alias}", now_iso())
            elif self._due("check", CHECK_EVERY):
                self.health.check_errors = restic.check(CHECK_SUBSET)
                self.db.set_setting(f"check:{self.alias}", now_iso())
            self.health.maintenance_error = None
        except ToolError as exc:
            self.health.maintenance_error = str(exc)
            logger.error("worker.maintenance_failed", camera=self.alias, error=str(exc))

    def _reconcile(self, restic: Restic) -> None:
        """Clips whose snapshot retention removed become `expired`. Never
        acts on an answer that would expire everything: an empty snapshot
        list against a non-empty index means the wrong repository answered."""
        ids = restic.snapshot_ids()
        backed_up, _ = self.db.totals(self.alias)
        if not ids and backed_up:
            self.health.index_mismatch = True
            return
        self.health.index_mismatch = False
        expired = self.db.expire_missing(self.alias, restic.repository, ids)
        if expired:
            logger.info("worker.clips_expired", camera=self.alias, count=expired)


class Workers:
    """The set of running workers follows the `[[cameras]]` list."""

    def __init__(
        self,
        settings: Callable[[], Settings],
        db: Db,
        gateway: Gateway,
        publish: Callable[..., None],
    ) -> None:
        self.settings, self.db, self.gateway, self.publish = settings, db, gateway, publish
        self._lock = threading.Lock()
        self.by_alias: dict[str, Worker] = {}

    def sync(self) -> None:
        """Start workers for newly enabled cameras, stop removed or disabled
        ones, and wake the rest so a changed interval applies at once."""
        wanted = {c.alias for c in self.settings().cameras if c.enabled}
        with self._lock:
            for alias in set(self.by_alias) - wanted:
                self.by_alias.pop(alias).shutdown()
            for alias in wanted - set(self.by_alias):
                worker = Worker(alias, self.settings, self.db, self.gateway, self.publish)
                self.by_alias[alias] = worker
                worker.start()
            for worker in self.by_alias.values():
                worker.reschedule()

    def get(self, alias: str) -> Worker | None:
        with self._lock:
            return self.by_alias.get(alias)

    def shutdown(self) -> None:
        with self._lock:
            workers = list(self.by_alias.values())
        for w in workers:
            w.shutdown()
        for w in workers:
            w.join(timeout=10)
