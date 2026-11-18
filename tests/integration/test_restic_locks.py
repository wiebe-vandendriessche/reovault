"""restic locks and checks, against the real pinned restic.

Production, 2026-11: a `restic check` couldn't remove its lock while the
backend hung, and every later command failed on it for days, because restic
never ignores a stale lock and ReoVault only unlocked at startup.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest

from reovault import restic as restic_mod
from reovault.restic import Restic, failure_message


@pytest.fixture
def repo(tmp_path, monkeypatch):
    binary = shutil.which("restic")
    if binary is None:
        pytest.fail("restic is not on PATH; install the pinned version (see CONTRIBUTING.md)")
    pw = tmp_path / "pw"
    pw.write_text("test-password")
    r = Restic.for_camera(
        binary,
        str(tmp_path / "repos"),
        "cam",
        {"RESTIC_PASSWORD_FILE": str(pw), "RESTIC_CACHE_DIR": str(tmp_path / "cache")},
    )
    r.init()
    data = tmp_path / "data"
    data.mkdir()
    for i in range(2):
        (data / f"f{i}").write_bytes(b"x" * 1000 * (i + 1))
        r.backup(data, timeout=60)
    # Don't sit out restic's lock wait in a test: a stale lock still makes the
    # command fail (exit 11) after it.
    monkeypatch.setattr(restic_mod, "LOCK_WAIT", "1s")
    return r


def _hold_lock(r: Restic) -> subprocess.Popen[bytes]:
    """A `restic backup --stdin` waiting for input holds a lock until killed."""
    proc = subprocess.Popen(
        [r.binary, "-r", r.repository, "backup", "--stdin", "--host", "reovault"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=r.env,
    )
    locks = Path(r.repository) / "locks"
    deadline = time.monotonic() + 20
    while not any(locks.iterdir()):
        assert time.monotonic() < deadline, "restic took no lock"
        time.sleep(0.05)
    return proc


def test_a_lock_left_by_a_killed_restic_heals_itself(repo):
    proc = _hold_lock(repo)
    proc.kill()  # SIGKILL: restic can't remove its lock
    proc.wait()
    assert any((Path(repo.repository) / "locks").iterdir())
    old = repo.snapshots()[0]
    repo.forget_ids([old.id])  # exclusive: trips over the stale lock, unlocks, retries
    assert [s.id for s in repo.snapshots()] != [old.id]
    assert len(repo.snapshots()) == 1


def test_a_check_that_meets_a_live_lock_failed_it_found_no_damage(repo):
    proc = _hold_lock(repo)  # alive: `unlock` must leave it alone
    try:
        result = repo.check("1%")
    finally:
        proc.kill()
        proc.wait()
    assert result.outcome == "failed"
    assert result.errors == 0
    assert "locked" in result.message
    assert "PID" in result.message  # who holds it, not just restic's last-line hint


def test_a_clean_check_is_ok(repo):
    assert repo.check("10%").outcome == "ok"


def test_the_useful_line_of_a_lock_error_is_kept():
    stderr = (
        "repo already locked, waiting up to 0s for the lock\n"
        "unable to create lock in backend: repository is already locked exclusively by "
        "PID 204834 on reovault by reovault (UID 1000, GID 1000)\n"
        "lock was created at 2026-11-13 22:48:36 (82h4m ago)\n"
        "storage ID 1a2b3c4d\n"
        "the `unlock` command can be used to remove stale locks\n"
    )
    message = failure_message(stderr)
    assert message is not None
    assert "PID 204834" in message and "22:48:36" in message
    assert failure_message('{"message_type":"exit_error","code":1,"message":"Fatal: boom"}') == (
        "Fatal: boom"
    )


def test_a_destination_that_doesnt_answer_is_told_apart_from_a_broken_repository():
    from reovault.restic import destination_down

    nextcloud_gone = (
        'rclone: CRITICAL: Failed to create file system for "r:/repo": read metadata failed: '
        'Propfind "http://127.0.0.1:9/repo": dial tcp 127.0.0.1:9: connect: connection refused\n'
        "Fatal: unable to open repository at rclone:r:/repo: error talking HTTP to rclone: "
        "exit status 1\n"
    )
    nextcloud_hung = (
        "Fatal: unable to open repository at rclone:reovault-Nextcloud:reovault/front-door: "
        'error talking HTTP to rclone: Get "http://localhost/file-1": context deadline exceeded '
        "(Client.Timeout exceeded while awaiting headers)\n"
    )
    assert destination_down(nextcloud_gone) and destination_down(nextcloud_hung)
    assert not destination_down("Fatal: wrong password or no key found\n")
    assert not destination_down("Fatal: repository contains errors\n")
