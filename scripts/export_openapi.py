"""Writes the `/api/v1` OpenAPI schema for the dashboard's generated types:

    uv run python scripts/export_openapi.py dashboard/openapi.json
    cd dashboard && npm run gen:api

The app is built against a throwaway temp dir: the schema depends only on
the route table, never on a real database, vault, or camera. CI reruns this
and fails when the committed file is stale.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


def main(out: Path) -> None:
    from reovault.config import Settings, StorageConfig, WebConfig
    from reovault.db.repository import Repository
    from reovault.fleet import Fleet
    from reovault.providers.gateway import GatewaySupervisor
    from reovault.storage.vault import EncryptedFsVault
    from reovault.web.app import create_app, openapi_schema

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        settings = Settings(
            storage=StorageConfig(
                config_dir=root, vault_dir=root / "vault", staging_dir=root / "staging"
            ),
            web=WebConfig(session_key_path=root / "session.key"),
        )
        repo = Repository(root / "db.sqlite")
        repo.migrate()
        fleet = Fleet(
            settings=settings,
            repository=repo,
            vault=EncryptedFsVault(root / "vault", os.urandom(32)),
            gateway=GatewaySupervisor(binary="reolink-cli"),
        )
        schema = openapi_schema(create_app(fleet, settings=settings, repository=repo))
        repo.close()
    out.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "dashboard/openapi.json"))
