"""Health endpoints.

``/health`` is liveness. It must not touch the database. A liveness probe that
queries Postgres turns a slow query into an orchestrator restart storm, which
converts a degradation into an outage.

``/ready`` is readiness. It checks the dependencies the process genuinely needs
to serve traffic and reports which one is unhappy.
"""

from __future__ import annotations

import time
from typing import Any

import redis
from fastapi import APIRouter, Response
from sqlalchemy import text

from sentinel_api.core.config import get_settings
from sentinel_api.core.db import get_engine

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
def ready(response: Response) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    ok = True

    started = time.perf_counter()
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["postgres"] = {"ok": True, "ms": round((time.perf_counter() - started) * 1000, 1)}
    except Exception as exc:
        ok = False
        checks["postgres"] = {"ok": False, "error": type(exc).__name__}

    started = time.perf_counter()
    try:
        client = redis.Redis.from_url(get_settings().redis_url, socket_connect_timeout=2)
        client.ping()
        checks["redis"] = {"ok": True, "ms": round((time.perf_counter() - started) * 1000, 1)}
    except Exception as exc:
        ok = False
        checks["redis"] = {"ok": False, "error": type(exc).__name__}

    # Object storage is NOT checked here yet — evidence upload is Phase 6, and
    # a readiness check for a subsystem that does not exist would be theatre.
    checks["object_storage"] = {"ok": None, "note": "NOT IMPLEMENTED (Phase 6)"}

    if not ok:
        response.status_code = 503
    return {"status": "ready" if ok else "degraded", "checks": checks}
