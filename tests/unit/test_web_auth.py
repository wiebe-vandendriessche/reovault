import os

import pytest

from reovault.config import WebConfig
from reovault.web.auth import (
    InsecureKeyFilePermissionsError,
    load_or_create_session_key,
    load_password_hash,
    write_password_file,
)


def test_load_password_hash_prefers_env_field_over_file(tmp_path):
    web = WebConfig(password_hash="$argon2id$fromenv", password_file=tmp_path / "web_password")
    assert load_password_hash(web) == "$argon2id$fromenv"


def test_load_password_hash_none_when_neither_configured(tmp_path):
    web = WebConfig(password_file=tmp_path / "missing")
    assert load_password_hash(web) is None


def test_write_and_load_password_file_roundtrip(tmp_path):
    path = tmp_path / "web_password"
    write_password_file(path, "$argon2id$abc")
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    web = WebConfig(password_file=path)
    assert load_password_hash(web) == "$argon2id$abc"


def test_load_password_hash_rejects_loose_permissions(tmp_path):
    path = tmp_path / "web_password"
    path.write_text("$argon2id$abc\n")
    os.chmod(path, 0o644)
    web = WebConfig(password_file=path)
    with pytest.raises(InsecureKeyFilePermissionsError):
        load_password_hash(web)


def test_load_or_create_session_key_creates_then_persists(tmp_path):
    path = tmp_path / "web_session.key"
    key1 = load_or_create_session_key(path)
    assert len(key1) == 32
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    key2 = load_or_create_session_key(path)
    assert key1 == key2  # persisted, not regenerated


def test_load_or_create_session_key_rejects_loose_permissions(tmp_path):
    path = tmp_path / "web_session.key"
    path.write_bytes(os.urandom(32))
    os.chmod(path, 0o644)
    with pytest.raises(InsecureKeyFilePermissionsError):
        load_or_create_session_key(path)
