"""The `CameraProvider` boundary. Camera-specific logic (Reolink or otherwise)
must never leak past this interface. See CLAUDE.md: "Keep camera-specific
functionality isolated behind a provider/adapter boundary." The archiver only
ever talks to this ABC, which is what makes it testable without a camera
(`FakeProvider`) and swappable for a different vendor later.
"""

from __future__ import annotations

import signal
from abc import ABC, abstractmethod
from datetime import datetime

from pydantic import SecretStr

from reovault.models import (
    DeviceInfo,
    DiscoveredDevice,
    ErrorClass,
    FetchResult,
    RegisteredDevice,
    RemoteRecording,
    StorageStatus,
)


class ProviderError(Exception):
    """Base for all provider-raised errors. Always carries an `ErrorClass` so
    the archiver's retry/backoff policy never has to know which camera vendor
    raised it. `detail` is the raw, verbose form (stderr, the real subprocess
    exit code, reolink-cli's own `error.code`) for the Problems tab's
    collapsible; `message` itself stays a short plain-English summary that
    never repeats the device alias."""

    def __init__(
        self,
        message: str,
        error_class: ErrorClass,
        *,
        retryable: bool | None = None,
        detail: str | None = None,
    ):
        super().__init__(message)
        self.error_class = error_class
        # network is retryable by default; everything else defaults to "no" unless
        # the caller says otherwise (e.g. a device error being retried twice slowly).
        self.retryable = retryable if retryable is not None else (error_class == ErrorClass.NETWORK)
        self.detail = detail


class InputError(ProviderError):
    def __init__(self, message: str, *, detail: str | None = None):
        super().__init__(message, ErrorClass.INPUT, retryable=False, detail=detail)


class NetworkError(ProviderError):
    def __init__(self, message: str, *, detail: str | None = None):
        super().__init__(message, ErrorClass.NETWORK, retryable=True, detail=detail)


class AuthError(ProviderError):
    def __init__(self, message: str, *, detail: str | None = None):
        super().__init__(message, ErrorClass.AUTH, retryable=False, detail=detail)


class DeviceError(ProviderError):
    def __init__(self, message: str, *, detail: str | None = None):
        super().__init__(message, ErrorClass.DEVICE, retryable=True, detail=detail)


class ProtocolError(ProviderError):
    """The device answered, but not in a recognized shape. Never retry-loop
    on this: it means a ReoVault bug or an upstream schema change, so it
    goes straight to quarantine."""

    def __init__(self, message: str, *, detail: str | None = None):
        super().__init__(message, ErrorClass.PROTOCOL, retryable=False, detail=detail)


class LocalError(ProviderError):
    """Ours, not the camera's: disk full, permissions, or, for
    `ReolinkCliProvider`, the local gateway sidecar being unreachable. Never
    mutates recording state; aborts the run."""

    def __init__(self, message: str, *, detail: str | None = None):
        super().__init__(message, ErrorClass.LOCAL, retryable=False, detail=detail)


def describe_exit_code(code: int) -> str:
    """The fallback error message when `reolink-cli` gives neither a JSON
    `error.message` nor anything on stderr. A negative code is Python's
    `subprocess.returncode` convention for a process killed by a signal
    (`-N` for signal N); named here as a normal, expected consequence of a
    process restart interrupting a `reolink-cli` call, not a device
    malfunction, since a bare "exit code -2" is unreadable to anyone who
    doesn't already know that convention."""
    if code < 0:
        sig = -code
        try:
            name = signal.Signals(sig).name
        except ValueError:
            name = f"signal {sig}"
        return f"reolink-cli was killed by {name} ({sig}), most likely by a process restart"
    return f"reolink-cli exited with code {code}"


class CameraProvider(ABC):
    """One instance per already-registered device. `device_alias` never
    carries a password, and nothing in this class ever touches one. Adding
    a device in the first place is a different boundary with a narrower
    invariant of its own; see `CameraRegistry` below."""

    @abstractmethod
    def list_recordings(
        self,
        *,
        from_utc: datetime,
        to_utc: datetime,
    ) -> list[RemoteRecording]:
        """Enumerate recordings in `[from_utc, to_utc]`. Callers are expected
        to widen the window themselves; this method does not widen it, so
        DST safety is the caller's responsibility, not baked in silently
        here where it would be easy to widen twice."""

    @abstractmethod
    def fetch(self, recording: RemoteRecording, dest_path: str) -> FetchResult:
        """Download `recording` to `dest_path` (a path already on the
        staging filesystem, same device as the vault, for an atomic rename
        into it). Must not overwrite an existing completed download of the
        same content; callers are responsible for staging-path uniqueness
        per attempt."""

    @abstractmethod
    def storage_status(self) -> StorageStatus:
        """SD card capacity/mount state, for the coverage/lag alarm."""


class CameraRegistry(ABC):
    """Discovering and registering cameras. Device-less, unlike
    `CameraProvider`: a device can't have a provider before it's been
    added, so adding one needs a separate boundary. ReoVault never
    *persists*, *logs*, *echoes*, or places on a command line the camera
    password; its only durable home is `reolink-cli`'s own encrypted
    `aliases.toml`, and `add_device`'s `password` is `SecretStr`
    specifically so an accidental `repr()` anywhere -- including a
    structlog kwarg -- renders `**********`. `discover`/`list_devices`/
    `add_device` never require the gateway; only `device_info` does,
    since it's the one call that actually talks to the camera rather than
    just reading/writing local config."""

    @abstractmethod
    def discover(self, *, timeout_secs: int = 2) -> list[DiscoveredDevice]:
        """Broadcasts a probe and returns whoever answers. A sleeping
        battery camera or one paired to a Home Hub/NVR normally won't
        appear here; neither is a fault in the scan."""

    @abstractmethod
    def list_devices(self) -> list[RegisteredDevice]:
        """Every camera already in the registry, informational only."""

    @abstractmethod
    def add_device(
        self,
        *,
        alias: str,
        host: str,
        user: str,
        password: SecretStr,
        channel: int | None = None,
    ) -> None:
        """Registers a new camera. Does not validate the credential (adding
        does not itself contact the camera), and never rolls back on a
        later failed reachability probe: a sleeping battery camera and a
        Home Hub child are both documented normal non-responders."""

    @abstractmethod
    def set_password(self, *, alias: str, password: SecretStr) -> None:
        """Rotates an existing device's stored credential. Deliberately not
        wired to any web route yet: the device-edit form never carries a
        password field, precisely so "edit device metadata" can never
        double as a way to leak or replace a credential by accident.
        Credential rotation is its own explicit, separate flow."""

    @abstractmethod
    def device_info(self, *, alias: str) -> DeviceInfo:
        """Model/firmware for an already-registered, reachable device. The
        one call in this ABC that needs the gateway."""
