"""Frame-aligned chunked decrypt-and-stream for `/recordings/{id}/stream`.

Chunk size must be a multiple of the vault's frame size (1 MiB by default),
and reads must be aligned to chunk boundaries. `VaultStore.open_range`
decrypts every frame that *overlaps* the requested slice; an unaligned or
too-small chunk re-decrypts the same frame on every chunk that touches it,
a silent CPU multiplier that never shows up as a wrong answer, only as a
hot CPU during playback.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from reovault.storage.vault import VaultStore

_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")


class StreamTruncatedError(Exception):
    """The vault reports fewer bytes than `plaintext_size` promised at some
    offset within `[start, end]`. Raised mid-generator, after headers are
    already on the wire: the caller lets this abort the response rather
    than yielding an error string into the byte stream."""

    def __init__(self, vault_path: str, offset: int) -> None:
        super().__init__(f"stream truncated at offset {offset} in {vault_path!r}")
        self.vault_path = vault_path
        self.offset = offset


def safe_download_filename(remote_name: str) -> str:
    """Synthesized from `remote_name`, never from `vault_path` (which must
    never reach a client). The real camera's `remote_name` carries no
    extension (e.g. `0120260916111127`), and this also sanitizes header
    injection: quotes, slashes, `..`, and CR/LF all become `_`."""
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", remote_name)[:100]
    return f"{cleaned or 'recording'}.mp4"


def iter_plaintext(
    vault: VaultStore, vault_path: str, start: int, end: int, chunk_size: int
) -> Iterator[bytes]:
    """Yields plaintext bytes covering `[start, end]` inclusive, in reads
    aligned to `chunk_size`. Raises `StreamTruncatedError` if the vault
    returns fewer bytes than expected before `end` is reached."""
    pos = start
    while pos <= end:
        take = min(chunk_size - (pos % chunk_size), end - pos + 1)
        data = vault.open_range(vault_path, pos, take)
        if not data:
            raise StreamTruncatedError(vault_path, pos)
        yield data
        pos += len(data)
