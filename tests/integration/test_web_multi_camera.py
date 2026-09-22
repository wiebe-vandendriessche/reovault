"""`?device=` scoping through the actual web layer with more than one
camera in the Fleet. Every other web test uses a single-device app, so this
is the one place the `_device_ctx` "multiple devices" branches actually run.
"""

from fastapi.testclient import TestClient

from reovault.fleet import Fleet
from reovault.providers.fake import FakeProvider
from reovault.providers.gateway import GatewaySupervisor
from reovault.web.app import create_app
from tests.integration.conftest import login


def _two_device_app(env, web_settings):
    """A Fleet with `env`'s own device (alias "doorbell") plus a second,
    "cam-b", both FakeProvider-backed. Both get an explicit ReoVault name
    (never shown as its alias): `upsert_device` only sets fields that are
    non-null in the call (COALESCE against the existing row), so this is
    safe to call again on env's already-seeded device."""
    env.repository.upsert_device(
        alias=env.device_alias, channel=env.channel, timezone="Europe/Brussels", name="Doorbell"
    )
    archiver_b, device_b_id = env.archiver_for(
        "cam-b", FakeProvider(), timezone="America/New_York", name="Cam B"
    )
    fleet = Fleet(
        settings=web_settings,
        repository=env.repository,
        vault=env.vault,
        gateway=GatewaySupervisor(binary="reolink-cli"),
    )
    fleet.archivers[env.device_id] = env.archiver(FakeProvider())
    fleet.archivers[device_b_id] = archiver_b
    app = create_app(fleet, settings=web_settings, repository=env.repository)
    return app, device_b_id


def test_device_scoped_route_without_device_param_errors_with_multiple_devices(
    env, web_settings, web_password_hash
):
    app, _device_b_id = _two_device_app(env, web_settings)
    client = TestClient(app)
    login(client)

    r = client.get("/")

    assert r.status_code == 400
    assert "?device=" in r.text


def test_device_param_selects_the_right_device(env, web_settings, web_password_hash):
    app, device_b_id = _two_device_app(env, web_settings)
    client = TestClient(app)
    login(client)

    r_a = client.get("/", params={"device": env.device_id})
    r_b = client.get("/", params={"device": device_b_id})

    assert r_a.status_code == 200
    assert r_b.status_code == 200
    assert "Doorbell" in r_a.text
    assert "Cam B" in r_b.text


def test_unknown_device_param_is_404(env, web_settings, web_password_hash):
    app, _device_b_id = _two_device_app(env, web_settings)
    client = TestClient(app)
    login(client)

    r = client.get("/", params={"device": 999999})

    assert r.status_code == 404


def test_bad_device_param_is_400(env, web_settings, web_password_hash):
    app, _device_b_id = _two_device_app(env, web_settings)
    client = TestClient(app)
    login(client)

    r = client.get("/", params={"device": "not-a-number"})

    assert r.status_code == 400


def test_recordings_are_isolated_per_device_even_though_repository_is_shared(
    env, web_settings, web_password_hash
):
    """The whole point of `?device=`: browsing camera A never shows camera
    B's recordings, even though both share one Repository/vault."""
    from datetime import UTC, datetime

    from reovault.models import RemoteRecording

    app, device_b_id = _two_device_app(env, web_settings)

    rec_a = RemoteRecording(
        remote_name="clip-a",
        start_utc=datetime(2026, 4, 17, 12, tzinfo=UTC),
        end_utc=None,
        duration_s=None,
        rec_type="md",
        stream="main",
        remote_size=10,
        raw_metadata={},
    )
    env.repository.discover_recording(device_id=env.device_id, channel=0, recording=rec_a)
    rec_b = RemoteRecording(
        remote_name="clip-b",
        start_utc=datetime(2026, 4, 17, 12, tzinfo=UTC),
        end_utc=None,
        duration_s=None,
        rec_type="md",
        stream="main",
        remote_size=10,
        raw_metadata={},
    )
    env.repository.discover_recording(device_id=device_b_id, channel=0, recording=rec_b)

    client = TestClient(app)
    login(client)

    r_a = client.get("/fragments/day", params={"date_": "2026-04-17", "device": env.device_id})
    r_b = client.get("/fragments/day", params={"date_": "2026-04-17", "device": device_b_id})

    assert "1 clips" in r_a.text or "1 clip" in r_a.text
    assert "1 clips" in r_b.text or "1 clip" in r_b.text
    # Cross-check: each view's total must be exactly 1, not 2 (which would
    # mean the query leaked across devices).
    assert r_a.text.count('id="h-') == 1
    assert r_b.text.count('id="h-') == 1


def test_multi_device_error_page_links_to_each_device(env, web_settings, web_password_hash):
    """Regression: the old bare PlainTextResponse said 'pass ?device=<id>'
    but never said what id to pass, with no nav to get anywhere else
    either. Every registered device must now appear as a working link."""
    app, device_b_id = _two_device_app(env, web_settings)
    client = TestClient(app)
    login(client)

    r = client.get("/")

    assert r.status_code == 400
    assert f'href="/?device={env.device_id}"' in r.text
    assert f'href="/?device={device_b_id}"' in r.text
    assert "Doorbell" in r.text
    assert "Cam B" in r.text
    # The app shell (nav) must still be there, unlike the old bare text.
    assert "Devices" in r.text


def test_disabling_the_only_camera_shows_a_recoverable_page_not_bare_text(
    env, web_settings, web_password_hash
):
    """Regression: turning off the only configured camera used to leave
    every other tab showing a bare 503 text response with no nav and no
    link back to Devices -- the one place that could undo it."""
    from reovault.fleet import Fleet
    from reovault.providers.gateway import GatewaySupervisor
    from reovault.web.app import create_app

    # A device row exists (disabled), but the Fleet has no archiver for it,
    # exactly like `devices_toggle` leaves things after disabling the only
    # enabled camera.
    env.repository.set_device_enabled(env.device_id, False)
    fleet = Fleet(
        settings=web_settings,
        repository=env.repository,
        vault=env.vault,
        gateway=GatewaySupervisor(binary="reolink-cli"),
    )
    app = create_app(fleet, settings=web_settings, repository=env.repository)
    client = TestClient(app)
    login(client)

    r = client.get("/")

    assert r.status_code == 503
    assert "Devices" in r.text  # the nav, not just the word
    assert 'href="/devices"' in r.text


def test_device_scoped_nav_links_all_carry_the_selected_device(
    env, web_settings, web_password_hash
):
    """The regression this whole file exists for: every device-scoped
    href/hx-get in the navbar must carry `?device=`, or a second click from
    a `?device=2` page silently falls back into the "multiple devices, no
    ?device=" 400 branch (see `durl` in app.py)."""
    app, device_b_id = _two_device_app(env, web_settings)
    client = TestClient(app)
    login(client)

    r = client.get("/", params={"device": device_b_id})

    assert r.status_code == 200
    for href in (
        f"/?device={device_b_id}",
        f"/schedule?device={device_b_id}",
        f"/recordings?device={device_b_id}",
        f"/runs?device={device_b_id}",
        f"/problems?device={device_b_id}",
    ):
        assert href in r.text, href
    # Fleet-wide routes never carry a device.
    assert 'href="/devices"' in r.text


def test_selected_device_sticks_across_pages_that_carry_no_device_param(
    env, web_settings, web_password_hash
):
    """Picking a camera once (an explicit `?device=`) must survive a visit
    to a page that never carries `?device=` at all -- a run/recording
    detail page, or a bare `/schedule` link clicked from one -- rather than
    falling back into the "which camera?" picker every time (see
    `_cookie_device_id`)."""
    app, device_b_id = _two_device_app(env, web_settings)
    client = TestClient(app)
    login(client)

    r = client.get("/", params={"device": device_b_id})
    assert r.status_code == 200
    assert client.cookies.get("rv_device") == str(device_b_id)

    # No ?device= at all: the cookie, not the ambiguity page, must win.
    r2 = client.get("/schedule")
    assert r2.status_code == 200
    assert f"device={device_b_id}" in r2.text
