"""Authentication service.

Holds the logic that decides who someone is. Deliberately separate from the
router so it can be tested without HTTP, and so the OIDC seam (ADR-0002) has
one place to plug into rather than being scattered through endpoint handlers.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field

import pyotp
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from sentinel_api.auth.passwords import verify_password
from sentinel_api.auth.secrets_box import SecretBox
from sentinel_api.auth.tokens import hash_ip, hash_refresh_token, new_refresh_token
from sentinel_api.core.errors import Forbidden, Unauthenticated
from sentinel_api.models import ActivityLog, AppUser, Membership, RefreshToken

# Backoff schedule after consecutive failures. Deliberately not a permanent
# lockout: a permanent lock is a denial-of-service anyone can trigger against a
# candidate on exam day by guessing their password five times.
_LOCKOUT_SECONDS = {5: 60, 6: 300, 7: 900, 8: 3600}
_MAX_LOCKOUT = 3600


@dataclass(frozen=True)
class OrgRole:
    org_id: uuid.UUID
    org_slug: str
    org_name: str
    roles: tuple[str, ...]


@dataclass
class AuthenticatedUser:
    user: AppUser
    orgs: list[OrgRole] = field(default_factory=list)

    @property
    def single_org(self) -> OrgRole | None:
        return self.orgs[0] if len(self.orgs) == 1 else None

    def find_org(self, org_id: uuid.UUID) -> OrgRole | None:
        return next((o for o in self.orgs if o.org_id == org_id), None)


class AuthService:
    def __init__(self, secret_box: SecretBox | None = None) -> None:
        self._box = secret_box

    # ------------------------------------------------------------------ login
    def authenticate(
        self, session: Session, *, email: str, password: str, ip: str | None = None
    ) -> AuthenticatedUser:
        """Verify credentials. Raises Unauthenticated on any failure.

        Every failure path returns the same error text. Distinguishing
        "no such user" from "wrong password" enumerates accounts, and an
        account list for a university is exactly the sort of thing an attacker
        wants before a credential-stuffing run.
        """
        generic = "Invalid email or password."

        user = session.scalar(select(AppUser).where(AppUser.email == email.strip().lower()))

        if user is None:
            # Still spend the cost of a hash so timing does not leak existence.
            verify_password(password, None)
            raise Unauthenticated(generic)

        now = dt.datetime.now(dt.UTC)
        if user.locked_until and user.locked_until > now:
            raise Unauthenticated(generic)

        if user.status != "active":
            verify_password(password, None)
            raise Unauthenticated(generic)

        if not verify_password(password, user.password_hash):
            self._record_failure(session, user, ip)
            raise Unauthenticated(generic)

        session.execute(
            text("SELECT set_config('sentinel.user_id', :u, true)"), {"u": str(user.id)}
        )
        self._record_success(session, user, ip)

        orgs = self.load_orgs(session, user.id)
        if not orgs:
            # Authenticated but belongs to nothing. Not an auth failure — a
            # provisioning one — and worth saying so, since the credentials
            # were correct and hiding it only confuses a legitimate user.
            raise Forbidden("This account is not a member of any organization.")

        return AuthenticatedUser(user=user, orgs=orgs)

    def _record_failure(self, session: Session, user: AppUser, ip: str | None) -> None:
        session.execute(
            text("SELECT set_config('sentinel.user_id', :u, true)"), {"u": str(user.id)}
        )
        user.failed_logins += 1
        delay = _LOCKOUT_SECONDS.get(user.failed_logins)
        if user.failed_logins > max(_LOCKOUT_SECONDS):
            delay = _MAX_LOCKOUT
        if delay:
            user.locked_until = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=delay)
        session.add(
            ActivityLog(
                actor_user_id=user.id,
                action="auth.login_failed",
                detail={"failed_logins": user.failed_logins},
                ip_hash=hash_ip(ip),
            )
        )

    def _record_success(self, session: Session, user: AppUser, ip: str | None) -> None:
        user.failed_logins = 0
        user.locked_until = None
        user.last_login_at = dt.datetime.now(dt.UTC)
        session.add(ActivityLog(actor_user_id=user.id, action="auth.login", ip_hash=hash_ip(ip)))

    def load_orgs(self, session: Session, user_id: uuid.UUID) -> list[OrgRole]:
        """Read the user's memberships.

        Login has to discover which organizations exist before it can bind to
        one, so this runs with no ``org_id`` in context. It sets
        ``sentinel.user_id`` first — the membership RLS policy permits
        ``user_id = current_user_id()`` precisely to make this query possible
        without a blanket exemption. The setting is transaction-local, so it
        does not widen anything beyond this call's transaction.
        """
        from sentinel_api.models import Organization  # local import: avoids cycle

        session.execute(
            text("SELECT set_config('sentinel.user_id', :uid, true)"), {"uid": str(user_id)}
        )

        rows = session.execute(
            select(Membership, Organization)
            .join(Organization, Organization.id == Membership.org_id)
            .where(Membership.user_id == user_id, Membership.status == "active")
            .order_by(Organization.name)
        ).all()

        grouped: dict[uuid.UUID, list[str]] = {}
        meta: dict[uuid.UUID, tuple[str, str]] = {}
        for membership, org in rows:
            if org.status != "active":
                continue
            grouped.setdefault(org.id, []).append(membership.role)
            meta[org.id] = (org.slug, org.name)

        return [
            OrgRole(
                org_id=oid, org_slug=meta[oid][0], org_name=meta[oid][1], roles=tuple(sorted(r))
            )
            for oid, r in grouped.items()
        ]

    # -------------------------------------------------------------------- mfa
    def mfa_required(self, roles: tuple[str, ...], required_roles: frozenset[str]) -> bool:
        """MFA is mandatory for roles that can read candidate webcam evidence."""
        return bool(set(roles) & required_roles)

    def verify_totp(self, user: AppUser, code: str) -> bool:
        if self._box is None:
            raise RuntimeError("SecretBox is required to verify TOTP codes.")
        if not user.mfa_secret_enc:
            return False
        secret = self._box.decrypt(user.mfa_secret_enc)
        # valid_window=1 accepts the adjacent 30s step, covering ordinary clock
        # skew on the user's phone without meaningfully widening the window.
        return pyotp.TOTP(secret).verify(code, valid_window=1)

    def provision_totp(self, user: AppUser, *, issuer: str = "Sentinel") -> tuple[str, str]:
        """Return (secret, provisioning URI). Caller stores the encrypted secret."""
        if self._box is None:
            raise RuntimeError("SecretBox is required to provision TOTP.")
        secret = pyotp.random_base32()
        uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name=issuer)
        return secret, uri

    # ---------------------------------------------------------------- refresh
    def issue_refresh(
        self,
        session: Session,
        *,
        user_id: uuid.UUID,
        ttl_seconds: int,
        family_id: uuid.UUID | None = None,
        user_agent: str | None = None,
        ip: str | None = None,
    ) -> tuple[str, RefreshToken]:
        raw, digest = new_refresh_token()
        row = RefreshToken(
            user_id=user_id,
            family_id=family_id or uuid.uuid4(),
            token_hash=digest,
            expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(seconds=ttl_seconds),
            user_agent=(user_agent or "")[:512] or None,
            ip_hash=hash_ip(ip),
        )
        session.add(row)
        session.flush()
        return raw, row

    def rotate_refresh(
        self,
        session: Session,
        *,
        raw_token: str,
        ttl_seconds: int,
        user_agent: str | None = None,
        ip: str | None = None,
    ) -> tuple[str, RefreshToken]:
        """Exchange a refresh token for a new one.

        Reuse detection: if the presented token has already been rotated or
        revoked, the token leaked. Revoke the whole family — the attacker and
        the legitimate user both lose the session, which is correct, because we
        cannot tell which one is presenting it.
        """
        digest = hash_refresh_token(raw_token)
        row = session.scalar(select(RefreshToken).where(RefreshToken.token_hash == digest))

        if row is None:
            raise Unauthenticated("Invalid refresh token.")

        # We now know who this is. Declare it, so that activity_log rows written
        # below satisfy the "org-less rows about yourself" read policy — an
        # INSERT ... RETURNING needs SELECT visibility of the row it just wrote.
        session.execute(
            text("SELECT set_config('sentinel.user_id', :u, true)"), {"u": str(row.user_id)}
        )

        now = dt.datetime.now(dt.UTC)

        if row.revoked_at is not None or row.replaced_by is not None:
            self._revoke_family(session, row.family_id, reason="refresh_reuse")
            session.add(
                ActivityLog(
                    actor_user_id=row.user_id,
                    action="auth.refresh_reuse_detected",
                    detail={"family_id": str(row.family_id)},
                    ip_hash=hash_ip(ip),
                )
            )
            # Commit before raising. The caller's session context rolls back on
            # exception, which would undo the revocation — the security action
            # would be silently reverted by the error that reports it, and the
            # stolen token would keep working.
            session.commit()
            raise Unauthenticated("Refresh token reuse detected; session revoked.")

        if row.expires_at <= now:
            raise Unauthenticated("Refresh token expired.")

        new_raw, new_row = self.issue_refresh(
            session,
            user_id=row.user_id,
            ttl_seconds=ttl_seconds,
            family_id=row.family_id,
            user_agent=user_agent,
            ip=ip,
        )
        row.revoked_at = now
        row.replaced_by = new_row.id
        return new_raw, new_row

    def revoke_refresh(self, session: Session, *, raw_token: str) -> None:
        digest = hash_refresh_token(raw_token)
        session.execute(
            update(RefreshToken)
            .where(RefreshToken.token_hash == digest, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=dt.datetime.now(dt.UTC))
        )

    def _revoke_family(self, session: Session, family_id: uuid.UUID, *, reason: str) -> None:
        session.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=dt.datetime.now(dt.UTC))
        )

    def user_for_refresh(self, session: Session, token_row: RefreshToken) -> AppUser:
        user = session.get(AppUser, token_row.user_id)
        if user is None or user.status != "active":
            raise Unauthenticated("Account is not active.")
        return user
