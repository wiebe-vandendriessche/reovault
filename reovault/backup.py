"""One camera's backup run: list the card, then batch by batch download,
verify, `restic backup`, and record what is safe.

The invariant: a clip is `backed_up` only after its size matched the card's,
restic exited 0 with a snapshot id, and the database committed. Local files
are deleted only after that. A crash anywhere before the commit just means
the next run backs the same files up again; restic stores the data once.

Downloading and uploading overlap: while restic uploads `batch/`, the next
batch downloads into `incoming/`. Local disk holds at most two batches, and
when restic can't upload the run stops downloading, so the clips wait on the
SD card.

Every snapshot also carries `batch/.reovault/index.json`: the camera's whole
index (every backed-up, lost and recently expired clip, detection types
included). A lost or stale database is rebuilt from the newest one, so no
clip is downloaded twice and older backups stay visible.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from reovault.camera import Camera, CardStatus, Downloaded, Found, scan_card
from reovault.db import Clip, Db, iso, parse_iso
from reovault.logging import get_logger
from reovault.restic import Restic
from reovault.tools import ToolError

logger = get_logger(__name__)

BATCH_BYTES = 2 * 1024**3
MAX_ATTEMPTS = 5
SIZE_RETRY_PAUSE_SECS = 10.0  # D340W: a short file right after a big one
FULL_SCAN_EVERY = timedelta(days=1)
# Timeouts scale with the bytes: a flat limit would kill a 2 GB batch.
_MIN_DOWNLOAD_RATE = 0.5 * 1024**2
_MIN_UPLOAD_RATE = 0.25 * 1024**2


MANIFEST_DIR = ".reovault"
MANIFEST_NAME = "index.json"


class _Upload:
    """One commit on a daemon thread: a stuck restic never holds up the
    process's exit, and its result or error is kept for `wait()`."""

    def __init__(self, fn: Callable[..., tuple[int, int]], *args: object) -> None:
        self._result: tuple[int, int] | None = None
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, args=(fn, args), daemon=True)
        self._thread.start()

    def _run(self, fn: Callable[..., tuple[int, int]], args: tuple[object, ...]) -> None:
        try:
            self._result = fn(*args)
        except BaseException as exc:  # noqa: BLE001 - handed to wait()
            self._error = exc

    def wait(self) -> tuple[int, int]:
        self._thread.join()
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


class RunError(Exception):
    """Ends a run with a message for the dashboard; nothing is lost."""


class AlreadyRunning(Exception):
    pass


@dataclass
class CameraState:
    """What a camera's worker remembers between runs, in memory only: the
    first run after a restart rescans the card."""

    card: CardStatus | None = None
    oldest_on_card: datetime | None = None
    scanned_at: datetime | None = None
    last_failed: bool = False
    # restic has no snapshots but the database says clips are backed up there:
    # the wrong or a wiped repository answered. Shown and alerted, never acted on.
    index_mismatch: bool = False
    # Since when the camera hasn't answered; None while it does.
    unreachable_since: datetime | None = None
    stop: threading.Event = field(default_factory=threading.Event)


@dataclass
class RunResult:
    outcome: str = "ok"  # ok | error | stopped
    clips: int = 0
    bytes: int = 0
    lost: int = 0
    error: str | None = None


@contextmanager
def camera_lock(staging: Path) -> Iterator[None]:
    """One run per camera at a time, across processes (`reovault run` vs the
    daemon): they share the staging folder."""
    staging.mkdir(parents=True, exist_ok=True)
    fd = os.open(staging / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise AlreadyRunning(f"a run for {staging.name} is already in progress") from None
        yield
    finally:
        os.close(fd)


@dataclass
class BackupRun:
    camera: Camera
    restic: Restic
    db: Db
    staging: Path  # this camera's own folder, e.g. /staging/doorbell
    state: CameraState
    interval: timedelta
    destination: str = "primary"  # the primary destination's name
    on_progress: Callable[[], None] = lambda: None
    # After every committed batch, on the upload thread: copies have news, and a
    # size-limited primary can trim while a long first run is still going.
    on_commit: Callable[[], None] = lambda: None
    # Why no more batches should start right now (None: go on). Checked before each.
    hold: Callable[[], str | None] = lambda: None
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    sleep: Callable[[float], None] = time.sleep

    @property
    def incoming(self) -> Path:
        return self.staging / "incoming"

    @property
    def batch_dir(self) -> Path:
        # Always this one path: restic's `forget` groups snapshots by path.
        return self.staging / "batch"

    def run(self, trigger: str) -> RunResult:
        """One run. Never raises: every failure ends up in the run's row. A
        run that finds another one in progress (`reovault run` next to the
        daemon) leaves no row at all, so it can't look like a failure."""
        alias = self.camera.alias
        result = RunResult()
        try:
            with camera_lock(self.staging):
                run_id = self.db.start_run(alias, trigger)
                try:
                    self._run(result, run_id)
                except (RunError, ToolError, OSError) as exc:
                    result.outcome, result.error = "error", str(exc)
                    if isinstance(exc, ToolError) and exc.unreachable:
                        self.state.unreachable_since = self.state.unreachable_since or self.clock()
                    logger.error("backup.run_failed", camera=alias, error=str(exc))
                except Exception as exc:  # noqa: BLE001 - a bug must not end the camera's thread
                    result.outcome, result.error = "error", f"unexpected error: {exc}"
                    logger.exception("backup.run_crashed", camera=alias)
                self.state.last_failed = result.outcome == "error"
                self.db.update_run(
                    run_id, clips=result.clips, bytes_=result.bytes, lost=result.lost
                )
                self.db.finish_run(run_id, result.outcome, result.error)
        except AlreadyRunning as exc:
            result.outcome, result.error = "skipped", str(exc)
            logger.info("backup.run_skipped", camera=alias, reason=str(exc))
        except OSError as exc:  # the staging folder itself (read-only, unmounted)
            result.outcome, result.error = "error", f"staging folder unusable: {exc}"
            logger.error("backup.run_failed", camera=alias, error=result.error)
            self.state.last_failed = True
            self.db.finish_run(self.db.start_run(alias, trigger), "error", result.error)
        self.on_progress()
        return result

    def _run(self, result: RunResult, run_id: int) -> None:
        self._ensure_repository()
        self._rebuild_index()
        self._recover(result, run_id)
        self._list()
        tried: set[int] = set()  # a clip that failed waits for the next run
        upload: _Upload | None = None
        try:
            while True:
                if self.state.stop.is_set():
                    result.outcome = "stopped"
                    break
                if reason := self.hold():
                    result.outcome, result.error = "stopped", reason
                    break
                # The batch being uploaded is already on disk, so this is
                # room for one more.
                free = shutil.disk_usage(self.staging).free
                if free < BATCH_BYTES * 1.2:
                    raise RunError(f"Not enough free space for a batch in {self.staging}")
                pending = [c for c in self.db.pending(self.camera.alias) if c.id not in tried]
                self._flag_sizeless(pending, tried)
                batch = pick(pending, self.clock(), self.interval)
                if not batch:
                    break
                tried.update(c.id for c in batch)
                started = time.monotonic()
                fetched = self._fetch(batch, result)
                download_secs = time.monotonic() - started
                try:
                    self._join(upload, result, run_id)
                finally:
                    # Even when that upload failed: the next run's `_recover`
                    # then sends both batches, nothing is downloaded twice.
                    upload = None
                    staged = self._stage(fetched)
                if staged:
                    upload = _Upload(self._commit, staged, run_id, download_secs)
                self._search_recent()
        except BaseException:
            # Still wait for the upload in flight, and keep both errors.
            if upload is not None:
                try:
                    self._join(upload, result, run_id)
                except Exception as exc:  # noqa: BLE001 - the main error wins
                    logger.error("backup.upload_failed", camera=self.camera.alias, error=str(exc))
            raise
        self._join(upload, result, run_id)  # an upload failure beats "stopped"

    def _flag_sizeless(self, pending: list[Clip], tried: set[int]) -> None:
        """A finished clip the card still lists without a size can't be
        verified, so it isn't downloaded; it counts an attempt each run so it
        shows up (and alerts) as a clip that keeps failing."""
        settled = self.clock() - 2 * self.interval
        for c in pending:
            if c.size is None and c.id not in tried and parse_iso(c.start_utc) < settled:
                self.db.record_failure(
                    c.id, "the camera reports no size for this clip, so it can't be verified"
                )
                tried.add(c.id)

    def _join(self, upload: _Upload | None, result: RunResult, run_id: int) -> None:
        """Waits for an upload; counters only ever change here, on the run's
        own thread."""
        if upload is None:
            return
        clips, size = upload.wait()
        result.clips += clips
        result.bytes += size
        self.db.update_run(run_id, clips=result.clips, bytes_=result.bytes, lost=result.lost)
        self.on_progress()

    # -- steps -----------------------------------------------------------

    def _recover(self, result: RunResult, run_id: int) -> None:
        """Wipe half-finished downloads; back up verified leftovers."""
        shutil.rmtree(self.incoming, ignore_errors=True)
        files = _files(self.batch_dir)
        if not files:
            return
        clips = self.db.clips_by_name(self.camera.alias, [f.stem for f in files])
        keep: dict[int, Path] = {}
        for f in files:
            clip = clips.get(f.stem)
            if clip is not None and clip.state == "pending":
                keep[clip.id] = f
            else:
                f.unlink()  # already safe, or not ours
        if keep:
            logger.info("backup.recovering_batch", camera=self.camera.alias, files=len(keep))
            clips_n, size = self._commit(keep, run_id)
            result.clips += clips_n
            result.bytes += size

    def _ensure_repository(self) -> None:
        state = self.restic.state()
        if state == "wrong_password":
            raise RunError("The restic password does not open this camera's repository")
        if state == "ok":
            self.db.mark_repo_seen(self.destination, self.camera.alias)
        if state == "missing":
            if self.db.repo_seen(self.destination, self.camera.alias):
                raise RunError(
                    "The backup destination has no repository for this camera any more, "
                    "but clips were backed up to it before. Check that the destination is "
                    "mounted and the path is right; nothing will be created."
                )
            logger.info("backup.repository_init", camera=self.camera.alias)
            self.restic.init()
            self.db.mark_repo_seen(self.destination, self.camera.alias)

    def _list(self) -> None:
        now = self.clock()
        due = (
            self.state.scanned_at is None
            or self.state.last_failed
            or now - self.state.scanned_at >= FULL_SCAN_EVERY
        )
        if not due:
            self._search_recent()
            return
        found, oldest = scan_card(self.camera, now)
        self.state.unreachable_since = None  # it answered
        self._upsert(found)
        self.state.oldest_on_card, self.state.scanned_at = oldest, now
        try:
            self.state.card = self.camera.storage()
        except ToolError as exc:  # only shown, never acted on
            logger.warning("backup.storage_status_failed", camera=self.camera.alias, error=str(exc))

    def _search_recent(self) -> None:
        now = self.clock()
        newest = self.db.newest_start(self.camera.alias) or now - 2 * self.interval
        found = self.camera.search(newest - timedelta(hours=2), now + timedelta(hours=2))
        self.state.unreachable_since = None  # it answered
        self._upsert(found)

    def _upsert(self, found: list[Found]) -> None:
        self.db.upsert_clips(
            self.camera.alias, [(f.name, f.start_utc, f.end_utc, f.size, f.rec_type) for f in found]
        )

    def _fetch(self, batch: list[Clip], result: RunResult) -> dict[Clip, Path]:
        """Downloads and verifies a batch into `incoming/`."""
        by_name = {c.name: c for c in batch}
        got = self._download(batch)
        # A short file is retried once after a pause, then counts as a failure.
        short = [n for n, p in got.files.items() if _short(p, by_name[n])]
        if short:
            for n in short:
                got.files.pop(n).unlink()
            self.sleep(SIZE_RETRY_PAUSE_SECS)
            again = self._download([by_name[n] for n in short])
            for n in short:
                p = again.files.get(n)
                if p is not None and not _short(p, by_name[n]):
                    got.files[n] = p
                elif p is not None:
                    got.failed[n] = (
                        f"downloaded {p.stat().st_size} bytes, expected {by_name[n].size}"
                    )
                    p.unlink()
                else:
                    got.failed[n] = again.failed.get(n, "no file on the retry")
        result.lost += self._settle_failures({by_name[n]: r for n, r in got.failed.items()})
        return {by_name[n]: p for n, p in got.files.items()}

    def _stage(self, fetched: dict[Clip, Path]) -> dict[int, Path]:
        """Moves verified files into `batch/<camera-local day>/`. Only ever
        called while no upload runs: restic must see `batch/` standing still."""
        staged: dict[int, Path] = {}
        tz = ZoneInfo(self.camera.timezone)
        for clip, path in fetched.items():
            day = parse_iso(clip.start_utc).astimezone(tz).date().isoformat()
            dest = self.batch_dir / day / f"{clip.name}{path.suffix}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, dest)  # same filesystem: atomic
            staged[clip.id] = dest
        shutil.rmtree(self.incoming, ignore_errors=True)
        return staged

    def _download(self, clips: list[Clip]) -> Downloaded:
        """One batch invocation. When it fails as a whole, a quick search
        tells the two causes apart: the camera is unreachable (re-raise: the
        run ends and no clip is blamed), or one clip breaks the batch (fall
        back to one clip at a time, so only that clip is charged and the
        rest still get backed up)."""
        try:
            return self.camera.download(
                [c.name for c in clips], self.incoming, timeout=_timeout(clips)
            )
        except ToolError as exc:
            now = self.clock()
            self.camera.search(now - timedelta(hours=1), now)  # raises when offline
            logger.warning(
                "backup.batch_failed_one_by_one", camera=self.camera.alias, error=str(exc)
            )
        # Only these clips' partial files: `incoming/` may also hold a batch
        # already verified (this is how short files are retried).
        for clip in clips:
            for partial in self.incoming.glob(f"{clip.name}.*"):
                partial.unlink()
        merged = Downloaded()
        for clip in clips:
            try:
                got = self.camera.download([clip.name], self.incoming, timeout=_timeout([clip]))
            except ToolError as exc:
                merged.failed[clip.name] = str(exc)
                continue
            merged.files.update(got.files)
            merged.failed.update(got.failed)
        return merged

    def _settle_failures(self, failed: dict[Clip, str]) -> int:
        """A clip the card no longer lists is lost; any other failure counts
        an attempt. Decided by searching its day again, not by the error
        text, which differs between models and reolink-cli versions."""
        if not failed:
            return 0
        tz = ZoneInfo(self.camera.timezone)
        by_day: dict[str, list[Clip]] = defaultdict(list)
        for clip in failed:
            by_day[parse_iso(clip.start_utc).astimezone(tz).date().isoformat()].append(clip)
        lost = 0
        for day, clips in by_day.items():
            # Two days, so a day the card overwrote completely still has a
            # later clip to compare against.
            start = datetime.fromisoformat(day).replace(tzinfo=tz)
            on_card = self.camera.search(
                start.astimezone(UTC), (start + timedelta(days=2)).astimezone(UTC)
            )
            names = {f.name for f in on_card}
            # Both days overwritten: the card scan's oldest clip is the proof.
            oldest = min((f.start_utc for f in on_card), default=self.state.oldest_on_card)
            for clip in clips:
                # Lost only with proof: not listed, and older than the oldest
                # clip the card still has (it overwrites oldest first). A
                # truncated or empty answer proves nothing and costs one
                # attempt instead.
                if clip.name not in names and oldest and parse_iso(clip.start_utc) < oldest:
                    self.db.mark_lost(clip.id)
                    lost += 1
                else:
                    self.db.record_failure(clip.id, failed[clip])
        if lost:
            logger.warning("backup.clips_lost", camera=self.camera.alias, count=lost)
        return lost

    def _commit(
        self, staged: dict[int, Path], run_id: int, download_secs: float | None = None
    ) -> tuple[int, int]:
        """`restic backup` of `batch/`, then the DB commit, then delete the
        files. Runs on the upload thread. Returns (clips, bytes)."""
        size = sum(p.stat().st_size for p in staged.values())
        self._write_manifest(staged)
        started = time.monotonic()
        summary = self.restic.backup(self.batch_dir, timeout=600 + size / _MIN_UPLOAD_RATE)
        upload_secs = time.monotonic() - started
        self.db.mark_backed_up(
            self.camera.alias,
            self.destination,
            summary["snapshot_id"],
            datetime.now(UTC),
            {cid: str(path) for cid, path in staged.items()},
        )
        self.db.add_batch(
            run_id, summary["snapshot_id"], len(staged), size, download_secs, upload_secs
        )
        for path in staged.values():
            path.unlink(missing_ok=True)
        self.on_commit()
        shutil.rmtree(self.batch_dir / MANIFEST_DIR, ignore_errors=True)
        for leftover in sorted(self.batch_dir.glob("*"), reverse=True):
            if leftover.is_dir() and not any(leftover.iterdir()):
                leftover.rmdir()
        logger.info(
            "backup.batch_done",
            camera=self.camera.alias,
            clips=len(staged),
            bytes=size,
            snapshot=summary["snapshot_id"][:8],
            download_secs=round(download_secs, 1) if download_secs is not None else None,
            upload_secs=round(upload_secs, 1),
        )
        return len(staged), size

    def _write_manifest(self, staged: dict[int, Path]) -> None:
        rows = self.db.index_rows(self.camera.alias, staged)
        path = self.batch_dir / MANIFEST_DIR / MANIFEST_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"format": 1, "camera": self.camera.alias, "clips": rows}), encoding="utf-8"
        )

    def _rebuild_index(self) -> None:
        """Snapshots this destination holds that the database has no location
        for (the database was lost, restored from an older copy, or a commit
        crashed after restic finished) mean the index is behind: take the
        newest snapshot's manifest, then record where every snapshot is. Two
        restic calls on top of the listing; if it fails the run fails, so a
        stale index never causes downloads."""
        alias, dest = self.camera.alias, self.destination
        snaps = self.restic.snapshots()
        known = self.db.location_roots(alias, dest)
        self.state.index_mismatch = not snaps and bool(known)
        if not (any(s.root not in known for s in snaps)):
            return
        latest = snaps[-1]
        manifest = f"{latest.paths[0]}/{MANIFEST_DIR}/{MANIFEST_NAME}"
        try:
            data = json.loads(self.restic.read(latest.id, manifest))
            # A clip the manifest says is in this snapshot must really be
            # there: a `restic backup` that exited 3 (a file unreadable)
            # still leaves an incomplete snapshot behind.
            present = self.restic.files(latest.id)
        except (ToolError, ValueError) as exc:
            raise RunError(
                "The backup has snapshots this database doesn't know, and the newest one has "
                f"no readable index to rebuild from: {exc}"
            ) from exc
        # Lost and expired clips carry no snapshot; only this snapshot's own
        # backed-up clips need checking.
        rows = [
            c
            for c in data["clips"]
            if c["state"] != "backed_up" or c["snapshot_id"] or c["path"] in present
        ]
        imported = self.db.import_index(alias, latest.root, iso(latest.time), rows)
        self.db.set_locations(alias, dest, [(sn.root, sn.id, sn.time) for sn in snaps])
        expired = self.db.expire_unlocated(alias)
        logger.info("backup.index_rebuilt", camera=alias, clips=imported, expired=expired)


def pick(pending: list[Clip], now: datetime, interval: timedelta) -> list[Clip]:
    """The next batch: clips recorded since the last run first, then the
    oldest (the next ones the card overwrites). Clips that keep failing go
    last and are tried at most once a day. A clip the card lists without a
    size waits until it reports one (likely still recording): without it the
    download can't be verified."""
    recent_cutoff = now - 2 * interval
    day_ago = now - timedelta(days=1)

    def stubborn(c: Clip) -> bool:
        return c.attempts >= MAX_ATTEMPTS

    def resting(c: Clip) -> bool:
        return (
            stubborn(c) and c.last_attempt_at is not None and parse_iso(c.last_attempt_at) > day_ago
        )

    due = [c for c in pending if not resting(c) and c.size is not None]
    recent = [c for c in due if not stubborn(c) and parse_iso(c.start_utc) >= recent_cutoff]
    rest = [c for c in due if c not in recent]
    ordered = recent + sorted(rest, key=lambda c: (stubborn(c), c.start_utc))
    batch: list[Clip] = []
    total = 0
    for clip in ordered:
        if batch and total + (clip.size or 0) > BATCH_BYTES:
            break
        batch.append(clip)
        total += clip.size or 0
    return batch


def _timeout(clips: list[Clip]) -> float:
    return 120 + sum(c.size or 0 for c in clips) / _MIN_DOWNLOAD_RATE


def _short(path: Path, clip: Clip) -> bool:
    return path.stat().st_size != clip.size


def _files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []
