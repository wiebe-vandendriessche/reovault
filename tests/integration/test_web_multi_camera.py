"""`?device=` scoping through the actual web layer with more than one
camera in the Fleet. Every other web test uses a single-device app, so this
is the one place the `device_ctx` "multiple devices" branches actually run.
"""

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from reovault.fleet import Fleet
from reovault.models import RemoteRecording
from reovault.providers.fake import FakeProvider
from reovault.providers.gateway import GatewaySupervisor
from reovault.web.app import create_app
from tests.integration.conftest import login


def _two_device_app(env, web_settings):
    """A Fleet with `env`'s own device (alias "doorbell") plus a second,
    "cam-b", both FakeProvider-backed. Both get an explicit ReoVault name:
    `upsert_device` only sets fields that are non-null in the call (COALESCE
    against the existing row), so this is safe to call again on env's
    already-seeded device."""
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


def _client(app) -> TestClient:
    return login(TestClient(app))


def test_device_scoped_route_without_device_param_errors_with_multiple_devices(
    env, web_settings, web_password_hash
):
    app, _device_b_id = _two_device_app(env, web_settings)
    client = _client(app)

    r = client.get("/api/v1/health")

    assert r.status_code == 400
    assert "?device=" in r.json()["detail"]


def test_device_param_selects_the_right_device(env, web_settings, web_password_hash):
    app, device_b_id = _two_device_app(env, web_settings)
    client = _client(app)

    r_a = client.get("/api/v1/activity", params={"device": env.device_id})
    r_b = client.get("/api/v1/activity", params={"device": device_b_id})

    assert r_a.status_code == 200
    assert r_b.status_code == 200
    assert r_a.json()["timezone"] == "Europe/Brussels"
    assert r_b.json()["timezone"] == "America/New_York"


def test_enabled_devices_lists_every_camera_for_the_picker(env, web_settings, web_password_hash):
    """The SPA's device picker: every enabled camera, with its ReoVault
    name and id, so a 400 "pick a camera" can always be resolved."""
    app, device_b_id = _two_device_app(env, web_settings)
    client = _client(app)

    r = client.get("/api/v1/devices/enabled")

    assert r.status_code == 200
    assert {(d["id"], d["name"]) for d in r.json()} == {
        (env.device_id, "Doorbell"),
        (device_b_id, "Cam B"),
    }


def test_unknown_device_param_is_404(env, web_settings, web_password_hash):
    app, _device_b_id = _two_device_app(env, web_settings)
    client = _client(app)

    r = client.get("/api/v1/health", params={"device": 999999})

    assert r.status_code == 404


def test_disabled_device_param_is_404(env, web_settings, web_password_hash):
    app, device_b_id = _two_device_app(env, web_settings)
    client = _client(app)

    r = client.put(f"/api/v1/devices/{device_b_id}/enabled", json={"enabled": False})
    assert r.status_code == 200

    assert client.get("/api/v1/health", params={"device": device_b_id}).status_code == 404
    # One camera left enabled: `?device=` becomes optional again.
    assert client.get("/api/v1/health").status_code == 200


def test_bad_device_param_is_422(env, web_settings, web_password_hash):
    app, _device_b_id = _two_device_app(env, web_settings)
    client = _client(app)

    r = client.get("/api/v1/health", params={"device": "not-a-number"})

    assert r.status_code == 422


def _recording(name: str) -> RemoteRecording:
    return RemoteRecording(
        remote_name=name,
        start_utc=datetime(2026, 4, 17, 12, tzinfo=UTC),
        end_utc=None,
        duration_s=None,
        rec_type="md",
        stream="main",
        remote_size=10,
        raw_metadata={},
    )


def test_recordings_are_isolated_per_device_even_though_repository_is_shared(
    env, web_settings, web_password_hash
):
    """The whole point of `?device=`: browsing camera A never shows camera
    B's recordings, even though both share one Repository/vault."""
    app, device_b_id = _two_device_app(env, web_settings)
    env.repository.discover_recording(
        device_id=env.device_id, channel=0, recording=_recording("clip-a")
    )
    env.repository.discover_recording(
        device_id=device_b_id, channel=0, recording=_recording("clip-b")
    )
    client = _client(app)

    r_a = client.get("/api/v1/day", params={"date_": "2026-04-17", "device": env.device_id})
    r_b = client.get("/api/v1/day", params={"date_": "2026-04-17", "device": device_b_id})

    # Each view's total must be exactly 1, not 2 (which would mean the
    # query leaked across devices).
    assert r_a.json()["total"] == 1
    assert r_b.json()["total"] == 1


def test_disabling_the_only_camera_is_a_recoverable_409_not_a_500(
    env, web_settings, web_password_hash
):
    """Regression: turning off the only configured camera must leave every
    device-scoped endpoint answering with a clear error, while the fleet-wide
    Devices endpoints (the one place that can undo it) keep working."""
    # A device row exists (disabled), but the Fleet has no archiver for it,
    # exactly like `set_enabled` leaves things after disabling the only
    # enabled camera.
    env.repository.set_device_enabled(env.device_id, False)
    fleet = Fleet(
        settings=web_settings,
        repository=env.repository,
        vault=env.vault,
        gateway=GatewaySupervisor(binary="reolink-cli"),
    )
    app = create_app(fleet, settings=web_settings, repository=env.repository)
    client = _client(app)

    r = client.get("/api/v1/health")

    assert r.status_code == 409
    assert "No camera is enabled" in r.json()["detail"]
    assert client.get("/api/v1/devices/enabled").json() == []
    devices = client.get("/api/v1/devices")
    assert devices.status_code == 200
    assert [d["enabled"] for d in devices.json()["devices"]] == [False]
