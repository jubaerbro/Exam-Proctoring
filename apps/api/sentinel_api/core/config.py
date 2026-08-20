"""Application configuration.

Everything comes from the environment. Nothing is defaulted to a value that
would be dangerous in production, and two settings are validated hard enough to
stop the process from starting.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "staging", "production"]


def repo_root() -> Path:
    """The repository root, found by walking up for `docker-compose.yml`.

    Key paths are allowed to be relative — `.keys/audit_ed25519.key` is much
    friendlier in a per-developer env file than an absolute Windows path. But a
    relative path resolved against the *process* working directory means the
    same configuration points somewhere different depending on whether you ran
    `uvicorn` from the repository root, `alembic` from `apps/api`, or `pytest`
    from wherever your IDE decided. That is a configuration setting that quietly
    means three things, which is worse than not supporting it at all.

    Resolving against the repository root instead makes a relative path mean one
    thing. Absolute paths — `/run/keys/...` in the container — are untouched.
    """
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "docker-compose.yml").is_file():
            return candidate
    # Installed without the repository around it (a wheel, a slim image layer).
    # `apps/api/sentinel_api/core/config.py` -> four levels up.
    return here.parents[4]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # -- environment -------------------------------------------------------
    sentinel_env: Environment = "development"
    log_level: str = "INFO"
    api_base_url: str = "http://localhost:8000"
    web_base_url: str = "http://localhost:3000"

    # -- database ----------------------------------------------------------
    database_url: str = "postgresql+psycopg://sentinel_app:sentinel@localhost:5432/sentinel"
    migration_database_url: str | None = None
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_statement_timeout_ms: int = 15_000

    # -- redis -------------------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"

    # -- object storage ----------------------------------------------------
    s3_endpoint: str = "http://localhost:9000"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_region: str = "us-east-1"
    s3_bucket_evidence: str = "sentinel-evidence"
    s3_bucket_testcases: str = "sentinel-testcases"
    s3_force_path_style: bool = True

    # -- signing -----------------------------------------------------------
    signing_key_provider: Literal["file", "kms"] = "file"
    signing_key_path: str = "/run/keys/audit_ed25519.key"
    signing_key_ref: str = ""

    jwt_private_key_path: str = "/run/keys/jwt_ed25519.key"
    jwt_access_ttl_seconds: int = 900
    jwt_refresh_ttl_seconds: int = 1_209_600
    jwt_issuer: str = "sentinel"

    # -- security ----------------------------------------------------------
    cors_origins: str = "http://localhost:3000"
    # Empty means a host-only cookie, which is both the safer default and the
    # one that works when the API and the SPA share a host.
    cookie_domain: str = ""
    cookie_secure: bool = False
    mfa_required_roles: str = "org_admin,reviewer"
    password_min_length: int = 12
    login_max_failures: int = 5

    # -- integrity ---------------------------------------------------------
    evidence_max_per_session: int = 40
    evidence_cooldown_seconds: int = 20
    evidence_max_long_edge: int = 640
    evidence_jpeg_quality: float = 0.7
    event_late_threshold_seconds: int = 30
    event_rate_per_minute: int = 60
    retention_default_days: int = 30
    retention_appeal_extension_days: int = 60

    # -- judge -------------------------------------------------------------
    judge_max_concurrent: int = 8
    judge_default_time_limit_ms: int = 2_000
    judge_default_memory_mb: int = 256

    # -- seed (development only) -------------------------------------------
    seed_org_slug: str = "demo-university"
    seed_password: str = Field(default="", repr=False)

    # ---------------------------------------------------------------- views
    @property
    def is_production(self) -> bool:
        return self.sentinel_env == "production"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def mfa_required_role_set(self) -> frozenset[str]:
        return frozenset(r.strip() for r in self.mfa_required_roles.split(",") if r.strip())

    @staticmethod
    def _resolve(path: str) -> Path:
        candidate = Path(path)
        return candidate if candidate.is_absolute() else (repo_root() / candidate).resolve()

    @property
    def audit_key_file(self) -> Path:
        """`SIGNING_KEY_PATH`, resolved. Relative paths are repo-root-relative."""
        return self._resolve(self.signing_key_path)

    @property
    def jwt_key_file(self) -> Path:
        """`JWT_PRIVATE_KEY_PATH`, resolved."""
        return self._resolve(self.jwt_private_key_path)

    @property
    def mfa_key_file(self) -> Path:
        """The TOTP secret-box key, which lives beside the JWT key by convention.

        Kept here rather than derived at each call site so that "where do keys
        live" has exactly one answer.
        """
        return self.jwt_key_file.parent / "mfa_aes.key"

    # ---------------------------------------------------------- validation
    @field_validator("database_url")
    @classmethod
    def _reject_superuser(cls, v: str) -> str:
        """Refuse to run as a database superuser.

        RLS is silently ignored for superusers. Connecting as ``postgres`` would
        disable every tenant-isolation policy in the system while every test
        that does not specifically check the role would still pass — which is
        precisely the kind of failure that reaches production unnoticed.
        """
        for forbidden in ("://postgres:", "://postgres@"):
            if forbidden in v:
                raise ValueError(
                    "DATABASE_URL must not use the 'postgres' superuser. "
                    "RLS is bypassed for superusers, which disables tenant isolation. "
                    "Use the sentinel_app role (NOBYPASSRLS)."
                )
        return v

    @model_validator(mode="after")
    def _production_guards(self) -> Settings:
        if not self.is_production:
            return self

        # A file-based signing key in production voids every tamper-evidence
        # claim Sentinel makes: anyone with host access can forge a chain that
        # verifies. Refusing to boot is the correct response.
        if self.signing_key_provider == "file":
            raise ValueError(
                "SIGNING_KEY_PROVIDER=file is not permitted when SENTINEL_ENV=production. "
                "Use a KMS/HSM the database administrator cannot administer. "
                "See SECURITY.md §3."
            )
        if self.signing_key_provider == "kms" and not self.signing_key_ref:
            raise ValueError("SIGNING_KEY_REF is required when SIGNING_KEY_PROVIDER=kms.")
        if not self.cookie_secure:
            raise ValueError("COOKIE_SECURE must be true in production.")
        if any(o.startswith("http://") for o in self.cors_origin_list):
            raise ValueError(
                "CORS_ORIGINS must not contain plaintext http:// origins in production."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
