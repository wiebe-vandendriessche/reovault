"""`redact_paths`: every error string leaving the API goes through it (see
`AppState.redact`). The realistic leak vector is `archive_runs.error` /
`recordings.last_error` text: an `OSError` message can carry an absolute
staging or vault path.
"""

from __future__ import annotations

from reovault.config import StorageConfig


def redact_paths(text: str | None, storage: StorageConfig) -> str:
    if not text:
        return ""
    result = text
    for placeholder, path in (
        ("<vault>", storage.vault_dir),
        ("<staging>", storage.staging_dir),
        ("<config>", storage.config_dir),
        ("<key>", storage.master_key_path),
    ):
        result = result.replace(str(path), placeholder)
    return result
