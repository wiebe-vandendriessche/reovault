"""`reovault` console script. `run`/`backfill`/`status`/`verify`/`export`/
`probe`/`reconcile`/`daemon` are real (Phases 4-5, see
docs/IMPLEMENTATION_PLAN.md: Build order); the dashboard (Phase 6) isn't
built yet.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
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
from reovault.db.repository import Repository
from reovault.logging import configure_logging, get_logger
from reovault.models import ArchiveRunResult
from reovault.providers.gateway import GatewaySupervisor
from reovault.providers.reolink_cli import ReolinkCliProvider
from reovault.storage.vault import EncryptedFsVault

app = typer.Typer(add_completion=False, help="ReoVault: archive Reolink recordings locally.")
key_app = typer.Typer(add_completion=False, help="Master key lifecycle (see plan: Key management).")
app.add_typer(key_app, name="key")
logger = get_logger(__name__)

_PASSPHRASE_ENV_VAR = "REOVAULT_MASTER_PASSPHRASE"


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
        passphrase = os.environ.get(_PASSPHRASE_ENV_VAR)
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


def _read_passphrase(*, confirm: bool) -> bytes:
    """Prefer `$REOVAULT_MASTER_PASSPHRASE` (matches `reolink-cli`'s own
    "never on argv" convention); fall back to a hidden prompt so this stays
    usable interactively without putting a secret in shell history."""
    env_value = os.environ.get(_PASSPHRASE_ENV_VAR)
    if env_value is not None:
        return env_value.encode()
    prompted: str = typer.prompt("Master passphrase", hide_input=True, confirmation_prompt=confirm)
    return prompted.encode()


@key_app.command("init")
def key_init() -> None:
    """Generate a new master key, wrapped by a passphrase. Refuses to
    overwrite an existing key (see plan: losing one is unrecoverable, so
    clobbering is never implicit)."""
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
        "Store this file somewhere other than /mnt/storage (Pluton's backup source) and "
        "record the passphrase separately. Losing either the key file or the passphrase "
        "makes every archived recording permanently unrecoverable."
    )


# -- Phase 4: wiring the provider, vault, and repository into a real archiver -----


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


def _bootstrap(device_alias: str | None) -> _Context:
    """Preflight (see plan: Startup preflight) plus wiring: real
    `ReolinkCliProvider` + `EncryptedFsVault` + `Repository` into an
    `Archiver`. Shared by every command that actually touches the camera or
    the vault."""
    settings = load_settings()
    device = _select_device(settings, device_alias)

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

    passphrase = os.environ.get(_PASSPHRASE_ENV_VAR)
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
        staging_dir=settings.storage.staging_dir,
        lock_path=settings.storage.config_dir / "reovault.lock",
    )
    return _Context(settings=settings, device=device, archiver=archiver)


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
    """See plan: Scheduling, `storage_sample` runs "each run", not on its own
    schedule. Best-effort: a sampling failure shouldn't turn a successful
    archive run into a reported failure."""
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
    """Trigger an archive run for the given window (see plan: state machine)."""
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
    """Trigger a deep backfill over a trailing window (see plan: Scheduling)."""
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
    """Show archived count, bytes, and SD card status.

    The coverage/lag alarm itself (comparing the oldest unarchived recording
    to the card's loop-overwrite horizon) is a Phase 5 (health.py) feature;
    this prints the raw numbers that feed it.
    """
    ctx = _bootstrap(device)
    row = ctx.archiver.repository.conn.execute(
        """
        SELECT COUNT(*) AS n, COALESCE(SUM(plaintext_size), 0) AS bytes
        FROM recordings WHERE device_id = ? AND state = 'archived'
        """,
        (ctx.archiver.device_id,),
    ).fetchone()
    storage = ctx.archiver.provider.storage_status()
    typer.echo(
        json.dumps(
            {
                "archived_count": row["n"],
                "archived_bytes": row["bytes"],
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
    """Re-decrypt and re-hash archived files (see plan: Testing/Pluton interop)."""
    ctx = _bootstrap(device)
    if not all_ and id_ is None:
        typer.echo("[FAIL] pass --all or --id N", err=True)
        raise typer.Exit(code=1)

    if id_ is not None:
        row = ctx.archiver.repository.get(id_)
        rows = [row] if row is not None else []
    else:
        fetched = (
            ctx.archiver.repository.get(r["id"])
            for r in ctx.archiver.repository.conn.execute(
                "SELECT id FROM recordings WHERE device_id = ? AND state = 'archived'",
                (ctx.archiver.device_id,),
            ).fetchall()
        )
        rows = [row for row in fetched if row is not None]

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
    files left by a crash (see plan: Reliability). The scheduler (`daemon`)
    also runs this every `schedule.reconcile_interval_hours`; exposed here
    for manual use ahead of that."""
    ctx = _bootstrap(device)
    ctx.archiver.reconcile()
    typer.echo("Reconciliation complete.")


@app.command()
def daemon(device: str | None = _DeviceOption) -> None:
    """Run continuously: the scheduler (see plan: Scheduling) plus the
    `/healthz` endpoint. This is the container's main process; one-shot
    `run`/`backfill` are for manual/testing use."""
    from reovault.scheduler import build_scheduler
    from reovault.web import create_app

    ctx = _bootstrap(device)
    scheduler = build_scheduler(ctx.archiver, ctx.settings.schedule)
    scheduler.start()
    logger.info(
        "daemon.started",
        archive_cron=ctx.settings.schedule.archive_cron,
        backfill_cron=ctx.settings.schedule.backfill_cron,
    )
    try:
        uvicorn.run(
            create_app(ctx.archiver),
            host=ctx.settings.web.host,
            port=ctx.settings.web.port,
            log_config=None,
        )
    finally:
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    sys.exit(app())
