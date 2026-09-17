"""`/healthz` for the Docker healthcheck (see plan: Deployment). The rest of
the dashboard (auth, pages, playback) is Phase 6; this file is its seed, not
a throwaway, Phase 6 adds routes to the same `FastAPI` app rather than
starting over.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from reovault import health
from reovault.archiver import Archiver
from reovault.providers.reolink_cli import ReolinkCliProvider


def create_app(archiver: Archiver) -> FastAPI:
    app = FastAPI(title="ReoVault")

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        checks: dict[str, Any] = {}
        healthy = True

        try:
            archiver.repository.conn.execute("SELECT 1").fetchone()
            checks["database"] = "ok"
        except Exception as exc:  # noqa: BLE001 - report, this endpoint must not raise
            checks["database"] = f"error: {exc}"
            healthy = False

        # Informational only: the gateway starts lazily on first use, so it
        # being down right now doesn't mean the daemon itself is broken.
        if isinstance(archiver.provider, ReolinkCliProvider):
            checks["gateway"] = "listening" if archiver.provider.gateway.is_listening() else "down"

        # Also informational: a coverage alarm means "behind schedule", not
        # "the process is broken". Surfaced here so `docker inspect` and
        # simple monitoring can see it without the full dashboard (Phase 6).
        try:
            status = health.coverage_margin(
                repository=archiver.repository, device_id=archiver.device_id
            )
            checks["coverage_alarm"] = status.alarm
        except Exception as exc:  # noqa: BLE001
            checks["coverage_alarm"] = f"error: {exc}"

        body = {"status": "ok" if healthy else "error", "checks": checks}
        return JSONResponse(body, status_code=200 if healthy else 503)

    return app
