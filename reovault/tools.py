"""Running the pinned external tools (reolink-cli, restic, rclone)."""

from __future__ import annotations

import signal
import subprocess
from collections.abc import Mapping, Sequence

INTERRUPT_GRACE_SECS = 30  # after a timeout's SIGINT, before SIGKILL


class ToolError(Exception):
    """A tool failed. `message` is a short plain sentence for the dashboard
    and alerts; `detail` is the raw form (exit code, stderr) for logs."""

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        detail: str | None = None,
        unreachable: bool = False,
        timed_out: bool = False,
    ):
        super().__init__(message)
        self.code = code
        self.detail = detail
        # The camera (or the network to it) didn't answer: it isn't a problem
        # with any clip, and the dashboard says "offline", not "failing".
        self.unreachable = unreachable
        self.timed_out = timed_out


def describe_exit(tool: str, code: int) -> str:
    """Fallback message when a tool printed nothing useful. A negative code
    is a signal (subprocess convention), usually our own restart."""
    if code < 0:
        try:
            name = signal.Signals(-code).name
        except ValueError:
            name = f"signal {-code}"
        return f"{tool} was killed by {name}, most likely by a restart"
    return f"{tool} exited with code {code}"


def run(
    cmd: Sequence[str],
    *,
    timeout: float,
    env: Mapping[str, str] | None = None,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run `cmd` to completion. A missing binary or a timeout raises
    `ToolError`; a non-zero exit is the caller's to interpret.

    On timeout the tool is interrupted first and killed only if it ignores
    that: restic removes its repository lock on SIGINT, while a killed restic
    leaves the lock behind for every later command to trip over."""
    try:
        proc = subprocess.Popen(
            list(cmd),
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
    except FileNotFoundError as exc:
        raise ToolError(f"{cmd[0]} is not installed or not on PATH") from exc
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        proc.send_signal(signal.SIGINT)
        try:
            proc.communicate(timeout=INTERRUPT_GRACE_SECS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
        raise ToolError(
            f"{cmd[0]} {cmd[1] if len(cmd) > 1 else ''} timed out after {timeout:.0f}s".strip(),
            detail=" ".join(cmd),
            timed_out=True,
        ) from exc
    except BaseException:
        proc.kill()
        proc.communicate()
        raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def assert_version(version_cmd: Sequence[str], version: str) -> None:
    """Fail at startup when a tool isn't the version this release was
    verified against, instead of misbehaving subtly later. `version_cmd` is
    e.g. `["restic", "version"]` or `["reolink-cli", "--version"]`."""
    try:
        out = run(version_cmd, timeout=10).stdout.strip()
    except ToolError as exc:
        raise RuntimeError(str(exc)) from exc
    if version not in out:
        first = out.splitlines()[0] if out else "nothing"
        raise RuntimeError(f"{version_cmd[0]} reports {first!r}, expected version {version}")
