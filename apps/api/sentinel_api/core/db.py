"""Database engine and session management.

The important part of this module is :func:`tenant_session`. Every request that
touches tenant-owned data runs inside a transaction that has declared which
tenant it belongs to, using ``SET LOCAL``. PostgreSQL row-level security reads
those settings and refuses rows outside the tenant.

``SET LOCAL`` rather than ``SET`` is mandatory, not stylistic. Under PgBouncer
transaction pooling a plain ``SET`` persists on the pooled backend after the
transaction ends and is inherited by the next request — which may belong to a
different organization. That is a cross-tenant data breach, and it is subtle
enough to survive code review, so ``tenancy/rls.py`` tests for it explicitly.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from sentinel_api.core.config import get_settings

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(
            settings.database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_pre_ping=True,
            future=True,
        )

        @event.listens_for(_engine, "connect")
        def _set_statement_timeout(dbapi_conn: Any, _record: Any) -> None:
            with dbapi_conn.cursor() as cur:
                cur.execute(f"SET statement_timeout = {settings.db_statement_timeout_ms}")

    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SessionFactory


def reset_engine() -> None:
    """Drop cached engine/factory. Used by tests that repoint the database."""
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionFactory = None


# --------------------------------------------------------------------------
# Tenant context
# --------------------------------------------------------------------------

# Only these values may be written into a SET LOCAL. They are validated by the
# caller as UUIDs / a known role name, but the allowlist here means a future
# refactor cannot widen this into a SQL injection surface by accident.
_ALLOWED_KEYS = frozenset({"sentinel.org_id", "sentinel.user_id", "sentinel.role"})


def apply_tenant_context(
    session: Session,
    *,
    org_id: str | None,
    user_id: str | None,
    role: str | None,
) -> None:
    """Declare the tenant context for the *current transaction only*."""
    params: dict[str, str | None] = {
        "sentinel.org_id": org_id,
        "sentinel.user_id": user_id,
        "sentinel.role": role,
    }
    for key, value in params.items():
        assert key in _ALLOWED_KEYS  # noqa: S101 - invariant, not user input
        # set_config(..., is_local => true) is the parameterizable equivalent of
        # SET LOCAL. Using it means the value is bound, never interpolated.
        session.execute(
            text("SELECT set_config(:k, :v, true)"),
            {"k": key, "v": value if value is not None else ""},
        )


@contextmanager
def tenant_session(
    *,
    org_id: str | None = None,
    user_id: str | None = None,
    role: str | None = None,
) -> Iterator[Session]:
    """Yield a session inside a transaction bound to one tenant.

    Commits on success, rolls back on any exception. The tenant context dies
    with the transaction.
    """
    session = get_session_factory()()
    try:
        session.begin()
        apply_tenant_context(session, org_id=org_id, user_id=user_id, role=role)
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


#: Role name used for server-initiated work inside a tenant. It is not a
#: membership role: no login can produce it, because `Principal.primary_role`
#: only ever returns one of the four values in `member_role`. Migration 0002
#: grants it organization-wide access so that auto-grading can write
#: `question_score` while the candidate's own role cannot.
SERVER_ROLE = "system"


@contextmanager
def elevated(session: Session, *, role: str | None = None) -> Iterator[Session]:
    """Temporarily act as the server *inside* the caller's transaction.

    Materializing a session's paper is the motivating case. The rows belong to
    the server, not to the candidate — migration 0002 makes `session_paper_item`
    unwritable by the `candidate` role on purpose, so a compromised candidate
    session cannot add itself an easier question. But the paper and the
    `exam_session` row have to be written atomically: a session with no paper is
    a candidate staring at an empty exam, and a paper with no session is a leak.

    Doing it in one transaction with a briefly widened role gets both. The
    previous role is restored in a `finally`, and even if that were skipped the
    setting is transaction-local (`set_config(..., true)`), so it cannot escape
    into the next request on a pooled connection.
    """
    previous = session.execute(text("SELECT current_setting('sentinel.role', true)")).scalar()
    session.execute(
        text("SELECT set_config('sentinel.role', :v, true)"), {"v": role or SERVER_ROLE}
    )
    try:
        yield session
    finally:
        session.execute(text("SELECT set_config('sentinel.role', :v, true)"), {"v": previous or ""})


@contextmanager
def server_session(*, org_id: str, actor_user_id: str | None = None) -> Iterator[Session]:
    """Tenant-bound session for work the *server* does on a tenant's behalf.

    Auto-grading is the motivating case. It happens during the candidate's
    submit request, but a candidate must never be able to write their own marks
    — so it cannot simply reuse the request's session, which carries
    ``sentinel.role = 'candidate'``. Running it here, in its own transaction
    with its own declared role, keeps the candidate's RLS context honest and
    keeps the grading write auditable as a server action.
    """
    session = get_session_factory()()
    try:
        session.begin()
        apply_tenant_context(session, org_id=org_id, user_id=actor_user_id, role=SERVER_ROLE)
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def system_session() -> Iterator[Session]:
    """Session with no tenant context.

    Used only for genuinely cross-tenant work: authentication (which must find a
    user before an organization is known), health checks, and the retention
    worker. RLS still applies — a query for tenant-owned rows returns nothing,
    because ``current_org_id()`` is NULL. That is the desired behaviour: a code
    path that forgets to establish tenancy sees no data rather than all data.
    """
    session = get_session_factory()()
    try:
        session.begin()
        apply_tenant_context(session, org_id=None, user_id=None, role=None)
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
