"""Request-scoped dependencies: the caller's identity, tenant, and session.

The chain is: cookie (or Authorization header) → access token → principal →
tenant-bound database session with ``SET LOCAL``. Nothing downstream needs to
remember to filter by organization, because the database will not return rows
from another one.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from sentinel_api.auth.tokens import TokenError
from sentinel_api.core.db import system_session, tenant_session
from sentinel_api.core.errors import Forbidden, Unauthenticated
from sentinel_api.core.runtime import get_runtime
from sentinel_api.tenancy.policies import Rule, permits, rule_for

ACCESS_COOKIE = "sentinel_access"
REFRESH_COOKIE = "sentinel_refresh"
CSRF_COOKIE = "sentinel_csrf"
CSRF_HEADER = "x-sentinel-csrf"


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    org_id: uuid.UUID | None
    roles: frozenset[str]
    has_mfa: bool

    @property
    def primary_role(self) -> str:
        """The role used for RLS context.

        Where a user holds several roles in one organization, the most
        privileged wins — a user who is both instructor and reviewer should not
        lose reviewer visibility because of alphabetical ordering.
        """
        for role in ("org_admin", "instructor", "reviewer", "candidate"):
            if role in self.roles:
                return role
        raise Forbidden("No usable role for this organization.")


def _extract_token(request: Request) -> str | None:
    """Explicit credentials beat ambient ones.

    An Authorization header is something the caller deliberately attached; a
    cookie is sent automatically by the browser. If both are present, honouring
    the cookie would mean a caller who presented a token could silently act as
    whoever the cookie belongs to — surprising, and a real hazard for API
    clients running in a browser context.
    """
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.cookies.get(ACCESS_COOKIE)


def get_principal(request: Request) -> Principal:
    token = _extract_token(request)
    if not token:
        raise Unauthenticated("No credentials supplied.")

    try:
        claims = get_runtime().access_codec.decode(token)
    except TokenError as exc:
        raise Unauthenticated(f"Invalid access token: {exc}") from exc

    if not claims.is_access:
        # An mfa_challenge token is not an access token. Accepting one here
        # would let a caller skip the second factor entirely.
        raise Unauthenticated("Token is not an access token.")

    return Principal(
        user_id=claims.sub,
        org_id=claims.org_id,
        roles=frozenset(claims.roles),
        has_mfa=claims.has_mfa,
    )


def enforce_csrf(request: Request) -> None:
    """Double-submit CSRF check for cookie-borne authentication.

    Only applies when the caller authenticated by cookie. A Bearer token is not
    sent automatically by the browser, so it is not CSRF-able.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    # If the caller sent a Bearer token, that is the credential this request
    # authenticated with (`_extract_token`, ADR-0017) — the cookie was ignored.
    # A browser will not attach an Authorization header to a cross-site request,
    # so the request is not CSRF-able, and demanding a CSRF header as well would
    # only break non-browser clients that happen to hold a stale cookie.
    #
    # Getting this wrong is not hypothetical: it made every write endpoint
    # return 403 to an API client that had ever logged in through the browser.
    if request.headers.get("authorization", "").lower().startswith("bearer "):
        return
    if not request.cookies.get(ACCESS_COOKIE):
        return

    cookie_value = request.cookies.get(CSRF_COOKIE)
    header_value = request.headers.get(CSRF_HEADER)
    if not cookie_value or not header_value or cookie_value != header_value:
        raise Forbidden("CSRF token missing or mismatched.")

    origin = request.headers.get("origin")
    if origin:
        allowed = get_runtime().settings.cors_origin_list
        if origin not in allowed:
            raise Forbidden("Request origin is not allowed.")


def route_rule(request: Request) -> Rule:
    """Resolve this route's declared policy, or fail loudly."""
    route = request.scope.get("route")
    path = getattr(route, "path", request.url.path)
    return rule_for(request.method, path)


def authorize(request: Request, principal: Principal = Depends(get_principal)) -> Principal:
    rule = route_rule(request)
    enforce_csrf(request)

    if not permits(rule, principal.roles):
        raise Forbidden("Your role does not permit this action.")

    if not rule.org_optional and principal.org_id is None:
        raise Forbidden("Select an organization first (POST /api/v1/auth/switch-org).")

    settings = get_runtime().settings
    if principal.roles & settings.mfa_required_role_set and not principal.has_mfa:
        # Reviewers and org admins can read webcam images of people in their
        # homes. That access is gated on a second factor, not just a password.
        raise Forbidden("Multi-factor authentication is required for this role.")

    return principal


def db(principal: Principal = Depends(authorize)) -> Iterator[Session]:
    """Tenant-bound session for the current request."""
    with tenant_session(
        org_id=str(principal.org_id) if principal.org_id else None,
        user_id=str(principal.user_id),
        role=principal.primary_role if principal.org_id else None,
    ) as session:
        yield session


def db_untenanted() -> Iterator[Session]:
    """Session with no tenant context, for login and health checks."""
    with system_session() as session:
        yield session
