"""Supervises the local `reolink-gateway` sidecar.

`reolink-cli` is a thin client: nearly every command that actually talks to
a camera (`vod search`, `vod download`, `storage status`, `info`) is
proxied through a `reolink-gateway` daemon on loopback that caches the
device session. That daemon is not started by `reolink-cli` on its own;
something has to run `reolink-cli gateway start` once and keep it alive,
which ReoVault does rather than the operator. The daemon is purely a local
implementation detail of talking to `reolink-cli`, and nothing outside this
process should ever need to know it exists. The gateway's browser-facing
`POST /api` HTTP surface and its MCP server are deliberately unused,
separate concerns served by the same binary.
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
        """`gateway status` defaults to JSON output, shaped
        `{"ok": true, "data": {"listening": bool, ...}}` (the bracketed
        `[LISTENING]`/`[DOWN]` text only appears with `--output text`). Any
        parsing failure, including the CLI not being on PATH, is treated as
        "not listening" rather than raised, since this is a best-effort
        probe used to decide whether to spawn a gateway, not a command
        whose own failure should propagate."""
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
        """Idempotent: no-op if already listening (including a gateway
        started by something else, e.g. a previous ReoVault process this
        one doesn't share state with). Otherwise spawns one and waits for
        it to come up."""
        if self.is_listening():
            return

        logger.info("gateway.starting", addr=self.addr)
        # Detached from this process's stdio so a gateway crash doesn't
        # take the daemon's logs down with it; the gateway has its own log
        # file.
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
        """Only stops a gateway this process spawned itself, never a
        pre-existing one, since that might be shared with another tool on
        the host."""
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None
