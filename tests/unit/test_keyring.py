"""Key management tests."""

import os

import pytest
from cryptography.exceptions import InvalidTag

from reovault.crypto.keyring import (
    InsecureKeyFilePermissionsError,
    KeyInsideVaultError,
    assert_outside_vault,
    generate_master_key,
    load_master_key,
    verify_passphrase,
)

# Small Argon2id params so tests run fast; production defaults are much heavier.
FAST_KDF = dict(iterations=1, memory_cost_kib=8, lanes=1)


def test_generate_and_load_round_trip(tmp_path):
    key_path = tmp_path / "master.key"
    generated = generate_master_key(key_path, b"correct horse battery staple", **FAST_KDF)
    loaded = load_master_key(key_path, b"correct horse battery staple")
    assert loaded == generated
    assert len(loaded) == 32


def test_key_file_is_0600(tmp_path):
    key_path = tmp_path / "master.key"
    generate_master_key(key_path, b"passphrase", **FAST_KDF)
    assert (key_path.stat().st_mode & 0o777) == 0o600


def test_refuses_to_overwrite_existing_key(tmp_path):
    key_path = tmp_path / "master.key"
    generate_master_key(key_path, b"passphrase", **FAST_KDF)
    with pytest.raises(FileExistsError):
        generate_master_key(key_path, b"other passphrase", **FAST_KDF)


def test_wrong_passphrase_raises_invalid_tag(tmp_path):
    key_path = tmp_path / "master.key"
    generate_master_key(key_path, b"right passphrase", **FAST_KDF)
    with pytest.raises(InvalidTag):
        load_master_key(key_path, b"wrong passphrase")


def test_verify_passphrase_true_and_false(tmp_path):
    key_path = tmp_path / "master.key"
    generate_master_key(key_path, b"right passphrase", **FAST_KDF)
    assert verify_passphrase(key_path, b"right passphrase") is True
    assert verify_passphrase(key_path, b"wrong passphrase") is False


def test_missing_key_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_master_key(tmp_path / "does-not-exist.key", b"whatever")


def test_group_readable_key_file_refuses_to_load(tmp_path):
    key_path = tmp_path / "master.key"
    generate_master_key(key_path, b"passphrase", **FAST_KDF)
    os.chmod(key_path, 0o644)
    with pytest.raises(InsecureKeyFilePermissionsError):
        load_master_key(key_path, b"passphrase")


def test_world_readable_key_file_refuses_to_load(tmp_path):
    key_path = tmp_path / "master.key"
    generate_master_key(key_path, b"passphrase", **FAST_KDF)
    os.chmod(key_path, 0o604)
    with pytest.raises(InsecureKeyFilePermissionsError):
        load_master_key(key_path, b"passphrase")


def test_key_inside_vault_is_rejected(tmp_path):
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    key_path = vault_dir / "master.key"
    with pytest.raises(KeyInsideVaultError):
        assert_outside_vault(key_path, vault_dir)


def test_key_outside_vault_is_accepted(tmp_path):
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    key_path = tmp_path / "config" / "master.key"
    assert_outside_vault(key_path, vault_dir)  # must not raise
