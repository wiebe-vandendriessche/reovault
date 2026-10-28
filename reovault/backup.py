"""One camera's backup run: list the card, then batch by batch download,
verify, `restic backup`, and record what is safe.

The invariant: a clip is `backed_up` only after its size matched the card's,
restic exited 0 with a snapshot id, and the database committed. Local files
are deleted only after that. A crash anywhere before the commit just means
the next run backs the same files up again; restic stores the data once.

Local disk never holds more than one batch: when restic can't upload, the
run stops downloading and the clips wait on the SD card.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from reovault.camera import Camera, CardStatus, Downloaded, Found, scan_card
from reovault.db import Clip, Db, parse_iso
from reovault.logging import get_logger
from reovault.restic import Restic
from reovault.tools import ToolError

logger = get_logger(__name__)

BATCH_BYTES = 2 * 1024**3
# Measured on a D340W: 1 invocation ~1.7 MB/s, 5 in parallel ~2.2 MB/s.
# ponytail: 1 until the Step 4 hardware check says 2 is worth it.
PARALLEL_DOWNLOADS = 1
MAX_ATTEMPTS = 5
SIZE_RETRY_PAUSE_SECS = 10.0  # D340W: a short file right after a big one
FULL_SCAN_EVERY = timedelta(days=1)
# Timeouts scale with the bytes: a flat limit would kill a 2 GB batch.
_MIN_DOWNLOAD_RATE = 0.5 * 1024**2
_MIN_UPLOAD_RATE = 0.25 * 1024**2


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
    on_progress: Callable[[], None] = lambda: None
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
        alias = self.camera.alias
        result = RunResult()
        run_id = self.db.start_run(alias, trigger)
        try:
            with camera_lock(self.staging):
                self._run(result, run_id)
        except (RunError, ToolError, AlreadyRunning, OSError) as exc:
            result.outcome, result.error = "error", str(exc)
            logger.error("backup.run_failed", camera=alias, error=str(exc))
        self.state.last_failed = result.outcome == "error"
        self.db.finish_run(run_id, result.outcome, result.error)
        self.on_progress()
        return result

    def _run(self, result: RunResult, run_id: int) -> None:
        self._recover(result)
        self._ensure_repository()
        self._list()
        tried: set[int] = set()  # a clip that failed waits for the next run
        while True:
            if self.state.stop.is_set():
                result.outcome = "stopped"
                return
            free = shutil.disk_usage(self.staging).free
            if free < BATCH_BYTES * 1.2:
                raise RunError(f"Not enough free space for a batch in {self.staging}")
            pending = [c for c in self.db.pending(self.camera.alias) if c.id not in tried]
            batch = pick(pending, self.clock(), self.interval)
            if not batch:
                return
            tried.update(c.id for c in batch)
            self._batch(batch, result)
            self.db.update_run(run_id, clips=result.clips, bytes_=result.bytes, lost=result.lost)
            self.on_progress()
            self._search_recent()

    # -- steps -----------------------------------------------------------

    def _recover(self, result: RunResult) -> None:
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
            self._ensure_repository()
            self._commit(keep, result)

    def _ensure_repository(self) -> None:
        state = self.restic.state()
        if state == "wrong_password":
            raise RunError("The restic password does not open this camera's repository")
        if state == "missing":
            if self.db.has_history(self.camera.alias, self.restic.repository):
                raise RunError(
                    "The backup destination has no repository for this camera any more, "
                    "but clips were backed up to it before. Check that the destination is "
                    "mounted and the path is right; nothing will be created."
                )
            logger.info("backup.repository_init", camera=self.camera.alias)
            self.restic.init()

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
        self._upsert(found)
        self.state.oldest_on_card, self.state.scanned_at = oldest, now
        try:
            self.state.card = self.camera.storage()
        except ToolError as exc:  # only shown, never acted on
            logger.warning("backup.storage_status_failed", camera=self.camera.alias, error=str(exc))

    def _search_recent(self) -> None:
        now = self.clock()
        newest = self.db.newest_start(self.camera.alias) or now - 2 * self.interval
        self._upsert(self.camera.search(newest - timedelta(hours=2), now + timedelta(hours=2)))

    def _upsert(self, found: list[Found]) -> None:
        self.db.upsert_clips(
            self.camera.alias, [(f.name, f.start_utc, f.end_utc, f.size, f.rec_type) for f in found]
        )

    def _batch(self, batch: list[Clip], result: RunResult) -> None:
        by_name = {c.name: c for c in batch}
        started = time.monotonic()
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

        staged: dict[int, Path] = {}
        tz = ZoneInfo(self.camera.timezone)
        for name, path in got.files.items():
            clip = by_name[name]
            day = parse_iso(clip.start_utc).astimezone(tz).date().isoformat()
            dest = self.batch_dir / day / f"{name}{path.suffix}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, dest)  # same filesystem: atomic
            staged[clip.id] = dest
        shutil.rmtree(self.incoming, ignore_errors=True)
        if staged:
            self._commit(staged, result, download_secs=time.monotonic() - started)

    def _download(self, clips: list[Clip]) -> Downloaded:
        """Up to `PARALLEL_DOWNLOADS` batch invocations at once. When any
        invocation fails as a whole (camera or network down), the run ends
        without blaming a single clip."""
        groups = [clips[i::PARALLEL_DOWNLOADS] for i in range(PARALLEL_DOWNLOADS)]
        groups = [g for g in groups if g]

        def fetch(i_group: tuple[int, list[Clip]]) -> Downloaded:
            i, group = i_group
            size = sum(c.size or 0 for c in group)
            return self.camera.download(
                [c.name for c in group],
                self.incoming / str(i),
                timeout=120 + size / _MIN_DOWNLOAD_RATE,
            )

        merged = Downloaded()
        with ThreadPoolExecutor(len(groups)) as pool:
            for got in pool.map(fetch, enumerate(groups)):
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
            start = datetime.fromisoformat(day).replace(tzinfo=tz)
            listed = {
                f.name
                for f in self.camera.search(
                    start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)
                )
            }
            for clip in clips:
                if clip.name in listed:
                    self.db.record_failure(clip.id, failed[clip])
                else:
                    self.db.mark_lost(clip.id)
                    lost += 1
        if lost:
            logger.warning("backup.clips_lost", camera=self.camera.alias, count=lost)
        return lost

    def _commit(
        self, staged: dict[int, Path], result: RunResult, download_secs: float | None = None
    ) -> None:
        size = sum(p.stat().st_size for p in staged.values())
        started = time.monotonic()
        summary = self.restic.backup(self.batch_dir, timeout=600 + size / _MIN_UPLOAD_RATE)
        upload_secs = time.monotonic() - started
        self.db.mark_backed_up(
            self.camera.alias,
            self.restic.repository,
            summary["snapshot_id"],
            {cid: str(path) for cid, path in staged.items()},
        )
        for path in staged.values():
            path.unlink(missing_ok=True)
        for leftover in sorted(self.batch_dir.glob("*"), reverse=True):
            if leftover.is_dir() and not any(leftover.iterdir()):
                leftover.rmdir()
        result.clips += len(staged)
        result.bytes += size
        logger.info(
            "backup.batch_done",
            camera=self.camera.alias,
            clips=len(staged),
            bytes=size,
            snapshot=summary["snapshot_id"][:8],
            download_secs=round(download_secs, 1) if download_secs is not None else None,
            upload_secs=round(upload_secs, 1),
        )


def pick(pending: list[Clip], now: datetime, interval: timedelta) -> list[Clip]:
    """The next batch: clips recorded since the last run first, then the
    oldest (the next ones the card overwrites). Clips that keep failing go
    last and are tried at most once a day."""
    recent_cutoff = now - 2 * interval
    day_ago = now - timedelta(days=1)

    def stubborn(c: Clip) -> bool:
        return c.attempts >= MAX_ATTEMPTS

    def resting(c: Clip) -> bool:
        return (
            stubborn(c) and c.last_attempt_at is not None and parse_iso(c.last_attempt_at) > day_ago
        )

    due = [c for c in pending if not resting(c)]
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


def _short(path: Path, clip: Clip) -> bool:
    return clip.size is not None and path.stat().st_size != clip.size


def _files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []
