"""Supervises the local `reolink-gateway` sidecar.

Corrected 2026-09-17 (see docs/IMPLEMENTATION_PLAN.md): `reolink-cli` is a thin
client. Nearly every command that actually talks to a camera (`vod search`,
`vod download`, `storage status`, `info`) is proxied through a `reolink-gateway`
daemon on loopback that caches the device session. That daemon is *not* started
by `reolink-cli` on its own; something has to run `reolink-cli gateway start`
once and keep it alive. In ReoVault that's us, not the operator. The daemon is
purely a local implementation detail of talking to `reolink-cli`, and nothing
outside this process should ever need to know it exists.

We deliberately never use the gateway's browser-facing `POST /api` HTTP surface
or the MCP server. Those are separate, unused concerns served by the same
binary.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass

from reovault.logging import get_logger
from reovault.providers.base import LocalError

logger = get_logger(__name__)


@dataclass
class GatewaySupervisor:
    binary: str
    addr: str = "127.0.0.1:9000"
    start_timeout_secs: float = 15.0
    poll_interval_secs: float = 0.5

    _process: subprocess.Popen[bytes] | None = None

    def is_listening(self) -> bool:
        """Verified 2026-09-17 against the real binary: `gateway status`
        defaults to JSON (not the `[LISTENING]`/`[DOWN]` bracketed text the
        skill docs show, that's `--output text` specifically), shaped
        `{"ok": true, "data": {"listening": bool, ...}}`. Any parsing
        failure (including the CLI genuinely not being on PATH) is treated
        as "not listening" rather than raised, since this is a best-effort
        probe used to decide whether to spawn a gateway, not a command whose
        own failure should propagate."""
        try:
            result = subprocess.run(
                [self.binary, "--gateway-addr", self.addr, "gateway", "status"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            envelope = json.loads(result.stdout)
            return bool(envelope.get("data", {}).get("listening", False))
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, AttributeError):
            return False

    def ensure_running(self) -> None:
        """Idempotent: no-op if already listening (including a gateway started
        by something else, e.g. a previous ReoVault process we don't share
        state with). Otherwise spawns our own and waits for it to come up."""
        if self.is_listening():
            return

        logger.info("gateway.starting", addr=self.addr)
        # Detached from our stdio so a gateway crash doesn't take the daemon's
        # logs down with it; the gateway has its own log file (see plan).
        self._process = subprocess.Popen(
            [self.binary, "gateway", "start", "--addr", self.addr],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        deadline = time.monotonic() + self.start_timeout_secs
        while time.monotonic() < deadline:
            if self.is_listening():
                logger.info("gateway.ready", addr=self.addr)
                return
            if self._process.poll() is not None:
                raise LocalError(
                    f"reolink-cli gateway process exited early (code {self._process.returncode})"
                )
            time.sleep(self.poll_interval_secs)

        raise LocalError(f"gateway did not report listening within {self.start_timeout_secs}s")

    def stop(self) -> None:
        """Only stops a gateway we ourselves spawned, never a pre-existing one,
        since that might be shared with another tool on the host."""
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None
