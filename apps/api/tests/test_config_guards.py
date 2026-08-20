"""Configuration guards.

These assert that Sentinel refuses to start in configurations that would void
its own claims. Refusing to boot is the correct response to a setting that
silently disables the product's central security property.
"""

from __future__ import annotations

import pytest

from sentinel_api.core.config import Settings

BASE = {
    "database_url": "postgresql+psycopg://sentinel_app:x@db:5432/sentinel",
    "cors_origins": "https://app.example.edu",
    "cookie_secure": True,
    "signing_key_provider": "kms",
    "signing_key_ref": "arn:aws:kms:eu-west-1:1:key/abc",
}

pytestmark = pytest.mark.security


def test_production_rejects_file_signing_key() -> None:
    """A file-based signing key in production means anyone with host access can
    forge a chain that verifies — which voids every tamper-evidence claim."""
    with pytest.raises(ValueError, match="SIGNING_KEY_PROVIDER=file"):
        Settings(**{**BASE, "sentinel_env": "production", "signing_key_provider": "file"})


def test_production_requires_a_kms_reference() -> None:
    with pytest.raises(ValueError, match="SIGNING_KEY_REF"):
        Settings(**{**BASE, "sentinel_env": "production", "signing_key_ref": ""})


def test_production_requires_secure_cookies() -> None:
    with pytest.raises(ValueError, match="COOKIE_SECURE"):
        Settings(**{**BASE, "sentinel_env": "production", "cookie_secure": False})


def test_production_rejects_plaintext_cors_origins() -> None:
    with pytest.raises(ValueError, match="http://"):
        Settings(
            **{
                **BASE,
                "sentinel_env": "production",
                "cors_origins": "https://app.example.edu,http://localhost:3000",
            }
        )


def test_superuser_database_url_is_rejected_in_every_environment() -> None:
    """RLS is bypassed for superusers.

    Connecting as `postgres` would disable every tenant-isolation policy while
    every functional test still passed. That failure mode is exactly why this
    is a hard error rather than a warning.
    """
    for env in ("development", "staging", "production"):
        with pytest.raises(ValueError, match="superuser"):
            Settings(
                **{
                    **BASE,
                    "sentinel_env": env,
                    "database_url": "postgresql+psycopg://postgres:x@db:5432/sentinel",
                }
            )


def test_development_permits_the_file_provider() -> None:
    s = Settings(
        **{
            **BASE,
            "sentinel_env": "development",
            "signing_key_provider": "file",
            "cookie_secure": False,
            "cors_origins": "http://localhost:3000",
        }
    )
    assert s.signing_key_provider == "file"
    assert s.is_production is False


def test_kms_provider_is_not_implemented() -> None:
    """Honesty check: production key custody does not exist yet, and the code
    says so loudly rather than silently falling back to something weaker."""
    from sentinel_api.auth.keys import KmsKeyProvider

    with pytest.raises(NotImplementedError, match="not implemented"):
        KmsKeyProvider("arn:aws:kms:eu-west-1:1:key/abc")


# ---------------------------------------------------------------- key paths


def test_relative_key_paths_resolve_against_the_repository_root() -> None:
    """A relative key path must mean one thing, not three.

    `uvicorn` is launched from the repository root, `alembic` from `apps/api`,
    and pytest from wherever the IDE decides. Resolving a relative path against
    the process working directory would make the same `.env.local` point at
    three different files, and the failure — "JWT signing key not found" — says
    nothing about why.
    """
    from sentinel_api.core.config import Settings, repo_root

    settings = Settings(
        signing_key_path=".keys/audit_ed25519.key",
        jwt_private_key_path=".keys/jwt_ed25519.key",
    )
    assert settings.audit_key_file == repo_root() / ".keys" / "audit_ed25519.key"
    assert settings.jwt_key_file == repo_root() / ".keys" / "jwt_ed25519.key"
    assert settings.mfa_key_file == repo_root() / ".keys" / "mfa_aes.key"


def test_absolute_key_paths_are_left_alone() -> None:
    """The container passes `/run/keys/...` and must keep getting it."""
    from sentinel_api.core.config import Settings

    settings = Settings(
        signing_key_path="/run/keys/audit_ed25519.key",
        jwt_private_key_path="/run/keys/jwt_ed25519.key",
    )
    assert str(settings.audit_key_file) == "/run/keys/audit_ed25519.key"
    assert str(settings.mfa_key_file) == "/run/keys/mfa_aes.key"


def test_repo_root_is_the_directory_holding_docker_compose() -> None:
    from sentinel_api.core.config import repo_root

    assert (repo_root() / "docker-compose.yml").is_file()
    assert (repo_root() / "apps" / "api" / "pyproject.toml").is_file()
