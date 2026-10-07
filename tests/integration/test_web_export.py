"""Bulk export: the zip holds exactly the archived clips of the range, each
decrypting back to the original bytes; ranges and sizes are capped; one
export at a time; a corrupt clip fails the transfer instead of shipping a
short file."""

import hashlib
import io
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from reovault.providers.fake import FakeProvider, ScriptedRecording
from reovault.web.api import export as export_api
from tests.integration.conftest import login, make_recording, make_web_app

DAY = datetime(2026, 4, 17, 10, tzinfo=UTC)  # 12:00 in Europe/Brussels


def _clips(n: int, size: int = 100) -> list[ScriptedRecording]:
    return [
        ScriptedRecording(
            make_recording(f"clip{i}.mp4", DAY + timedelta(minutes=i), size=size),
            bytes([i + 1]) * size,
        )
        for i in range(n)
    ]


@pytest.fixture
def archived(env):
    clips = _clips(3)
    provider = FakeProvider(recordings=clips)
    env.archiver(provider).run(
        trigger="manual", from_utc=DAY - timedelta(hours=1), to_utc=DAY + timedelta(hours=1)
    )
    return {c.recording.remote_name: c.content for c in clips}


@pytest.fixture
def client(env, web_settings, web_device, web_password_hash):
    web_settings.web.stream_chunk_bytes = 16  # matches the vault frame size in `env`
    return login(TestClient(make_web_app(env, web_settings, web_device)))


def test_preview_counts_archived_clips_in_the_local_range(client, archived):
    body = client.get("/api/v1/export/preview", params={"from_": "2026-04-17", "to": "2026-04-17"})
    assert body.json() == {"count": 3, "bytes": 300}


def test_zip_contents_decrypt_to_the_original_bytes(client, archived):
    response = client.get("/api/v1/export", params={"from_": "2026-04-17", "to": "2026-04-17"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"] == (
        'attachment; filename="reovault_doorbell_20260417-20260417.zip"'
    )
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert zf.testzip() is None
        names = zf.namelist()
        assert len(names) == 3
        assert all(n.startswith("2026-04-17/12") for n in names)  # camera-local
        got = {hashlib.sha256(zf.read(n)).hexdigest() for n in names}
    want = {hashlib.sha256(b).hexdigest() for b in archived.values()}
    assert got == want


def test_range_outside_any_clip_is_a_422_not_an_empty_zip(client, archived):
    response = client.get("/api/v1/export", params={"from_": "2026-04-18", "to": "2026-04-18"})
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("from_", "to"),
    [("2026-04-18", "2026-04-17"), ("2026-01-01", "2026-03-01"), ("17/04/2026", "2026-04-17")],
)
def test_bad_ranges_are_rejected(client, from_, to):
    response = client.get("/api/v1/export/preview", params={"from_": from_, "to": to})
    assert response.status_code == 422


def test_clip_cap(client, archived, monkeypatch):
    monkeypatch.setattr(export_api, "MAX_CLIPS", 2)
    response = client.get(
        "/api/v1/export/preview", params={"from_": "2026-04-17", "to": "2026-04-17"}
    )
    assert response.status_code == 422


def test_one_export_at_a_time(client, archived):
    assert export_api._export_lock.acquire(blocking=False)
    try:
        response = client.get("/api/v1/export", params={"from_": "2026-04-17", "to": "2026-04-17"})
        assert response.status_code == 409
    finally:
        export_api._export_lock.release()


def test_slot_is_freed_after_an_export(client, archived):
    for _ in range(2):
        response = client.get("/api/v1/export", params={"from_": "2026-04-17", "to": "2026-04-17"})
        assert response.status_code == 200
    assert not export_api._export_lock.locked()


def test_corrupt_clip_fails_the_transfer(client, archived, env):
    row = env.repository.conn.execute(
        "SELECT vault_path FROM recordings WHERE state='archived' LIMIT 1"
    ).fetchone()
    path = env.vault.vault_dir / row["vault_path"]
    data = bytearray(path.read_bytes())
    data[-5] ^= 0xFF
    path.write_bytes(bytes(data))
    with pytest.raises(Exception):  # noqa: B017 - any transport-level failure is the point
        client.get("/api/v1/export", params={"from_": "2026-04-17", "to": "2026-04-17"})
    assert not export_api._export_lock.locked()


def test_export_requires_auth(env, web_settings, web_device, web_password_hash):
    anon = TestClient(make_web_app(env, web_settings, web_device))
    response = anon.get("/api/v1/export", params={"from_": "2026-04-17", "to": "2026-04-17"})
    assert response.status_code == 401
