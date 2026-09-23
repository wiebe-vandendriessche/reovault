"""ReoVault: local-first, self-hosted archiving of Reolink camera recordings."""

import os
import subprocess
from importlib.metadata import version


def _resolve_version() -> str:
    # Release builds bake the git tag in as REOVAULT_VERSION (see Dockerfile
    # + .github/workflows/release.yml); that's authoritative when present.
    env_version = os.environ.get("REOVAULT_VERSION")
    if env_version:
        return env_version
    # Otherwise (local dev, CI, non-release Docker builds) describe the
    # working tree so the version reflects what's actually running instead
    # of the static, rarely-bumped pyproject.toml version.
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=os.path.dirname(__file__),
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return version("reovault")


__version__ = _resolve_version()
