"""Domain dataclasses. These are the provider/archiver-facing shapes — distinct
from `db.repository` rows (which additionally carry archive-state bookkeeping) and
from a provider's raw JSON (which is preserved verbatim in `raw_metadata`, see
plan: Data model)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class RecordingState(StrEnum):
    DISCOVERED = "discovered"
    DOWNLOADING = "downloading"
    VERIFYING = "verifying"
    ARCHIVED = "archived"
    FAILED = "failed"
    QUARANTINED = "quarantined"


class ErrorClass(StrEnum):
    """Mirrors `reolink-cli`'s exit-code classes (see plan: Failure classification),
    plus `LOCAL` for failures that are ours (disk full, permissions, crypto,
    the gateway sidecar itself being down)."""

    INPUT = "input"
    NETWORK = "network"
    AUTH = "auth"
    DEVICE = "device"
    PROTOCOL = "protocol"
    LOCAL = "local"


@dataclass(frozen=True, slots=True)
class RemoteRecording:
    """One entry from a provider's `list_recordings()`. Timestamps are always
    UTC by the time they leave the provider boundary (see plan: Time handling —
    the provider converts the device's naive local ISO time using the device's
    configured IANA timezone)."""

    remote_name: str
    start_utc: datetime
    end_utc: datetime | None
    duration_s: float | None
    rec_type: str | None
    stream: str | None
    remote_size: int | None
    raw_metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StorageStatus:
    total_gb: float | None
    remain_gb: float | None
    formatted: bool | None
    mounted: bool | None


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Result of `CameraProvider.fetch()`: the recording landed at `local_path`,
    exactly `bytes_written` bytes, on the local filesystem. The caller (Archiver)
    is responsible for hashing/verifying/encrypting it — the provider's job ends
    at "the plaintext MP4 is on disk"."""

    local_path: str
    bytes_written: int


@dataclass(slots=True)
class ArchiveRunResult:
    trigger: str
    discovered: int = 0
    downloaded: int = 0
    skipped_dup: int = 0
    failed: int = 0
    bytes_archived: int = 0
    error: str | None = None
    errors: list[str] = field(default_factory=list)
