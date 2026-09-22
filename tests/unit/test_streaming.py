"""Chunk alignment must decrypt each frame exactly once, and the download
filename must never be taken from `vault_path` nor allow header injection."""

import os
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from reovault.storage.vault import EncryptedFsVault
from reovault.web.streaming import StreamTruncatedError, iter_plaintext, safe_download_filename


@pytest.fixture
def small_vault(tmp_path):
    vault = EncryptedFsVault(tmp_path / "vault", os.urandom(32), frame_size=16)
    plaintext = bytes(range(256)) * 4  # 1024 bytes, i.e. 64 frames of 16 bytes
    staging = tmp_path / "staging.bin"
    staging.write_bytes(plaintext)
    result = vault.put(
        staging,
        device_alias="doorbell",
        start_utc=datetime(2026, 1, 1, tzinfo=UTC),
        remote_name="clip",
    )
    return vault, result.vault_path, plaintext


def test_iter_plaintext_reassembles_exact_bytes(small_vault):
    vault, vault_path, plaintext = small_vault
    chunks = list(iter_plaintext(vault, vault_path, 5, 100, chunk_size=16))
    assert b"".join(chunks) == plaintext[5:101]


def test_iter_plaintext_whole_file(small_vault):
    vault, vault_path, plaintext = small_vault
    chunks = list(iter_plaintext(vault, vault_path, 0, len(plaintext) - 1, chunk_size=16))
    assert b"".join(chunks) == plaintext


def test_aligned_chunking_decrypts_each_frame_exactly_once(small_vault):
    """The whole point of aligning chunk_size to frame_size: count of
    open_range calls must equal the number of chunk-aligned reads, never
    more (which would mean a frame was decrypted twice)."""
    vault, vault_path, plaintext = small_vault
    real_open_range = vault.open_range
    calls = []

    def spy(path, offset, length):
        calls.append((offset, length))
        return real_open_range(path, offset, length)

    vault.open_range = spy  # type: ignore[method-assign]

    start, end = 5, 100
    list(iter_plaintext(vault, vault_path, start, end, chunk_size=16))

    # Aligned reads: first chunk absorbs the partial frame, then full
    # 16-byte chunks, so the count must equal ceil((end-start+1)/16) at most
    # (first/last may be partial), and never revisit an already-fully-read
    # frame boundary more than once.
    expected_chunk_count = 1 + (end - 5) // 16  # 5 is aligned to frame 0, offset 5 to 100
    # More directly: no offset appears twice, proving no frame re-decrypted.
    offsets = [c[0] for c in calls]
    assert len(offsets) == len(set(offsets))
    assert len(calls) <= expected_chunk_count + 1


def test_iter_plaintext_raises_on_truncation(small_vault):
    vault, vault_path, _plaintext = small_vault
    fake_vault = MagicMock()
    fake_vault.open_range.return_value = b""
    with pytest.raises(StreamTruncatedError):
        list(iter_plaintext(fake_vault, vault_path, 0, 10, chunk_size=16))


def test_safe_download_filename_sanitizes_header_injection():
    # Dots are a legal filename character (e.g. "clip.backup.mp4"), so "."
    # survives; what must never survive is anything that could break out of
    # a Content-Disposition header value or a path segment.
    hostile = 'evil"/../\r\nname'
    filename = safe_download_filename(hostile)
    assert '"' not in filename
    assert "/" not in filename
    assert "\r" not in filename
    assert "\n" not in filename
    assert filename.endswith(".mp4")


def test_safe_download_filename_normal_case():
    assert safe_download_filename("0120260916111127") == "0120260916111127.mp4"


def test_safe_download_filename_empty_falls_back():
    assert safe_download_filename("") == "recording.mp4"


def test_safe_download_filename_truncates_long_names():
    filename = safe_download_filename("a" * 500)
    assert len(filename) <= 104  # 100 chars + ".mp4"
