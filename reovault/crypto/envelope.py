"""Per-file envelope encryption.

Frame layout on disk, right after the header:

    [nonce_0][ct_0][tag_0] [nonce_1][ct_1][tag_1] ... [nonce_n][ct_n][tag_n]

Every frame but the last holds exactly `frame_size` plaintext bytes. AES-GCM
is a stream cipher, so ciphertext length equals plaintext length; only the
16-byte tag adds overhead. That fixed stride is what lets a plaintext byte
range map arithmetically onto a frame range with no index, which is exactly
what `read_range` below does.

Each frame's AAD is `file_uuid || frame_index || is_final`, and frame 0's AAD
is additionally prefixed with the raw header bytes, so header tampering fails
frame 0's decryption. Library: `cryptography`'s AESGCM. No hand-rolled
primitives, and no wrapping of `cryptography.exceptions.InvalidTag`, this
module lets it propagate exactly as the plan requires: tamper cases must
raise `InvalidTag`, never return plaintext.
"""

from __future__ import annotations

import math
import os
import struct
import uuid
from dataclasses import dataclass
from typing import Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class ReadableStream(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...


class WritableStream(Protocol):
    def write(self, data: bytes, /) -> int: ...


class SeekableReadableStream(ReadableStream, Protocol):
    """What `decrypt_stream`/`read_range` need beyond `ReadableStream`:
    random access, so frame N can be located without reading frames
    0..N-1 first. Real files and `io.BytesIO` both satisfy this; a
    one-way pipe does not, which is intentional (see module docstring:
    seekable input only)."""

    def seek(self, offset: int, whence: int = 0, /) -> int: ...
    def tell(self) -> int: ...


MAGIC = b"REOVAULT1"
FORMAT_VERSION = 1

NONCE_SIZE = 12
TAG_SIZE = 16
DEK_SIZE = 32
WRAPPED_DEK_SIZE = DEK_SIZE + TAG_SIZE  # ciphertext + tag from wrapping the DEK
FILE_UUID_SIZE = 16
DEFAULT_FRAME_SIZE = 1024 * 1024  # 1 MiB

# magic(9s) version(B) frame_size(I) file_uuid(16s) dek_nonce(12s) wrapped_dek(48s)
_HEADER_STRUCT = struct.Struct(f">{len(MAGIC)}sBI{FILE_UUID_SIZE}s{NONCE_SIZE}s{WRAPPED_DEK_SIZE}s")
HEADER_SIZE = _HEADER_STRUCT.size


class EnvelopeFormatError(ValueError):
    """The bytes at the start of a file are not a recognizable ReoVault
    envelope header. Distinct from `InvalidTag`: this is "not our format",
    not "the format's authentication failed"."""


@dataclass(frozen=True, slots=True)
class EnvelopeHeader:
    version: int
    frame_size: int
    file_uuid: bytes
    dek_nonce: bytes
    wrapped_dek: bytes

    def pack(self) -> bytes:
        return _HEADER_STRUCT.pack(
            MAGIC, self.version, self.frame_size, self.file_uuid, self.dek_nonce, self.wrapped_dek
        )

    @classmethod
    def unpack(cls, data: bytes) -> EnvelopeHeader:
        if len(data) != HEADER_SIZE:
            raise EnvelopeFormatError(f"expected {HEADER_SIZE} header bytes, got {len(data)}")
        magic, version, frame_size, file_uuid, dek_nonce, wrapped_dek = _HEADER_STRUCT.unpack(data)
        if magic != MAGIC:
            raise EnvelopeFormatError(f"bad magic {magic!r}, not a ReoVault envelope")
        return cls(
            version=version,
            frame_size=frame_size,
            file_uuid=file_uuid,
            dek_nonce=dek_nonce,
            wrapped_dek=wrapped_dek,
        )


def wrap_dek(dek: bytes, master_key: bytes) -> tuple[bytes, bytes]:
    """Wrap a per-file DEK with the master key. Returns `(nonce, wrapped)`."""
    nonce = os.urandom(NONCE_SIZE)
    wrapped = AESGCM(master_key).encrypt(nonce, dek, None)
    return nonce, wrapped


def unwrap_dek(nonce: bytes, wrapped: bytes, master_key: bytes) -> bytes:
    """Unwrap a DEK. Raises `cryptography.exceptions.InvalidTag` if
    `master_key` is wrong or `wrapped`/`nonce` were tampered with."""
    return AESGCM(master_key).decrypt(nonce, wrapped, None)


def _frame_aad(
    file_uuid: bytes, frame_index: int, is_final: bool, header_bytes: bytes | None
) -> bytes:
    base = file_uuid + frame_index.to_bytes(4, "big") + (b"\x01" if is_final else b"\x00")
    return header_bytes + base if header_bytes is not None else base


def _stride(frame_size: int) -> int:
    return NONCE_SIZE + frame_size + TAG_SIZE


def encrypt_stream(
    src: ReadableStream,
    dst: WritableStream,
    master_key: bytes,
    *,
    frame_size: int = DEFAULT_FRAME_SIZE,
) -> EnvelopeHeader:
    """Stream-encrypt `src` into the envelope format, writing to `dst`.
    Returns the header that was written (callers needing plaintext/ciphertext
    sizes should track bytes read/written themselves; this function's only
    job is the encryption, not staging or fsync, that's the vault's job).
    """
    dek = os.urandom(DEK_SIZE)
    dek_nonce, wrapped_dek = wrap_dek(dek, master_key)
    file_uuid = uuid.uuid4().bytes
    header = EnvelopeHeader(
        version=FORMAT_VERSION,
        frame_size=frame_size,
        file_uuid=file_uuid,
        dek_nonce=dek_nonce,
        wrapped_dek=wrapped_dek,
    )
    header_bytes = header.pack()
    dst.write(header_bytes)

    aesgcm = AESGCM(dek)
    frame_index = 0
    chunk = src.read(frame_size)
    while True:
        next_chunk = src.read(frame_size)
        is_final = next_chunk == b""
        aad = _frame_aad(
            file_uuid, frame_index, is_final, header_bytes if frame_index == 0 else None
        )
        nonce = os.urandom(NONCE_SIZE)
        ct_and_tag = aesgcm.encrypt(nonce, chunk, aad)
        dst.write(nonce)
        dst.write(ct_and_tag)
        if is_final:
            break
        chunk = next_chunk
        frame_index += 1
    return header


def _read_header(src: SeekableReadableStream) -> EnvelopeHeader:
    src.seek(0)
    return EnvelopeHeader.unpack(src.read(HEADER_SIZE))


def _body_size(src: SeekableReadableStream) -> int:
    src.seek(0, os.SEEK_END)
    total = src.tell()
    return total - HEADER_SIZE


def _frame_plan(body_size: int, frame_size: int) -> tuple[int, int]:
    """Returns `(total_frames, stride)`. Requires `body_size > 0` (an
    envelope always has at least one frame, even for empty plaintext)."""
    if body_size <= 0:
        raise EnvelopeFormatError("envelope has no frame data")
    stride = _stride(frame_size)
    return math.ceil(body_size / stride), stride


def _read_frame(
    src: SeekableReadableStream, header: EnvelopeHeader, frame_index: int, on_disk_size: int
) -> bytes:
    pos = HEADER_SIZE + frame_index * _stride(header.frame_size)
    src.seek(pos)
    raw = src.read(on_disk_size)
    if len(raw) != on_disk_size:
        raise EnvelopeFormatError(f"truncated frame {frame_index}: expected {on_disk_size} bytes")
    return raw


def decrypt_stream(
    src: SeekableReadableStream, dst: WritableStream, master_key: bytes
) -> EnvelopeHeader:
    """Stream-decrypt a ReoVault envelope from `src` (must be seekable) into
    `dst`. Raises `cryptography.exceptions.InvalidTag` on any tamper: a
    flipped ciphertext byte, a truncated final frame treated as complete,
    reordered or cross-file-spliced frames, or a corrupted header (frame 0's
    AAD includes the header bytes)."""
    header = _read_header(src)
    dek = unwrap_dek(header.dek_nonce, header.wrapped_dek, master_key)
    aesgcm = AESGCM(dek)

    body_size = _body_size(src)
    total_frames, stride = _frame_plan(body_size, header.frame_size)

    for i in range(total_frames):
        is_final = i == total_frames - 1
        on_disk_size = stride if not is_final else body_size - i * stride
        raw = _read_frame(src, header, i, on_disk_size)
        nonce, ct_and_tag = raw[:NONCE_SIZE], raw[NONCE_SIZE:]
        aad = _frame_aad(header.file_uuid, i, is_final, header.pack() if i == 0 else None)
        plaintext = aesgcm.decrypt(nonce, ct_and_tag, aad)
        dst.write(plaintext)
    return header


def read_range(src: SeekableReadableStream, master_key: bytes, offset: int, length: int) -> bytes:
    """Decrypt only the frames covering plaintext `[offset, offset + length)`
    and return exactly that slice. Used by the dashboard's `Range` playback
    so seeking a long recording never decrypts more than the frames touched.
    `src` must be seekable."""
    if length <= 0:
        return b""
    header = _read_header(src)
    dek = unwrap_dek(header.dek_nonce, header.wrapped_dek, master_key)
    aesgcm = AESGCM(dek)

    body_size = _body_size(src)
    total_frames, stride = _frame_plan(body_size, header.frame_size)

    start_frame = offset // header.frame_size
    end_frame = min((offset + length - 1) // header.frame_size, total_frames - 1)

    out = bytearray()
    for i in range(start_frame, end_frame + 1):
        is_final = i == total_frames - 1
        on_disk_size = stride if not is_final else body_size - i * stride
        raw = _read_frame(src, header, i, on_disk_size)
        nonce, ct_and_tag = raw[:NONCE_SIZE], raw[NONCE_SIZE:]
        aad = _frame_aad(header.file_uuid, i, is_final, header.pack() if i == 0 else None)
        out += aesgcm.decrypt(nonce, ct_and_tag, aad)

    slice_start = offset - start_frame * header.frame_size
    return bytes(out[slice_start : slice_start + length])
