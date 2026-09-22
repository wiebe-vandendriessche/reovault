"""Encrypted vault: write, verify, and frame-aligned range reads."""

import hashlib
import json
import os
from datetime import UTC, datetime

import pytest
from cryptography.exceptions import InvalidTag

from reovault.storage.vault import EncryptedFsVault

MASTER_KEY = os.urandom(32)
START = datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def vault(tmp_path):
    return EncryptedFsVault(tmp_path / "vault", MASTER_KEY, frame_size=16)


@pytest.fixture
def staging_file(tmp_path):
    path = tmp_path / "staging" / "20260417_120000.mp4"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"fake video bytes" * 10)
    return path


def test_put_writes_date_sharded_path(vault, staging_file):
    result = vault.put(
        staging_file, device_alias="doorbell", start_utc=START, remote_name="20260417_120000.mp4"
    )
    assert result.vault_path == "doorbell/2026/04/17/20260417T120000Z_20260417_120000.mp4.enc"
    assert (vault.vault_dir / result.vault_path).exists()


def test_put_result_matches_plaintext(vault, staging_file):
    plaintext = staging_file.read_bytes()
    result = vault.put(
        staging_file, device_alias="doorbell", start_utc=START, remote_name="clip.mp4"
    )
    assert result.plaintext_size == len(plaintext)
    assert result.plaintext_sha256 == hashlib.sha256(plaintext).hexdigest()


def test_no_tmp_file_left_behind_after_put(vault, staging_file):
    vault.put(staging_file, device_alias="doorbell", start_utc=START, remote_name="clip.mp4")
    leftovers = list(vault.vault_dir.rglob("*.tmp"))
    assert leftovers == []


def test_sidecar_is_written_next_to_ciphertext(vault, staging_file):
    result = vault.put(
        staging_file, device_alias="doorbell", start_utc=START, remote_name="clip.mp4"
    )
    sidecar_path = vault.vault_dir / (result.vault_path + ".json")
    assert sidecar_path.exists()
    sidecar = json.loads(sidecar_path.read_text())
    assert sidecar["plaintext_sha256"] == result.plaintext_sha256
    assert sidecar["plaintext_size"] == result.plaintext_size
    assert sidecar["ciphertext_size"] == result.ciphertext_size
    assert sidecar["algorithm"] == "AES-256-GCM"


def test_get_round_trips_plaintext(vault, staging_file, tmp_path):
    plaintext = staging_file.read_bytes()
    result = vault.put(
        staging_file, device_alias="doorbell", start_utc=START, remote_name="clip.mp4"
    )
    out_path = tmp_path / "exported.mp4"
    vault.get(result.vault_path, out_path)
    assert out_path.read_bytes() == plaintext


def test_verify_true_for_untouched_file(vault, staging_file):
    result = vault.put(
        staging_file, device_alias="doorbell", start_utc=START, remote_name="clip.mp4"
    )
    assert vault.verify(result.vault_path) is True


def test_verify_false_when_ciphertext_tampered(vault, staging_file):
    result = vault.put(
        staging_file, device_alias="doorbell", start_utc=START, remote_name="clip.mp4"
    )
    ciphertext_path = vault.vault_dir / result.vault_path
    data = bytearray(ciphertext_path.read_bytes())
    data[-1] ^= 0xFF
    ciphertext_path.write_bytes(bytes(data))
    assert vault.verify(result.vault_path) is False


def test_verify_false_when_sidecar_missing(vault, staging_file):
    result = vault.put(
        staging_file, device_alias="doorbell", start_utc=START, remote_name="clip.mp4"
    )
    (vault.vault_dir / (result.vault_path + ".json")).unlink()
    assert vault.verify(result.vault_path) is False


def test_verify_false_when_hash_recorded_wrong(vault, staging_file):
    result = vault.put(
        staging_file, device_alias="doorbell", start_utc=START, remote_name="clip.mp4"
    )
    sidecar_path = vault.vault_dir / (result.vault_path + ".json")
    sidecar = json.loads(sidecar_path.read_text())
    sidecar["plaintext_sha256"] = "0" * 64
    sidecar_path.write_text(json.dumps(sidecar))
    assert vault.verify(result.vault_path) is False


def test_delete_removes_ciphertext_and_sidecar(vault, staging_file):
    result = vault.put(
        staging_file, device_alias="doorbell", start_utc=START, remote_name="clip.mp4"
    )
    vault.delete(result.vault_path)
    assert not (vault.vault_dir / result.vault_path).exists()
    assert not (vault.vault_dir / (result.vault_path + ".json")).exists()


def test_delete_is_safe_on_missing_file(vault):
    vault.delete("doorbell/2026/04/17/does-not-exist.mp4.enc")  # must not raise


def test_open_range_returns_exact_slice(vault, staging_file):
    plaintext = staging_file.read_bytes()
    result = vault.put(
        staging_file, device_alias="doorbell", start_utc=START, remote_name="clip.mp4"
    )
    got = vault.open_range(result.vault_path, 5, 10)
    assert got == plaintext[5:15]


def test_put_does_not_touch_staging_file(vault, staging_file):
    original = staging_file.read_bytes()
    vault.put(staging_file, device_alias="doorbell", start_utc=START, remote_name="clip.mp4")
    assert staging_file.exists()
    assert staging_file.read_bytes() == original


def test_get_on_hand_tampered_file_raises_invalid_tag(vault, staging_file, tmp_path):
    """`get()`/export deliberately does NOT swallow tamper errors the way
    `verify()` does; a caller asking to decrypt gets the real exception."""
    result = vault.put(
        staging_file, device_alias="doorbell", start_utc=START, remote_name="clip.mp4"
    )
    ciphertext_path = vault.vault_dir / result.vault_path
    data = bytearray(ciphertext_path.read_bytes())
    data[-1] ^= 0xFF
    ciphertext_path.write_bytes(bytes(data))
    with pytest.raises(InvalidTag):
        vault.get(result.vault_path, tmp_path / "out.mp4")


def test_two_devices_same_timestamp_do_not_collide(vault, staging_file):
    r1 = vault.put(staging_file, device_alias="doorbell-a", start_utc=START, remote_name="clip.mp4")
    r2 = vault.put(staging_file, device_alias="doorbell-b", start_utc=START, remote_name="clip.mp4")
    assert r1.vault_path != r2.vault_path
    assert vault.verify(r1.vault_path) is True
    assert vault.verify(r2.vault_path) is True
