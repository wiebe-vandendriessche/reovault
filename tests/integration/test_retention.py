"""Retention: age and size limits prune the oldest archived clips first,
keep the row (so nothing is re-downloaded), survive a crash mid-prune, and
the dashboard asks for confirmation before a policy that deletes anything."""

from datetime import UTC, datetime, timedelta

import pytest

from reovault import retention
from reovault.config import RetentionConfig
from reovault.providers.fake import FakeProvider, ScriptedRecording
from tests.integration.conftest import make_recording

DAY0 = datetime(2026, 4, 1, 12, tzinfo=UTC)
WINDOW_FROM = DAY0 - timedelta(days=1)
WINDOW_TO = DAY0 + timedelta(days=30)


@pytest.fixture(autouse=True)
def byte_sized_gb(monkeypatch):
    """1 "GB" = 1 byte, so a cap can be expressed in a test's tiny clips."""
    monkeypatch.setattr(retention, "GB", 1)


def _provider(*days: int) -> FakeProvider:
    recs = []
    for d in days:
        start = DAY0 + timedelta(days=d)
        rec = make_recording(f"clip{d}.mp4", start, size=100)
        recs.append(ScriptedRecording(rec, bytes([d]) * 100))
    return FakeProvider(recordings=recs)


def _states(env) -> dict[str, str]:
    rows = env.repository.conn.execute("SELECT remote_name, state FROM recordings").fetchall()
    return {r["remote_name"]: r["state"] for r in rows}


def _archive(env, provider: FakeProvider, archiver=None):
    archiver = archiver or env.archiver(provider)
    archiver.run(trigger="manual", from_utc=WINDOW_FROM, to_utc=WINDOW_TO)
    return archiver


def _clip_bytes(env) -> int:
    row = env.repository.conn.execute(
        "SELECT ciphertext_size FROM recordings WHERE state = 'archived' LIMIT 1"
    ).fetchone()
    return int(row["ciphertext_size"])


def test_no_limits_is_a_no_op(env):
    _archive(env, _provider(0, 1, 2))
    result = retention.enforce(env.repository, env.vault, RetentionConfig())
    assert result.count == 0
    assert set(_states(env).values()) == {"archived"}


def test_age_limit_prunes_only_clips_older_than_n_days(env):
    _archive(env, _provider(0, 1, 5))
    now = DAY0 + timedelta(days=5, hours=1)

    result = retention.enforce(env.repository, env.vault, RetentionConfig(max_age_days=2), now=now)

    assert result.count == 2
    assert _states(env) == {"clip0.mp4": "pruned", "clip1.mp4": "pruned", "clip5.mp4": "archived"}


def test_size_cap_prunes_oldest_first_across_devices(env):
    _archive(env, _provider(0, 2))
    other, _ = env.archiver_for("garden", _provider(1, 3))
    _archive(env, None, other)
    clip = _clip_bytes(env)

    retention.enforce(env.repository, env.vault, RetentionConfig(max_vault_gb=2 * clip))

    # day 0 (doorbell) and day 1 (garden) are the two oldest overall.
    pruned = env.repository.conn.execute(
        "SELECT start_utc FROM recordings WHERE state = 'pruned' ORDER BY start_utc"
    ).fetchall()
    assert [r["start_utc"][:10] for r in pruned] == ["2026-04-01", "2026-04-02"]
    assert env.repository.archived_ciphertext_total() <= 2 * clip
    assert len(env.vault.list_vault_paths()) == 2


def test_pruned_row_keeps_history_and_loses_its_file(env):
    _archive(env, _provider(0))
    path = env.repository.conn.execute("SELECT vault_path FROM recordings").fetchone()[0]

    retention.enforce(env.repository, env.vault, RetentionConfig(max_vault_gb=1))

    row = env.repository.conn.execute("SELECT * FROM recordings").fetchone()
    assert row["state"] == "pruned"
    assert row["vault_path"] is None
    assert row["plaintext_sha256"] is not None
    assert not (env.tmp_path / "vault" / path).exists()


def test_crash_between_file_delete_and_row_update_is_finished_next_time(env):
    _archive(env, _provider(0, 1))
    oldest = env.repository.oldest_archived(limit=1)[0]
    env.vault.delete(oldest.vault_path)  # the crash: file gone, row still archived

    retention.enforce(env.repository, env.vault, RetentionConfig(max_vault_gb=_clip_bytes(env)))

    assert _states(env) == {"clip0.mp4": "pruned", "clip1.mp4": "archived"}


def test_preview_counts_without_deleting(env):
    _archive(env, _provider(0, 1, 2))
    clip = _clip_bytes(env)

    result = retention.preview(env.repository, RetentionConfig(max_vault_gb=clip))

    assert (result.count, result.bytes) == (2, 2 * clip)
    assert set(_states(env).values()) == {"archived"}


def test_archive_run_stays_under_cap_and_never_redownloads_pruned_clips(env):
    provider = _provider(0, 1, 2, 3)
    archiver = env.archiver(provider)
    policy = RetentionConfig(max_vault_gb=1)  # placeholder, set once sizes are known
    archiver.retention = lambda: retention.enforce(env.repository, env.vault, policy)

    _archive(env, provider, archiver)
    clip = env.repository.conn.execute("SELECT MAX(ciphertext_size) FROM recordings").fetchone()[0]
    assert set(_states(env).values()) == {"pruned"}  # 1-byte cap prunes everything

    policy.max_vault_gb = 2 * clip
    fetched = provider.fetch_calls
    _archive(env, provider, archiver)  # the camera still lists all four

    assert provider.fetch_calls == fetched
    assert set(_states(env).values()) == {"pruned"}


def test_archive_run_with_cap_keeps_the_newest(env):
    provider = _provider(0, 1, 2, 3)
    archiver = env.archiver(provider)
    archiver.run(trigger="manual", from_utc=WINDOW_FROM, to_utc=DAY0)  # day 0 only
    clip = _clip_bytes(env)

    policy = RetentionConfig(max_vault_gb=2 * clip)
    archiver.retention = lambda: retention.enforce(env.repository, env.vault, policy)
    _archive(env, provider, archiver)

    assert _states(env) == {
        "clip0.mp4": "pruned",
        "clip1.mp4": "pruned",
        "clip2.mp4": "archived",
        "clip3.mp4": "archived",
    }
    assert len(env.vault.list_vault_paths()) == 2


def test_reconcile_never_readopts_a_file_for_a_pruned_row(env):
    archiver = _archive(env, _provider(0))
    row = env.repository.oldest_archived(limit=1)[0]
    env.repository.mark_pruned(row.id)  # row pruned but file left behind

    archiver.reconcile()

    assert _states(env) == {"clip0.mp4": "pruned"}
    assert env.vault.list_vault_paths() == []


def test_pruned_clips_are_not_backlog_for_the_coverage_alarm(env):
    _archive(env, _provider(0))
    retention.enforce(env.repository, env.vault, RetentionConfig(max_vault_gb=1))

    assert env.repository.oldest_unarchived_start_utc(env.device_id) is None


def test_retention_save_requires_confirmation_when_it_would_delete(env, auth_client):
    _archive(env, _provider(0, 1))
    body = {"max_age_days": None, "max_vault_gb": 1}

    r = auth_client.put("/api/v1/retention", json=body)
    assert r.status_code == 200
    assert r.json()["saved"] is False
    confirm = r.json()["confirm"]
    assert confirm["count"] == 2
    assert confirm["bytes"] > 0
    stored = env.repository.get_setting(retention.RETENTION_SETTING_KEY)
    assert RetentionConfig.model_validate_json(stored).max_vault_gb is None

    # A confirm token for different values does not count.
    r = auth_client.put(
        "/api/v1/retention", json={**body, "max_vault_gb": 2, "confirm": confirm["token"]}
    )
    assert r.json()["saved"] is False
    assert r.json()["confirm"] is not None

    r = auth_client.put("/api/v1/retention", json={**body, "confirm": confirm["token"]})
    assert r.json()["saved"] is True
    assert r.json()["confirm"] is None
    assert r.json()["retention"]["max_vault_gb"] == 1
    stored = env.repository.get_setting(retention.RETENTION_SETTING_KEY)
    assert RetentionConfig.model_validate_json(stored).max_vault_gb == 1
    assert auth_client.get("/api/v1/retention").json()["max_vault_gb"] == 1


def test_retention_save_without_impact_saves_directly(env, auth_client):
    r = auth_client.put("/api/v1/retention", json={"max_age_days": 90, "max_vault_gb": None})
    assert r.json()["saved"] is True
    assert r.json()["retention"]["max_age_days"] == 90


def test_retention_rejects_zero(env, auth_client):
    r = auth_client.put("/api/v1/retention", json={"max_age_days": 0})
    assert r.status_code == 422


def test_retention_is_read_only_when_env_pinned(env, auth_client, monkeypatch):
    monkeypatch.setenv("REOVAULT_RETENTION__MAX_AGE_DAYS", "30")

    assert auth_client.get("/api/v1/retention").json()["env_pinned"] is True
    r = auth_client.put("/api/v1/retention", json={"max_age_days": 1})

    assert r.status_code == 409
    assert "environment variables" in r.json()["detail"]
    assert env.repository.get_setting(retention.RETENTION_SETTING_KEY) is None
