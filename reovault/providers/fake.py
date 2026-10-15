"""In-memory `CameraProvider` for tests. Every reliability behavior in the
archiver (retry/backoff, dedup, crash recovery) is exercised through this,
not the real camera. Scriptable to fail in the specific ways `reolink-cli`
is documented to fail: timeouts, size mismatches, truncated downloads,
duplicate names, empty results.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import SecretStr

from reovault.models import (
    DeviceInfo,
    DiscoveredDevice,
    FetchResult,
    RegisteredDevice,
    RemoteRecording,
    StorageStatus,
)
from reovault.providers.base import (
    CameraProvider,
    CameraRegistry,
    DeviceError,
    RecordingGoneError,
)


@dataclass
class ScriptedRecording:
    """A recording the fake camera "has", plus how its download should behave."""

    recording: RemoteRecording
    content: bytes
    # Called once per fetch attempt; return None to succeed normally, or raise
    # to simulate a provider error, or return truncated bytes to simulate a
    # short/corrupt download that the archiver's hash+size check must catch.
    on_fetch: Callable[[], bytes | None] | None = None


@dataclass
class FakeProvider(CameraProvider):
    device_alias: str = "fake"
    recordings: list[ScriptedRecording] = field(default_factory=list)
    storage: StorageStatus = field(
        default_factory=lambda: StorageStatus(
            total_gb=64.0, remain_gb=32.0, formatted=True, mounted=True
        )
    )
    # Raised by list_recordings() instead of returning, once, to simulate a
    # `vod search` failure (auth expired, network drop, ...). Cleared after
    # being raised so a second call in the same test can succeed.
    list_error: Exception | None = None
    fetch_calls: int = 0
    list_calls: int = 0

    def list_recordings(self, *, from_utc: datetime, to_utc: datetime) -> list[RemoteRecording]:
        self.list_calls += 1
        if self.list_error is not None:
            error, self.list_error = self.list_error, None
            raise error
        return [
            sr.recording for sr in self.recordings if from_utc <= sr.recording.start_utc <= to_utc
        ]

    def fetch(self, recording: RemoteRecording, dest_path: str) -> FetchResult:
        self.fetch_calls += 1
        sr = self._find(recording)
        content = sr.on_fetch() if sr.on_fetch else sr.content
        if content is None:
            content = sr.content
        path = Path(dest_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return FetchResult(local_path=str(path), bytes_written=len(content))

    def storage_status(self) -> StorageStatus:
        return self.storage

    def _find(self, recording: RemoteRecording) -> ScriptedRecording:
        for sr in self.recordings:
            if (
                sr.recording.remote_name == recording.remote_name
                and sr.recording.start_utc == recording.start_utc
            ):
                return sr
        raise KeyError(f"no scripted recording for {recording.remote_name!r}")


@dataclass
class FakeRegistry(CameraRegistry):
    """In-memory `CameraRegistry` for tests. Scriptable discovery results,
    an in-memory device list that `add_device` actually appends to (so a
    test can add-then-list and see it), and a spy on every password ever
    passed in, so the password-never-logged test can assert on what this
    fake *received* without needing a real `reolink-cli` binary."""

    discovered: list[DiscoveredDevice] = field(default_factory=list)
    devices: list[RegisteredDevice] = field(default_factory=list)
    device_infos: dict[str, DeviceInfo] = field(default_factory=dict)
    add_device_error: Exception | None = None
    # Every password this fake was ever handed, as SecretStr (never a plain
    # str) -- a test asserts against `.get_secret_value()` explicitly,
    # which is the point: accessing it must always be a deliberate act.
    received_passwords: list[SecretStr] = field(default_factory=list, repr=False)

    def discover(self, *, timeout_secs: int = 2) -> list[DiscoveredDevice]:
        return list(self.discovered)

    def list_devices(self) -> list[RegisteredDevice]:
        return list(self.devices)

    def add_device(
        self,
        *,
        alias: str,
        host: str,
        user: str,
        password: SecretStr,
        channel: int | None = None,
    ) -> None:
        self.received_passwords.append(password)
        if self.add_device_error is not None:
            error, self.add_device_error = self.add_device_error, None
            raise error
        self.devices.append(
            RegisteredDevice(
                alias=alias,
                host=host,
                user=user,
                has_password=True,
                channel=channel,
                description=None,
                disabled=False,
            )
        )

    def device_info(self, *, alias: str) -> DeviceInfo:
        if alias not in self.device_infos:
            raise DeviceError(f"no scripted device_info for {alias!r}")
        return self.device_infos[alias]


@dataclass
class DemoProvider(CameraProvider):
    """A pretend camera for trying the dashboard with more than one device
    (`provider = "fake"` in a `[[devices]]` entry). Its "SD card" is a
    sliding window over the wall clock: one small clip every `every`, the
    last `window` of them on the card, so new clips appear and old ones
    roll off like a real looping card. Every `gone_every`-th clip raises
    `RecordingGoneError`, so the lost/at-risk states have something to show.
    Clips are tiny so the vault doesn't fill with junk."""

    every: timedelta = timedelta(minutes=15)
    window: timedelta = timedelta(days=7)
    gone_every: int = 97
    clip_bytes: int = 4096
    storage: StorageStatus = field(
        default_factory=lambda: StorageStatus(
            total_gb=64.0, remain_gb=1.5, formatted=True, mounted=True
        )
    )
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    _TYPES = ("md", "people", "vehicle")

    def _slot(self, i: int) -> RemoteRecording:
        start = datetime.fromtimestamp(i * self.every.total_seconds(), UTC)
        return RemoteRecording(
            remote_name=f"demo_{start:%Y%m%d_%H%M%S}.mp4",
            start_utc=start,
            end_utc=start + timedelta(seconds=30),
            duration_s=30.0,
            rec_type=self._TYPES[i % len(self._TYPES)],
            stream="main",
            remote_size=self.clip_bytes,
            raw_metadata={},
        )

    def list_recordings(self, *, from_utc: datetime, to_utc: datetime) -> list[RemoteRecording]:
        step = self.every.total_seconds()
        oldest = self.now() - self.window
        lo = math.ceil(max(from_utc, oldest).timestamp() / step)
        hi = math.floor(min(to_utc, self.now()).timestamp() / step)
        return [self._slot(i) for i in range(lo, hi + 1)]

    def fetch(self, recording: RemoteRecording, dest_path: str) -> FetchResult:
        i = round(recording.start_utc.timestamp() / self.every.total_seconds())
        if i % self.gone_every == 0 or recording.start_utc < self.now() - self.window:
            raise RecordingGoneError(f"{recording.remote_name} is no longer on the demo card")
        content = recording.remote_name.encode().ljust(self.clip_bytes, b"\0")
        path = Path(dest_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return FetchResult(local_path=str(path), bytes_written=len(content))

    def storage_status(self) -> StorageStatus:
        return self.storage
