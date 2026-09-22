"""The archive state machine: the core of the system.

Implements state-machine steps 1-8, retry/backoff classification, the
single-instance lock, and reconciliation (orphaned staging files, and vault
files left unreferenced by a crash between the atomic rename and the DB
commit that follows it).

Ordering invariant this module exists to uphold: **no failure can lose an
archived recording, and no crash can mark an unfinished one as archived.**
"""

from __future__ import annotations

import fcntl
import os
import random
import re
import shutil
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from reovault.db.repository import RecordingRow, Repository, parse_iso_utc
from reovault.logging import get_logger
from reovault.models import ArchiveRunResult, ErrorClass, RemoteRecording
from reovault.providers.base import AuthError, CameraProvider, ProviderError
from reovault.storage.vault import VaultStore

logger = get_logger(__name__)

BASE_BACKOFF_SECS = 60
MAX_BACKOFF_SECS = 6 * 3600
JITTER_FRACTION = 0.25

# How many attempts a class gets before escalating from `failed` (retryable)
# to `quarantined` (needs a human). Input and protocol errors have no entry:
# they quarantine on the first failure.
_MAX_ATTEMPTS: dict[ErrorClass, int] = {
    ErrorClass.NETWORK: 8,
    ErrorClass.DEVICE: 2,
}

_VAULT_FILENAME_RE = re.compile(r"^(?P<start>\d{8}T\d{6}Z)_(?P<remote_name>.+)\.enc$")


class AlreadyRunningError(Exception):
    """Another `run()`/`reconcile()` holds the single-instance lock, so a
    manual run can never race the scheduled one."""


class _AbortRun(Exception):
    """Internal signal: stop processing further recordings in this run.
    Raised for auth failures (retrying a bad password can lock the account)
    and for local failures (ours, not the camera's; continuing would just
    fail identically on every remaining recording)."""


@contextmanager
def instance_lock(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise AlreadyRunningError(f"another run holds the lock at {lock_path}") from exc
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def compute_backoff(attempts: int) -> timedelta:
    """Exponential backoff capped at 6h with ±25% jitter. `attempts` is the
    attempt number just made (1-indexed): attempt 1 -> ~1m, attempt 2 ->
    ~2m, attempt 3 -> ~4m, ..."""
    base = min(BASE_BACKOFF_SECS * (2 ** (attempts - 1)), MAX_BACKOFF_SECS)
    jitter = base * JITTER_FRACTION
    delay = base + random.uniform(-jitter, jitter)
    return timedelta(seconds=max(delay, 0.0))


def assert_same_filesystem(staging_dir: Path, vault_dir: Path) -> None:
    """Atomic rename (state-machine step 6) requires the `.tmp` file and its
    final destination to be on the same filesystem; failing loudly here
    beats failing mid-archive at 05:00."""
    staging_dir.mkdir(parents=True, exist_ok=True)
    vault_dir.mkdir(parents=True, exist_ok=True)
    if os.stat(staging_dir).st_dev != os.stat(vault_dir).st_dev:
        raise RuntimeError(
            f"staging_dir {staging_dir} and vault_dir {vault_dir} are on different "
            "filesystems; atomic rename between them is not possible"
        )


def assert_pinned_cli_version(binary: str, pinned_version: str) -> None:
    """Fails loudly at startup if the installed `reolink-cli` isn't the
    pinned version this integration was verified against, rather than
    surfacing as a subtle behavior mismatch later."""
    try:
        result = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10)
    except FileNotFoundError as exc:
        raise RuntimeError(f"{binary!r} not found on PATH") from exc
    if pinned_version not in result.stdout:
        raise RuntimeError(
            f"{binary} --version reported {result.stdout.strip()!r}, "
            f"expected the pinned version {pinned_version!r}"
        )


def _parse_vault_path(vault_path: str) -> tuple[str, datetime, str] | None:
    """Recovers `(device_alias, start_utc, remote_name)` from a vault path
    shaped like `<alias>/<YYYY>/<MM>/<DD>/<start>_<remote_name>.enc`. None
    if it doesn't match that shape at all, in which case reconciliation
    can't attribute it to any recording."""
    parts = PurePosixPath(vault_path).parts
    if len(parts) < 2:
        return None
    match = _VAULT_FILENAME_RE.match(parts[-1])
    if not match:
        return None
    start_utc = datetime.strptime(match.group("start"), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    return parts[0], start_utc, match.group("remote_name")


@dataclass
class Archiver:
    repository: Repository
    provider: CameraProvider
    vault: VaultStore
    device_id: int
    device_alias: str
    channel: int
    # Must be a directory exclusively owned by this device.
    # `_reconcile_staging` below sweeps this directory's entire contents, so
    # pointing two Archivers at the same staging_dir lets one camera's
    # reconcile delete another's in-flight download.
    staging_dir: Path
    lock_path: Path
    processed_ids: set[int] = field(default_factory=set, init=False, repr=False)
    # Cooperative cancellation for the dashboard's Stop button: a plain flag,
    # not a hard kill, so cancellation can only ever land *between*
    # recordings, never mid-fetch or mid-encrypt. That keeps the module's
    # own invariant intact -- a canceled run finishes exactly like any other
    # early exit, via `_AbortRun`, with everything already finalized staying
    # finalized and nothing partially written ever marked archived.
    cancel_requested: threading.Event = field(
        default_factory=threading.Event, init=False, repr=False
    )

    def request_cancel(self) -> None:
        self.cancel_requested.set()

    # -- the state machine ------------------------------------------------

    def run(self, *, trigger: str, from_utc: datetime, to_utc: datetime) -> ArchiveRunResult:
        with instance_lock(self.lock_path):
            return self._run_locked(trigger=trigger, from_utc=from_utc, to_utc=to_utc)

    def _run_locked(
        self, *, trigger: str, from_utc: datetime, to_utc: datetime
    ) -> ArchiveRunResult:
        # Widening absorbs DST ambiguity; duplicates from the overlap cost
        # nothing, the unique index absorbs them.
        widened_from = from_utc - timedelta(hours=1)
        widened_to = to_utc + timedelta(hours=1)
        run_id = self.repository.start_run(
            device_id=self.device_id,
            trigger=trigger,
            window_from_utc=widened_from,
            window_to_utc=widened_to,
        )
        result = ArchiveRunResult(trigger=trigger)
        self.processed_ids = set()
        self.cancel_requested.clear()
        staging_run_dir = self.staging_dir / str(run_id)

        try:
            recordings = self.provider.list_recordings(from_utc=widened_from, to_utc=widened_to)
        except AuthError as exc:
            return self._finish(run_id, result, outcome="aborted", error=exc)
        except ProviderError as exc:
            return self._finish(run_id, result, outcome="failed", error=exc)

        result.discovered = len(recordings)
        try:
            for rec in recordings:
                if self.cancel_requested.is_set():
                    raise _AbortRun("canceled")
                self._maybe_process_discovered(rec, result, staging_run_dir)
            for row in self.repository.pending(device_id=self.device_id):
                if row.id in self.processed_ids:
                    continue
                if self.cancel_requested.is_set():
                    raise _AbortRun("canceled")
                self._process_one(
                    row.id, self._row_to_remote_recording(row), result, staging_run_dir
                )
        except _AbortRun as exc:
            return self._finish(run_id, result, outcome="aborted", error=exc)

        shutil.rmtree(staging_run_dir, ignore_errors=True)
        outcome = "success" if result.failed == 0 else "partial"
        return self._finish(run_id, result, outcome=outcome)

    def _maybe_process_discovered(
        self, rec: RemoteRecording, result: ArchiveRunResult, staging_run_dir: Path
    ) -> None:
        row_id, created = self.repository.discover_recording(
            device_id=self.device_id, channel=self.channel, recording=rec
        )
        if not created:
            # Already known: archived (dedup, count it), or mid-flight/failed/
            # quarantined from another context. Leave anything not fresh
            # alone here; `pending()` below picks up what's actually due.
            row = self.repository.get(row_id)
            if row is not None and row.state == "archived":
                result.skipped_dup += 1
            return
        self._process_one(row_id, rec, result, staging_run_dir)

    def _process_one(
        self,
        row_id: int,
        rec: RemoteRecording,
        result: ArchiveRunResult,
        staging_run_dir: Path,
    ) -> None:
        self.processed_ids.add(row_id)
        self.repository.transition_downloading(row_id)  # step 2, commit

        staging_path = staging_run_dir / rec.remote_name
        staging_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            fetch_result = self.provider.fetch(rec, str(staging_path))  # step 3
        except ProviderError as exc:
            self._handle_provider_failure(row_id, exc, result)
            return
        except OSError as exc:
            result.error = str(exc)
            raise _AbortRun(str(exc)) from exc

        if rec.remote_size is not None and fetch_result.bytes_written != rec.remote_size:
            staging_path.unlink(missing_ok=True)
            self._handle_size_mismatch(row_id, rec, fetch_result.bytes_written, result)
            return

        self.repository.transition_verifying(row_id)  # step 4 boundary, commit

        try:
            put_result = self.vault.put(  # steps 5-6: encrypt, fsync, atomic rename
                staging_path,
                device_alias=self.device_alias,
                start_utc=rec.start_utc,
                remote_name=rec.remote_name,
            )
        except OSError as exc:
            result.error = str(exc)
            raise _AbortRun(str(exc)) from exc

        self.repository.finalize_archived(  # step 7, commit
            row_id,
            vault_path=put_result.vault_path,
            plaintext_sha256=put_result.plaintext_sha256,
            plaintext_size=put_result.plaintext_size,
            ciphertext_size=put_result.ciphertext_size,
        )
        staging_path.unlink(missing_ok=True)  # step 8

        result.downloaded += 1
        result.bytes_archived += put_result.plaintext_size

    def _handle_provider_failure(
        self, row_id: int, exc: ProviderError, result: ArchiveRunResult
    ) -> None:
        result.failed += 1
        result.errors.append(str(exc))

        if exc.error_class == ErrorClass.AUTH:
            # Stop the run immediately: retrying a bad password can lock the
            # device account, so this recording gets one more chance next
            # run, not a tight retry loop right now.
            self.repository.mark_failed(
                row_id,
                error=str(exc),
                error_class=exc.error_class,
                error_detail=exc.detail,
                next_attempt_at=None,
            )
            raise _AbortRun(str(exc)) from exc

        if exc.error_class == ErrorClass.INPUT or exc.error_class == ErrorClass.PROTOCOL:
            # Never retry-loop on a ReoVault bug or an upstream schema change.
            self.repository.mark_quarantined(
                row_id, error=str(exc), error_class=exc.error_class, error_detail=exc.detail
            )
            return

        row = self.repository.get(row_id)
        attempts = row.attempts if row is not None else 1
        max_attempts = _MAX_ATTEMPTS.get(exc.error_class, 1)
        if attempts >= max_attempts:
            self.repository.mark_quarantined(
                row_id, error=str(exc), error_class=exc.error_class, error_detail=exc.detail
            )
        else:
            next_attempt_at = datetime.now(UTC) + compute_backoff(attempts)
            self.repository.mark_failed(
                row_id,
                error=str(exc),
                error_class=exc.error_class,
                error_detail=exc.detail,
                next_attempt_at=next_attempt_at,
            )

    def _handle_size_mismatch(
        self, row_id: int, rec: RemoteRecording, actual: int, result: ArchiveRunResult
    ) -> None:
        message = f"downloaded {actual} bytes, expected {rec.remote_size} (remote_size)"
        result.failed += 1
        result.errors.append(message)
        # A silent, persistent size mismatch on the same recording points at
        # something structural (protocol drift), not a transient network
        # blip, so it's classified like a protocol error: quarantine, don't
        # retry-loop.
        self.repository.mark_quarantined(row_id, error=message, error_class=ErrorClass.PROTOCOL)

    def _finish(
        self,
        run_id: int,
        result: ArchiveRunResult,
        *,
        outcome: str,
        error: Exception | None = None,
    ) -> ArchiveRunResult:
        if error is not None:
            result.error = str(error)
            logger.error("archiver.run_" + outcome, error=str(error))
        self.repository.finish_run(
            run_id,
            outcome=outcome,
            discovered=result.discovered,
            downloaded=result.downloaded,
            skipped_dup=result.skipped_dup,
            failed=result.failed,
            bytes_archived=result.bytes_archived,
            error=result.error,
        )
        return result

    def _row_to_remote_recording(self, row: RecordingRow) -> RemoteRecording:
        return RemoteRecording(
            remote_name=row.remote_name,
            start_utc=parse_iso_utc(row.start_utc),
            end_utc=None,
            duration_s=None,
            rec_type=None,
            stream=None,
            remote_size=row.remote_size,
            raw_metadata={},
        )

    # -- reconciliation -----------------------------------------------------
    # Sweeps orphaned staging files (a crash between steps 2 and 8) and
    # adopts/deletes unreferenced vault files (a crash between steps 6 and
    # 7). Takes the same lock as `run()`: nothing here is safe to do while a
    # run is actually in flight.

    def reconcile(self) -> None:
        with instance_lock(self.lock_path):
            self._reconcile_stuck_rows()
            self._reconcile_staging()
            self._reconcile_vault()

    def _reconcile_stuck_rows(self) -> None:
        for row in self.repository.stuck_in_progress(device_id=self.device_id):
            logger.warning(
                "archiver.reconcile.requeue_stuck_row", recording_id=row.id, state=row.state
            )
            self.repository.requeue_after_crash(row.id)

    def _reconcile_staging(self) -> None:
        if not self.staging_dir.exists():
            return
        for child in self.staging_dir.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)

    def _reconcile_vault(self) -> None:
        # `vault` is shared across every device in the fleet (namespaced
        # internally by `<alias>/...`), so `list_vault_paths()` returns
        # every camera's files, not just this one's. Without this skip, a
        # camera whose channel doesn't match another device's (the lookup
        # below resolves by alias but reuses `self.channel`) could resolve
        # to no device at all and have `_adopt_or_delete_orphan` *delete* a
        # perfectly healthy file belonging to a different camera.
        prefix = f"{self.device_alias}/"
        for vault_path in self.vault.list_vault_paths():
            if not vault_path.startswith(prefix):
                continue
            if self.repository.find_archived_by_vault_path(vault_path) is not None:
                continue  # already correctly tracked
            self._adopt_or_delete_orphan(vault_path)

    def _adopt_or_delete_orphan(self, vault_path: str) -> None:
        row: RecordingRow | None = None
        parsed = _parse_vault_path(vault_path)
        if parsed is not None:
            device_alias, start_utc, remote_name = parsed
            device_id = self.repository.get_device_id(alias=device_alias, channel=self.channel)
            if device_id is not None:
                row = self.repository.find_by_natural_key(
                    device_id=device_id,
                    channel=self.channel,
                    remote_name=remote_name,
                    start_utc=start_utc,
                )

        if row is None or row.state == "archived":
            logger.warning(
                "archiver.reconcile.delete_unattributed_vault_file", vault_path=vault_path
            )
            self.vault.delete(vault_path)
            return

        if not self.vault.verify(vault_path):
            logger.warning("archiver.reconcile.delete_corrupt_orphan", vault_path=vault_path)
            self.vault.delete(vault_path)
            return

        info = self.vault.describe(vault_path)
        self.repository.finalize_archived(
            row.id,
            vault_path=vault_path,
            plaintext_sha256=info.plaintext_sha256,
            plaintext_size=info.plaintext_size,
            ciphertext_size=info.ciphertext_size,
        )
        logger.info("archiver.reconcile.adopted_orphan", vault_path=vault_path, recording_id=row.id)
