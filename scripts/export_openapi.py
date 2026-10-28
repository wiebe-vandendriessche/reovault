"""Writes the `/api/v1` OpenAPI schema for the dashboard's generated types:

    uv run python scripts/export_openapi.py dashboard/openapi.json
    cd dashboard && npm run gen:api

The schema depends only on the route table, so the app is built over a
throwaway temp dir. CI reruns this and fails when the committed file is stale.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


def main(out: Path) -> None:
    from reovault.camera import Gateway
    from reovault.config import Settings, StorageConfig, WebConfig
    from reovault.config_store import ConfigStore
    from reovault.db import Db
    from reovault.runtime import Runtime
    from reovault.web.app import create_app, openapi_schema

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        settings = Settings(
            storage=StorageConfig(data_dir=root, staging_dir=root / "staging"),
            web=WebConfig(session_key_path=root / "session.key"),
        )
        rt = Runtime(ConfigStore(None, settings), Db(root / "db.sqlite"), Gateway("x", "x"))
        schema = openapi_schema(create_app(rt, dashboard_dir=root))
        rt.db.close()
    out.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "dashboard/openapi.json"))
