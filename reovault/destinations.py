"""Destinations: where backups live.

One enabled destination is the *primary*: each camera's thread backs clips up
there first. Every other enabled destination receives copies of the primary's
snapshots (`restic copy`), on a thread of its own, so a slow or offline copy
never blocks a backup or fills the local disk.

Which thread touches which repository (so no two ever race on a prune):
- the primary: the camera's thread (backup, retention, check), plus copy
  threads *reading* it (a shared lock);
- a copy: only its destination thread (copy, retention, check).
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from reovault import retention
from reovault.backup import _MIN_UPLOAD_RATE
from reovault.config import DestinationConfig, Settings
from reovault.db import Db, iso, now_iso, parse_iso
from reovault.logging import get_logger
from reovault.restic import Restic, Snapshot
from reovault.retention import CopyView, Snap, copy_candidates
from reovault.tools import ToolError

logger = get_logger(__name__)


def restic_env(settings: Settings) -> dict[str, str]:
    env = {
        "RESTIC_CACHE_DIR": os.environ.get(
            "RESTIC_CACHE_DIR", str(settings.storage.data_dir / "restic-cache")
        )
    }
    if settings.backup.password_file is not None:
        env["RESTIC_PASSWORD_FILE"] = str(settings.backup.password_file)
    return env


def restic_for(settings: Settings, cam_id: str, dest: DestinationConfig | None = None) -> Restic:
    """The restic repository of one camera on a destination (the primary when
    none is given)."""
    dest = dest or settings.primary()
    if dest is None:
        raise ToolError("No backup destination is set up yet (add one under Settings)")
    return Restic.for_camera(settings.tools.restic, dest.repository, cam_id, restic_env(settings))


def usage(db: Db, destination: str) -> list[Snap]:
    """Every snapshot on a destination, with the footage it holds."""
    return [
        Snap(r["camera"], r["snapshot_id"], r["root"], parse_iso(r["time"]), int(r["bytes"]))
        for r in db.snapshot_usage(destination)
    ]


def snaps_of(camera: str, snapshots: list[Snapshot], used: list[Snap]) -> list[Snap]:
    """A camera's snapshots as retention sees them: restic's listing (what is
    really there) with the footage sizes the index knows."""
    held = {s.id: s.bytes for s in used if s.camera == camera}
    return [Snap(camera, s.id, s.root, s.time, held.get(s.id, 0)) for s in snapshots]


def copy_view(db: Db, dest: DestinationConfig, cameras: list[str]) -> CopyView:
    floor = db.get_setting(f"copy_floor:{dest.name}")
    return CopyView(
        name=dest.name,
        keep_days=dest.keep_days,
        floor=parse_iso(floor) if floor else None,
        roots={c: frozenset(db.location_roots(c, dest.name)) for c in cameras},
    )


def update_status(db: Db, name: str, **fields: Any) -> None:
    """What the dashboard and alerts show about a destination, kept in the
    index so a restart remembers it: `{used, short, too_small, blocked, ...}`."""
    raw = db.get_setting(f"dest_status:{name}")
    status = json.loads(raw) if raw else {}
    status.update(fields)
    db.set_setting(f"dest_status:{name}", json.dumps(status))


def record_cut(db: Db, name: str, used: list[Snap], cut: retention.Cut, camera: str) -> None:
    """What the dashboard and alerts need after a retention pass. `short_until`
    keeps "the quota cut retention short" on for a week after the last such
    cut: the next pass has nothing left to trim and must not clear it."""
    # Each camera's pass reports its own verdict; the destination shows them all,
    # so a later camera with nothing to report can't clear an earlier one's.
    by_camera = dict(status_of(db, name).get("by_camera", {}))
    by_camera[camera] = {"too_small": cut.too_small, "blocked": cut.blocked}
    fields: dict[str, Any] = {
        "used": sum(s.bytes for s in used),
        "by_camera": by_camera,
        "too_small": any(v["too_small"] for v in by_camera.values()),
        "blocked": sum(v["blocked"] for v in by_camera.values()),
    }
    if cut.short and cut.ids:
        fields["short_until"] = iso(datetime.now(UTC) + QUOTA_NOTICE)
    update_status(db, name, **fields)


def quota_short(status: dict[str, Any]) -> bool:
    until = status.get("short_until")
    return isinstance(until, str) and parse_iso(until) > datetime.now(UTC)


def lag(db: Db, settings: Settings, dest: DestinationConfig) -> tuple[int, datetime | None]:
    """(snapshots this copy still lacks and wants, the oldest one's time)."""
    primary = settings.primary()
    if primary is None or primary is dest:
        return 0, None
    cameras = [c.id for c in settings.cameras]
    ours = usage(db, primary.name)
    todo = copy_candidates(ours, copy_view(db, dest, cameras))
    return len(todo), (min(s.time for s in todo) if todo else None)


def status_of(db: Db, name: str) -> dict[str, Any]:
    raw = db.get_setting(f"dest_status:{name}")
    parsed: dict[str, Any] = json.loads(raw) if raw else {}
    return parsed


# -- copy threads ---------------------------------------------------------

QUOTA_NOTICE = timedelta(days=7)
ROUND_EVERY = timedelta(minutes=15)  # between rounds, when nothing wakes a thread sooner
CHECK_EVERY = timedelta(days=7)
CHECK_SUBSET = "500M"
FALLBACK_COPY_SECS = 1800.0


class DestinationWorker:
    """One copy destination: for every camera, copies the primary's new
    snapshots into it, then applies its own retention and quota. Wakes when a
    camera finishes a backup, and every `ROUND_EVERY` otherwise."""

    def __init__(
        self,
        name: str,
        settings: Callable[[], Settings],
        db: Db,
        publish: Callable[..., None],
    ) -> None:
        self.name, self.settings, self.db, self.publish = name, settings, db, publish
        self._wake = threading.Event()
        self._quit = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=f"dest-{name}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def shutdown(self) -> None:
        self._quit.set()
        self._wake.set()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def _loop(self) -> None:
        while not self._quit.is_set():
            try:
                self.run_round()
            except Exception:  # noqa: BLE001 - the thread must outlive any bug
                logger.exception("destination.round_crashed", destination=self.name)
                self._fail("unexpected error, see the log")
            self._wake.wait(ROUND_EVERY.total_seconds())
            self._wake.clear()

    def _fail(self, message: str) -> None:
        if self.settings().destination(self.name) not in self.settings().copies():
            return  # no longer a copy (made primary, turned off): nothing to report
        raw = status_of(self.db, self.name)
        update_status(self.db, self.name, error=message, failures=int(raw.get("failures", 0)) + 1)
        self.publish("destinations")

    def run_round(self) -> None:
        settings = self.settings()
        dest, primary = settings.destination(self.name), settings.primary()
        if dest is None or primary is None or not dest.enabled or dest is primary:
            return
        errors: list[str] = []
        for cam in settings.cameras:
            if self._quit.is_set():
                return
            try:
                self._camera(settings, dest, primary, cam.id)
            except ToolError as exc:
                logger.error(
                    "destination.camera_failed",
                    destination=dest.name,
                    camera=cam.id,
                    error=str(exc),
                )
                errors.append(f"{cam.id}: {exc}")
        if errors:
            self._fail("; ".join(errors))
        else:
            update_status(self.db, dest.name, error=None, failures=0, last_ok=now_iso())
            self.publish("destinations")

    def _camera(
        self, settings: Settings, dest: DestinationConfig, primary: DestinationConfig, cam_id: str
    ) -> None:
        src, dst = restic_for(settings, cam_id, primary), restic_for(settings, cam_id, dest)
        if src.state() != "ok":
            return  # nothing backed up yet
        state = dst.state()
        if state == "wrong_password":
            raise ToolError("the restic password doesn't open this destination's repository")
        if state == "missing":
            if self.db.repo_seen(dest.name, cam_id):
                raise ToolError(
                    "the destination has no repository for this camera any more, but there was "
                    "one before. Check that it is reachable and the path is right: nothing "
                    "will be created."
                )
            logger.info("destination.repository_init", destination=dest.name, camera=cam_id)
            dst.init(copy_of=src)
        self.db.mark_repo_seen(dest.name, cam_id)

        cameras = [c.id for c in settings.cameras]
        theirs = dst.snapshots()
        self.db.set_locations(cam_id, dest.name, [(s.root, s.id, s.time) for s in theirs])
        used = usage(self.db, primary.name)
        ours = snaps_of(cam_id, src.snapshots(), used)
        todo = copy_candidates(ours, copy_view(self.db, dest, cameras))
        cap = dest.max_gb * retention.GiB
        held = sum(s.bytes for s in usage(self.db, dest.name))
        for done, snap in enumerate(todo):
            if self._quit.is_set():
                break
            if cap and held + snap.bytes > cap * retention.QUOTA_HIGH:
                # Everything older would be cut again at once: stop here, and
                # remember it so older snapshots aren't tried every round.
                self._floor(dest, snap.time + timedelta(seconds=1))
                update_status(self.db, dest.name, short_until=iso(datetime.now(UTC) + QUOTA_NOTICE))
                break
            held += snap.bytes
            self._progress(cam_id, done, len(todo))
            logger.info(
                "destination.copying", destination=dest.name, camera=cam_id, snapshot=snap.id[:8]
            )
            dst.copy_from(src, snap.id, timeout=600 + snap.bytes / _MIN_UPLOAD_RATE)
            self.db.set_setting(f"copied_at:{dest.name}", now_iso())
        self._progress(cam_id, None, 0)
        theirs = dst.snapshots() if todo else theirs
        self.db.set_locations(cam_id, dest.name, [(s.root, s.id, s.time) for s in theirs])
        self._retain(settings, dest, cam_id, dst, theirs)
        self._check(dest, cam_id, dst)

    def _floor(self, dest: DestinationConfig, at: datetime) -> None:
        """Never copy snapshots older than `at` (only ever moves forward)."""
        current = self.db.get_setting(f"copy_floor:{dest.name}")
        if current is None or parse_iso(current) < at:
            self.db.set_setting(f"copy_floor:{dest.name}", iso(at))

    def _progress(self, cam_id: str, done: int | None, total: int) -> None:
        """Seeding progress for the dashboard: snapshots copied of those wanted."""
        seeding = dict(status_of(self.db, self.name).get("seeding", {}))
        if done is None or total <= 1:
            seeding.pop(cam_id, None)
        else:
            seeding[cam_id] = [done, total]
        update_status(self.db, self.name, seeding=seeding)
        self.publish("destinations")

    def _retain(
        self,
        settings: Settings,
        dest: DestinationConfig,
        cam_id: str,
        dst: Restic,
        theirs: list[Snapshot],
    ) -> None:
        used = usage(self.db, dest.name)
        cut = retention.decide(
            camera=cam_id,
            snaps=snaps_of(cam_id, theirs, used),
            usage=used,
            keep_days=dest.keep_days,
            max_gb=dest.max_gb,
        )
        record_cut(self.db, dest.name, used, cut, cam_id)
        if cut.floor is not None:
            self._floor(dest, cut.floor + timedelta(seconds=1))  # not copied back tomorrow
        if cut.ids:
            logger.info(
                "destination.forgetting", destination=dest.name, camera=cam_id, n=len(cut.ids)
            )
            dst.forget_ids(cut.ids)
            after = dst.snapshots()
            self.db.set_locations(cam_id, dest.name, [(s.root, s.id, s.time) for s in after])
        expired = self.db.expire_unlocated(cam_id)
        if expired:
            logger.info("destination.clips_expired", camera=cam_id, count=expired)

    def _check(self, dest: DestinationConfig, cam_id: str, dst: Restic) -> None:
        key = f"check:{dest.name}:{cam_id}"
        last = self.db.get_setting(key)
        if last is None:
            self.db.set_setting(key, now_iso())  # first check a week from now
        elif datetime.now(UTC) - parse_iso(last) >= CHECK_EVERY:
            self.db.set_setting(key, now_iso())
            errors = dst.check(CHECK_SUBSET)
            self.db.set_setting(f"check_errors:{dest.name}:{cam_id}", str(errors))
            self.publish("destinations")


class Destinations:
    """The copy threads follow the config: one per enabled non-primary
    destination, started and stopped as destinations are added, disabled,
    made primary or removed."""

    def __init__(
        self, settings: Callable[[], Settings], db: Db, publish: Callable[..., None]
    ) -> None:
        self.settings, self.db, self.publish = settings, db, publish
        self._lock = threading.Lock()
        self.by_name: dict[str, DestinationWorker] = {}

    def sync(self) -> None:
        wanted = {d.name for d in self.settings().copies()}
        with self._lock:
            for name in set(self.by_name) - wanted:
                self.by_name.pop(name).shutdown()
                # A copy's old failures mustn't stick to it as a primary.
                update_status(self.db, name, error=None, failures=0, seeding={})
            for name in wanted - set(self.by_name):
                worker = DestinationWorker(name, self.settings, self.db, self.publish)
                self.by_name[name] = worker
                worker.start()
            for worker in self.by_name.values():
                worker.wake()

    def wake(self) -> None:
        """A camera finished a backup: there may be something new to copy."""
        with self._lock:
            for worker in self.by_name.values():
                worker.wake()

    def shutdown(self) -> None:
        with self._lock:
            workers = list(self.by_name.values())
        for w in workers:
            w.shutdown()
        for w in workers:
            w.join(timeout=10)
