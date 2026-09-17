"""The `CameraProvider` boundary. Camera-specific logic (Reolink or otherwise)
must never leak past this interface — see CLAUDE.md: "Keep camera-specific
functionality isolated behind a provider/adapter boundary." The archiver only
ever talks to this ABC, which is what makes it testable without a camera
(`FakeProvider`) and swappable for a different vendor later.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from reovault.models import ErrorClass, FetchResult, RemoteRecording, StorageStatus


class ProviderError(Exception):
    """Base for all provider-raised errors. Always carries an `ErrorClass` so
    the archiver's retry/backoff policy (see plan: Failure classification) never
    has to know which camera vendor raised it."""

    def __init__(self, message: str, error_class: ErrorClass, *, retryable: bool | None = None):
        super().__init__(message)
        self.error_class = error_class
        # network is retryable by default; everything else defaults to "no" unless
        # the caller says otherwise (e.g. a device error being retried twice slowly).
        self.retryable = retryable if retryable is not None else (error_class == ErrorClass.NETWORK)


class InputError(ProviderError):
    def __init__(self, message: str):
        super().__init__(message, ErrorClass.INPUT, retryable=False)


class NetworkError(ProviderError):
    def __init__(self, message: str):
        super().__init__(message, ErrorClass.NETWORK, retryable=True)


class AuthError(ProviderError):
    def __init__(self, message: str):
        super().__init__(message, ErrorClass.AUTH, retryable=False)


class DeviceError(ProviderError):
    def __init__(self, message: str):
        super().__init__(message, ErrorClass.DEVICE, retryable=True)


class ProtocolError(ProviderError):
    """The device answered, but not in a shape we understand. Per plan: never
    retry-loop on this — it means a ReoVault bug or an upstream schema change,
    so it goes straight to quarantine."""

    def __init__(self, message: str):
        super().__init__(message, ErrorClass.PROTOCOL, retryable=False)


class LocalError(ProviderError):
    """Ours, not the camera's: disk full, permissions, or — for
    `ReolinkCliProvider` — the local gateway sidecar being unreachable. Never
    mutates recording state; aborts the run."""

    def __init__(self, message: str):
        super().__init__(message, ErrorClass.LOCAL, retryable=False)


class CameraProvider(ABC):
    """One instance per registered device. `device_alias` never carries a
    password — see plan: Camera credentials."""

    @abstractmethod
    def list_recordings(
        self,
        *,
        from_utc: datetime,
        to_utc: datetime,
    ) -> list[RemoteRecording]:
        """Enumerate recordings in `[from_utc, to_utc]`. Callers are expected to
        widen the window per plan: Time handling — this method does not widen
        it itself, so DST safety is the caller's responsibility, not baked in
        silently here where it would be easy to widen twice."""

    @abstractmethod
    def fetch(self, recording: RemoteRecording, dest_path: str) -> FetchResult:
        """Download `recording` to `dest_path` (a path already on the staging
        filesystem, same device as the vault — see plan: atomic rename). Must
        not overwrite an existing completed download of the same content;
        callers are responsible for staging-path uniqueness per attempt."""

    @abstractmethod
    def storage_status(self) -> StorageStatus:
        """SD card capacity/mount state, for the coverage/lag alarm."""
