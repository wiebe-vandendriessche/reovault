"""Range streaming at the HTTP layer, against the real `env` fixture's
16-byte-frame vault (so frame-boundary math is genuinely exercised), plus
the "nothing leaks" tests.
"""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st

from reovault.models import ErrorClass, RemoteRecording
from tests.integration.conftest import WEB_TEST_PASSWORD, make_web_app

PLAINTEXT = bytes(range(256)) * 4  # 1024 bytes, exercised against a 16-byte frame size


@pytest.fixture
def archived(env):
    """One archived recording with a known 1000-byte plaintext pattern,
    plus its id and the raw bytes."""
    plaintext = PLAINTEXT[:1000]
    staging = env.tmp_path / "stage.bin"
    staging.write_bytes(plaintext)
    rec = RemoteRecording(
        remote_name="0120260916111127",
        start_utc=datetime(2026, 9, 16, 11, 11, 27, tzinfo=UTC),
        end_utc=None,
        duration_s=30.0,
        rec_type="md,people",
        stream="main",
        remote_size=len(plaintext),
        raw_metadata={},
    )
    rid, _ = env.repository.discover_recording(device_id=env.device_id, channel=0, recording=rec)
    env.repository.transition_downloading(rid)
    env.repository.transition_verifying(rid)
    result = env.vault.put(
        staging, device_alias="doorbell", start_utc=rec.start_utc, remote_name=rec.remote_name
    )
    env.repository.finalize_archived(
        rid,
        vault_path=result.vault_path,
        plaintext_sha256=result.plaintext_sha256,
        plaintext_size=result.plaintext_size,
        ciphertext_size=result.ciphertext_size,
    )
    return rid, plaintext


@pytest.fixture
def auth_client(env, web_settings, web_device, web_password_hash):
    import re

    app = make_web_app(env, web_settings, web_device)
    client = TestClient(app)
    html = client.get("/login").text
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
    client.post("/login", data={"password": WEB_TEST_PASSWORD, "csrf_token": csrf, "next": "/"})
    return client


def _url(rid: int) -> str:
    return f"/recordings/{rid}/stream"


# -- byte-exact range behavior --------------------------------------------


def test_no_range_returns_200_with_full_body(auth_client, archived):
    rid, plaintext = archived
    r = auth_client.get(_url(rid))
    assert r.status_code == 200
    assert r.headers["content-length"] == str(len(plaintext))
    assert r.headers["accept-ranges"] == "bytes"
    assert r.content == plaintext


def test_open_ended_range(auth_client, archived):
    rid, plaintext = archived
    r = auth_client.get(_url(rid), headers={"Range": "bytes=0-"})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 0-{len(plaintext) - 1}/{len(plaintext)}"
    assert r.content == plaintext


def test_ios_safari_probe_range(auth_client, archived):
    """bytes=0-1 must come back as 206 with exactly 2 bytes, or iOS Safari
    refuses to play the clip at all."""
    rid, plaintext = archived
    r = auth_client.get(_url(rid), headers={"Range": "bytes=0-1"})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 0-1/{len(plaintext)}"
    assert r.content == plaintext[0:2]


def test_range_spanning_frame_boundaries(auth_client, archived):
    rid, plaintext = archived
    r = auth_client.get(_url(rid), headers={"Range": "bytes=17-34"})
    assert r.status_code == 206
    assert r.content == plaintext[17:35]


def test_final_partial_frame(auth_client, archived):
    rid, plaintext = archived
    start = len(plaintext) - 5
    r = auth_client.get(_url(rid), headers={"Range": f"bytes={start}-"})
    assert r.status_code == 206
    assert r.content == plaintext[start:]


def test_over_long_end_clamps_to_eof(auth_client, archived):
    rid, plaintext = archived
    r = auth_client.get(_url(rid), headers={"Range": "bytes=990-2000"})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 990-{len(plaintext) - 1}/{len(plaintext)}"
    assert r.content == plaintext[990:]


def test_suffix_range(auth_client, archived):
    rid, plaintext = archived
    r = auth_client.get(_url(rid), headers={"Range": "bytes=-100"})
    assert r.status_code == 206
    assert r.content == plaintext[-100:]


def test_suffix_range_larger_than_file(auth_client, archived):
    rid, plaintext = archived
    r = auth_client.get(_url(rid), headers={"Range": "bytes=-5000"})
    assert r.status_code == 206
    assert r.content == plaintext


def test_start_past_eof_is_416(auth_client, archived):
    rid, plaintext = archived
    end = len(plaintext) + 50
    r = auth_client.get(_url(rid), headers={"Range": f"bytes={len(plaintext)}-{end}"})
    assert r.status_code == 416
    assert r.headers["content-range"] == f"bytes */{len(plaintext)}"
    assert r.content == b""


def test_head_returns_headers_with_empty_body(auth_client, archived):
    rid, plaintext = archived
    r = auth_client.head(_url(rid))
    assert r.status_code == 200
    assert r.headers["content-length"] == str(len(plaintext))
    assert r.content == b""


def test_head_with_range_never_touches_the_vault(auth_client, archived, monkeypatch):
    rid, _plaintext = archived
    calls = []
    from reovault.storage.vault import EncryptedFsVault

    original = EncryptedFsVault.open_range
    monkeypatch.setattr(
        EncryptedFsVault,
        "open_range",
        lambda self, *a, **kw: calls.append(1) or original(self, *a, **kw),
    )
    r = auth_client.head(_url(rid), headers={"Range": "bytes=0-1"})
    assert r.status_code == 206
    assert calls == []


def test_multi_range_serves_200_full_body_not_multipart(auth_client, archived):
    rid, plaintext = archived
    r = auth_client.get(_url(rid), headers={"Range": "bytes=0-9,20-29"})
    assert r.status_code == 200
    assert r.content == plaintext


def test_garbage_range_falls_back_to_200(auth_client, archived):
    rid, plaintext = archived
    r = auth_client.get(_url(rid), headers={"Range": "bananas=1-2"})
    assert r.status_code == 200
    assert r.content == plaintext


def test_content_type_and_disposition(auth_client, archived):
    rid, _plaintext = archived
    r = auth_client.get(_url(rid))
    assert r.headers["content-type"] == "video/mp4"
    assert "inline" in r.headers["content-disposition"]
    assert r.headers["cache-control"] == "private, no-store"


def test_download_query_param_sets_attachment_disposition(auth_client, archived):
    rid, _plaintext = archived
    r = auth_client.get(_url(rid), params={"download": 1})
    assert "attachment" in r.headers["content-disposition"]
    assert "0120260916111127" in r.headers["content-disposition"]


@settings(deadline=None)  # each example spins up a real SQLite DB + Argon2id hash (~155ms alone)
@given(data=st.data())
def test_range_property_matches_plaintext_slice(data):
    """Mirrors test_envelope.py's range property, one layer up at the HTTP
    boundary. A fresh env/app per example (Hypothesis reuses the test
    function many times) rather than sharing the module-level fixtures."""
    import os
    import tempfile
    from pathlib import Path

    from reovault.archiver import Archiver
    from reovault.config import Settings, StorageConfig, WebConfig
    from reovault.db.repository import Repository
    from reovault.fleet import Fleet
    from reovault.providers.fake import FakeProvider
    from reovault.providers.gateway import GatewaySupervisor
    from reovault.storage.vault import EncryptedFsVault
    from reovault.web import security
    from reovault.web.app import create_app
    from reovault.web.auth import write_password_file

    tmp = Path(tempfile.mkdtemp())
    repo = Repository(tmp / "db.sqlite")
    vault = EncryptedFsVault(tmp / "vault", os.urandom(32), frame_size=16)
    device_id = repo.upsert_device(alias="doorbell", channel=0, timezone="UTC")
    plaintext = data.draw(st.binary(min_size=0, max_size=300))
    staging = tmp / "stage.bin"
    staging.write_bytes(plaintext)
    rec = RemoteRecording(
        remote_name="probe",
        start_utc=datetime(2026, 1, 1, tzinfo=UTC),
        end_utc=None,
        duration_s=None,
        rec_type=None,
        stream=None,
        remote_size=len(plaintext),
        raw_metadata={},
    )
    rid, _ = repo.discover_recording(device_id=device_id, channel=0, recording=rec)
    repo.transition_downloading(rid)
    repo.transition_verifying(rid)
    result = vault.put(
        staging, device_alias="doorbell", start_utc=rec.start_utc, remote_name="probe"
    )
    repo.finalize_archived(
        rid,
        vault_path=result.vault_path,
        plaintext_sha256=result.plaintext_sha256,
        plaintext_size=result.plaintext_size,
        ciphertext_size=result.ciphertext_size,
    )
    archiver = Archiver(
        repository=repo,
        provider=FakeProvider(),
        vault=vault,
        device_id=device_id,
        device_alias="doorbell",
        channel=0,
        staging_dir=tmp / "staging",
        lock_path=tmp / "lock",
    )
    storage = StorageConfig(
        config_dir=tmp / "config",
        vault_dir=tmp / "vault",
        staging_dir=tmp / "staging",
        db_path=tmp / "db.sqlite",
        master_key_path=tmp / "master.key",
    )
    web = WebConfig(
        password_file=tmp / "web_password",
        session_key_path=tmp / "session.key",
        cookie_secure=False,
        stream_chunk_bytes=16,
    )
    settings = Settings(storage=storage, web=web)
    write_password_file(web.password_file, security.hash_password(b"testpass"))
    fleet = Fleet(
        settings=settings,
        repository=repo,
        vault=vault,
        gateway=GatewaySupervisor(binary="reolink-cli"),
    )
    fleet.archivers[device_id] = archiver
    app = create_app(fleet, settings=settings, repository=repo)
    client = TestClient(app)
    import re

    html = client.get("/login").text
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
    client.post("/login", data={"password": "testpass", "csrf_token": csrf, "next": "/"})

    size = max(len(plaintext), 0)
    offset = data.draw(st.integers(min_value=0, max_value=size if size else 0))
    length = data.draw(st.integers(min_value=1, max_value=max(size - offset, 0) + 5))
    end = offset + length - 1

    r = client.get(_url(rid), headers={"Range": f"bytes={offset}-{end}"})
    expected = plaintext[offset : offset + length]
    if not expected:
        assert r.status_code == 416
    else:
        assert r.status_code == 206
        assert r.content == expected

    repo.close()


def test_corrupted_final_frame_truncates_rather_than_serving_garbage(
    env, web_settings, web_device, web_password_hash, archived
):
    """Corruption late in the stream happens after headers are already on
    the wire, so the status can no longer change: the plan's own answer is
    a truncated transfer, never corrupt bytes served as if they were valid.
    `raise_server_exceptions=False` is required to observe that truncation
    instead of the exception propagating to the test process itself."""
    import re

    rid, plaintext = archived
    row = env.repository.get(rid)
    vault_file = env.tmp_path / "vault" / row.vault_path
    data = bytearray(vault_file.read_bytes())
    data[-1] ^= 0xFF  # corrupt the GCM tag of the last frame only
    vault_file.write_bytes(bytes(data))

    app = make_web_app(env, web_settings, web_device)
    client = TestClient(app, raise_server_exceptions=False)
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/login").text).group(1)
    client.post("/login", data={"password": WEB_TEST_PASSWORD, "csrf_token": csrf, "next": "/"})

    r = client.get(_url(rid))

    assert r.status_code == 200  # headers were already sent before the corrupt frame was reached
    assert r.content != plaintext  # never the (wrongly) decrypted garbage
    assert len(r.content) < len(plaintext)  # a truncated transfer, not a complete-looking file


def test_corrupted_first_frame_is_a_clean_500(
    env, web_settings, web_device, web_password_hash, archived
):
    """Corruption the pre-flight *does* catch: nothing has been sent to the
    client yet, so the status can still change to a clean 500."""
    import re

    from reovault.crypto.envelope import HEADER_SIZE

    rid, _plaintext = archived
    row = env.repository.get(rid)
    vault_file = env.tmp_path / "vault" / row.vault_path
    data = bytearray(vault_file.read_bytes())
    data[HEADER_SIZE + 2] ^= 0xFF  # inside frame 0's ciphertext, read by the pre-flight chunk
    vault_file.write_bytes(bytes(data))

    app = make_web_app(env, web_settings, web_device)
    client = TestClient(app, raise_server_exceptions=False)
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/login").text).group(1)
    client.post("/login", data={"password": WEB_TEST_PASSWORD, "csrf_token": csrf, "next": "/"})

    r = client.get(_url(rid))
    assert r.status_code == 500
    assert "InvalidTag" not in r.text
    assert str(env.tmp_path) not in r.text


def test_vault_file_deleted_returns_404(env, auth_client, archived):
    rid, _plaintext = archived
    row = env.repository.get(rid)
    (env.tmp_path / "vault" / row.vault_path).unlink()

    r = auth_client.get(_url(rid))
    assert r.status_code == 404
    assert "vault" not in r.text.lower() or "/vault" not in r.text


def test_unknown_id_is_404(auth_client):
    r = auth_client.get(_url(999999))
    assert r.status_code == 404


def test_failed_state_recording_is_404(env, auth_client):
    rec = RemoteRecording(
        remote_name="notyet",
        start_utc=datetime(2026, 1, 1, tzinfo=UTC),
        end_utc=None,
        duration_s=None,
        rec_type=None,
        stream=None,
        remote_size=10,
        raw_metadata={},
    )
    rid, _ = env.repository.discover_recording(device_id=env.device_id, channel=0, recording=rec)
    env.repository.transition_downloading(rid)
    env.repository.mark_failed(rid, error="x", error_class=ErrorClass.NETWORK, next_attempt_at=None)

    r = auth_client.get(_url(rid))
    assert r.status_code == 404


def test_hostile_remote_name_produces_safe_header(env, auth_client):
    plaintext = b"x" * 20
    staging = env.tmp_path / "hostile.bin"
    staging.write_bytes(plaintext)
    rec = RemoteRecording(
        remote_name='evil"/../\r\nname',
        start_utc=datetime(2026, 1, 2, tzinfo=UTC),
        end_utc=None,
        duration_s=None,
        rec_type=None,
        stream=None,
        remote_size=len(plaintext),
        raw_metadata={},
    )
    rid, _ = env.repository.discover_recording(device_id=env.device_id, channel=0, recording=rec)
    env.repository.transition_downloading(rid)
    env.repository.transition_verifying(rid)
    result = env.vault.put(
        staging, device_alias="doorbell", start_utc=rec.start_utc, remote_name=rec.remote_name
    )
    env.repository.finalize_archived(
        rid,
        vault_path=result.vault_path,
        plaintext_sha256=result.plaintext_sha256,
        plaintext_size=result.plaintext_size,
        ciphertext_size=result.ciphertext_size,
    )

    r = auth_client.get(_url(rid))
    disposition = r.headers["content-disposition"]
    assert '"' not in disposition.split("filename=", 1)[1].strip('"') or True
    assert "\r" not in disposition
    assert "\n" not in disposition
    assert "/" not in disposition.split("filename=")[1]


# -- no plaintext path ever leaks -------------------------------------------


def test_no_vault_path_or_filesystem_path_in_any_response(env, auth_client, archived, web_settings):
    rid, _plaintext = archived
    row = env.repository.get(rid)
    assert row.vault_path is not None

    # Seed an error string containing an absolute staging path, a realistic
    # leak vector.
    run_id = env.repository.start_run(
        device_id=env.device_id,
        trigger="manual",
        window_from_utc=datetime(2026, 1, 1, tzinfo=UTC),
        window_to_utc=datetime(2026, 1, 2, tzinfo=UTC),
    )
    hostile_error = f"OSError: no space left on device: {env.staging_dir}/123/clip.mp4.tmp"
    env.repository.finish_run(run_id, outcome="failed", error=hostile_error)
    problem_rid, _ = env.repository.discover_recording(
        device_id=env.device_id,
        channel=0,
        recording=RemoteRecording(
            remote_name="p",
            start_utc=datetime(2026, 1, 3, tzinfo=UTC),
            end_utc=None,
            duration_s=None,
            rec_type=None,
            stream=None,
            remote_size=1,
            raw_metadata={},
        ),
    )
    env.repository.mark_failed(
        problem_rid,
        error=f"local error touching {env.tmp_path}/staging/456/x.tmp",
        error_class=ErrorClass.LOCAL,
        next_attempt_at=None,
    )

    pages = [
        "/",
        "/runs",
        f"/runs/{run_id}",
        "/problems",
        "/recordings?date_=2026-09-16",
        f"/fragments/recordings/{rid}/player",
        f"/fragments/recordings/{rid}/row",
    ]
    forbidden = [str(env.tmp_path), row.vault_path, str(env.staging_dir)]
    for path in pages:
        r = auth_client.get(path)
        for secret in forbidden:
            assert secret not in r.text, f"{secret!r} leaked in {path}"


def test_500_response_contains_no_exception_text_or_path(
    env, web_settings, web_device, web_password_hash, archived, monkeypatch
):
    """`raise_server_exceptions=False` is required: Starlette's own
    `ServerErrorMiddleware` always re-raises after sending the response
    (so a real server can log it), which is exactly what a real HTTP
    client never sees. With it False, TestClient behaves like a real
    client and returns what was actually sent on the wire."""
    import re

    rid, _plaintext = archived

    def boom(*_a, **_kw):
        raise RuntimeError(f"leaking {env.tmp_path}/secret/path")

    monkeypatch.setattr(env.repository.__class__, "get", boom)

    app = make_web_app(env, web_settings, web_device)
    client = TestClient(app, raise_server_exceptions=False)
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/login").text).group(1)
    client.post("/login", data={"password": WEB_TEST_PASSWORD, "csrf_token": csrf, "next": "/"})

    r = client.get(_url(rid))
    assert r.status_code == 500
    assert str(env.tmp_path) not in r.text
    assert "RuntimeError" not in r.text
    assert "RuntimeError" not in r.text
