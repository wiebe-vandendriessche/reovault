"""Live run progress: counters land on the still-running run row before
`finish_run`, writes are throttled, the hook fires, and a broken hook never
fails the run."""

from datetime import UTC, datetime, timedelta

import pytest

from reovault import archiver as archiver_mod
from reovault.providers.fake import FakeProvider, ScriptedRecording
from tests.integration.conftest import make_recording

T0 = datetime(2026, 4, 17, 10, tzinfo=UTC)


def _provider(n: int, on_third=None) -> FakeProvider:
    recs = []
    for i in range(n):
        rec = make_recording(f"c{i}.mp4", T0 + timedelta(minutes=i), size=10)
        recs.append(ScriptedRecording(rec, bytes([i]) * 10, on_fetch=on_third if i == 2 else None))
    return FakeProvider(recordings=recs)


def _run(archiver):
    return archiver.run(
        trigger="manual", from_utc=T0 - timedelta(hours=1), to_utc=T0 + timedelta(hours=1)
    )


def test_counters_are_visible_mid_run(env, monkeypatch):
    monkeypatch.setattr(archiver_mod, "_PROGRESS_INTERVAL_SECS", 0)
    seen = {}

    def snapshot():
        seen["row"] = env.repository.running_run(env.device_id)
        return None  # then fetch normally

    _run(env.archiver(_provider(5, on_third=snapshot)))
    row = seen["row"]
    assert row is not None and row.finished_at is None
    assert row.discovered == 5
    assert row.downloaded == 2  # the two clips before the third were already reported


def test_hook_fires_with_the_device_id(env, monkeypatch):
    monkeypatch.setattr(archiver_mod, "_PROGRESS_INTERVAL_SECS", 0)
    calls = []
    archiver = env.archiver(_provider(3))
    archiver.on_progress = calls.append
    _run(archiver)
    assert calls and set(calls) == {env.device_id}
    assert len(calls) == 1 + 3  # once when discovered, once per clip


def test_reports_are_throttled(env, monkeypatch):
    monkeypatch.setattr(archiver_mod, "_PROGRESS_INTERVAL_SECS", 3600)
    calls = []
    archiver = env.archiver(_provider(4))
    archiver.on_progress = calls.append
    _run(archiver)
    assert len(calls) == 1  # only the forced report once the clips are discovered


def test_a_failing_hook_never_fails_the_run(env, monkeypatch):
    monkeypatch.setattr(archiver_mod, "_PROGRESS_INTERVAL_SECS", 0)

    def boom(_device_id):
        raise RuntimeError("dashboard went away")

    archiver = env.archiver(_provider(3))
    archiver.on_progress = boom
    result = _run(archiver)
    assert result.downloaded == 3
    assert env.repository.last_finished_run(env.device_id).outcome == "success"


def test_progress_never_overwrites_a_finished_run(env):
    run_id = env.repository.start_run(
        device_id=env.device_id, trigger="manual", window_from_utc=T0, window_to_utc=T0
    )
    env.repository.finish_run(run_id, outcome="success", downloaded=7)
    env.repository.update_run_progress(
        run_id, discovered=1, downloaded=1, skipped_dup=0, failed=0, bytes_archived=1
    )
    assert env.repository.get_run(run_id).downloaded == 7


@pytest.fixture
def wired_app(env, web_settings, web_device, web_password_hash):
    from tests.integration.conftest import make_web_app

    return make_web_app(env, web_settings, web_device)


def test_create_app_wires_the_hook_to_an_activity_hint(wired_app, env, monkeypatch):
    seen = []
    monkeypatch.setattr(wired_app.state.rv.bus, "publish", lambda t, **d: seen.append((t, d)))
    archiver = wired_app.state.rv.fleet.get(env.device_id)
    assert archiver.on_progress is not None
    archiver.on_progress(env.device_id)
    assert seen == [("activity", {"device_id": env.device_id})]
