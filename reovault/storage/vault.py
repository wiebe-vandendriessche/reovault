"""`VaultStore` boundary and `EncryptedFsVault`: staging, verify, atomic
finalize. See plan: Vault layout and Reliability (state machine steps 5-8).

Layout on disk, relative to `vault_dir`:

    <device-alias>/<YYYY>/<MM>/<DD>/<start>_<remote-name>.enc
                                    <start>_<remote-name>.enc.json

The `.json` sidecar is redundant with the database on purpose: the vault
stays self-describing even if the database is ever lost, which is exactly
the scenario Pluton restores into (see plan: Encryption > Format).

This module only handles the plaintext-in, ciphertext-on-disk half of the
state machine (steps 5-8: encrypt, fsync, atomic rename, then the sidecar).
Steps 1-4 (discover, transition to downloading, `vod download`, verify size
against `remote_size`) belong to the archiver (Phase 4, not yet built) and
the provider layer.
"""

from __future__ import annotations

import hashlib
import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from reovault.crypto.envelope import (
    DEFAULT_FRAME_SIZE,
    FORMAT_VERSION,
    decrypt_stream,
    encrypt_stream,
    read_range,
)


class VaultError(Exception):
    """Base for vault-layer problems, distinct from crypto-layer ones
    (`cryptography.exceptions.InvalidTag` and `EnvelopeFormatError` are left
    to propagate as-is, they already say exactly what went wrong)."""


class SidecarMissingError(VaultError):
    """A `.enc` file exists with no `.enc.json` beside it. Per plan:
    reconciliation should treat this the way it treats any orphaned file,
    not something `verify()` silently tolerates."""


@dataclass(frozen=True, slots=True)
class PutResult:
    vault_path: str  # relative, POSIX-style
    plaintext_sha256: str
    plaintext_size: int
    ciphertext_size: int


class VaultStore(ABC):
    """One instance per vault directory. Mirrors the plan's architecture
    diagram: `put/get/verify/delete/open_range`."""

    @abstractmethod
    def put(
        self,
        staging_path: Path,
        *,
        device_alias: str,
        start_utc: datetime,
        remote_name: str,
    ) -> PutResult:
        """Encrypt `staging_path` into the vault, atomically. Never called
        before the caller has verified `staging_path`'s size against the
        provider's `remote_size` (see plan step 4); this method's own
        contribution to the invariant is steps 5-8."""

    @abstractmethod
    def get(self, vault_path: str, dest_path: Path) -> None:
        """Decrypt the full file at `vault_path` to `dest_path` (used by
        `reovault export` and ad-hoc recovery)."""

    @abstractmethod
    def verify(self, vault_path: str) -> bool:
        """Re-decrypt and re-hash, comparing against the sidecar's recorded
        `plaintext_sha256`. False on any failure (hash mismatch, missing
        sidecar, or a failed GCM tag, i.e. bit rot or tampering), never an
        exception: this is a health check, not a parser."""

    @abstractmethod
    def delete(self, vault_path: str) -> None:
        """Remove an unreferenced vault file plus its sidecar. Only ever
        used by reconciliation on files no DB row points to (see plan:
        Reliability), never to delete something the camera still has."""

    @abstractmethod
    def open_range(self, vault_path: str, offset: int, length: int) -> bytes:
        """Decrypt only the plaintext bytes `[offset, offset + length)`. Used
        by the dashboard's `Range` playback (Phase 6)."""


class _HashingReader:
    """Wraps a binary file so `encrypt_stream`'s sequential `.read()` calls
    also accumulate a SHA-256 and byte count, in the same single pass. Avoids
    reading the staged plaintext twice just to hash it separately."""

    def __init__(self, inner: BinaryIO):
        self._inner = inner
        self.sha256 = hashlib.sha256()
        self.bytes_read = 0

    def read(self, n: int = -1) -> bytes:
        chunk = self._inner.read(n)
        self.sha256.update(chunk)
        self.bytes_read += len(chunk)
        return chunk


class _HashingSink:
    """A write-only sink that only hashes, used by `verify()` so re-decryption
    never touches disk for a file we're just checking."""

    def __init__(self) -> None:
        self.sha256 = hashlib.sha256()

    def write(self, data: bytes) -> int:
        self.sha256.update(data)
        return len(data)


class EncryptedFsVault(VaultStore):
    def __init__(self, vault_dir: Path, master_key: bytes, *, frame_size: int = DEFAULT_FRAME_SIZE):
        self.vault_dir = Path(vault_dir)
        self.master_key = master_key
        self.frame_size = frame_size

    def put(
        self,
        staging_path: Path,
        *,
        device_alias: str,
        start_utc: datetime,
        remote_name: str,
    ) -> PutResult:
        final_path = self.vault_dir / self._relative_path(device_alias, start_utc, remote_name)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = final_path.with_name(final_path.name + ".tmp")

        with staging_path.open("rb") as raw_src, tmp_path.open("wb") as dst:
            hashing_src = _HashingReader(raw_src)
            encrypt_stream(hashing_src, dst, self.master_key, frame_size=self.frame_size)
            dst.flush()
            os.fsync(dst.fileno())

        ciphertext_size = tmp_path.stat().st_size
        _fsync_dir(final_path.parent)  # the .tmp file's existence is now durable
        os.replace(tmp_path, final_path)
        _fsync_dir(final_path.parent)  # the rename itself is now durable

        result = PutResult(
            vault_path=str(final_path.relative_to(self.vault_dir).as_posix()),
            plaintext_sha256=hashing_src.sha256.hexdigest(),
            plaintext_size=hashing_src.bytes_read,
            ciphertext_size=ciphertext_size,
        )
        self._write_sidecar(final_path, result)
        return result

    def get(self, vault_path: str, dest_path: Path) -> None:
        final_path = self.vault_dir / vault_path
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        with final_path.open("rb") as src, dest_path.open("wb") as dst:
            decrypt_stream(src, dst, self.master_key)

    def verify(self, vault_path: str) -> bool:
        from cryptography.exceptions import InvalidTag

        final_path = self.vault_dir / vault_path
        try:
            sidecar = self._read_sidecar(final_path)
            sink = _HashingSink()
            with final_path.open("rb") as src:
                decrypt_stream(src, sink, self.master_key)
            return bool(sink.sha256.hexdigest() == sidecar["plaintext_sha256"])
        except (InvalidTag, OSError, KeyError, ValueError, VaultError):
            return False

    def delete(self, vault_path: str) -> None:
        final_path = self.vault_dir / vault_path
        final_path.unlink(missing_ok=True)
        self._sidecar_path(final_path).unlink(missing_ok=True)

    def open_range(self, vault_path: str, offset: int, length: int) -> bytes:
        final_path = self.vault_dir / vault_path
        with final_path.open("rb") as src:
            return read_range(src, self.master_key, offset, length)

    # -- internals --------------------------------------------------------

    @staticmethod
    def _relative_path(device_alias: str, start_utc: datetime, remote_name: str) -> Path:
        start_compact = start_utc.strftime("%Y%m%dT%H%M%SZ")
        return Path(
            device_alias,
            f"{start_utc:%Y}",
            f"{start_utc:%m}",
            f"{start_utc:%d}",
            f"{start_compact}_{remote_name}.enc",
        )

    @staticmethod
    def _sidecar_path(final_path: Path) -> Path:
        return final_path.with_name(final_path.name + ".json")

    def _write_sidecar(self, final_path: Path, result: PutResult) -> None:
        sidecar = {
            "plaintext_sha256": result.plaintext_sha256,
            "plaintext_size": result.plaintext_size,
            "ciphertext_size": result.ciphertext_size,
            "algorithm": "AES-256-GCM",
            "format_version": FORMAT_VERSION,
            "created_at": datetime.now(UTC).isoformat(),
        }
        sidecar_path = self._sidecar_path(final_path)
        tmp_path = sidecar_path.with_name(sidecar_path.name + ".tmp")
        tmp_path.write_text(json.dumps(sidecar, indent=2))
        os.replace(tmp_path, sidecar_path)

    def _read_sidecar(self, final_path: Path) -> dict[str, Any]:
        sidecar_path = self._sidecar_path(final_path)
        if not sidecar_path.exists():
            raise SidecarMissingError(f"no sidecar for {final_path}")
        loaded: dict[str, Any] = json.loads(sidecar_path.read_text())
        return loaded


def _fsync_dir(path: Path) -> None:
    """Linux-only (matches the plan's Docker deployment target): fsync a
    directory's own metadata, so a rename or a new file's existence survives
    a crash, not just the file's own content."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
