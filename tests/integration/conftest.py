import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from reovault.archiver import Archiver
from reovault.config import DeviceConfig, Settings, StorageConfig, WebConfig
from reovault.db.repository import Repository
from reovault.models import RemoteRecording
from reovault.providers.fake import FakeProvider
from reovault.storage.vault import EncryptedFsVault
from reovault.web import security
from reovault.web.app import create_app
from reovault.web.auth import write_password_file

WEB_TEST_PASSWORD = "hunter2-hunter2"  # noqa: S105 - test fixture, not a real credential
WEB_TEST_EMAIL = "owner@example.com"


def make_recording(
    name: str = "20260417_120000.mp4",
    start: datetime | None = None,
    *,
    end: datetime | None = None,
    duration_s: float | None = None,
    rec_type: str | None = "md",
    stream: str | None = "main",
    size: int | None = 1024,
    raw_metadata: dict | None = None,
) -> RemoteRecording:
    """A `RemoteRecording` factory for tests that don't care about most of
    its fields. `start` defaults to a fixed timestamp rather than `now()` so
    tests stay deterministic; `raw_metadata` defaults to `{"fileName": name}`
    since that's the shape `reolink-cli` actually returns."""
    return RemoteRecording(
        remote_name=name,
        start_utc=start or datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC),
        end_utc=end,
        duration_s=duration_s,
        rec_type=rec_type,
        stream=stream,
        remote_size=size,
        raw_metadata=raw_metadata if raw_metadata is not None else {"fileName": name},
    )


@dataclass
class Env:
    tmp_path: Path
    repository: Repository
    vault: EncryptedFsVault
    device_id: int
    device_alias: str
    channel: int
    staging_dir: Path
    lock_path: Path

    def archiver(self, provider: FakeProvider) -> Archiver:
        return Archiver(
            repository=self.repository,
            provider=provider,
            vault=self.vault,
            device_id=self.device_id,
            device_alias=self.device_alias,
            channel=self.channel,
            staging_dir=self.staging_dir,
            lock_path=self.lock_path,
        )

    def archiver_for(
        self,
        alias: str,
        provider: FakeProvider,
        *,
        channel: int = 0,
        timezone: str = "Europe/Brussels",
        name: str | None = None,
    ) -> tuple[Archiver, int]:
        """Registers a second device under `alias` and builds its own
        Archiver, for multi-camera tests that need more than the `env`
        fixture's single seeded device."""
        device_id = self.repository.upsert_device(
            alias=alias, channel=channel, timezone=timezone, name=name
        )
        archiver = Archiver(
            repository=self.repository,
            provider=provider,
            vault=self.vault,
            device_id=device_id,
            device_alias=alias,
            channel=channel,
            staging_dir=self.tmp_path / f"staging-{alias}",
            lock_path=self.tmp_path / f"{alias}.lock",
        )
        return archiver, device_id


@pytest.fixture
def env(tmp_path: Path) -> Env:
    repository = Repository(tmp_path / "reovault.db")
    # Small frame_size so range/streaming tests genuinely exercise frame
    # boundaries rather than always fitting a whole tiny fixture in a
    # single frame.
    vault = EncryptedFsVault(tmp_path / "vault", os.urandom(32), frame_size=16)
    device_alias = "doorbell"
    channel = 0
    device_id = repository.upsert_device(
        alias=device_alias, channel=channel, timezone="Europe/Brussels"
    )
    return Env(
        tmp_path=tmp_path,
        repository=repository,
        vault=vault,
        device_id=device_id,
        device_alias=device_alias,
        channel=channel,
        staging_dir=tmp_path / "staging",
        lock_path=tmp_path / "reovault.lock",
    )


@pytest.fixture
def web_settings(env: Env) -> Settings:
    """A `Settings` built entirely from explicit overrides, never from
    `load_settings()`'s file/env sources: a test must not be able to pick
    up the developer's real `reovault.toml` or ambient `REOVAULT_*` env."""
    storage = StorageConfig(
        config_dir=env.tmp_path / "config",
        vault_dir=env.tmp_path / "vault",
        staging_dir=env.staging_dir,
        db_path=env.tmp_path / "reovault.db",
        master_key_path=env.tmp_path / "master.key",
    )
    web = WebConfig(
        email=WEB_TEST_EMAIL,
        password_file=env.tmp_path / "web_password",
        session_key_path=env.tmp_path / "web_session.key",
        cookie_secure=False,  # tests run over plain http
        stream_chunk_bytes=16,  # matches the vault's frame_size in `env`
    )
    return Settings(storage=storage, web=web)


@pytest.fixture
def web_device() -> DeviceConfig:
    return DeviceConfig(alias="doorbell", channel=0, timezone="Europe/Brussels")


@pytest.fixture
def web_password_hash(web_settings: Settings) -> str:
    phc = security.hash_password(WEB_TEST_PASSWORD.encode())
    write_password_file(web_settings.web.password_file, phc)
    return phc


def make_web_app(
    env: Env,
    web_settings: Settings,
    web_device: DeviceConfig,
    *,
    provider: FakeProvider | None = None,
    scheduler=None,  # noqa: ANN001 - BackgroundScheduler | None, kept loose to avoid importing apscheduler here
    registry=None,  # noqa: ANN001 - CameraRegistry | None, kept loose for the same reason
    dashboard_dir: Path | None = None,
):
    """`web_device` is kept as a parameter for every existing call site's
    sake, but create_app no longer takes it directly: the device's timezone
    comes from its repository row, already set by `env`'s own
    `upsert_device` call, so there's nothing left to read off `web_device`
    here.

    Builds a single-device Fleet directly (bypassing `Fleet.add`, which
    would construct a real `ReolinkCliProvider` via `build_archiver`) so
    the test's `FakeProvider`-backed archiver from `env.archiver(...)` is
    what actually gets served."""
    from reovault.fleet import Fleet
    from reovault.providers.gateway import GatewaySupervisor

    archiver = env.archiver(provider or FakeProvider())
    fleet = Fleet(
        settings=web_settings,
        repository=env.repository,
        vault=env.vault,
        gateway=GatewaySupervisor(binary="reolink-cli"),
    )
    fleet.archivers[env.device_id] = archiver
    return create_app(
        fleet,
        settings=web_settings,
        repository=env.repository,
        scheduler=scheduler,
        registry=registry,
        dashboard_dir=dashboard_dir,
        config=make_config(env, web_settings),
    )


def make_config(env: Env, web_settings: Settings):  # noqa: ANN201 - ConfigStore
    """A real, writable reovault.toml in the test's tmp dir, declaring the
    `env` camera, as the daemon would have: dashboard edits are written to
    it and applied through the same listener as a hand edit. Infrastructure
    (storage, web) stays the in-memory `web_settings`."""
    from reovault.config_store import ConfigStore

    path = env.tmp_path / "reovault.toml"
    if not path.exists():
        path.write_text(
            "# test config\n"
            "[[devices]]\n"
            f'alias = "{env.device_alias}"\n'
            f"channel = {env.channel}\n"
            'timezone = "Europe/Brussels"\n'
        )
    return ConfigStore(path, web_settings)


@pytest.fixture
def web_app(env: Env, web_settings: Settings, web_device: DeviceConfig, web_password_hash: str):
    return make_web_app(env, web_settings, web_device)


@pytest.fixture
def client(web_app) -> TestClient:  # noqa: ANN001
    return TestClient(web_app)


def csrf_token(client: TestClient) -> str:
    """The token the dashboard reads from `GET /api/v1/session` and sends as
    `X-CSRF-Token` on every mutating request."""
    response = client.get("/api/v1/session")
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


def login(client: TestClient, password: str = WEB_TEST_PASSWORD) -> TestClient:
    """Logs `client` in through the real `/api/v1/login` flow and sets the
    session's CSRF header as a default, matching what the dashboard does.
    `TestClient` persists cookies across requests like a browser. Used
    directly by tests that build a custom app the `auth_client` fixture
    doesn't."""
    response = client.post(
        "/api/v1/login",
        json={"email": WEB_TEST_EMAIL, "password": password},
        headers={"X-CSRF-Token": csrf_token(client)},
    )
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return client


@pytest.fixture
def auth_client(client: TestClient) -> TestClient:
    """A `TestClient` that has already logged in."""
    return login(client)
