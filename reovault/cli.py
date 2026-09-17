"""`reovault` console script. Commands beyond `doctor`/`config-check` land in
later phases (see docs/IMPLEMENTATION_PLAN.md: Build order) — they're stubbed
here so `--help` documents the eventual surface without pretending it works
today.
"""

from __future__ import annotations

import sys

import typer

from reovault import __version__
from reovault.config import load_settings
from reovault.logging import configure_logging, get_logger

app = typer.Typer(add_completion=False, help="ReoVault — archive Reolink recordings locally.")
logger = get_logger(__name__)


@app.callback()
def main() -> None:
    configure_logging()


@app.command()
def version() -> None:
    """Print the ReoVault version."""
    typer.echo(__version__)


@app.command()
def doctor() -> None:
    """Sanity-check config, storage paths, and DB migration — no camera contact.

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

    if not settings.devices:
        typer.echo("[WARN] no devices configured in reovault.toml yet")

    if not ok:
        raise typer.Exit(code=1)


def _not_yet_implemented(command: str, phase: str) -> None:
    typer.echo(f"'{command}' lands in {phase} — see docs/IMPLEMENTATION_PLAN.md", err=True)
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
