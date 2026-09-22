"""`/healthz`: the unauthenticated body is status-only once the app sits
behind a reachable hostname; `?verbose=1` restores the old detail (gateway,
per-device coverage alarm) for an authenticated dashboard caller. Under
multi-camera, the gateway is one shared check but coverage alarms are
broken out per device under `checks["devices"]`.
"""

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from reovault.fleet import Fleet
from reovault.providers.fake import FakeProvider
from reovault.providers.gateway import GatewaySupervisor
from reovault.providers.reolink_cli import ReolinkCliProvider
from reovault.web.app import create_app
from tests.integration.conftest import make_web_app


def test_healthz_ok_when_database_reachable(env, web_settings, web_device):
    app = make_web_app(env, web_settings, web_device)
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_503_when_database_unreachable(env, web_settings, web_device):
    archiver = env.archiver(FakeProvider())
    archiver.repository.close()  # simulate a broken connection
    fleet = Fleet(
        settings=web_settings,
        repository=env.repository,
        vault=env.vault,
        gateway=GatewaySupervisor(binary="reolink-cli"),
    )
    fleet.archivers[env.device_id] = archiver
    app = create_app(fleet, settings=web_settings, repository=env.repository)
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 503
    assert response.json()["status"] == "error"


def test_healthz_verbose_reports_gateway_state_for_reolink_cli_provider(
    env, web_settings, web_device
):
    gateway = MagicMock()
    gateway.is_listening.return_value = True
    provider = ReolinkCliProvider(
        alias="doorbell", timezone="UTC", binary="reolink-cli", gateway=gateway
    )
    app = make_web_app(env, web_settings, web_device, provider=provider)
    client = TestClient(app)

    response = client.get("/healthz", params={"verbose": "1"})

    assert response.status_code == 200
    assert response.json()["checks"]["gateway"] == "listening"


def test_healthz_verbose_omits_gateway_check_for_fake_provider(env, web_settings, web_device):
    app = make_web_app(env, web_settings, web_device)
    client = TestClient(app)

    response = client.get("/healthz", params={"verbose": "1"})

    assert "gateway" not in response.json()["checks"]


def test_healthz_verbose_reports_coverage_alarm_state(env, web_settings, web_device):
    from datetime import UTC, datetime, timedelta

    oldest_on_card = datetime.now(UTC) - timedelta(days=10)
    env.repository.record_storage_sample(
        device_id=env.device_id,
        total_gb=1.0,
        remain_gb=1.0,
        formatted=True,
        mounted=True,
        oldest_recording_utc=oldest_on_card,
    )
    from reovault.models import RemoteRecording

    rec = RemoteRecording(
        remote_name="stuck",
        start_utc=oldest_on_card + timedelta(days=1),
        end_utc=None,
        duration_s=None,
        rec_type=None,
        stream=None,
        remote_size=10,
        raw_metadata={},
    )
    env.repository.discover_recording(device_id=env.device_id, channel=0, recording=rec)

    app = make_web_app(env, web_settings, web_device)
    client = TestClient(app)

    response = client.get("/healthz", params={"verbose": "1"})

    assert response.status_code == 200  # a coverage alarm doesn't fail the healthcheck itself
    assert response.json()["checks"]["devices"][str(env.device_id)]["coverage_alarm"] is True


def test_healthz_is_unauthenticated(env, web_settings, web_device, web_password_hash):
    """/healthz stays reachable even with a password configured (see the
    allowlist test in test_web_auth_routes.py for the exhaustive version)."""
    app = make_web_app(env, web_settings, web_device)
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 200
