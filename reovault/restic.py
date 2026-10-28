"""restic, one repository per camera at `<base>/<alias>`.

Verified with restic 0.19.1 (2026-10-28): exit 10 = repository missing,
12 = wrong password; `--host` is a per-command flag (backup, forget,
snapshots), not a global one; backing up the same fixed directory every time
keeps one `forget` group, so `--keep-within` works across batch snapshots.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from reovault.tools import ToolError, describe_exit, run

HOST = "reovault"  # fixed: Docker gives a recreated container a new hostname
MISSING, WRONG_PASSWORD = 10, 12

RepoState = Literal["ok", "missing", "wrong_password"]


@dataclass(frozen=True, slots=True)
class Restic:
    binary: str
    repository: str  # this camera's own repository, `<base>/<alias>`
    env: dict[str, str]

    @classmethod
    def for_camera(cls, binary: str, base: str, alias: str, env: dict[str, str]) -> Restic:
        return cls(binary, f"{base.rstrip('/')}/{alias}", {**os.environ, **env})

    def _run(self, *args: str, timeout: float = 600) -> subprocess.CompletedProcess[str]:
        return run([self.binary, "-r", self.repository, *args], timeout=timeout, env=self.env)

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

    def init(self) -> None:
        proc = self._run("init", timeout=300)
        if proc.returncode != 0:
            raise self._fail("init", proc)

    def unlock(self) -> None:
        """Removes stale locks only (a process that died holding one)."""
        self._run("unlock", timeout=120)

    def backup(self, path: Path, timeout: float) -> dict[str, Any]:
        """Back up `path`. Success means exit 0 *and* a snapshot id: exit 3
        (some file unreadable) is a failure for the whole batch."""
        proc = self._run("backup", "--host", HOST, "--json", str(path), timeout=timeout)
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

    def snapshot_ids(self) -> set[str]:
        proc = self._run("snapshots", "--host", HOST, "--json", timeout=300)
        if proc.returncode != 0:
            raise self._fail("snapshots", proc)
        return {s["id"] for s in json.loads(proc.stdout or "[]")}

    def forget(self, keep_days: int) -> None:
        # `--prune` mixes JSON and text output, so only the exit code counts.
        proc = self._run(
            "forget",
            "--host",
            HOST,
            "--group-by",
            "host",
            "--keep-within",
            f"{keep_days}d",
            "--prune",
            timeout=6 * 3600,
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
