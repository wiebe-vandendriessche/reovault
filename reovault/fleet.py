"""Multi-camera wiring: one `Archiver` per device, sharing the process-wide
`Repository`, `EncryptedFsVault`, and `GatewaySupervisor`.

Per device, what's *not* shared: the provider (each binds `--camera
<alias>`), the `Archiver` itself, its lock file, and its staging
subdirectory. The lock and staging isolation are the two properties that
keep two cameras' archive runs and reconciliations from ever touching each
other's files; see `Archiver.staging_dir`'s docstring and
`Archiver._reconcile_vault` for what happens when that isolation is
missing.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from reovault import retention
from reovault.archiver import Archiver
from reovault.config import Settings
from reovault.db.repository import DeviceRow, Repository
from reovault.logging import get_logger
from reovault.providers.gateway import GatewaySupervisor
from reovault.providers.reolink_cli import ReolinkCliProvider
from reovault.storage.vault import VaultStore

logger = get_logger(__name__)


def build_archiver(
    *,
    settings: Settings,
    device: DeviceRow,
    repository: Repository,
    vault: VaultStore,
    gateway: GatewaySupervisor,
    current_settings: Callable[[], Settings] | None = None,
) -> Archiver:
    # A fresh Archiver is exactly the signal that no thread of this process
    # can be running a job for this device yet -- so any archive_runs row
    # still marked "running" for it is necessarily left over from a process
    # that died before finishing (see Repository.abort_stale_running_runs).
    # Without this, that row wedges the dashboard's "run now" button and the
    # activity card forever, with no way to clear it short of DB surgery.
    aborted = repository.abort_stale_running_runs(
        device.id, error="interrupted by a process restart"
    )
    if aborted:
        logger.warning(
            "fleet.aborted_stale_running_run", device_id=device.id, device_alias=device.alias
        )
    provider = ReolinkCliProvider(
        alias=device.alias,
        timezone=device.timezone,
        binary=settings.reolink_cli.binary,
        gateway=gateway,
        search_timeout_secs=settings.reolink_cli.search_timeout_secs,
        download_timeout_secs=settings.reolink_cli.download_timeout_secs,
    )
    return Archiver(
        repository=repository,
        provider=provider,
        vault=vault,
        device_id=device.id,
        device_alias=device.alias,
        channel=device.channel,
        # Namespaced by integer device id, not alias: `reolink-cli device
        # add --help` confirms an alias may contain spaces, mixed case, or
        # non-ASCII, so an alias-derived path would need sanitizing and can
        # collide after a rename. The id is stable and always safe as a
        # path segment.
        staging_dir=settings.storage.staging_dir / str(device.id),
        lock_path=settings.storage.config_dir / f"reovault-{device.id}.lock",
        # Live settings when the daemon has a ConfigStore, so a retention
        # edit (dashboard or file) applies from the next clip on.
        retention=retention.archiver_hook(
            current_settings or (lambda: settings), repository, vault
        ),
    )


class Fleet:
    """One `Archiver` per enabled device, keyed by integer device id.
    `archivers` is guarded by a lock because the web layer can add a device
    at runtime from a request thread while the scheduler's own thread is
    concurrently reading `ids()`/`get()` to dispatch a job."""

    def __init__(
        self,
        *,
        settings: Settings,
        repository: Repository,
        vault: VaultStore,
        gateway: GatewaySupervisor,
    ) -> None:
        self._settings = settings
        # Swapped for `ConfigStore.current` by the daemon; see build_archiver.
        self.current_settings: Callable[[], Settings] = lambda: settings
        self._repository = repository
        self._gateway = gateway
        self._lock = threading.Lock()
        self.archivers: dict[int, Archiver] = {}
        # Applied to every archiver, current and future; see
        # `Archiver.on_progress`.
        self._on_progress: Callable[[int], None] | None = None
        # Public: the vault is shared across every device (namespaced
        # internally by `<alias>/...`), so a caller streaming a recording by
        # id needs this directly rather than through a specific device's
        # archiver -- a *disabled* device's historical footage must still
        # be streamable even though it has no entry in `archivers`.
        self.vault = vault

    def get(self, device_id: int) -> Archiver | None:
        with self._lock:
            return self.archivers.get(device_id)

    def ids(self) -> list[int]:
        with self._lock:
            return list(self.archivers.keys())

    def add(self, device: DeviceRow) -> Archiver:
        archiver = build_archiver(
            settings=self._settings,
            device=device,
            repository=self._repository,
            vault=self.vault,
            gateway=self._gateway,
            current_settings=self.current_settings,
        )
        archiver.on_progress = self._on_progress
        with self._lock:
            self.archivers[device.id] = archiver
        return archiver

    def set_progress_hook(self, hook: Callable[[int], None] | None) -> None:
        with self._lock:
            self._on_progress = hook
            for archiver in self.archivers.values():
                archiver.on_progress = hook

    def remove(self, device_id: int) -> None:
        with self._lock:
            self.archivers.pop(device_id, None)


def build_fleet(
    *,
    settings: Settings,
    repository: Repository,
    vault: VaultStore,
    gateway: GatewaySupervisor,
    current_settings: Callable[[], Settings] | None = None,
) -> Fleet:
    """Seeds a Fleet from every currently-enabled device row. Called once at
    startup, after `_seed_devices_from_toml` has upserted `[[devices]]`."""
    fleet = Fleet(settings=settings, repository=repository, vault=vault, gateway=gateway)
    if current_settings is not None:
        fleet.current_settings = current_settings
    for device in repository.list_devices(enabled_only=True):
        fleet.add(device)
    return fleet
