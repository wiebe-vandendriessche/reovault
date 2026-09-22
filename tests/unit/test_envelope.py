"""Crypto envelope tests: tamper and range-read property tests must pass."""

import io
import os

import pytest
from cryptography.exceptions import InvalidTag
from hypothesis import given
from hypothesis import strategies as st

from reovault.crypto.envelope import (
    HEADER_SIZE,
    EnvelopeHeader,
    decrypt_stream,
    encrypt_stream,
    read_range,
)

MASTER_KEY = os.urandom(32)
SMALL_FRAME = 16  # small so multi-frame behavior is exercised without huge fixtures


def _encrypt(plaintext: bytes, *, frame_size: int = SMALL_FRAME, master_key: bytes = MASTER_KEY):
    src, dst = io.BytesIO(plaintext), io.BytesIO()
    header = encrypt_stream(src, dst, master_key, frame_size=frame_size)
    return dst.getvalue(), header


def _decrypt(ciphertext: bytes, master_key: bytes = MASTER_KEY) -> bytes:
    dst = io.BytesIO()
    decrypt_stream(io.BytesIO(ciphertext), dst, master_key)
    return dst.getvalue()


@pytest.mark.parametrize(
    "plaintext",
    [
        b"",
        b"x",
        b"a" * SMALL_FRAME,  # exactly one full frame
        b"a" * (SMALL_FRAME + 1),  # one full frame plus one byte
        b"a" * (SMALL_FRAME * 5),  # exact multiple of several frames
        os.urandom(SMALL_FRAME * 5 + 7),  # several frames plus a partial one
        os.urandom(SMALL_FRAME * 50),  # stands in for "multi-GiB", same code path at scale
    ],
    ids=[
        "empty",
        "one-byte",
        "one-frame",
        "one-frame-plus-one",
        "exact-multiple",
        "ragged",
        "many-frames",
    ],
)
def test_round_trip(plaintext):
    ciphertext, _ = _encrypt(plaintext)
    assert _decrypt(ciphertext) == plaintext


def test_header_is_at_the_front_and_parses():
    ciphertext, header = _encrypt(b"hello world", frame_size=SMALL_FRAME)
    assert EnvelopeHeader.unpack(ciphertext[:HEADER_SIZE]) == header


def test_wrong_master_key_fails_closed():
    ciphertext, _ = _encrypt(b"secret recording bytes")
    with pytest.raises(InvalidTag):
        _decrypt(ciphertext, master_key=os.urandom(32))


def test_tamper_flip_ciphertext_byte_raises_invalid_tag():
    ciphertext, _ = _encrypt(b"a" * (SMALL_FRAME * 3))
    tampered = bytearray(ciphertext)
    tampered[HEADER_SIZE + 20] ^= 0xFF  # inside the first frame's ciphertext
    with pytest.raises(InvalidTag):
        _decrypt(bytes(tampered))


def test_tamper_truncate_last_frame_does_not_return_plaintext():
    plaintext = b"a" * (SMALL_FRAME * 3 + 5)
    ciphertext, _ = _encrypt(plaintext)
    truncated = ciphertext[:-3]
    with pytest.raises((InvalidTag, ValueError)):
        _decrypt(truncated)


def test_tamper_reorder_two_frames_raises_invalid_tag():
    plaintext = b"a" * SMALL_FRAME + b"b" * SMALL_FRAME + b"c" * SMALL_FRAME
    ciphertext, _ = _encrypt(plaintext, frame_size=SMALL_FRAME)
    stride = 12 + SMALL_FRAME + 16
    body = bytearray(ciphertext[HEADER_SIZE:])
    frame0, frame1 = bytes(body[:stride]), bytes(body[stride : 2 * stride])
    body[:stride], body[stride : 2 * stride] = frame1, frame0
    swapped = ciphertext[:HEADER_SIZE] + bytes(body)
    with pytest.raises(InvalidTag):
        _decrypt(swapped)


def test_tamper_splice_frame_between_files_raises_invalid_tag():
    plaintext = b"a" * SMALL_FRAME * 3
    ciphertext_a, _ = _encrypt(plaintext)
    ciphertext_b, _ = _encrypt(plaintext)  # different file_uuid, same plaintext
    stride = 12 + SMALL_FRAME + 16
    spliced = bytearray(ciphertext_a)
    spliced[HEADER_SIZE : HEADER_SIZE + stride] = ciphertext_b[HEADER_SIZE : HEADER_SIZE + stride]
    with pytest.raises(InvalidTag):
        _decrypt(bytes(spliced))


def test_tamper_corrupt_header_raises_invalid_tag():
    ciphertext, _ = _encrypt(b"a" * SMALL_FRAME * 2)
    tampered = bytearray(ciphertext)
    tampered[20] ^= 0xFF  # inside file_uuid, part of frame 0's AAD
    with pytest.raises(InvalidTag):
        _decrypt(bytes(tampered))


@given(
    plaintext=st.binary(min_size=0, max_size=SMALL_FRAME * 10),
    data=st.data(),
)
def test_range_read_matches_plaintext_slice(plaintext, data):
    ciphertext, _ = _encrypt(plaintext)
    offset = data.draw(st.integers(min_value=0, max_value=max(len(plaintext), 0)))
    length = data.draw(st.integers(min_value=0, max_value=max(len(plaintext) - offset, 0) + 5))

    result = read_range(io.BytesIO(ciphertext), MASTER_KEY, offset, length)
    expected = plaintext[offset : offset + length]
    assert result == expected
