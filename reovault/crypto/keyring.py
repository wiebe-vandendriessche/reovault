"""Master key load/unwrap, Argon2id, permission checks.

    REOVAULT_MASTER_PASSPHRASE (env var or Docker secret)
            | Argon2id (params recorded in the key file header)
            v
      unwraps  <config>/master.key   (0600, 32 random bytes, outside the vault)
            |
            v  wraps each file's DEK  (see envelope.py)

The key file's own AEAD tag is the canary for `reovault key verify`:
unwrapping only succeeds if the passphrase is right, so a successful
`load_master_key` already proves it. No separate canary plaintext is stored.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

MAGIC = b"RVKEY001"
SALT_SIZE = 16
NONCE_SIZE = 12
MASTER_KEY_SIZE = 32
WRAPPED_KEY_SIZE = MASTER_KEY_SIZE + 16  # ciphertext + GCM tag

DEFAULT_ITERATIONS = 3
DEFAULT_MEMORY_COST_KIB = 65536  # 64 MiB
DEFAULT_LANES = 4

# magic(8s) salt(16s) iterations(I) memory_cost_kib(I) lanes(I) nonce(12s) wrapped_key(48s)
_STRUCT = struct.Struct(f">{len(MAGIC)}s{SALT_SIZE}sIII{NONCE_SIZE}s{WRAPPED_KEY_SIZE}s")


class KeyFileError(Exception):
    """Base for keyring problems that should stop startup."""


class InsecureKeyFilePermissionsError(KeyFileError):
    """`master.key` is group- or world-readable. Same guard `reolink-cli`
    applies to `credentials.key`."""


class KeyInsideVaultError(KeyFileError):
    """`master.key` lives under the vault path, which a separate backup tool
    would then replicate to the same destinations as the ciphertext it
    protects, collapsing the security boundary."""


class KeyFileFormatError(KeyFileError):
    """The bytes at `master.key` are not a recognizable ReoVault key file."""


@dataclass(frozen=True, slots=True)
class _KeyFile:
    salt: bytes
    iterations: int
    memory_cost_kib: int
    lanes: int
    nonce: bytes
    wrapped_key: bytes

    def pack(self) -> bytes:
        return _STRUCT.pack(
            MAGIC,
            self.salt,
            self.iterations,
            self.memory_cost_kib,
            self.lanes,
            self.nonce,
            self.wrapped_key,
        )

    @classmethod
    def unpack(cls, data: bytes) -> _KeyFile:
        if len(data) != _STRUCT.size:
            raise KeyFileFormatError(f"expected {_STRUCT.size} bytes, got {len(data)}")
        magic, salt, iterations, memory_cost_kib, lanes, nonce, wrapped_key = _STRUCT.unpack(data)
        if magic != MAGIC:
            raise KeyFileFormatError(f"bad magic {magic!r}, not a ReoVault key file")
        return cls(salt, iterations, memory_cost_kib, lanes, nonce, wrapped_key)


def _derive_kek(
    passphrase: bytes, salt: bytes, iterations: int, memory_cost_kib: int, lanes: int
) -> bytes:
    kdf = Argon2id(
        salt=salt,
        length=MASTER_KEY_SIZE,
        iterations=iterations,
        lanes=lanes,
        memory_cost=memory_cost_kib,
    )
    return kdf.derive(passphrase)


def check_permissions(path: Path) -> None:
    """Refuse anything group- or world-readable/writable, before the file's
    contents are touched at all (mirrors `reolink-cli`'s own guard on
    `credentials.key`)."""
    mode = path.stat().st_mode
    if mode & 0o077:
        raise InsecureKeyFilePermissionsError(
            f"{path} is group- or world-accessible (mode {oct(mode & 0o777)}); "
            "chmod 600 it before starting ReoVault"
        )


def assert_outside_vault(key_path: Path, vault_dir: Path) -> None:
    """The key file must not live under the vault directory: a separate
    backup/retention tool typically treats the vault as its source, and a
    key file inside it would get replicated alongside the ciphertext it
    unlocks."""
    key_resolved = key_path.resolve()
    vault_resolved = vault_dir.resolve()
    if key_resolved == vault_resolved or vault_resolved in key_resolved.parents:
        raise KeyInsideVaultError(
            f"master key at {key_path} is inside the vault directory {vault_dir}; "
            f"move it outside {vault_dir} before starting ReoVault"
        )


def generate_master_key(
    path: Path,
    passphrase: bytes,
    *,
    iterations: int = DEFAULT_ITERATIONS,
    memory_cost_kib: int = DEFAULT_MEMORY_COST_KIB,
    lanes: int = DEFAULT_LANES,
) -> bytes:
    """Create a new `master.key` at `path`, wrapped by `passphrase`. Refuses
    to overwrite an existing key file, since losing one is unrecoverable and
    clobbering should never happen implicitly. Returns the raw master key so
    callers/tests can round-trip without re-deriving."""
    if path.exists():
        raise FileExistsError(f"{path} already exists; refusing to overwrite a master key")

    master_key = os.urandom(MASTER_KEY_SIZE)
    salt = os.urandom(SALT_SIZE)
    kek = _derive_kek(passphrase, salt, iterations, memory_cost_kib, lanes)
    nonce = os.urandom(NONCE_SIZE)
    wrapped_key = AESGCM(kek).encrypt(nonce, master_key, None)
    key_file = _KeyFile(salt, iterations, memory_cost_kib, lanes, nonce, wrapped_key)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_bytes(key_file.pack())
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, path)
    return master_key


def load_master_key(path: Path, passphrase: bytes) -> bytes:
    """Unwrap the master key. Raises `InsecureKeyFilePermissionsError` on
    loose permissions (checked before the file is even read), and lets
    `cryptography.exceptions.InvalidTag` propagate unwrapped if `passphrase`
    is wrong."""
    check_permissions(path)
    key_file = _KeyFile.unpack(path.read_bytes())
    kek = _derive_kek(
        passphrase, key_file.salt, key_file.iterations, key_file.memory_cost_kib, key_file.lanes
    )
    return AESGCM(kek).decrypt(key_file.nonce, key_file.wrapped_key, None)


def verify_passphrase(path: Path, passphrase: bytes) -> bool:
    """`reovault key verify`: True if `passphrase` unwraps `path`."""
    from cryptography.exceptions import InvalidTag

    try:
        load_master_key(path, passphrase)
    except InvalidTag:
        return False
    return True
