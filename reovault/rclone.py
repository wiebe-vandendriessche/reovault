"""Connecting rclone remotes from the dashboard.

ReoVault never parses or writes `rclone.conf` itself: rclone's own
`config create` does, so secrets are obscured by rclone and the file keeps
rclone's format. Remotes ReoVault makes are named `reovault-<destination>`;
remotes you made by hand are never touched.

Secrets travel on the command line of a short-lived rclone process inside the
container, where only this user can see it (the camera password, by contrast,
goes over stdin). Error text is scrubbed of them before it reaches anyone.
"""

from __future__ import annotations

import configparser
import re
from dataclasses import dataclass

from reovault.tools import ToolError, run

PREFIX = "reovault-"
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True, slots=True)
class Field:
    key: str
    label: str
    secret: bool = False
    required: bool = True
    default: str = ""
    help: str = ""


@dataclass(frozen=True, slots=True)
class RemoteType:
    id: str
    label: str
    rclone_type: str
    fields: tuple[Field, ...]
    obscure: bool = True  # rclone obscures the password-type fields itself
    paste: bool = False  # the whole `[remote]` section is pasted instead of fields
    help: str = ""


TYPES: tuple[RemoteType, ...] = (
    RemoteType(
        "nextcloud",
        "Nextcloud / WebDAV",
        "webdav",
        (
            Field("url", "WebDAV URL", help="Nextcloud: https://HOST/remote.php/dav/files/USER/"),
            Field("user", "Username"),
            Field("pass", "App password", secret=True, help="Create one under Settings, Security."),
            Field("vendor", "Server", default="nextcloud", help="nextcloud, owncloud or other"),
        ),
    ),
    RemoteType(
        "sftp",
        "SFTP (NAS, Hetzner Storage Box)",
        "sftp",
        (
            Field("host", "Host"),
            Field("user", "Username"),
            Field("pass", "Password", secret=True),
            Field("port", "Port", required=False, default="22"),
        ),
    ),
    RemoteType(
        "s3",
        "S3-compatible",
        "s3",
        (
            Field("provider", "Provider", default="Other", help="AWS, Minio, Wasabi, Other ..."),
            Field("access_key_id", "Access key"),
            Field("secret_access_key", "Secret key", secret=True),
            Field("endpoint", "Endpoint", required=False),
            Field("region", "Region", required=False),
        ),
        obscure=False,
    ),
    RemoteType(
        "b2",
        "Backblaze B2",
        "b2",
        (
            Field("account", "Key ID"),
            Field("key", "Application key", secret=True),
        ),
        obscure=False,
    ),
    RemoteType(
        "onedrive",
        "OneDrive",
        "onedrive",
        (),
        obscure=False,
        paste=True,
        help="Run `rclone config` on a computer with a browser, make a OneDrive remote, then "
        "paste its [section] from rclone.conf here.",
    ),
    RemoteType(
        "drive",
        "Google Drive",
        "drive",
        (),
        obscure=False,
        paste=True,
        help="Run `rclone config` on a computer with a browser, make a Google Drive remote, "
        "then paste its [section] from rclone.conf here.",
    ),
)
BY_ID = {t.id: t for t in TYPES}
# Pasted sections may only carry these keys: browser-login remotes need little
# more than a token, and an arbitrary key must not be able to change behaviour.
_PASTE_KEYS = {
    "type", "token", "drive_id", "drive_type", "client_id", "client_secret", "root_folder_id",
    "scope", "team_drive", "region", "service_account_file", "shared_with_me",
}  # fmt: skip


def remote_name(destination: str) -> str:
    return PREFIX + destination


def parse_section(text: str, expected_type: str) -> dict[str, str]:
    """The key/value pairs of one pasted rclone `[remote]` section."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(text if text.lstrip().startswith("[") else f"[remote]\n{text}")
    except configparser.Error:
        raise ToolError(
            "That isn't a valid rclone section. Paste it as it is in rclone.conf."
        ) from None
    if len(parser.sections()) != 1:
        raise ToolError("Paste exactly one [remote] section.")
    values = dict(parser[parser.sections()[0]])
    if values.get("type") != expected_type:
        raise ToolError(f"The section's type must be {expected_type}, not {values.get('type')!r}.")
    unknown = sorted(k for k in values if k not in _PASTE_KEYS or not _KEY.fullmatch(k))
    if unknown:
        raise ToolError(f"These settings aren't accepted here: {', '.join(unknown)}.")
    if "token" not in values:
        raise ToolError("The section has no token: log in with `rclone config` first.")
    values.pop("type")
    return values


def _scrub(text: str, secrets: list[str]) -> str:
    for s in filter(None, secrets):
        text = text.replace(s, "***")
    return text


def remotes(binary: str) -> set[str]:
    proc = run([binary, "listremotes"], timeout=30)
    return {line.rstrip(":") for line in proc.stdout.split() if line.endswith(":")}


def create(
    binary: str,
    destination: str,
    kind: RemoteType,
    values: dict[str, str],
) -> str:
    """Creates `reovault-<destination>` (rclone writes the file) and returns its
    name. Raises `ToolError` with a clean message when rclone refuses."""
    name = remote_name(destination)
    if name in remotes(binary):
        raise ToolError(f"An rclone remote named {name} already exists.")
    secrets = [v for f in kind.fields if f.secret and (v := values.get(f.key))]
    if kind.paste:
        params = parse_section(values.get("section", ""), kind.rclone_type)
        secrets += [params["token"]]
        obscure = ["--no-obscure"]
    else:
        params = {f.key: values.get(f.key, "") or f.default for f in kind.fields}
        missing = [f.label for f in kind.fields if f.required and not params[f.key]]
        if missing:
            raise ToolError(f"Fill in: {', '.join(missing)}.")
        params = {k: v for k, v in params.items() if v}
        obscure = ["--obscure" if kind.obscure else "--no-obscure"]
    args = [f"{k}={v}" for k, v in params.items()]
    proc = run(
        [binary, "config", "create", name, kind.rclone_type, *args, "--non-interactive", *obscure],
        timeout=60,
    )
    if proc.returncode != 0:
        last = (proc.stderr.strip().splitlines() or ["rclone failed"])[-1]
        raise ToolError(f"rclone could not create the remote: {_scrub(last, secrets)}")
    return name


def check(binary: str, name: str) -> None:
    """Can the remote be listed? Fast failure, so a wrong password is reported
    now and not at the first backup."""
    proc = run(
        [binary, "lsd", f"{name}:", "--contimeout", "10s", "--retries", "1",
         "--low-level-retries", "1"],
        timeout=90,
    )  # fmt: skip
    if proc.returncode != 0:
        last = (proc.stderr.strip().splitlines() or ["rclone failed"])[-1]
        raise ToolError(f"The destination doesn't answer: {last.split(' ', 3)[-1][:240]}")


def delete(binary: str, name: str) -> None:
    run([binary, "config", "delete", name], timeout=30)
