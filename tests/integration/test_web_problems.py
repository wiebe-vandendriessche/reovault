"""Problems page: retry must keep looking like a problem row, and the
manual-run activity card's Stop button (see `Archiver.request_cancel`)."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from reovault.fleet import Fleet
from reovault.models import ErrorClass, RemoteRecording
from reovault.providers.fake import FakeProvider
from reovault.providers.gateway import GatewaySupervisor
from reovault.web import security
from reovault.web.app import create_app
from reovault.web.auth import write_password_file
from tests.integration.conftest import extract_body_csrf


def _seed_failed_recording(env) -> int:
    rec = RemoteRecording(
        remote_name="0120260918225912",
        start_utc=datetime(2026, 9, 18, 20, 59, 12, tzinfo=UTC),
        end_utc=None,
        duration_s=None,
        rec_type="md,vehicle",
        stream="mainStream",
        remote_size=21905004,
        raw_metadata={"fileName": "0120260918225912"},
    )
    rid, _ = env.repository.discover_recording(device_id=env.device_id, channel=0, recording=rec)
    env.repository.transition_downloading(rid)
    env.repository.mark_failed(
        rid, error="exit code -2", error_class=ErrorClass.DEVICE, next_attempt_at=None
    )
    return rid


def test_retrying_from_problems_keeps_problem_row_shape(env, auth_client):
    """Regression: retrying used to swap in fragments/rec_row.html (the
    Footage day view's row, meant for a *different* page) and only ever
    replaced the <article>, leaving the trailing error <details> orphaned
    beside it -- a broken hybrid row showing a local time, type pills, and
    a stale "exit code -2" detail no longer attached to anything that
    explains it."""
    rid = _seed_failed_recording(env)
    client = auth_client
    csrf = extract_body_csrf(client.get("/problems").text)

    r = client.post(
        f"/recordings/{rid}/retry", params={"context": "problem"}, headers={"X-CSRF-Token": csrf}
    )

    assert r.status_code == 200
    assert f'id="rec-{rid}"' in r.text
    assert "Queued for the next run" in r.text
    # Not the Footage row shape: no bare local-time span, no type pills.
    assert "rec-time" not in r.text
    # The row's own error explanation must still be present and attached
    # to the same returned fragment, not orphaned.
    assert "exit code -2" in r.text


def test_retrying_from_footage_still_collapses_to_a_plain_row(env, auth_client):
    """The Footage day view's own Retry button (in rec_player.html) has no
    `context=problem` and must keep getting rec_row.html back, exactly like
    its Close button already does."""
    rid = _seed_failed_recording(env)
    client = auth_client
    csrf = extract_body_csrf(client.get("/problems").text)

    r = client.post(f"/recordings/{rid}/retry", headers={"X-CSRF-Token": csrf})

    assert r.status_code == 200
    assert f'id="rec-{rid}"' in r.text
    assert "Queued for the next run" not in r.text


def test_stop_button_appears_while_a_run_is_in_progress(env, auth_client):
    client = auth_client

    # No run yet: no Stop button.
    r = client.get("/fragments/activity")
    assert "Stop" not in r.text

    env.repository.start_run(
        device_id=env.device_id,
        trigger="manual",
        window_from_utc=datetime(2026, 1, 1, tzinfo=UTC),
        window_to_utc=datetime(2026, 1, 2, tzinfo=UTC),
    )
    r = client.get("/fragments/activity")
    assert "Stop" in r.text
    assert 'hx-post="/actions/stop' in r.text  # durl() may append ?device=


def test_actions_stop_sets_the_archivers_cancel_flag(env, web_settings, web_device):
    """`/actions/stop` must reach the exact same Archiver instance the
    scheduler would be running the job on (see fleet.Fleet: one persistent
    Archiver per device, reused across every job), not a throwaway copy."""
    provider = FakeProvider()
    archiver = env.archiver(provider)
    fleet = Fleet(
        settings=web_settings,
        repository=env.repository,
        vault=env.vault,
        gateway=GatewaySupervisor(binary="reolink-cli"),
    )
    fleet.archivers[env.device_id] = archiver
    app = create_app(fleet, settings=web_settings, repository=env.repository)
    client = TestClient(app)

    write_password_file(web_settings.web.password_file, security.hash_password(b"x" * 12))
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/login").text).group(1)
    client.post("/login", data={"password": "x" * 12, "csrf_token": csrf, "next": "/"})

    env.repository.start_run(
        device_id=env.device_id,
        trigger="manual",
        window_from_utc=datetime(2026, 1, 1, tzinfo=UTC),
        window_to_utc=datetime(2026, 1, 2, tzinfo=UTC),
    )
    assert not archiver.cancel_requested.is_set()

    csrf2 = extract_body_csrf(client.get("/").text)
    r = client.post("/actions/stop", headers={"X-CSRF-Token": csrf2})

    assert r.status_code == 200
    assert archiver.cancel_requested.is_set()


def test_actions_stop_is_a_no_op_with_nothing_running(env, auth_client):
    client = auth_client
    csrf = extract_body_csrf(client.get("/").text)

    r = client.post("/actions/stop", headers={"X-CSRF-Token": csrf})

    assert r.status_code == 200
