"""The safety net: what you need to get your footage back if this server is
lost, and a reminder to keep the one thing ReoVault can't recover for you, the
restic password. The dashboard never shows that password."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from reovault.db import now_iso, parse_iso
from reovault.destinations import status_of
from reovault.web.api.cameras import PASSWORD_CONFIRMED
from reovault.web.deps import AppState, State

router = APIRouter(prefix="/recovery", tags=["recovery"])


class RecoveryDestinationOut(BaseModel):
    name: str
    role: str
    repository: str
    config_backed_up_at: datetime | None
    config_error: str | None


class RecoveryOut(BaseModel):
    password_file: str | None  # where it lives (a path), never its content
    password_file_found: bool
    confirmed_at: datetime | None
    destinations: list[RecoveryDestinationOut]
    cameras: list[str]


def _out(st: AppState) -> RecoveryOut:
    s = st.settings
    pw = s.backup.password_file
    confirmed = st.db.get_setting(PASSWORD_CONFIRMED)
    out = []
    for d in s.destinations:
        status = status_of(st.db, d.name)
        ok = status.get("config_ok")
        out.append(
            RecoveryDestinationOut(
                name=d.name,
                role="off" if not d.enabled else "primary" if d is s.primary() else "copy",
                repository=st.rt.scrub_credentials(d.repository) or "",
                config_backed_up_at=parse_iso(ok) if ok else None,
                config_error=st.redact(status.get("config_error")),
            )
        )
    return RecoveryOut(
        password_file=str(pw) if pw else None,
        password_file_found=bool(pw and pw.is_file()),
        confirmed_at=parse_iso(confirmed) if confirmed else None,
        destinations=out,
        cameras=[c.alias for c in s.cameras],
    )


@router.get("")
def recovery(st: State) -> RecoveryOut:
    return _out(st)


@router.post("/confirm")
def confirm(st: State) -> RecoveryOut:
    """The user says they stored the restic password somewhere other than here."""
    st.db.set_setting(PASSWORD_CONFIRMED, now_iso())
    return _out(st)


_SHEET = """\
REOVAULT RECOVERY SHEET
Made {when}

If this server is lost, your footage is not: it is in ordinary restic
repositories, readable with plain restic and the restic password. Nothing here
contains that password. Keep a copy of it somewhere that is NOT this server
(a password manager); without it every backup is unreadable.

DESTINATIONS
{destinations}

CAMERAS
{cameras}

GET A CLIP BACK (any computer with restic{rclone_note})
{commands}

GET REOVAULT RUNNING AGAIN
  1. Install ReoVault (Docker) on the new server.
  2. Restore the config backup from a destination into /config:
       restic -r <destination>/reovault-config restore latest --target /tmp/cfg
     It holds reovault.toml, rclone.conf and the camera registry
     (reolink-cli/). Put them back where compose.yaml mounts them.
  3. Put the restic password back as the restic_password secret.
  4. Start ReoVault. It finds the snapshots, rebuilds its index from the newest
     one, and carries on without downloading anything twice.

More: docs/restore-without-reovault.md
"""


@router.get("/sheet", response_class=PlainTextResponse)
def sheet(st: State) -> PlainTextResponse:
    s = st.settings
    uses_rclone = any(d.repository.startswith("rclone:") for d in s.destinations if d.enabled)
    dests = (
        "\n".join(
            f"  {d.name} ({'primary' if d is s.primary() else 'copy' if d.enabled else 'off'}): "
            f"{st.rt.scrub_credentials(d.repository)}"
            for d in s.destinations
        )
        or "  (none connected)"
    )
    first = next((d for d in s.destinations if d.enabled), None)
    cameras = "\n".join(f"  {c.alias}  ({c.timezone})" for c in s.cameras) or "  (none)"
    if first is None or not s.cameras:
        commands = "  (connect a destination and a camera first)"
    else:
        repo = f"{st.rt.scrub_credentials(first.repository)}/{s.cameras[0].alias}"
        commands = (
            f"  export RESTIC_PASSWORD_FILE=./restic_password.txt\n"
            f'  restic -r "{repo}" snapshots\n'
            f'  restic -r "{repo}" find 0120261027221944.mp4      # a clip, by its camera name\n'
            f'  restic -r "{repo}" dump <snapshot> <path> > clip.mp4'
        )
    text = _SHEET.format(
        when=now_iso(),
        destinations=dests,
        cameras=cameras,
        rclone_note=" and rclone with your rclone.conf" if uses_rclone else "",
        commands=commands,
    )
    return PlainTextResponse(
        text,
        headers={
            "Content-Disposition": 'attachment; filename="reovault-recovery-sheet.txt"',
            "Cache-Control": "private, no-store",
        },
    )
