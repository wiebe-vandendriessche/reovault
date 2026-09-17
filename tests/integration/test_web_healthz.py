"""See plan: Deployment, "Healthcheck hits /healthz"."""

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from reovault.providers.fake import FakeProvider
from reovault.providers.reolink_cli import ReolinkCliProvider
from reovault.web import create_app


def test_healthz_ok_when_database_reachable(env):
    archiver = env.archiver(FakeProvider())
    client = TestClient(create_app(archiver))

    response = client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["database"] == "ok"


def test_healthz_503_when_database_unreachable(env):
    archiver = env.archiver(FakeProvider())
    archiver.repository.close()  # simulate a broken connection
    client = TestClient(create_app(archiver))

    response = client.get("/healthz")

    assert response.status_code == 503
    assert response.json()["status"] == "error"


def test_healthz_reports_gateway_state_for_reolink_cli_provider(env):
    gateway = MagicMock()
    gateway.is_listening.return_value = True
    provider = ReolinkCliProvider(
        alias="doorbell", timezone="UTC", binary="reolink-cli", gateway=gateway
    )
    archiver = env.archiver(provider)
    client = TestClient(create_app(archiver))

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["checks"]["gateway"] == "listening"


def test_healthz_omits_gateway_check_for_fake_provider(env):
    archiver = env.archiver(FakeProvider())
    client = TestClient(create_app(archiver))

    response = client.get("/healthz")

    assert "gateway" not in response.json()["checks"]


def test_healthz_reports_coverage_alarm_state(env):
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

    archiver = env.archiver(FakeProvider())
    client = TestClient(create_app(archiver))

    response = client.get("/healthz")

    assert response.status_code == 200  # a coverage alarm doesn't fail the healthcheck itself
    assert response.json()["checks"]["coverage_alarm"] is True
