"""`reovault` console script."""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

import typer

from reovault import __version__
from reovault.backup import BackupRun, CameraState
from reovault.camera import Gateway
from reovault.config import Settings, load_settings
from reovault.config_store import ConfigStore
from reovault.db import Db
from reovault.logging import configure_logging
from reovault.runtime import Runtime
from reovault.tools import ToolError, assert_version
from reovault.worker import camera_for, restic_for

app = typer.Typer(add_completion=False, help="ReoVault: back up Reolink SD cards with restic.")


@app.callback()
def main() -> None:
    configure_logging()


@app.command()
def version() -> None:
    """Print the ReoVault version."""
    typer.echo(__version__)


@app.command()
def run(camera: str = typer.Option(..., "--camera", help="The camera's alias.")) -> None:
    """Run one backup of one camera now, in the foreground."""
    settings = load_settings()
    cam = settings.camera(camera)
    if cam is None:
        typer.echo(f"[FAIL] no [[cameras]] entry with alias {camera!r}", err=True)
        raise typer.Exit(1)
    gateway = Gateway(settings.tools.reolink_cli, settings.tools.gateway_addr)
    try:
        result = BackupRun(
            camera=camera_for(settings, cam, gateway),
            restic=restic_for(settings, cam.alias),
            db=Db(settings.storage.data_dir / "reovault.db"),
            staging=settings.storage.staging_dir / cam.alias,
            state=CameraState(),
            interval=timedelta(minutes=settings.backup.interval_minutes),
        ).run("manual")
    finally:
        gateway.stop()
    typer.echo(json.dumps(asdict(result)))
    if result.outcome != "ok":
        raise typer.Exit(1)


def _preflight(settings: Settings) -> list[str]:
    """Problems that would stop backups: wrong tool versions, unwritable
    folders. Only checks the tools actually in use."""
    tools = settings.tools
    checks = [([tools.restic, "version"], tools.restic_version)]
    if any(not c.demo for c in settings.cameras):
        checks.append(([tools.reolink_cli, "--version"], tools.reolink_cli_version))
    if (settings.backup.repository or "").startswith("rclone:"):
        checks.append(([tools.rclone, "version"], tools.rclone_version))
    problems = []
    for cmd, wanted in checks:
        try:
            assert_version(cmd, wanted)
        except RuntimeError as exc:
            problems.append(str(exc))
    for folder in (settings.storage.data_dir, settings.storage.staging_dir):
        folder.mkdir(parents=True, exist_ok=True)
        if not os.access(folder, os.W_OK):
            problems.append(f"{folder} is not writable")
    return problems


@app.command()
def doctor() -> None:
    """Check tools, folders and every camera's repository. Changes nothing."""
    settings = load_settings()
    problems = _preflight(settings)
    for cam in settings.cameras:
        if not cam.enabled:
            continue
        try:
            state = restic_for(settings, cam.alias).state()
        except ToolError as exc:
            problems.append(f"{cam.alias}: {exc}")
            continue
        typer.echo(f"[{'OK' if state == 'ok' else 'INFO'}] {cam.alias}: repository {state}")
        if state == "wrong_password":
            problems.append(f"{cam.alias}: the restic password doesn't open its repository")
    repo = settings.backup.repository or ""
    if repo.startswith("/") and not _on_mount(Path(repo)):
        typer.echo(f"[WARN] {repo} is not on a mounted volume; in Docker it would be lost.")
    for p in problems:
        typer.echo(f"[FAIL] {p}", err=True)
    if problems:
        raise typer.Exit(1)
    typer.echo("[OK] ready")


def _on_mount(path: Path) -> bool:
    p = path.resolve()
    while not p.exists():
        p = p.parent
    while p != p.parent:
        if os.path.ismount(p):
            return p != Path("/")
        p = p.parent
    return False


@app.command()
def daemon() -> None:
    """Back up every enabled camera on its schedule and serve the dashboard."""
    import uvicorn

    from reovault.web.app import create_app

    settings = load_settings()
    problems = _preflight(settings)
    if problems:
        for p in problems:
            typer.echo(f"[FAIL] {p}", err=True)
        raise typer.Exit(1)
    path = Path(os.environ.get("REOVAULT_CONFIG_FILE", "reovault.toml"))
    rt = Runtime(
        config=ConfigStore(path, settings),
        db=Db(settings.storage.data_dir / "reovault.db"),
        gateway=Gateway(settings.tools.reolink_cli, settings.tools.gateway_addr),
    )
    stop = rt.start()
    try:
        uvicorn.run(
            create_app(rt),
            host=settings.web.host,
            port=settings.web.port,
            proxy_headers=True,
            forwarded_allow_ips=settings.web.forwarded_allow_ips,
            log_config=None,
            # Open dashboards hold an SSE stream; don't wait on them forever.
            timeout_graceful_shutdown=3,
        )
    finally:
        rt.stop(stop)


web_app = typer.Typer(add_completion=False, help="Dashboard password.")
app.add_typer(web_app, name="web")


@web_app.command("set-password")
def web_set_password() -> None:
    """Set the dashboard password: hidden prompt, written to a 0600 file,
    never through argv or reovault.toml. Needs `web.email` (the login name
    password managers autofill)."""
    from reovault.web import security
    from reovault.web.auth import write_password_file

    settings = load_settings()
    if not settings.web.email:
        typer.echo("[FAIL] set web.email in reovault.toml or $REOVAULT_WEB__EMAIL first", err=True)
        raise typer.Exit(1)
    password = typer.prompt("Dashboard password", hide_input=True, confirmation_prompt=True)
    write_password_file(settings.web.password_file, security.hash_password(password.encode()))
    typer.echo(f"Password set at {settings.web.password_file} (mode 0600).")
    typer.echo(f"Login identity: {settings.web.email}")
