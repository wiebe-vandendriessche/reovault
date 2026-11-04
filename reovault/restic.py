"""restic, one repository per camera at `<base>/<cam_id>`.

Verified with restic 0.19.1 (2026-10-28): exit 10 = repository missing,
12 = wrong password; `--host` is a per-command flag (backup, forget,
snapshots), not a global one; backing up the same fixed directory every time
keeps one `forget` group.

`restic copy` (verified 2026-11-02): a copied snapshot gets a new id and records
the source in its `original` field, also through a copy of a copy; copying a
snapshot twice does nothing; the destination must be initialised with
`--copy-chunker-params`. Retention is done by ReoVault (`forget <ids>`), not by
`--keep-within`, so a snapshot is never forgotten before its copies exist.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from reovault.tools import ToolError, describe_exit, run

HOST = "reovault"  # fixed: Docker gives a recreated container a new hostname
MISSING, WRONG_PASSWORD = 10, 12

RepoState = Literal["ok", "missing", "wrong_password"]


LOCK_WAIT = "10m"
LOCK_WAIT_SECS = 600  # added to the timeout of anything that may wait for a lock


@dataclass(frozen=True, slots=True)
class Snapshot:
    id: str
    time: datetime
    original: str | None
    paths: tuple[str, ...]

    @property
    def root(self) -> str:
        """The id this snapshot had where it was first made: what clips point
        at, whichever destination it sits on."""
        return self.original or self.id


def _snapshot(raw: dict[str, Any]) -> Snapshot:
    return Snapshot(
        id=raw["id"],
        time=datetime.fromisoformat(raw["time"]),
        original=raw.get("original"),
        paths=tuple(raw.get("paths") or ()),
    )


@dataclass(frozen=True, slots=True)
class Restic:
    binary: str
    repository: str  # this camera's own repository, `<base>/<cam_id>`
    env: dict[str, str]

    @classmethod
    def for_camera(cls, binary: str, base: str, cam_id: str, env: dict[str, str]) -> Restic:
        return cls(binary, f"{base.rstrip('/')}/{cam_id}", {**os.environ, **env})

    def _run(
        self, *args: str, timeout: float = 600, wait_lock: bool = False
    ) -> subprocess.CompletedProcess[str]:
        """`wait_lock`: the command takes a lock another ReoVault thread may hold
        (backup, copy, forget, or a listing during a prune): wait for it instead
        of failing, and don't let the timeout cut the wait short."""
        wait = ["--retry-lock", LOCK_WAIT] if wait_lock else []
        return run(
            [self.binary, "-r", self.repository, *wait, *args],
            timeout=timeout + (LOCK_WAIT_SECS if wait_lock else 0),
            env=self.env,
        )

    def _fail(self, what: str, proc: subprocess.CompletedProcess[str]) -> ToolError:
        last = proc.stderr.strip().splitlines()[-1:] or [describe_exit("restic", proc.returncode)]
        return ToolError(
            f"restic {what} failed: {last[0]}", code=proc.returncode, detail=proc.stderr[-4000:]
        )

    def state(self) -> RepoState:
        proc = self._run("cat", "config", timeout=120)
        if proc.returncode == 0:
            return "ok"
        if proc.returncode == MISSING:
            return "missing"
        if proc.returncode == WRONG_PASSWORD:
            return "wrong_password"
        raise self._fail("open", proc)

    def _password_file(self) -> str:
        path = self.env.get("RESTIC_PASSWORD_FILE")
        if not path:
            raise ToolError("Copying needs the restic password as a file (RESTIC_PASSWORD_FILE)")
        return path

    def init(self, copy_of: Restic | None = None) -> None:
        """A copy destination must use the source's chunker parameters, or
        identical files stop deduplicating."""
        extra = (
            [
                "--from-repo",
                copy_of.repository,
                "--from-password-file",
                self._password_file(),
                "--copy-chunker-params",
            ]
            if copy_of is not None
            else []
        )
        proc = self._run("init", *extra, timeout=300)
        if proc.returncode != 0:
            raise self._fail("init", proc)

    def copy_from(self, source: Restic, snapshot_id: str, timeout: float) -> None:
        """Copies one snapshot from `source` into this repository (same
        password). One per call: a copy reads and rewrites the whole snapshot,
        so a call is as long as its snapshot, and the source's lock is only
        held that long."""
        proc = self._run(
            "copy",
            "--from-repo",
            source.repository,
            "--from-password-file",
            self._password_file(),
            "--host",
            HOST,
            snapshot_id,
            timeout=timeout,
            wait_lock=True,
        )
        if proc.returncode != 0:
            raise self._fail("copy", proc)

    def unlock(self) -> None:
        """Removes stale locks only (a process that died holding one)."""
        self._run("unlock", timeout=120)

    def backup(self, path: Path, timeout: float) -> dict[str, Any]:
        """Back up `path`. Success means exit 0 *and* a snapshot id: exit 3
        (some file unreadable) is a failure for the whole batch."""
        proc = self._run(
            "backup", "--host", HOST, "--json", str(path), timeout=timeout, wait_lock=True
        )
        summary = next(
            (
                msg
                for line in proc.stdout.splitlines()
                if line.startswith("{")
                for msg in [json.loads(line)]
                if msg.get("message_type") == "summary"
            ),
            None,
        )
        if proc.returncode != 0 or not summary or not summary.get("snapshot_id"):
            raise self._fail("backup", proc)
        return summary

    def snapshots(self) -> list[Snapshot]:
        proc = self._run("snapshots", "--host", HOST, "--json", timeout=300, wait_lock=True)
        if proc.returncode != 0:
            raise self._fail("snapshots", proc)
        return sorted((_snapshot(r) for r in json.loads(proc.stdout or "[]")), key=lambda x: x.time)

    def latest(self) -> Snapshot:
        """The newest snapshot (by parsed time: restic writes local time with
        an offset, which doesn't sort as text)."""
        snaps = self.snapshots()
        if not snaps:
            raise ToolError("restic found no snapshot")
        return snaps[-1]

    def files(self, snapshot_id: str) -> set[str]:
        """Every file path in a snapshot."""
        proc = self._run("ls", "--json", snapshot_id, timeout=600)
        if proc.returncode != 0:
            raise self._fail("ls", proc)
        nodes = (json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{"))
        return {n["path"] for n in nodes if n.get("type") == "file"}

    def read(self, snapshot_id: str, path: str) -> str:
        """One small text file out of a snapshot."""
        proc = self._run("--no-lock", "dump", snapshot_id, path, timeout=600)
        if proc.returncode != 0:
            raise self._fail("dump", proc)
        return proc.stdout

    def forget_ids(self, ids: list[str]) -> None:
        """Forgets exactly these snapshots and prunes what only they used.
        `--prune` mixes JSON and text output, so only the exit code counts."""
        if not ids:
            return
        proc = self._run("forget", "--prune", *ids, timeout=6 * 3600, wait_lock=True)
        if proc.returncode != 0:
            raise self._fail("forget", proc)

    def forget_last(self, n: int) -> None:
        """Keep only the newest `n` snapshots (for the small config repository)."""
        proc = self._run(
            "forget", "--host", HOST, "--keep-last", str(n), "--prune", timeout=1800, wait_lock=True
        )
        if proc.returncode != 0:
            raise self._fail("forget", proc)

    def check(self, subset: str) -> int:
        """Number of errors found reading a `subset` (e.g. "500M") of the data."""
        proc = self._run("check", "--read-data-subset", subset, "--json", timeout=6 * 3600)
        summary = next(
            (json.loads(line) for line in proc.stdout.splitlines() if '"summary"' in line), None
        )
        if summary is None:
            raise self._fail("check", proc)
        return int(summary.get("num_errors", 0))

    def dump(self, snapshot_id: str, path: str) -> subprocess.Popen[bytes]:
        """Streams one file out of a snapshot on stdout. Without a lock: the
        weekly `check` holds an exclusive one for as long as it reads, and a
        download must not wait hours for it. Reading is safe next to `check`;
        next to a `prune` a pack can vanish mid-read, and then the download
        aborts visibly instead of delivering a broken file."""
        return subprocess.Popen(
            [self.binary, "-r", self.repository, "--no-lock", "dump", snapshot_id, path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
        )
