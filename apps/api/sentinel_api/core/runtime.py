"""Process-wide singletons, wired once at startup.

Kept in one module so tests can substitute a component without monkeypatching
import sites, and so there is a single answer to "where does the signing key
come from".
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sentinel_api.auth.keys import KeyProvider, build_provider
from sentinel_api.auth.secrets_box import SecretBox
from sentinel_api.auth.service import AuthService
from sentinel_api.auth.tokens import AccessTokenCodec
from sentinel_api.core.config import Settings


@dataclass
class Runtime:
    settings: Settings
    audit_keys: KeyProvider
    access_codec: AccessTokenCodec
    auth: AuthService
    #: Optional on purpose. Redis is only needed to enqueue judge work, and an
    #: API that refuses to start without it would take the whole assessment
    #: engine down for a feature most exams do not use. `judge_router` turns a
    #: missing queue into a 409 that says the code is saved.
    redis: object | None = None

    @property
    def audit_key_fingerprint(self) -> str:
        return self.audit_keys.fingerprint()


_runtime: Runtime | None = None


def build_runtime(settings: Settings) -> Runtime:
    # The audit key and the JWT key are separate on purpose: anything able to
    # mint a session token must not thereby be able to forge an audit event.
    # `settings.audit_key_file` / `jwt_key_file`, not the raw strings: a relative
    # path in an env file resolves against the repository root rather than
    # against whatever directory the process happened to start in. See
    # `core.config.repo_root`.
    audit_keys = build_provider(
        settings.signing_key_provider,
        path=str(settings.audit_key_file),
        key_ref=settings.signing_key_ref,
    )

    jwt_key = _load_ed25519(settings.jwt_key_file)
    codec = AccessTokenCodec(
        jwt_key, issuer=settings.jwt_issuer, ttl_seconds=settings.jwt_access_ttl_seconds
    )

    box_path = settings.mfa_key_file
    box = SecretBox.from_file(box_path) if box_path.exists() else None

    redis_client = None
    try:
        import redis as redis_lib

        redis_client = redis_lib.from_url(settings.redis_url, decode_responses=True)
    except Exception:  # noqa: BLE001 - a missing queue must not stop the API booting
        redis_client = None

    return Runtime(
        settings=settings,
        audit_keys=audit_keys,
        access_codec=codec,
        auth=AuthService(box),
        redis=redis_client,
    )


def set_runtime(runtime: Runtime) -> None:
    global _runtime
    _runtime = runtime


def get_runtime() -> Runtime:
    if _runtime is None:
        raise RuntimeError("Runtime not initialised. The application lifespan must run first.")
    return _runtime


def _load_ed25519(path: Path) -> Ed25519PrivateKey:
    if not path.exists():
        raise FileNotFoundError(f"JWT signing key not found at {path}. Run scripts/keygen.py.")
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError("JWT signing key must be Ed25519.")
    return key
