"""`reovault` console script. Commands beyond `doctor`/`config-check` land in
later phases (see docs/IMPLEMENTATION_PLAN.md: Build order). They're stubbed
here so `--help` documents the eventual surface without pretending it works
today.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import typer

from reovault import __version__
from reovault.config import load_settings
from reovault.crypto import keyring
from reovault.logging import configure_logging, get_logger

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


def _not_yet_implemented(command: str, phase: str) -> None:
    typer.echo(f"'{command}' lands in {phase}, see docs/IMPLEMENTATION_PLAN.md", err=True)
    raise typer.Exit(code=2)


@app.command()
def probe() -> None:
    """Connect to the configured device and print what it sees (no writes)."""
    _not_yet_implemented("probe", "Phase 1 (Provider)")


@app.command()
def run() -> None:
    """Trigger an archive run for the configured window."""
    _not_yet_implemented("run", "Phase 4 (Archiver)")


@app.command()
def backfill() -> None:
    """Trigger a deep backfill over a wider window."""
    _not_yet_implemented("backfill", "Phase 4 (Archiver)")


@app.command()
def status() -> None:
    """Show archived count, bytes, and coverage margin."""
    _not_yet_implemented("status", "Phase 4 (Archiver)")


@app.command()
def verify() -> None:
    """Re-decrypt and re-hash archived files."""
    _not_yet_implemented("verify", "Phase 3/4 (Crypto + Archiver)")


@app.command()
def export() -> None:
    """Decrypt one archived recording back to a plaintext file."""
    _not_yet_implemented("export", "Phase 3/4 (Crypto + Archiver)")


if __name__ == "__main__":
    sys.exit(app())
