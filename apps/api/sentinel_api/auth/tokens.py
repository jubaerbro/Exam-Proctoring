"""Access and refresh tokens.

Access token: JWT, EdDSA, 15 minutes, carries identity and roles but **not
permissions**. Permissions are resolved server-side per request, so revoking a
role takes effect within one access-token lifetime instead of requiring a
blacklist.

Refresh token: opaque 256-bit random, stored only as a SHA-256 hash, rotating,
with family reuse detection. Presenting an already-rotated token means the token
leaked — the entire family is revoked and the event is logged. That signal is
the main reason to prefer rotating opaque refresh tokens over long-lived JWTs.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ALGORITHM = "EdDSA"


@dataclass(frozen=True)
class AccessClaims:
    sub: uuid.UUID
    org_id: uuid.UUID | None
    roles: tuple[str, ...]
    jti: uuid.UUID
    exp: dt.datetime
    amr: tuple[str, ...]  # authentication methods: ("pwd",) or ("pwd", "otp")
    typ: str = "access"

    @property
    def has_mfa(self) -> bool:
        return "otp" in self.amr

    @property
    def is_access(self) -> bool:
        return self.typ == "access"


class TokenError(Exception):
    pass


class AccessTokenCodec:
    def __init__(self, private_key: Ed25519PrivateKey, *, issuer: str, ttl_seconds: int) -> None:
        self._private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        self._public_pem = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self._issuer = issuer
        self._ttl = ttl_seconds

    def issue(
        self,
        *,
        user_id: uuid.UUID,
        org_id: uuid.UUID | None,
        roles: tuple[str, ...],
        amr: tuple[str, ...] = ("pwd",),
        typ: str = "access",
        ttl_seconds: int | None = None,
    ) -> tuple[str, AccessClaims]:
        now = dt.datetime.now(dt.UTC)
        exp = now + dt.timedelta(seconds=ttl_seconds if ttl_seconds is not None else self._ttl)
        jti = uuid.uuid4()
        payload: dict[str, Any] = {
            "iss": self._issuer,
            "sub": str(user_id),
            "org_id": str(org_id) if org_id else None,
            "roles": list(roles),
            "amr": list(amr),
            "typ": typ,
            "jti": str(jti),
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()),
            "exp": int(exp.timestamp()),
        }
        token = jwt.encode(payload, self._private_pem, algorithm=ALGORITHM)
        return token, AccessClaims(user_id, org_id, roles, jti, exp, amr, typ)

    def decode(self, token: str) -> AccessClaims:
        try:
            payload = jwt.decode(
                token,
                self._public_pem,
                # Pinning the algorithm list is what stops an "alg: none" or
                # HMAC-with-the-public-key confusion attack.
                algorithms=[ALGORITHM],
                issuer=self._issuer,
                options={"require": ["exp", "iat", "sub", "jti"]},
            )
        except jwt.PyJWTError as exc:
            raise TokenError(str(exc)) from exc

        return AccessClaims(
            sub=uuid.UUID(payload["sub"]),
            org_id=uuid.UUID(payload["org_id"]) if payload.get("org_id") else None,
            roles=tuple(payload.get("roles", [])),
            jti=uuid.UUID(payload["jti"]),
            exp=dt.datetime.fromtimestamp(payload["exp"], dt.UTC),
            amr=tuple(payload.get("amr", ())),
            typ=payload.get("typ", "access"),
        )


# --------------------------------------------------------------------------
# Refresh tokens
# --------------------------------------------------------------------------

REFRESH_BYTES = 32  # 256 bits


def new_refresh_token() -> tuple[str, bytes]:
    """Return (opaque token to give the client, SHA-256 digest to store)."""
    raw = secrets.token_urlsafe(REFRESH_BYTES)
    return raw, hash_refresh_token(raw)


def hash_refresh_token(raw: str) -> bytes:
    """Refresh tokens are stored hashed.

    A database read must not yield usable credentials — the same reason
    passwords are hashed, applied to the longer-lived secret.
    """
    return hashlib.sha256(raw.encode("utf-8")).digest()


def hash_ip(ip: str | None) -> bytes | None:
    """IPs are stored hashed. They are personal data with limited forensic
    value here, since the session identity is already known."""
    if not ip:
        return None
    return hashlib.sha256(ip.encode("utf-8")).digest()
