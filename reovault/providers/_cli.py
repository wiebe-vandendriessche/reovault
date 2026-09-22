"""Shared `reolink-cli` subprocess invocation: JSON envelope parsing and
exit-code-to-error mapping, used by both `ReolinkCliProvider` and
`ReolinkCliRegistry` so the two stay in sync on how a `reolink-cli` call
turns into a `ProviderError`.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from typing import Any

from reovault.providers.base import (
    AuthError,
    DeviceError,
    InputError,
    LocalError,
    NetworkError,
    ProtocolError,
    ProviderError,
    describe_exit_code,
)
from reovault.providers.gateway import GatewaySupervisor

_EXIT_CODE_ERRORS: dict[int, Callable[..., ProviderError]] = {
    1: InputError,
    2: NetworkError,
    3: AuthError,
    4: DeviceError,
    5: ProtocolError,
}


def run_cli(
    binary: str,
    args: list[str],
    *,
    timeout: int,
    gateway: GatewaySupervisor | None = None,
    stdin_line: str | None = None,
) -> dict[str, Any]:
    """Run `binary --output json <args>`, adding `--gateway-addr` (and
    calling `gateway.ensure_running()` first) when `gateway` is given.
    `stdin_line`, when given, is piped on stdin rather than argv, since
    argv is world-visible via `ps` (used for passwords).

    Returns the parsed JSON envelope on success. Raises the `ProviderError`
    subclass matching the exit code on failure (unmapped codes fall back to
    `DeviceError`), a process-not-found on missing `binary`, and a timeout
    as `NetworkError`.
    """
    cmd = [binary]
    if gateway is not None:
        gateway.ensure_running()
        cmd += ["--gateway-addr", gateway.addr]
    cmd += ["--output", "json", *args]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, input=stdin_line
        )
    except FileNotFoundError as exc:
        raise LocalError(f"{binary!r} not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise NetworkError(
            f"reolink-cli {args[0] if args else ''} timed out after {timeout}s",
            detail=f"command: {' '.join(cmd)}",
        ) from exc

    stdout = result.stdout.strip()
    try:
        envelope = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError as exc:
        raise ProtocolError(
            "reolink-cli returned output that isn't valid JSON",
            detail=f"command: {' '.join(cmd)}\nstdout: {stdout[:2000]!r}",
        ) from exc

    if result.returncode == 0 and envelope.get("ok", True):
        return envelope

    error = envelope.get("error") or {}
    message = error.get("message") or result.stderr.strip() or describe_exit_code(result.returncode)
    error_cls = _EXIT_CODE_ERRORS.get(result.returncode, DeviceError)
    detail_lines = [f"exit code: {result.returncode}"]
    if error.get("code"):
        detail_lines.append(f"reolink-cli error code: {error['code']}")
    if result.stderr.strip():
        detail_lines.append(f"stderr: {result.stderr.strip()}")
    raise error_cls(message, detail="\n".join(detail_lines))
