"""RFC 9457 problem+json error handling.

One rule matters more than the format: **a request for an object in another
tenant returns 404, never 403.** A 403 confirms that the object exists, and
existence is itself information — "is candidate X enrolled at university Y" is
exactly the sort of thing the tenant boundary is meant to hide.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_BASE = "https://sentinel.dev/errors"
CONTENT_TYPE = "application/problem+json"


class SentinelError(Exception):
    """Base class for errors that map onto a problem document."""

    status: int = 500
    slug: str = "internal-error"
    title: str = "Internal error"

    def __init__(self, detail: str | None = None, **extra: Any) -> None:
        self.detail = detail or self.title
        self.extra = extra
        super().__init__(self.detail)


class NotFound(SentinelError):
    status, slug, title = 404, "not-found", "Not found"


class Unauthenticated(SentinelError):
    status, slug, title = 401, "unauthenticated", "Authentication required"


class Forbidden(SentinelError):
    """Caller is authenticated and the object is in their tenant, but their role
    does not permit the action. Never used for cross-tenant access."""

    status, slug, title = 403, "forbidden", "Not permitted"


class Conflict(SentinelError):
    status, slug, title = 409, "conflict", "Conflict"


class ValidationFailed(SentinelError):
    status, slug, title = 422, "validation-failed", "Validation failed"


class RateLimited(SentinelError):
    status, slug, title = 429, "rate-limited", "Too many requests"


class MFARequired(SentinelError):
    status, slug, title = 401, "mfa-required", "Multi-factor authentication required"


def problem_response(
    *, status: int, slug: str, title: str, detail: str, instance: str, request_id: str, **extra: Any
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"{PROBLEM_BASE}/{slug}",
        "title": title,
        "status": status,
        "detail": detail,
        "instance": instance,
        "request_id": request_id,
    }
    body.update(extra)
    return JSONResponse(status_code=status, content=body, media_type=CONTENT_TYPE)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(SentinelError)
    async def _sentinel(request: Request, exc: SentinelError) -> JSONResponse:
        return problem_response(
            status=exc.status,
            slug=exc.slug,
            title=exc.title,
            detail=exc.detail,
            instance=request.url.path,
            request_id=getattr(request.state, "request_id", "-"),
            **exc.extra,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {
                "pointer": "/" + "/".join(str(p) for p in e["loc"][1:]),
                "message": e["msg"],
            }
            for e in exc.errors()
        ]
        return problem_response(
            status=422,
            slug="validation-failed",
            title="Validation failed",
            detail="The request body did not validate.",
            instance=request.url.path,
            request_id=getattr(request.state, "request_id", "-"),
            errors=errors,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem_response(
            status=exc.status_code,
            slug="http-error",
            title=str(exc.detail),
            detail=str(exc.detail),
            instance=request.url.path,
            request_id=getattr(request.state, "request_id", "-"),
        )
