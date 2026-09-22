"""reovault/fleet.py: one Archiver per device, correctly isolated staging
paths, and add/remove."""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from reovault.config import Settings, StorageConfig
from reovault.db.repository import Repository
from reovault.fleet import Fleet, build_fleet
from reovault.providers.gateway import GatewaySupervisor
from reovault.storage.vault import EncryptedFsVault


@pytest.fixture
def settings(tmp_path) -> Settings:
    storage = StorageConfig(
        config_dir=tmp_path / "config",
        vault_dir=tmp_path / "vault",
        staging_dir=tmp_path / "staging",
        db_path=tmp_path / "reovault.db",
        master_key_path=tmp_path / "master.key",
    )
    return Settings(storage=storage)


@pytest.fixture
def repository(tmp_path) -> Repository:
    r = Repository(tmp_path / "reovault.db")
    yield r
    r.close()


@pytest.fixture
def vault(tmp_path) -> EncryptedFsVault:
    return EncryptedFsVault(tmp_path / "vault", os.urandom(32))


@pytest.fixture
def gateway() -> GatewaySupervisor:
    return GatewaySupervisor(binary="reolink-cli")


def test_build_fleet_seeds_one_archiver_per_enabled_device(settings, repository, vault, gateway):
    id_a = repository.upsert_device(alias="cam-a", channel=0, timezone="Europe/Brussels")
    id_b = repository.upsert_device(alias="cam-b", channel=0, timezone="Europe/Brussels")

    fleet = build_fleet(settings=settings, repository=repository, vault=vault, gateway=gateway)

    assert set(fleet.ids()) == {id_a, id_b}
    assert fleet.get(id_a).device_alias == "cam-a"
    assert fleet.get(id_b).device_alias == "cam-b"


def test_disabled_devices_are_not_seeded(settings, repository, vault, gateway):
    id_a = repository.upsert_device(alias="cam-a", channel=0, timezone="Europe/Brussels")
    repository.set_device_enabled(id_a, False)

    fleet = build_fleet(settings=settings, repository=repository, vault=vault, gateway=gateway)

    assert fleet.ids() == []
    assert fleet.get(id_a) is None


def test_each_device_gets_an_isolated_staging_directory(settings, repository, vault, gateway):
    id_a = repository.upsert_device(alias="cam-a", channel=0, timezone="Europe/Brussels")
    id_b = repository.upsert_device(alias="cam-b", channel=0, timezone="Europe/Brussels")

    fleet = build_fleet(settings=settings, repository=repository, vault=vault, gateway=gateway)

    staging_a = fleet.get(id_a).staging_dir
    staging_b = fleet.get(id_b).staging_dir
    assert staging_a != staging_b
    assert staging_a == settings.storage.staging_dir / str(id_a)
    assert staging_b == settings.storage.staging_dir / str(id_b)
    # And the lock files are likewise per device.
    assert fleet.get(id_a).lock_path != fleet.get(id_b).lock_path


def test_add_and_remove(settings, repository, vault, gateway):
    fleet = Fleet(settings=settings, repository=repository, vault=vault, gateway=gateway)
    assert fleet.ids() == []

    device_id = repository.upsert_device(alias="cam-c", channel=0, timezone="Europe/Brussels")
    device = repository.get_device(device_id)
    fleet.add(device)
    assert fleet.ids() == [device_id]
    assert fleet.get(device_id) is not None

    fleet.remove(device_id)
    assert fleet.ids() == []
    assert fleet.get(device_id) is None


def test_build_archiver_aborts_a_run_orphaned_by_a_dead_process(
    settings, repository, vault, gateway
):
    """A `finished_at IS NULL` archive_runs row left by a killed/crashed
    process must not wedge the dashboard's run button forever: the moment
    this device gets a fresh Archiver (a fresh process start, or a device
    re-enabled through the web UI), any such row is exactly known to be
    stale, since nothing in *this* process could have started it."""
    device_id = repository.upsert_device(alias="cam-d", channel=0, timezone="Europe/Brussels")
    run_id = repository.start_run(
        device_id=device_id,
        trigger="manual",
        window_from_utc=datetime(2026, 1, 1, tzinfo=UTC),
        window_to_utc=datetime(2026, 1, 2, tzinfo=UTC),
    )
    assert repository.running_run(device_id) is not None

    fleet = Fleet(settings=settings, repository=repository, vault=vault, gateway=gateway)
    fleet.add(repository.get_device(device_id))

    assert repository.running_run(device_id) is None
    run = repository.get_run(run_id)
    assert run is not None
    assert run.outcome == "aborted"
    assert run.finished_at is not None
