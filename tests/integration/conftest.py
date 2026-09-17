import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from reovault.archiver import Archiver
from reovault.db.repository import Repository
from reovault.providers.fake import FakeProvider
from reovault.storage.vault import EncryptedFsVault


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


@pytest.fixture
def env(tmp_path: Path) -> Env:
    repository = Repository(tmp_path / "reovault.db")
    vault = EncryptedFsVault(tmp_path / "vault", os.urandom(32), frame_size=16)
    device_alias = "doorbell"
    channel = 0
    device_id = repository.upsert_device(alias=device_alias, channel=channel, timezone="UTC")
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
