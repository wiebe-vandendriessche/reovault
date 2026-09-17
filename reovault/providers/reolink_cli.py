"""`CameraProvider` backed by the real `reolink-cli` subprocess.

Everything here that is NOT explicitly cited as verified against
`skills/reolink-cli/` upstream docs (2026-09-17) should be treated as
best-effort pending real fixture capture on the homelab (see plan: Phase 1,
"Capture real fixtures from the doorbell here"). The `vod search` payload shape
in particular is UNVERIFIED, see plan's Risks section, so this parser is
deliberately tolerant and raises `ProtocolError` (quarantine, never a retry
loop) rather than guessing when a recording's shape doesn't match any known
key, exactly as CLAUDE.md requires: distinguish verified behavior from
reverse-engineered behavior, and never assume schema from memory.

Verified against upstream docs, used directly below:
- Exit code classes 0/1/2/3/4/5 -> ok/input/network/auth/device/protocol.
- `storage status --output json` -> `data.totalGB/remainGB/formatted/mounted`.
- `vod search [--from ISO --to ISO] [--limit 0] --output json` -> naive local
  ISO timestamps, `truncated`/`scanned` in the response.
- `vod download NAME -o FILE` for by-name download (resolves the plan's open
  question in favor of `-o`, not `--file/--directory`).
- The gateway sidecar must be running for all of the above (see gateway.py).
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from reovault.logging import get_logger
from reovault.models import FetchResult, RemoteRecording, StorageStatus
from reovault.providers.base import (
    AuthError,
    CameraProvider,
    DeviceError,
    InputError,
    LocalError,
    NetworkError,
    ProtocolError,
)
from reovault.providers.gateway import GatewaySupervisor

logger = get_logger(__name__)

_LOCAL_ISO = "%Y-%m-%dT%H:%M:%S"

_EXIT_CODE_ERRORS: dict[int, type[Exception]] = {
    1: InputError,
    2: NetworkError,
    3: AuthError,
    4: DeviceError,
    5: ProtocolError,
}

# `vod search` field names: verified 2026-09-17 against a real D340W doorbell
# (v3.0.0.6460, protocol v20). Real keys are name/startTime/endTime/fileSize/
# recordType/streamType; the other candidates are kept as fallbacks in case a
# different protocol version or model uses different names (never observed,
# but cheaper to keep tolerant than to assume one device generalizes to all).
_NAME_KEYS = ("name", "fileName", "remoteName")
_START_KEYS = ("startTime", "start", "beginTime")
_END_KEYS = ("endTime", "end")
_SIZE_KEYS = ("fileSize", "size")
_TYPE_KEYS = ("recordType", "type", "recType")
_STREAM_KEYS = ("streamType", "stream")


def _first(d: dict[str, object], keys: tuple[str, ...]) -> object | None:
    for k in keys:
        if k in d:
            return d[k]
    return None


def _first_str(d: dict[str, object], keys: tuple[str, ...]) -> str | None:
    value = _first(d, keys)
    return value if isinstance(value, str) else None


class ReolinkCliProvider(CameraProvider):
    def __init__(
        self,
        *,
        alias: str,
        timezone: str,
        binary: str,
        gateway: GatewaySupervisor,
        search_timeout_secs: int = 30,
        download_timeout_secs: int = 300,
    ):
        self.alias = alias
        self.tz = ZoneInfo(timezone)
        self.binary = binary
        self.gateway = gateway
        self.search_timeout_secs = search_timeout_secs
        self.download_timeout_secs = download_timeout_secs

    # -- CameraProvider ------------------------------------------------------

    def list_recordings(self, *, from_utc: datetime, to_utc: datetime) -> list[RemoteRecording]:
        local_from = from_utc.astimezone(self.tz).strftime(_LOCAL_ISO)
        local_to = to_utc.astimezone(self.tz).strftime(_LOCAL_ISO)
        envelope = self._run(
            ["vod", "search", "--from", local_from, "--to", local_to, "--limit", "0"],
            timeout=self.search_timeout_secs,
        )
        data = envelope.get("data", {})
        if isinstance(data, dict) and data.get("truncated"):
            # Per plan: never silently archive a partial listing. The caller's
            # overlap-window design means the next run will pick up the rest,
            # but this run must not claim completeness.
            logger.error(
                "vod_search.truncated",
                alias=self.alias,
                scanned=data.get("scanned"),
                window=(local_from, local_to),
            )
        items = self._extract_items(data)
        return [self._parse_recording(item) for item in items]

    def fetch(self, recording: RemoteRecording, dest_path: str) -> FetchResult:
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        self._run(
            ["vod", "download", recording.remote_name, "-o", dest_path],
            timeout=self.download_timeout_secs,
        )
        size = Path(dest_path).stat().st_size
        return FetchResult(local_path=dest_path, bytes_written=size)

    def storage_status(self) -> StorageStatus:
        envelope = self._run(["storage", "status"], timeout=self.search_timeout_secs)
        data = envelope.get("data", {})
        # Verified 2026-09-17 against a real D340W: fields are nested one
        # level deeper than the plan originally assumed, under `data.items[0]`
        # (an array, presumably to support NVR/multi-disk devices), not flat
        # under `data`. Fall back to flat `data` if `items` is absent, in
        # case a different model/protocol version really does return it flat.
        items = data.get("items")
        disk = items[0] if isinstance(items, list) and items else data
        return StorageStatus(
            total_gb=disk.get("totalGB"),
            remain_gb=disk.get("remainGB"),
            formatted=disk.get("formatted"),
            mounted=disk.get("mounted"),
        )

    # -- internals -------------------------------------------------------

    def _extract_items(self, data: object) -> list[dict[str, object]]:
        if isinstance(data, list):
            return [i for i in data if isinstance(i, dict)]
        if isinstance(data, dict):
            for key in ("files", "recordings", "items", "list"):
                value = data.get(key)
                if isinstance(value, list):
                    return [i for i in value if isinstance(i, dict)]
        raise ProtocolError(
            f"vod search response for {self.alias!r} has no recognizable recording list; "
            "upstream schema may have changed, see plan's Unverified section"
        )

    def _parse_recording(self, item: dict[str, object]) -> RemoteRecording:
        name = _first(item, _NAME_KEYS)
        start = _first(item, _START_KEYS)
        if not isinstance(name, str) or not isinstance(start, str):
            raise ProtocolError(
                f"vod search item missing a recognizable name/start field: {item!r}"
            )
        start_local = datetime.strptime(start, _LOCAL_ISO).replace(tzinfo=self.tz)
        end_raw = _first(item, _END_KEYS)
        end_utc = None
        if isinstance(end_raw, str):
            # Explicit UTC, not astimezone(tz=None): that converts to the
            # *process's* system timezone, not UTC, and would silently
            # produce wrong values anywhere this doesn't happen to run with
            # TZ=UTC (see plan: Time handling, "store UTC only").
            end_utc = datetime.strptime(end_raw, _LOCAL_ISO).replace(tzinfo=self.tz).astimezone(UTC)
        size = _first(item, _SIZE_KEYS)
        return RemoteRecording(
            remote_name=name,
            start_utc=start_local.astimezone(UTC),
            end_utc=end_utc,
            duration_s=None,
            rec_type=_first_str(item, _TYPE_KEYS),
            stream=_first_str(item, _STREAM_KEYS),
            remote_size=size if isinstance(size, int) else None,
            raw_metadata=item,
        )

    def _run(self, args: list[str], *, timeout: int) -> dict[str, Any]:
        self.gateway.ensure_running()
        cmd = [
            self.binary,
            "--camera",
            self.alias,
            "--gateway-addr",
            self.gateway.addr,
            "--output",
            "json",
            *args,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as exc:
            raise LocalError(f"{self.binary!r} not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise NetworkError(f"{' '.join(cmd)} timed out after {timeout}s") from exc

        stdout = result.stdout.strip()
        try:
            envelope = json.loads(stdout) if stdout else {}
        except json.JSONDecodeError as exc:
            raise ProtocolError(f"non-JSON output from {' '.join(cmd)}: {stdout[:200]!r}") from exc

        if result.returncode == 0 and envelope.get("ok", True):
            return envelope

        error = envelope.get("error", {})
        message = error.get("message") or result.stderr.strip() or f"exit code {result.returncode}"
        error_cls = _EXIT_CODE_ERRORS.get(result.returncode, DeviceError)
        raise error_cls(message)
