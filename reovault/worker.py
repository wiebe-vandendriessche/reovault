"""One thread per enabled camera. Everything that touches a camera's restic
repository happens in that camera's thread, one step after another: backup
runs, the daily `forget` and the weekly `check`. So a repository is never used
by two ReoVault operations at once, and no restic lock is ever waited on.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from reovault import retention
from reovault.backup import BackupRun, CameraState, RunResult
from reovault.camera import Camera, DemoCamera, Gateway, ReolinkCamera
from reovault.config import CameraConfig, DestinationConfig, Settings
from reovault.db import Db, now_iso, parse_iso
from reovault.destinations import (
    CHECK_EVERY,
    CHECK_SUBSET,
    copy_view,
    record_cut,
    restic_for,
    snaps_of,
    usage,
)
from reovault.logging import get_logger
from reovault.restic import Restic
from reovault.retention import GB
from reovault.tools import ToolError

logger = get_logger(__name__)

FORGET_EVERY = timedelta(days=1)


def camera_for(settings: Settings, cfg: CameraConfig, gateway: Gateway) -> Camera:
    if cfg.demo:
        return DemoCamera(id=cfg.id, timezone=cfg.timezone)
    assert cfg.host is not None  # the config requires one unless demo
    return ReolinkCamera(
        id=cfg.id,
        timezone=cfg.timezone,
        host=cfg.host,
        user=cfg.user,
        password=cfg.password,
        binary=settings.tools.reolink_cli,
        gateway=gateway,
    )


@dataclass
class Health:
    """What the maintenance steps found, for status and alerts."""

    check_errors: int = 0
    maintenance_error: str | None = None


@dataclass
class Worker:
    id: str
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
    _camera_cfg: CameraConfig | None = None  # what `_camera` was built from
    # Told after every run, so copy destinations look for new snapshots at once.
    on_backed_up: Callable[[], None] = field(default=lambda: None)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name=f"camera-{self.id}", daemon=True)
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
            cfg = settings.camera(self.id)
            if cfg is None or not cfg.enabled:
                return
            try:
                if first:
                    first = False
                    # Where the card starts, until this run's full scan says again.
                    remembered = self.db.get_setting(f"card_start:{self.id}")
                    if remembered and self.state.oldest_on_card is None:
                        self.state.oldest_on_card = parse_iso(remembered)
                    with contextlib.suppress(ToolError):
                        restic_for(settings, self.id).unlock()  # locks a crash left behind
                self._backup(settings, cfg)
                self._maintain(settings)
                self.on_backed_up()
            except Exception:  # noqa: BLE001 - the camera's thread must outlive any bug
                logger.exception("worker.loop_failed", camera=self.id)
            self._last_end = datetime.now(UTC)
            self._wait()

    def _wait(self) -> None:
        """Sleep until the next scheduled run; "Run now" or shutdown wake it
        early, a reschedule only moves the deadline."""
        while not self._quit.is_set():
            interval = timedelta(minutes=self.settings().backup.interval_minutes)
            assert self._last_end is not None
            self.next_run_at = self._last_end + interval
            self.publish("activity", camera=self.id)
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
        # Rebuilt whenever its entry changes (address, login, timezone): an edit
        # in the dashboard or the file applies from the next run, no restart.
        if self._camera is None or cfg != self._camera_cfg:
            self._camera, self._camera_cfg = camera_for(settings, cfg, self.gateway), cfg
        self.running = True
        self.publish("activity", camera=self.id)
        try:
            primary = settings.primary()
            return BackupRun(
                camera=self._camera,
                restic=restic_for(settings, self.id, primary),
                destination=primary.name if primary else "",
                db=self.db,
                staging=settings.storage.staging_dir / self.id,
                state=self.state,
                interval=timedelta(minutes=settings.backup.interval_minutes),
                on_progress=lambda: self.publish("activity", camera=self.id),
                on_commit=lambda: self._after_commit(settings),
                hold=lambda: self._hold(settings),
            ).run(trigger)
        except ToolError as exc:  # no destination configured
            logger.error("worker.run_not_started", camera=self.id, error=str(exc))
            return None
        finally:
            self.running = False

    def _due(self, key: str, every: timedelta) -> bool:
        last = self.db.get_setting(key)
        return last is None or datetime.now(UTC) - parse_iso(last) >= every

    def _over_limit(self, primary: DestinationConfig, fraction: float) -> bool:
        used = sum(s.bytes for s in usage(self.db, primary.name))
        return used >= primary.max_gb * GB * fraction

    def _hold(self, settings: Settings) -> str | None:
        """A primary at its size limit that can't trim (its copies don't have the
        old snapshots yet) must not keep growing: wait for the copies instead of
        filling the disk."""
        primary = settings.primary()
        if primary is None or not primary.max_gb:
            return None
        if not self._over_limit(primary, 1.0):
            return None
        return (
            f"{primary.name} is at its {primary.max_gb} GB limit and its copies haven't "
            "caught up, so backups wait for them."
        )

    def _after_commit(self, settings: Settings) -> None:
        self.on_backed_up()
        primary = settings.primary()
        if primary is not None and primary.max_gb:
            self._maintain(settings)  # a size limit can't wait for the run to end

    def _maintain(self, settings: Settings) -> None:
        """The primary's share of upkeep, in this camera's thread: where each
        snapshot is, retention and quota, and the weekly read-back."""
        primary = settings.primary()
        if primary is None:
            return
        try:
            restic = restic_for(settings, self.id, primary)
            # Keyed by destination too: after a primary switch, the old
            # repository's check results say nothing about the new one.
            tag = f"{primary.name}:{self.id}"
            self.health.check_errors = int(self.db.get_setting(f"check_errors:{tag}") or 0)
            if restic.state() != "ok":
                return
            # Stamped before running: a forget or check that fails or times
            # out (up to 6 h) waits for its next due time instead of being
            # retried after every backup run.
            # A size limit is checked hourly: a first run on a full card can fill
            # the disk long before a daily pass.
            every = timedelta(hours=1) if primary.max_gb else FORGET_EVERY
            over = bool(primary.max_gb) and self._over_limit(primary, retention.QUOTA_HIGH)
            if over or self._due(f"forget:{tag}", every):
                self.db.set_setting(f"forget:{tag}", now_iso())
                self._retain(settings, primary, restic)
            if self.db.get_setting(f"check:{tag}") is None:
                # First check a week after the first backup, not right away.
                self.db.set_setting(f"check:{tag}", now_iso())
            elif self._due(f"check:{tag}", CHECK_EVERY):
                self.db.set_setting(f"check:{tag}", now_iso())
                self.health.check_errors = restic.check(CHECK_SUBSET)
                # Persisted: a restart must not "resolve" a corruption alert.
                self.db.set_setting(f"check_errors:{tag}", str(self.health.check_errors))
            self.health.maintenance_error = None
        except ToolError as exc:
            self.health.maintenance_error = str(exc)
            logger.error("worker.maintenance_failed", camera=self.id, error=str(exc))

    def _retain(self, settings: Settings, primary: DestinationConfig, restic: Restic) -> None:
        """Record where the primary's snapshots are, forget what retention and
        the quota allow (never what a copy still needs), and record it again.
        Reconciling here, in the thread that also commits, can't race a backup."""
        snapshots = restic.snapshots()
        if not snapshots:
            return
        self.db.set_locations(self.id, primary.name, [(s.root, s.id, s.time) for s in snapshots])
        used = usage(self.db, primary.name)
        cameras = [c.id for c in settings.cameras]
        cut = retention.decide(
            camera=self.id,
            snaps=snaps_of(self.id, snapshots, used),
            usage=used,
            keep_days=primary.keep_days,
            max_gb=primary.max_gb,
            copies=[copy_view(self.db, d, cameras) for d in settings.copies()],
        )
        record_cut(self.db, primary.name, used, cut, self.id)
        if cut.ids:
            logger.info(
                "worker.forgetting", camera=self.id, destination=primary.name, n=len(cut.ids)
            )
            restic.forget_ids(cut.ids)
            after = restic.snapshots()
            self.db.set_locations(self.id, primary.name, [(s.root, s.id, s.time) for s in after])
        expired = self.db.expire_unlocated(self.id)
        if expired:
            logger.info("worker.clips_expired", camera=self.id, count=expired)


class Workers:
    """The set of running workers follows the `[[cameras]]` list."""

    def __init__(
        self,
        settings: Callable[[], Settings],
        db: Db,
        gateway: Gateway,
        publish: Callable[..., None],
        on_backed_up: Callable[[], None] = lambda: None,
    ) -> None:
        self.settings, self.db, self.gateway, self.publish = settings, db, gateway, publish
        self.on_backed_up = on_backed_up
        self._lock = threading.Lock()
        self.by_id: dict[str, Worker] = {}

    def sync(self) -> None:
        """Start workers for newly enabled cameras, stop removed or disabled
        ones, and wake the rest so a changed interval applies at once."""
        wanted = {c.id for c in self.settings().cameras if c.enabled}
        with self._lock:
            for cam_id in set(self.by_id) - wanted:
                self.by_id.pop(cam_id).shutdown()
            for cam_id in wanted - set(self.by_id):
                worker = Worker(
                    cam_id,
                    self.settings,
                    self.db,
                    self.gateway,
                    self.publish,
                    on_backed_up=self.on_backed_up,
                )
                self.by_id[cam_id] = worker
                worker.start()
            for worker in self.by_id.values():
                worker.reschedule()

    def get(self, cam_id: str) -> Worker | None:
        with self._lock:
            return self.by_id.get(cam_id)

    def shutdown(self) -> None:
        with self._lock:
            workers = list(self.by_id.values())
        for w in workers:
            w.shutdown()
        for w in workers:
            w.join(timeout=10)
