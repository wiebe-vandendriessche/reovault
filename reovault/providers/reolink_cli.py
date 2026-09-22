"""`CameraProvider` backed by the real `reolink-cli` subprocess.

Recording fields are read through lists of candidate keys rather than one
fixed name, and an unrecognized `vod search` response shape raises
`ProtocolError` (quarantine, never a retry loop) instead of guessing, since
the payload shape isn't guaranteed stable across protocol versions or
camera models.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from reovault.logging import get_logger
from reovault.models import FetchResult, RemoteRecording, StorageStatus
from reovault.providers._cli import run_cli
from reovault.providers.base import CameraProvider, ProtocolError
from reovault.providers.gateway import GatewaySupervisor

logger = get_logger(__name__)

_LOCAL_ISO = "%Y-%m-%dT%H:%M:%S"

# `vod search` field names. Real keys are name/startTime/endTime/fileSize/
# recordType/streamType; the other candidates are kept as fallbacks in case
# a different protocol version or model uses different names, since staying
# tolerant is cheaper than assuming one device generalizes to all.
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
            # Never silently archive a partial listing: the caller's
            # overlap-window design means the next run will pick up the
            # rest, but this run must not claim completeness.
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
        # Fields are nested one level deeper, under `data.items[0]` (an
        # array, presumably to support NVR/multi-disk devices), not flat
        # under `data`. Fall back to flat `data` if `items` is absent, in
        # case a different model/protocol version returns it flat.
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
            "vod search response has no recognizable recording list",
            detail=(f"upstream schema may have changed\nresponse: {data!r}"[:2000]),
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
            # TZ=UTC.
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
        return run_cli(
            self.binary,
            ["--camera", self.alias, *args],
            timeout=timeout,
            gateway=self.gateway,
        )
