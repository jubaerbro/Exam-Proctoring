"""Sentinel API application."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from sentinel_api.assessment.judge_router import router as judge_router
from sentinel_api.assessment.router import router as assessment_router
from sentinel_api.auth.router import router as auth_router
from sentinel_api.core.config import get_settings
from sentinel_api.core.errors import install_error_handlers
from sentinel_api.core.health import router as health_router
from sentinel_api.core.logging import configure_logging
from sentinel_api.core.runtime import build_runtime, get_runtime, set_runtime
from sentinel_api.reporting.router import router as org_router

public_router = APIRouter(prefix="/api/v1", tags=["verification"])


@public_router.get("/public-keys")
def public_keys() -> dict[str, list[dict[str, str]]]:
    """Audit chain public keys.

    Unauthenticated on purpose. Verification that requires Sentinel's
    permission is not verification — an auditor must be able to check a chain
    without an account, and a candidate must be able to check their own export
    after their account is gone.
    """
    import base64

    rt = get_runtime()
    raw = rt.audit_keys.public_key_bytes()
    return {
        "keys": [
            {
                "key_fingerprint": rt.audit_key_fingerprint,
                "algorithm": "ed25519",
                "public_key_b64": base64.b64encode(raw).decode("ascii"),
            }
        ]
    }


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    set_runtime(build_runtime(settings))
    yield


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Sentinel API",
        version="0.1.0",
        description=(
            "Evidence-first assessment infrastructure. "
            "PHASE 3: authentication, organizations, roles, question banks, exam "
            "authoring, assignment, delivery, autosave, submission, auto-grading, "
            "and a sandboxed code judge with sample and hidden tests. "
            "Integrity monitoring, the audit chain, evidence capture and the review "
            "layer are NOT IMPLEMENTED."
        ),
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["content-type", "authorization", "x-sentinel-csrf", "idempotency-key"],
    )

    @app.middleware("http")
    async def context_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["x-request-id"] = request.state.request_id
        response.headers["server-timing"] = f"app;dur={(time.perf_counter() - started) * 1000:.1f}"

        # Security headers. CSP is intentionally strict; the API serves JSON,
        # so nothing here needs to execute script.
        response.headers.setdefault("x-content-type-options", "nosniff")
        response.headers.setdefault("referrer-policy", "no-referrer")
        response.headers.setdefault("x-frame-options", "DENY")
        response.headers.setdefault(
            "content-security-policy", "default-src 'none'; frame-ancestors 'none'"
        )
        response.headers.setdefault(
            "permissions-policy", "camera=(self), microphone=(self), display-capture=(self)"
        )
        if settings.is_production:
            response.headers.setdefault(
                "strict-transport-security", "max-age=63072000; includeSubDomains; preload"
            )
        return response

    install_error_handlers(app)

    app.include_router(health_router)
    app.include_router(public_router)
    app.include_router(auth_router)
    app.include_router(org_router)
    app.include_router(assessment_router)
    app.include_router(judge_router)
    return app


app = create_app()
