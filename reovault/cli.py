"""`reovault` console script. `run`/`backfill`/`status`/`verify`/`export`/
`probe`/`reconcile` operate on a single selected device (`_bootstrap`/
`_select_device`); `daemon` runs every enabled device at once through a
`Fleet` (`_load_runtime`) and serves the dashboard (see reovault/web/app.py).
`web set-password` sets the dashboard's login password.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import typer
import uvicorn

from reovault import __version__, health
from reovault.archiver import Archiver, assert_pinned_cli_version, assert_same_filesystem
from reovault.config import DeviceConfig, Settings, load_settings
from reovault.crypto import keyring
from reovault.db.repository import RecordingRow, Repository
from reovault.fleet import Fleet, build_fleet
from reovault.logging import configure_logging, get_logger
from reovault.models import ArchiveRunResult
from reovault.providers.gateway import GatewaySupervisor
from reovault.providers.reolink_cli import ReolinkCliProvider
from reovault.providers.reolink_cli_registry import ReolinkCliRegistry
from reovault.storage.vault import EncryptedFsVault

app = typer.Typer(add_completion=False, help="ReoVault: archive Reolink recordings locally.")
key_app = typer.Typer(add_completion=False, help="Master key lifecycle.")
app.add_typer(key_app, name="key")
logger = get_logger(__name__)

_PASSPHRASE_ENV_VAR = "REOVAULT_MASTER_PASSPHRASE"
_PASSPHRASE_FILE_ENV_VAR = "REOVAULT_MASTER_PASSPHRASE_FILE"


@app.callback()
def main() -> None:
    configure_logging()


@app.command()
def version() -> None:
    """Print the ReoVault version."""
    typer.echo(__version__)


@app.command()
def doctor() -> None:
    """Sanity-check config, storage paths, and DB migration. No camera contact.

    Mirrors `reolink-cli doctor`'s spirit (offline checks first) but is
    ReoVault's own preflight, not a passthrough to the CLI's.
    """
    ok = True
    try:
        settings = load_settings()
    except Exception as exc:  # noqa: BLE001 - report, don't crash
        typer.echo(f"[FAIL] config: {exc}")
        raise typer.Exit(code=1) from exc
    typer.echo("[ OK ] config loaded")

    for path_name, path in (
        ("storage.config_dir", settings.storage.config_dir),
        ("storage.vault_dir", settings.storage.vault_dir),
        ("storage.staging_dir", settings.storage.staging_dir),
    ):
        try:
            path.mkdir(parents=True, exist_ok=True)
            typer.echo(f"[ OK ] {path_name} writable ({path})")
        except OSError as exc:
            typer.echo(f"[FAIL] {path_name} not writable ({path}): {exc}")
            ok = False

    # reolink_cli.registry_dir holds reolink-cli's own aliases.toml/
    # credentials.key. Adding a camera from the dashboard writes there, and
    # reolink-cli itself refuses group- or world-readable credential files,
    # so a root-owned or 0755 mount here is the single most likely
    # first-run breakage; catch it before a camera add fails deep inside a
    # subprocess call.
    registry_dir = settings.reolink_cli.registry_dir
    try:
        registry_dir.mkdir(parents=True, exist_ok=True)
        probe = registry_dir / ".reovault-doctor-probe"
        probe.write_text("")
        probe.unlink()
        typer.echo(f"[ OK ] reolink_cli.registry_dir writable ({registry_dir})")
    except OSError as exc:
        typer.echo(f"[FAIL] reolink_cli.registry_dir not writable ({registry_dir}): {exc}")
        ok = False

    try:
        from reovault.db.repository import Repository

        repo = Repository(settings.storage.db_path)
        repo.close()
        typer.echo(f"[ OK ] database migrates cleanly ({settings.storage.db_path})")
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"[FAIL] database: {exc}")
        ok = False

    key_path = settings.storage.master_key_path
    if not key_path.exists():
        typer.echo(f"[WARN] no master key yet at {key_path}; run 'reovault key init'")
    else:
        try:
            keyring.assert_outside_vault(key_path, settings.storage.vault_dir)
            keyring.check_permissions(key_path)
            typer.echo(
                f"[ OK ] master key exists, outside the vault, permissions 0600 ({key_path})"
            )
        except keyring.KeyFileError as exc:
            typer.echo(f"[FAIL] master key: {exc}")
            ok = False
        passphrase = _env_passphrase()
        if passphrase is None:
            typer.echo(f"[WARN] ${_PASSPHRASE_ENV_VAR} not set; master key unwrap not verified")
        else:
            from cryptography.exceptions import InvalidTag

            try:
                keyring.load_master_key(key_path, passphrase.encode())
                typer.echo("[ OK ] master key unwraps with the configured passphrase")
            except InvalidTag:
                typer.echo(f"[FAIL] master key: ${_PASSPHRASE_ENV_VAR} does not unwrap {key_path}")
                ok = False

    if not settings.devices:
        typer.echo("[WARN] no devices configured in reovault.toml yet")

    if not ok:
        raise typer.Exit(code=1)


def _env_passphrase() -> str | None:
    """Prefer `$REOVAULT_MASTER_PASSPHRASE`; fall back to
    `$REOVAULT_MASTER_PASSPHRASE_FILE` (a Docker secret path). A file, not
    another env var, because the archiver and gateway spawn subprocesses
    that inherit the process environment, so the camera-facing binary
    would otherwise carry the vault passphrase for no reason."""
    value = os.environ.get(_PASSPHRASE_ENV_VAR)
    if value is not None:
        return value
    file_path = os.environ.get(_PASSPHRASE_FILE_ENV_VAR)
    if file_path is not None:
        return Path(file_path).read_text().strip()
    return None


def _read_passphrase(*, confirm: bool) -> bytes:
    """Prefer the environment/Docker-secret-file (matches `reolink-cli`'s
    own "never on argv" convention); fall back to a hidden prompt so this
    stays usable interactively without putting a secret in shell history."""
    env_value = _env_passphrase()
    if env_value is not None:
        return env_value.encode()
    prompted: str = typer.prompt("Master passphrase", hide_input=True, confirmation_prompt=confirm)
    return prompted.encode()


@key_app.command("init")
def key_init() -> None:
    """Generate a new master key, wrapped by a passphrase. Refuses to
    overwrite an existing key: losing one is unrecoverable, so clobbering
    is never implicit."""
    settings = load_settings()
    key_path = settings.storage.master_key_path
    try:
        keyring.assert_outside_vault(key_path, settings.storage.vault_dir)
    except keyring.KeyInsideVaultError as exc:
        typer.echo(f"[FAIL] {exc}", err=True)
        raise typer.Exit(code=1) from exc

    passphrase = _read_passphrase(confirm=True)
    try:
        keyring.generate_master_key(key_path, passphrase)
    except FileExistsError as exc:
        typer.echo(f"[FAIL] {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Master key created at {key_path} (mode 0600).")
    typer.echo(
        "Back it up now: losing this file and the passphrase makes the archive unrecoverable."
    )


@key_app.command("verify")
def key_verify() -> None:
    """Confirm the passphrase unwraps the master key."""
    settings = load_settings()
    key_path = settings.storage.master_key_path
    if not key_path.exists():
        typer.echo(f"[FAIL] no master key at {key_path}", err=True)
        raise typer.Exit(code=1)
    passphrase = _read_passphrase(confirm=False)
    if keyring.verify_passphrase(key_path, passphrase):
        typer.echo("[ OK ] passphrase unwraps the master key")
    else:
        typer.echo("[FAIL] passphrase does not unwrap the master key", err=True)
        raise typer.Exit(code=1)


@key_app.command("backup")
def key_backup(
    to: Path | None = typer.Option(None, "--to", help="Copy the key file here (mode 0600)."),
) -> None:
    """Emit the key file (optionally to `--to PATH`) plus the standing
    warning: losing both the key file and the passphrase makes the archive
    unrecoverable. This never touches the passphrase itself."""
    settings = load_settings()
    key_path = settings.storage.master_key_path
    if not key_path.exists():
        typer.echo(f"[FAIL] no master key at {key_path}", err=True)
        raise typer.Exit(code=1)

    if to is not None:
        shutil.copyfile(key_path, to)
        os.chmod(to, 0o600)
        typer.echo(f"Copied {key_path} -> {to} (mode 0600).")
    else:
        typer.echo(f"Master key: {key_path}")

    typer.echo(
        f"Store this file somewhere other than {settings.storage.vault_dir} "
        "(wherever your backup/retention tooling reads from) and record the "
        "passphrase separately. Losing either the key file or the passphrase "
        "makes every archived recording permanently unrecoverable."
    )


# -- Wiring the provider, vault, and repository into a real archiver ------


@dataclass
class _Context:
    settings: Settings
    device: DeviceConfig
    archiver: Archiver


def _select_device(settings: Settings, device_alias: str | None) -> DeviceConfig:
    if not settings.devices:
        typer.echo(
            "[FAIL] no devices configured; add a [[devices]] entry to reovault.toml", err=True
        )
        raise typer.Exit(code=1)
    if device_alias is not None:
        for device in settings.devices:
            if device.alias == device_alias:
                return device
        typer.echo(f"[FAIL] no device named {device_alias!r} in reovault.toml", err=True)
        raise typer.Exit(code=1)
    if len(settings.devices) > 1:
        typer.echo("[FAIL] multiple devices configured; pass --device <alias>", err=True)
        raise typer.Exit(code=1)
    return settings.devices[0]


def _preflight() -> tuple[Settings, bytes]:
    """Settings load, pinned CLI version check, same-filesystem check, and
    master key unwrap: the common startup sequence every entry point that
    touches the camera or the vault needs before building anything
    device-specific. Returns the loaded settings and the unwrapped master
    key bytes."""
    settings = load_settings()

    try:
        assert_pinned_cli_version(settings.reolink_cli.binary, settings.reolink_cli.pinned_version)
    except RuntimeError as exc:
        typer.echo(f"[FAIL] {exc}", err=True)
        raise typer.Exit(code=1) from exc

    try:
        assert_same_filesystem(settings.storage.staging_dir, settings.storage.vault_dir)
    except RuntimeError as exc:
        typer.echo(f"[FAIL] {exc}", err=True)
        raise typer.Exit(code=1) from exc

    passphrase = _env_passphrase()
    if passphrase is None:
        typer.echo(f"[FAIL] ${_PASSPHRASE_ENV_VAR} is not set", err=True)
        raise typer.Exit(code=1)
    try:
        master_key = keyring.load_master_key(settings.storage.master_key_path, passphrase.encode())
    except keyring.KeyFileError as exc:
        typer.echo(f"[FAIL] master key: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except Exception as exc:  # cryptography.exceptions.InvalidTag: wrong passphrase
        typer.echo(f"[FAIL] master key: passphrase does not unwrap it ({exc})", err=True)
        raise typer.Exit(code=1) from exc

    return settings, master_key


def _bootstrap(device_alias: str | None) -> _Context:
    """Preflight plus wiring: real `ReolinkCliProvider` + `EncryptedFsVault`
    + `Repository` into an `Archiver`. Shared by every command that
    actually touches the camera or the vault."""
    settings, master_key = _preflight()
    device = _select_device(settings, device_alias)

    repository = Repository(settings.storage.db_path)
    device_id = repository.upsert_device(
        alias=device.alias, channel=device.channel, timezone=device.timezone
    )
    vault = EncryptedFsVault(settings.storage.vault_dir, master_key)
    gateway = GatewaySupervisor(
        binary=settings.reolink_cli.binary, addr=settings.reolink_cli.gateway_addr
    )
    provider = ReolinkCliProvider(
        alias=device.alias,
        timezone=device.timezone,
        binary=settings.reolink_cli.binary,
        gateway=gateway,
        search_timeout_secs=settings.reolink_cli.search_timeout_secs,
        download_timeout_secs=settings.reolink_cli.download_timeout_secs,
    )
    archiver = Archiver(
        repository=repository,
        provider=provider,
        vault=vault,
        device_id=device_id,
        device_alias=device.alias,
        channel=device.channel,
        # Namespaced by device id even for a one-shot single-device command:
        # once a second device is ever registered, an unscoped staging_dir
        # here would let this command's reconcile sweep that device's
        # in-flight download away.
        staging_dir=settings.storage.staging_dir / str(device_id),
        lock_path=settings.storage.config_dir / f"reovault-{device_id}.lock",
    )
    return _Context(settings=settings, device=device, archiver=archiver)


@dataclass
class _Runtime:
    """The daemon's full multi-camera wiring: everything `_Context` has,
    but for every enabled device at once, plus the shared infrastructure
    the Fleet builds each Archiver from."""

    settings: Settings
    repository: Repository
    vault: EncryptedFsVault
    gateway: GatewaySupervisor
    fleet: Fleet
    registry: ReolinkCliRegistry


def _seed_devices_from_toml(settings: Settings, repository: Repository) -> None:
    """Each `[[devices]]` entry is upserted on every startup: TOML stays a
    valid way to declare cameras for a scripted deploy, but never deletes
    or disables a device the dashboard has since added or turned off,
    since `upsert_device` only sets `enabled`/`created_at` on first
    insert."""
    for device in settings.devices:
        repository.upsert_device(
            alias=device.alias,
            channel=device.channel,
            timezone=device.timezone,
            name=device.name,
        )


def _load_runtime() -> _Runtime:
    """Preflight plus full multi-camera wiring for `daemon`. Every enabled
    device gets its own Archiver via `Fleet`; devices added later through
    the dashboard call `fleet.add()` directly rather than going through
    this function again."""
    settings, master_key = _preflight()

    repository = Repository(settings.storage.db_path)
    _seed_devices_from_toml(settings, repository)
    vault = EncryptedFsVault(settings.storage.vault_dir, master_key)
    gateway = GatewaySupervisor(
        binary=settings.reolink_cli.binary, addr=settings.reolink_cli.gateway_addr
    )
    fleet = build_fleet(settings=settings, repository=repository, vault=vault, gateway=gateway)
    registry = ReolinkCliRegistry(binary=settings.reolink_cli.binary, gateway=gateway)
    return _Runtime(
        settings=settings,
        repository=repository,
        vault=vault,
        gateway=gateway,
        fleet=fleet,
        registry=registry,
    )


_DeviceOption = typer.Option(
    None, "--device", help="Device alias, only needed with multiple devices."
)


@app.command()
def probe(device: str | None = _DeviceOption) -> None:
    """Connect to the configured device and print what it sees (no writes)."""
    ctx = _bootstrap(device)
    info: dict[str, Any] = {
        "storage": asdict(ctx.archiver.provider.storage_status()),
    }
    now = datetime.now(UTC)
    recordings = ctx.archiver.provider.list_recordings(
        from_utc=now - timedelta(hours=24), to_utc=now
    )
    info["recordings_last_24h"] = len(recordings)
    info["sample"] = [r.remote_name for r in recordings[:5]]
    typer.echo(json.dumps(info, indent=2, default=str))


def _sample_storage_after_run(ctx: _Context) -> None:
    """`storage_sample` runs "each run", not on its own schedule.
    Best-effort: a sampling failure shouldn't turn a successful archive run
    into a reported failure."""
    try:
        health.record_storage_sample(
            repository=ctx.archiver.repository,
            provider=ctx.archiver.provider,
            device_id=ctx.archiver.device_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("cli.storage_sample_failed", error=str(exc))


@app.command()
def run(
    device: str | None = _DeviceOption,
    from_: datetime = typer.Option(
        ..., "--from", help="ISO start, UTC (e.g. 2026-04-17T00:00:00), not device-local time."
    ),
    to: datetime = typer.Option(..., "--to", help="ISO end, UTC."),
) -> None:
    """Trigger an archive run for the given window."""
    ctx = _bootstrap(device)
    result = ctx.archiver.run(
        trigger="manual", from_utc=from_.replace(tzinfo=UTC), to_utc=to.replace(tzinfo=UTC)
    )
    _sample_storage_after_run(ctx)
    _print_run_result(result)


@app.command()
def backfill(
    device: str | None = _DeviceOption,
    days: int | None = typer.Option(None, help="Defaults to schedule.backfill_days from config."),
) -> None:
    """Trigger a deep backfill over a trailing window."""
    ctx = _bootstrap(device)
    window_days = days if days is not None else ctx.settings.schedule.backfill_days
    to = datetime.now(UTC)
    from_ = to - timedelta(days=window_days)
    result = ctx.archiver.run(trigger="backfill", from_utc=from_, to_utc=to)
    _sample_storage_after_run(ctx)
    _print_run_result(result)


def _print_run_result(result: ArchiveRunResult) -> None:
    typer.echo(
        json.dumps(
            {
                "trigger": result.trigger,
                "discovered": result.discovered,
                "downloaded": result.downloaded,
                "skipped_dup": result.skipped_dup,
                "failed": result.failed,
                "bytes_archived": result.bytes_archived,
                "error": result.error,
            },
            indent=2,
        )
    )
    if result.error is not None:
        raise typer.Exit(code=1)


@app.command()
def status(device: str | None = _DeviceOption) -> None:
    """Show archived count, bytes, and SD card status. The coverage/lag
    alarm itself, comparing the oldest unarchived recording to the card's
    loop-overwrite horizon, lives in health.py; this prints the raw
    numbers that feed it."""
    ctx = _bootstrap(device)
    totals = ctx.archiver.repository.totals(ctx.archiver.device_id)
    storage = ctx.archiver.provider.storage_status()
    typer.echo(
        json.dumps(
            {
                "archived_count": totals.archived_count,
                "archived_bytes": totals.archived_plaintext_bytes,
                "sd_card": asdict(storage),
            },
            indent=2,
        )
    )


@app.command()
def verify(
    device: str | None = _DeviceOption,
    all_: bool = typer.Option(False, "--all", help="Verify every archived recording."),
    id_: int | None = typer.Option(None, "--id", help="Verify a single recording by row id."),
) -> None:
    """Re-decrypt and re-hash archived files."""
    ctx = _bootstrap(device)
    if not all_ and id_ is None:
        typer.echo("[FAIL] pass --all or --id N", err=True)
        raise typer.Exit(code=1)

    if id_ is not None:
        row = ctx.archiver.repository.get(id_)
        rows: Iterable[RecordingRow] = [row] if row is not None else []
    else:
        # iter_archived batches on id (500 at a time) rather than loading
        # every archived row at once, so `--all` stays fine at 313k rows/year.
        rows = ctx.archiver.repository.iter_archived(device_id=ctx.archiver.device_id)

    failed = 0
    for row in rows:
        if row.vault_path is None:
            continue
        ok = ctx.archiver.vault.verify(row.vault_path)
        typer.echo(f"{'OK  ' if ok else 'FAIL'} id={row.id} {row.vault_path}")
        if not ok:
            failed += 1

    if failed:
        raise typer.Exit(code=1)


@app.command()
def export(
    id_: int = typer.Argument(..., metavar="ID"),
    out: Path = typer.Option(..., "-o", "--output", help="Destination plaintext file."),
    device: str | None = _DeviceOption,
) -> None:
    """Decrypt one archived recording back to a plaintext file."""
    ctx = _bootstrap(device)
    row = ctx.archiver.repository.get(id_)
    if row is None or row.vault_path is None or row.state != "archived":
        typer.echo(f"[FAIL] no archived recording with id {id_}", err=True)
        raise typer.Exit(code=1)
    ctx.archiver.vault.get(row.vault_path, out)
    typer.echo(f"Exported {row.remote_name} -> {out}")


@app.command()
def reconcile(device: str | None = _DeviceOption) -> None:
    """Sweep orphaned staging files and adopt/delete unreferenced vault
    files left by a crash. The scheduler (`daemon`) also runs this every
    `schedule.reconcile_interval_hours`; exposed here for manual use ahead
    of that."""
    ctx = _bootstrap(device)
    ctx.archiver.reconcile()
    typer.echo("Reconciliation complete.")


@app.command()
def daemon() -> None:
    """Run continuously: the scheduler plus the dashboard, for every enabled
    device at once. This is the container's main process; one-shot
    `run`/`backfill` (still single-device, via `--device`) are for
    manual/testing use. Starting with zero devices configured is fine: the
    dashboard's Devices tab can add the first camera without a restart."""
    from reovault.scheduler import build_scheduler, reschedule_all_devices
    from reovault.web.app import create_app

    runtime = _load_runtime()
    settings = runtime.settings
    # Non-loopback + cookie_secure=False is the expected, supported shape
    # for a direct-port plain-HTTP deployment, not a misconfiguration. Log
    # it once at startup as an acknowledgement of that trade-off, not an
    # alarm (binding 0.0.0.0 alone is normal even behind a reverse proxy,
    # so that combination stays silent).
    if (
        settings.web.host not in ("127.0.0.1", "localhost", "::1")
        and not settings.web.cookie_secure
    ):
        logger.info(
            "daemon.plain_http",
            host=settings.web.host,
            detail="cookie_secure=False: the session cookie travels over plain HTTP",
        )

    scheduler = build_scheduler()
    reschedule_all_devices(scheduler, runtime.fleet, runtime.repository, settings)
    scheduler.start()
    logger.info("daemon.started", device_count=len(runtime.fleet.ids()))

    try:
        uvicorn.run(
            create_app(
                runtime.fleet,
                settings=settings,
                repository=runtime.repository,
                scheduler=scheduler,
                registry=runtime.registry,
            ),
            host=settings.web.host,
            port=settings.web.port,
            proxy_headers=True,
            forwarded_allow_ips=settings.web.forwarded_allow_ips,
            log_config=None,
        )
    finally:
        scheduler.shutdown(wait=False)


# -- Web password lifecycle -------------------------------------------------

web_app = typer.Typer(add_completion=False, help="Dashboard password lifecycle.")
app.add_typer(web_app, name="web")


@web_app.command("set-password")
def web_set_password() -> None:
    """Set (or replace) the dashboard's login password. Same posture as
    `key init`: hidden prompt with confirmation, written to a 0600 file
    beside `master.key`, never through argv or the TOML file. Refuses if
    no `web.email` is configured: shipping a login with a password but no
    identity is worse than failing loudly here, since the email field only
    exists for password-manager autofill and a blank one defeats that."""
    from reovault.web import security
    from reovault.web.auth import write_password_file

    settings = load_settings()
    if not settings.web.email:
        typer.echo(
            "[FAIL] no web.email configured; set it in reovault.toml or "
            "$REOVAULT_WEB__EMAIL before setting a password",
            err=True,
        )
        raise typer.Exit(code=1)
    password = typer.prompt("Dashboard password", hide_input=True, confirmation_prompt=True)
    phc = security.hash_password(password.encode())
    write_password_file(settings.web.password_file, phc)
    typer.echo(f"Password set at {settings.web.password_file} (mode 0600).")
    typer.echo(f"Login identity: {settings.web.email}")


if __name__ == "__main__":
    sys.exit(app())
