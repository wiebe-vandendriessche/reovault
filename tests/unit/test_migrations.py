"""The migration runner: ordering, skip-if-applied, recorded version,
rollback-and-retry on a broken script, and idempotence across two
Repository instances on one file."""

from __future__ import annotations

import sqlite3

import pytest

from reovault.db import repository as repo_module
from reovault.db.repository import Repository


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "reovault.db"


def test_fresh_database_records_every_baseline_migration(db_path):
    repo = Repository(db_path)
    try:
        rows = repo.conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        versions = [r["version"] for r in rows]
        # Whatever ships today, at minimum baseline + devices-runtime +
        # app_settings (0001-0003) must all be applied on a fresh DB.
        assert versions[:3] == [1, 2, 3]
        assert versions == sorted(versions)
    finally:
        repo.close()


def test_reopening_the_same_file_is_idempotent(db_path):
    repo1 = Repository(db_path)
    repo1.close()
    # Second open must not re-run 0001 (which would fail: CREATE TABLE IF
    # NOT EXISTS is safe, but a real column-add migration would raise
    # "duplicate column" if replayed).
    repo2 = Repository(db_path)
    try:
        count = repo2.conn.execute("SELECT COUNT(*) AS n FROM schema_migrations").fetchone()["n"]
        assert count == len(repo_module._discover_migrations())
    finally:
        repo2.close()


def test_new_columns_reach_a_database_that_predates_them(db_path):
    """The exact scenario 0002/0003 exist to fix: a DB created before those
    files existed must still gain the new columns/table on next open, not
    just new indexes (which `CREATE INDEX IF NOT EXISTS` already handled)."""
    conn = sqlite3.connect(db_path)
    conn.executescript(repo_module._discover_migrations()[0][1])  # 0001 only
    conn.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (1, 'x')")
    conn.commit()
    conn.close()

    repo = Repository(db_path)
    try:
        # 0002's columns and 0003's table must now exist.
        repo.conn.execute("SELECT host, user, description, created_at, enabled FROM devices")
        repo.conn.execute("SELECT key, value, updated_at FROM app_settings")
        # 0004's device name and 0005's recording error detail, same story.
        repo.conn.execute("SELECT name FROM devices")
        repo.conn.execute("SELECT last_error_detail FROM recordings")
    finally:
        repo.close()


def test_broken_migration_rolls_back_and_is_retried(db_path, monkeypatch):
    good = repo_module._discover_migrations()

    def broken():
        return [*good, (999, "CREATE TABLE ok(x); THIS IS NOT SQL;")]

    monkeypatch.setattr(repo_module, "_discover_migrations", broken)
    with pytest.raises(sqlite3.OperationalError):
        Repository(db_path)

    # The failed migration must not be recorded, and its valid-looking first
    # statement must not have stuck around either (SQLite DDL is
    # transactional, so the whole script rolled back together).
    conn = sqlite3.connect(db_path)
    try:
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()]
        assert 999 not in versions
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='ok'"
            ).fetchall()
        ]
        assert tables == []
    finally:
        conn.close()

    # Retrying with the patch undone succeeds -- a fresh open just runs the
    # real 1-3 cleanly, with 999 never having existed.
    monkeypatch.undo()
    repo = Repository(db_path)
    repo.close()
