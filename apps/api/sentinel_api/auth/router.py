"""Authentication endpoints."""

from __future__ import annotations

import datetime as dt
import secrets
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from sentinel_api.auth.service import AuthenticatedUser, OrgRole
from sentinel_api.core.db import system_session
from sentinel_api.core.errors import Forbidden, Unauthenticated
from sentinel_api.core.runtime import get_runtime
from sentinel_api.models import ActivityLog, AppUser
from sentinel_api.tenancy.deps import (
    ACCESS_COOKIE,
    CSRF_COOKIE,
    REFRESH_COOKIE,
    Principal,
    authorize,
    db_untenanted,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

MFA_CHALLENGE_TTL = 300  # 5 minutes to type a 6-digit code is generous but humane


# --------------------------------------------------------------------------- schemas


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=1024)


class OrgSummary(BaseModel):
    org_id: uuid.UUID
    slug: str
    name: str
    roles: list[str]


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"  # noqa: S105 - not a credential
    expires_in: int
    org_id: uuid.UUID | None
    roles: list[str]
    organizations: list[OrgSummary]


class MFAChallengeResponse(BaseModel):
    mfa_required: bool = True
    challenge_token: str
    expires_in: int = MFA_CHALLENGE_TTL


class MFAVerifyRequest(BaseModel):
    challenge_token: str
    code: str = Field(min_length=6, max_length=8)


class SwitchOrgRequest(BaseModel):
    org_id: uuid.UUID


class MFAEnrolBeginResponse(BaseModel):
    secret: str
    provisioning_uri: str
    #: Present so a client can say "you have already enrolled" rather than
    #: silently replacing a working authenticator.
    already_enrolled: bool


class MFAEnrolConfirmRequest(BaseModel):
    secret: str = Field(min_length=16, max_length=64)
    code: str = Field(min_length=6, max_length=8)


class MFAEnrolConfirmResponse(BaseModel):
    mfa_enabled: bool = True
    #: The caller's current access token predates enrolment, so its `amr` claim
    #: says `pwd` only. Sign in again to obtain one that satisfies the MFA gate.
    reauthenticate_required: bool = True


class MeResponse(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str
    org_id: uuid.UUID | None
    roles: list[str]
    mfa_enabled: bool
    organizations: list[OrgSummary]


# --------------------------------------------------------------------------- helpers


def _org_summaries(orgs: list[OrgRole]) -> list[OrgSummary]:
    return [
        OrgSummary(org_id=o.org_id, slug=o.org_slug, name=o.org_name, roles=list(o.roles))
        for o in orgs
    ]


def _set_auth_cookies(response: Response, *, access: str, refresh: str) -> str:
    settings = get_runtime().settings
    common: dict[str, Any] = {
        "httponly": True,
        "secure": settings.cookie_secure,
        "samesite": "lax",
        "domain": settings.cookie_domain or None,
    }
    response.set_cookie(
        ACCESS_COOKIE, access, max_age=settings.jwt_access_ttl_seconds, path="/", **common
    )
    response.set_cookie(
        REFRESH_COOKIE,
        refresh,
        max_age=settings.jwt_refresh_ttl_seconds,
        # Scoped to the refresh endpoint: the refresh token is not sent on every
        # request, so an exploit on any other route cannot reach it.
        path="/api/v1/auth",
        **common,
    )
    # The CSRF cookie is deliberately NOT httponly — the SPA must read it to
    # echo it in a header. It carries no authority on its own.
    csrf = secrets.token_urlsafe(32)
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        max_age=settings.jwt_refresh_ttl_seconds,
        path="/",
        httponly=False,
        secure=settings.cookie_secure,
        samesite="lax",
        domain=settings.cookie_domain or None,
    )
    return csrf


def _issue_session(
    session: Session,
    response: Response,
    request: Request,
    auth_user: AuthenticatedUser,
    *,
    org: OrgRole | None,
    amr: tuple[str, ...],
) -> TokenResponse:
    rt = get_runtime()
    access, claims = rt.access_codec.issue(
        user_id=auth_user.user.id,
        org_id=org.org_id if org else None,
        roles=org.roles if org else (),
        amr=amr,
    )
    raw_refresh, _ = rt.auth.issue_refresh(
        session,
        user_id=auth_user.user.id,
        ttl_seconds=rt.settings.jwt_refresh_ttl_seconds,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )
    _set_auth_cookies(response, access=access, refresh=raw_refresh)
    return TokenResponse(
        access_token=access,
        expires_in=rt.settings.jwt_access_ttl_seconds,
        org_id=org.org_id if org else None,
        roles=list(org.roles) if org else [],
        organizations=_org_summaries(auth_user.orgs),
    )


# --------------------------------------------------------------------------- routes


@router.post("/login", response_model=None)
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    session: Session = Depends(db_untenanted),
) -> TokenResponse | MFAChallengeResponse:
    rt = get_runtime()
    ip = request.client.host if request.client else None

    auth_user = rt.auth.authenticate(session, email=str(body.email), password=body.password, ip=ip)

    # Bind to the single organization if there is exactly one; otherwise the
    # client must choose. There is deliberately no "all orgs" token — every
    # request is scoped to exactly one tenant.
    org = auth_user.single_org
    roles_for_mfa = org.roles if org else tuple(r for o in auth_user.orgs for r in o.roles)

    if auth_user.user.mfa_enabled:
        challenge, _ = rt.access_codec.issue(
            user_id=auth_user.user.id,
            org_id=org.org_id if org else None,
            roles=(),
            amr=("pwd",),
            typ="mfa_challenge",
            ttl_seconds=MFA_CHALLENGE_TTL,
        )
        return MFAChallengeResponse(challenge_token=challenge)

    if rt.auth.mfa_required(tuple(roles_for_mfa), rt.settings.mfa_required_role_set):
        # The role requires a second factor but the account has not enrolled.
        # Failing closed is the only safe answer: these roles can read webcam
        # imagery of candidates. No session is issued.
        #
        # What *is* issued is a token that can do nothing except enrol an
        # authenticator. Without it the requirement is unsatisfiable — Phase 1
        # shipped with the check but no enrolment route, which made the reviewer
        # role permanently unusable rather than merely gated.
        enrolment, _ = rt.access_codec.issue(
            user_id=auth_user.user.id,
            org_id=None,
            roles=(),
            amr=("pwd",),
            typ="mfa_enrol",
            ttl_seconds=ENROLMENT_TTL,
        )
        raise Forbidden(
            "This role requires multi-factor authentication. Enrol a TOTP "
            "authenticator before signing in.",
            enrolment_token=enrolment,
            enrolment_expires_in=ENROLMENT_TTL,
        )

    return _issue_session(session, response, request, auth_user, org=org, amr=("pwd",))


@router.post("/mfa/verify", response_model=TokenResponse)
def verify_mfa(
    body: MFAVerifyRequest,
    request: Request,
    response: Response,
    session: Session = Depends(db_untenanted),
) -> TokenResponse:
    rt = get_runtime()
    try:
        claims = rt.access_codec.decode(body.challenge_token)
    except Exception as exc:
        raise Unauthenticated("Invalid or expired MFA challenge.") from exc

    if claims.typ != "mfa_challenge":
        raise Unauthenticated("Token is not an MFA challenge.")

    user = session.get(AppUser, claims.sub)
    if user is None or user.status != "active":
        raise Unauthenticated("Account is not active.")

    if not rt.auth.verify_totp(user, body.code):
        raise Unauthenticated("Incorrect verification code.")

    orgs = rt.auth.load_orgs(session, user.id)
    auth_user = AuthenticatedUser(user=user, orgs=orgs)
    org = next((o for o in orgs if o.org_id == claims.org_id), None) or auth_user.single_org

    return _issue_session(session, response, request, auth_user, org=org, amr=("pwd", "otp"))


ENROLMENT_TTL = 900  # 15 minutes: long enough to install an authenticator app


def _enrolling_user_id(request: Request) -> uuid.UUID:
    """Accept either a normal session or a short-lived enrolment token.

    The second case is the one that matters. MFA is mandatory for `reviewer` and
    `org_admin`, and login fails closed for an account in one of those roles that
    has not enrolled — which, with no way in, meant the role could never be used
    at all. That was Phase 1's most conspicuous gap.

    The way out is not to relax the login check. It is to hand back a token that
    can do exactly one thing: enrol an authenticator. It carries no roles, no
    organization, and `typ='mfa_enrol'`, so `get_principal` rejects it
    everywhere else in the API.
    """
    rt = get_runtime()
    header = request.headers.get("authorization", "")
    raw = (
        header[7:].strip()
        if header.lower().startswith("bearer ")
        else request.cookies.get(ACCESS_COOKIE)
    )
    if not raw:
        raise Unauthenticated("No credentials supplied.")
    try:
        claims = rt.access_codec.decode(raw)
    except Exception as exc:
        raise Unauthenticated("Invalid or expired token.") from exc
    if claims.typ not in ("access", "mfa_enrol"):
        raise Unauthenticated("This token cannot be used to enrol an authenticator.")
    return claims.sub


@router.post("/mfa/enrol", response_model=MFAEnrolBeginResponse)
def begin_mfa_enrolment(
    user_id: uuid.UUID = Depends(_enrolling_user_id),
) -> MFAEnrolBeginResponse:
    """Step 1 of TOTP enrolment: issue a secret and a provisioning URI.

    The secret is returned to the caller and **not** stored. Storing it here
    would enable an account whose owner never successfully scanned the QR code,
    and locking someone out of a role they were just granted is a worse failure
    than making them repeat the step. `confirm` echoes the secret back with a
    code derived from it, which proves the authenticator actually holds it.

    That echo is safe because this endpoint is authenticated and the secret has
    no authority until it is confirmed — but it does mean the secret passes
    through the client twice, so this route is one of the reasons the API is
    HTTPS-only outside development.
    """
    rt = get_runtime()
    with system_session() as session:
        user = session.get(AppUser, user_id)
        if user is None:
            raise Unauthenticated("Account not found.")
        secret, uri = rt.auth.provision_totp(user, issuer="Sentinel")
        return MFAEnrolBeginResponse(
            secret=secret, provisioning_uri=uri, already_enrolled=user.mfa_enabled
        )


@router.post("/mfa/enrol/confirm", response_model=MFAEnrolConfirmResponse)
def confirm_mfa_enrolment(
    body: MFAEnrolConfirmRequest,
    user_id: uuid.UUID = Depends(_enrolling_user_id),
) -> MFAEnrolConfirmResponse:
    """Step 2: prove the authenticator holds the secret, then store it encrypted.

    Re-enrolment is permitted — people replace phones — but it requires a valid
    code from the *new* secret, so an attacker with a stolen session cannot
    silently swap the second factor for one they control without also passing a
    challenge. That is weaker than requiring the *old* code as well, which is
    the correct control and is recorded as a limitation rather than claimed.
    """
    import pyotp

    rt = get_runtime()
    if rt.auth._box is None:  # noqa: SLF001 - the runtime owns this collaborator
        raise Forbidden(
            "TOTP enrolment is unavailable: no MFA key is configured. Run scripts/keygen.py."
        )

    if not pyotp.TOTP(body.secret).verify(body.code, valid_window=1):
        raise Unauthenticated("That code does not match the secret. Check your authenticator.")

    with system_session() as session:
        user = session.get(AppUser, user_id)
        if user is None:
            raise Unauthenticated("Account not found.")
        # Declare who this is before writing to `activity_log`. The org-less
        # read policy is "rows about yourself", and SQLAlchemy's INSERT ...
        # RETURNING needs SELECT visibility of the row it just wrote — without
        # this the insert fails with an RLS violation. Same trap as
        # `AuthService.rotate_refresh`.
        session.execute(
            text("SELECT set_config('sentinel.user_id', :u, true)"), {"u": str(user.id)}
        )
        user.mfa_secret_enc = rt.auth._box.encrypt(body.secret)  # noqa: SLF001
        user.mfa_enabled_at = dt.datetime.now(dt.UTC)
        session.add(
            ActivityLog(
                actor_user_id=user.id,
                action="auth.mfa_enrolled",
                object_type="app_user",
                object_id=user.id,
            )
        )
    return MFAEnrolConfirmResponse()


@router.post("/refresh", response_model=TokenResponse)
def refresh(request: Request, response: Response) -> TokenResponse:
    rt = get_runtime()
    raw = request.cookies.get(REFRESH_COOKIE)
    if not raw:
        raise Unauthenticated("No refresh token supplied.")

    with system_session() as session:
        new_raw, row = rt.auth.rotate_refresh(
            session,
            raw_token=raw,
            ttl_seconds=rt.settings.jwt_refresh_ttl_seconds,
            user_agent=request.headers.get("user-agent"),
            ip=request.client.host if request.client else None,
        )
        user = rt.auth.user_for_refresh(session, row)
        orgs = rt.auth.load_orgs(session, user.id)

        # Roles are re-read from the database on every refresh rather than
        # copied from the old token. This is why revoking a role takes effect
        # within one access-token lifetime and needs no blacklist.
        org = next((o for o in orgs if o.org_id == _org_hint(request)), None)
        if org is None and len(orgs) == 1:
            org = orgs[0]

        access, _ = rt.access_codec.issue(
            user_id=user.id,
            org_id=org.org_id if org else None,
            roles=org.roles if org else (),
            amr=("pwd", "otp") if user.mfa_enabled else ("pwd",),
        )
        _set_auth_cookies(response, access=access, refresh=new_raw)
        return TokenResponse(
            access_token=access,
            expires_in=rt.settings.jwt_access_ttl_seconds,
            org_id=org.org_id if org else None,
            roles=list(org.roles) if org else [],
            organizations=_org_summaries(orgs),
        )


def _org_hint(request: Request) -> uuid.UUID | None:
    """Best-effort read of the org from an expired access cookie.

    Refresh should keep the caller in the organization they were working in.
    The value is a hint only — it is re-validated against live memberships.
    """
    token = request.cookies.get(ACCESS_COOKIE)
    if not token:
        return None
    try:
        import jwt as _jwt

        payload = _jwt.decode(token, options={"verify_signature": False, "verify_exp": False})
        return uuid.UUID(payload["org_id"]) if payload.get("org_id") else None
    except Exception:
        return None


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response) -> Response:
    rt = get_runtime()
    raw = request.cookies.get(REFRESH_COOKIE)
    if raw:
        with system_session() as session:
            rt.auth.revoke_refresh(session, raw_token=raw)

    for name, path in ((ACCESS_COOKIE, "/"), (REFRESH_COOKIE, "/api/v1/auth"), (CSRF_COOKIE, "/")):
        response.delete_cookie(name, path=path, domain=rt.settings.cookie_domain or None)
    response.status_code = 204
    return response


@router.post("/switch-org", response_model=TokenResponse)
def switch_org(
    body: SwitchOrgRequest,
    request: Request,
    response: Response,
    principal: Principal = Depends(authorize),
) -> TokenResponse:
    rt = get_runtime()
    with system_session() as session:
        user = session.get(AppUser, principal.user_id)
        if user is None:
            raise Unauthenticated("Account not found.")
        orgs = rt.auth.load_orgs(session, user.id)
        target = next((o for o in orgs if o.org_id == body.org_id), None)
        if target is None:
            # 404-equivalent: do not confirm that the organization exists.
            raise Forbidden("You are not a member of that organization.")

        access, _ = rt.access_codec.issue(
            user_id=user.id,
            org_id=target.org_id,
            roles=target.roles,
            amr=("pwd", "otp") if user.mfa_enabled else ("pwd",),
        )
        raw_refresh, _ = rt.auth.issue_refresh(
            session,
            user_id=user.id,
            ttl_seconds=rt.settings.jwt_refresh_ttl_seconds,
            user_agent=request.headers.get("user-agent"),
            ip=request.client.host if request.client else None,
        )
        _set_auth_cookies(response, access=access, refresh=raw_refresh)
        return TokenResponse(
            access_token=access,
            expires_in=rt.settings.jwt_access_ttl_seconds,
            org_id=target.org_id,
            roles=list(target.roles),
            organizations=_org_summaries(orgs),
        )


@router.get("/me", response_model=MeResponse)
def me(principal: Principal = Depends(authorize)) -> MeResponse:
    rt = get_runtime()
    with system_session() as session:
        user = session.get(AppUser, principal.user_id)
        if user is None:
            raise Unauthenticated("Account not found.")
        orgs = rt.auth.load_orgs(session, user.id)
        return MeResponse(
            user_id=user.id,
            email=user.email,
            display_name=user.display_name,
            org_id=principal.org_id,
            roles=sorted(principal.roles),
            mfa_enabled=user.mfa_enabled,
            organizations=_org_summaries(orgs),
        )
