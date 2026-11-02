"""Download the pinned reolink-cli, restic and rclone, verify each against its
published SHA-256, and put the binaries in one folder. Stdlib only.

The versions come from the Dockerfile's ARG lines (the one source of truth),
or from REOLINK_CLI_VERSION / RESTIC_RELEASE / RCLONE_RELEASE when set, which
is how the Dockerfile's own `tools` stage passes them in.

    python3 scripts/fetch_tools.py .tools                     # local dev/tests
    python3 scripts/fetch_tools.py --only restic,rclone ~/bin  # CI
"""

from __future__ import annotations

import argparse
import bz2
import hashlib
import io
import os
import platform
import re
import stat
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

# Next to scripts/ in a checkout; absent when the Dockerfile copies this file
# alone, where the versions come from the environment instead.
DOCKERFILE = Path(__file__).resolve().parent.parent / "Dockerfile"
ARGS = {
    "reolink-cli": "REOLINK_CLI_VERSION",
    "restic": "RESTIC_RELEASE",
    "rclone": "RCLONE_RELEASE",
}


def pinned() -> dict[str, str]:
    text = DOCKERFILE.read_text() if DOCKERFILE.exists() else ""
    versions = {}
    for tool, arg in ARGS.items():
        m = re.search(rf"^ARG {arg}=(\S+)$", text, re.M)
        versions[tool] = os.environ.get(arg) or (m.group(1) if m else "")
        if not versions[tool]:
            sys.exit(f"no version for {tool}: set {arg} or add `ARG {arg}=...` to the Dockerfile")
    return versions


def get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 - fixed https URLs
        data: bytes = resp.read()
        return data


def checked(url: str, sums: str, name: str) -> bytes:
    data = get(url)
    want = next((ln.split()[0] for ln in sums.splitlines() if ln.split()[-1:] == [name]), None)
    if want is None:
        sys.exit(f"{name} is not listed in its checksum file")
    if hashlib.sha256(data).hexdigest() != want:
        sys.exit(f"{name}: SHA-256 mismatch")
    print(f"{name}: OK")
    return data


def write(dest: Path, name: str, data: bytes) -> None:
    path = dest / name
    path.write_bytes(data)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def reolink_cli(v: str, arch: str, dest: Path) -> None:
    rl_arch = {"amd64": "x86_64", "arm64": "arm64"}[arch]
    name = f"reolink-cli-{v}-external-linux-{rl_arch}.tar.gz"
    base = f"https://github.com/reolink/reolink-cli/releases/download/v{v}"
    data = checked(f"{base}/{name}", get(f"{base}/{name}.sha256").decode(), name)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for binary in ("reolink-cli", "reolink-gateway"):
            member = tar.extractfile(f"{name[: -len('.tar.gz')]}/bin/{binary}")
            assert member is not None
            write(dest, binary, member.read())


def restic(v: str, arch: str, dest: Path) -> None:
    name = f"restic_{v}_linux_{arch}.bz2"
    base = f"https://github.com/restic/restic/releases/download/v{v}"
    data = checked(f"{base}/{name}", get(f"{base}/SHA256SUMS").decode(), name)
    write(dest, "restic", bz2.decompress(data))


def rclone(v: str, arch: str, dest: Path) -> None:
    name = f"rclone-v{v}-linux-{arch}.zip"
    base = f"https://github.com/rclone/rclone/releases/download/v{v}"
    data = checked(f"{base}/{name}", get(f"{base}/SHA256SUMS").decode(errors="replace"), name)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        write(dest, "rclone", z.read(f"rclone-v{v}-linux-{arch}/rclone"))


FETCHERS = {"reolink-cli": reolink_cli, "restic": restic, "rclone": rclone}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("dest", type=Path)
    p.add_argument(
        "--arch", default={"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine(), "amd64")
    )
    p.add_argument("--only", default=",".join(FETCHERS), help="comma-separated tools")
    args = p.parse_args()
    args.dest.mkdir(parents=True, exist_ok=True)
    versions = pinned()
    for tool in args.only.split(","):
        FETCHERS[tool](versions[tool], args.arch, args.dest)


if __name__ == "__main__":
    main()
