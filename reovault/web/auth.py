"""Password and session-key file lifecycle: 0600 files outside source
control, written via tmp-file + atomic replace.

A web password verifier is offline-attackable, so it stays out of
`reovault.toml`: it lives in a 0600 file in the data folder, or in
`REOVAULT_WEB__PASSWORD_HASH` for the Docker-secret path.
"""

from __future__ import annotations

import os
from pathlib import Path

from reovault.config import WebConfig


class InsecureKeyFilePermissionsError(Exception):
    """A credential file is group- or world-accessible."""


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


def load_password_hash(web: WebConfig) -> str | None:
    """`None` means no password is configured yet; callers should refuse
    UI routes with 503 and point at `reovault web set-password`, without
    refusing to start (that would take `/healthz` down with it)."""
    if web.password_hash:
        return web.password_hash
    if web.password_file.exists():
        check_permissions(web.password_file)
        return web.password_file.read_text().strip()
    return None


def write_password_file(path: Path, phc_encoded: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(phc_encoded + "\n")
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, path)


def load_or_create_session_key(path: Path) -> bytes:
    """Persists across restarts: a 30-day cookie that dies on every restart
    is worse than useless. Random, never derived from the restic password,
    so a web secret and the backups never share a compromise."""
    if path.exists():
        check_permissions(path)
        return path.read_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = os.urandom(32)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_bytes(key)
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, path)
    return key


__all__ = [
    "InsecureKeyFilePermissionsError",
    "load_or_create_session_key",
    "load_password_hash",
    "write_password_file",
]
