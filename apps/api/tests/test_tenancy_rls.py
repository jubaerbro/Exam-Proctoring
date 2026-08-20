"""Tenant isolation tests.

These are the tests that decide whether Sentinel may hold candidate data. They
run as the application database role — non-owner, NOBYPASSRLS — because running
them as the owner or a superuser would bypass every policy and produce a green
suite that proves nothing.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import ProgrammingError

from tests.conftest import demo_org_id

pytestmark = pytest.mark.security


TENANT_TABLE_QUERY = """
SELECT c.relname
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r'
  AND EXISTS (
    SELECT 1 FROM information_schema.columns col
    WHERE col.table_schema = 'public'
      AND col.table_name = c.relname
      AND col.column_name = 'org_id'
  )
"""


def test_app_role_is_not_superuser_and_cannot_bypass_rls(app_engine: Engine) -> None:
    """The single assumption every other RLS test rests on.

    PostgreSQL ignores row-level security entirely for superusers and for roles
    with BYPASSRLS. If the application connected as one, every policy below
    would be decorative and every test would still pass.
    """
    with app_engine.connect() as conn:
        row = conn.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()
    assert row.rolsuper is False, "Application role must not be a superuser."
    assert row.rolbypassrls is False, "Application role must not have BYPASSRLS."


def test_every_tenant_table_has_forced_rls(admin_engine: Engine) -> None:
    """The invariant that stops a new table from being a silent hole.

    ENABLE alone is not enough: PostgreSQL skips policies for the table owner
    unless FORCE is set, so a deployment where the app connects as the owner
    would lose isolation with no error anywhere.
    """
    with admin_engine.connect() as conn:
        unprotected = [
            r[0]
            for r in conn.execute(
                text(
                    TENANT_TABLE_QUERY
                    + " AND (c.relrowsecurity = false OR c.relforcerowsecurity = false)"
                )
            )
        ]
        total = conn.execute(text("SELECT count(*) FROM (" + TENANT_TABLE_QUERY + ") t")).scalar()

    assert unprotected == [], f"Tenant tables without forced RLS: {sorted(unprotected)}"
    assert total and total >= 30, f"Expected the full tenant schema, found {total} tables."


def test_every_tenant_table_has_a_policy(admin_engine: Engine) -> None:
    with admin_engine.connect() as conn:
        tables = {r[0] for r in conn.execute(text(TENANT_TABLE_QUERY))}
        with_policy = {
            r[0]
            for r in conn.execute(
                text("SELECT DISTINCT tablename FROM pg_policies WHERE schemaname = 'public'")
            )
        }
    assert tables - with_policy == set(), (
        f"Tenant tables with no policy: {sorted(tables - with_policy)}"
    )


def test_no_org_context_returns_no_rows(app_engine: Engine) -> None:
    """A code path that forgets tenancy must see nothing, not everything.

    This is the whole reason RLS is worth the trouble: the failure mode of a
    forgotten filter is an empty result, not a cross-tenant dump.
    """
    with app_engine.connect() as conn:
        conn.execute(text("SELECT set_config('sentinel.org_id', '', true)"))
        count = conn.execute(text("SELECT count(*) FROM exam")).scalar()
    assert count == 0


def test_cross_tenant_read_is_empty(app_engine: Engine, second_org: dict) -> None:
    """Org A cannot see org B's exams even asking for them by primary key."""
    org_id = demo_org_id(app_engine)

    with app_engine.connect() as conn:
        conn.execute(text("SELECT set_config('sentinel.org_id', :o, true)"), {"o": str(org_id)})
        conn.execute(text("SELECT set_config('sentinel.role', 'instructor', true)"))

        by_pk = conn.execute(
            text("SELECT count(*) FROM exam WHERE id = :i"), {"i": second_org["exam_id"]}
        ).scalar()
        assert by_pk == 0, "Cross-tenant read by primary key returned a row."

        leaked = conn.execute(
            text("SELECT count(*) FROM exam WHERE org_id = :o"), {"o": second_org["org_id"]}
        ).scalar()
        assert leaked == 0, "Cross-tenant read by org_id returned rows."


def test_cross_tenant_write_is_rejected(app_engine: Engine, second_org: dict) -> None:
    """WITH CHECK stops a caller stamping another tenant's org_id onto a row."""
    org_id = demo_org_id(app_engine)
    with app_engine.connect() as conn:
        owner = conn.execute(text("SELECT id FROM app_user LIMIT 1")).scalar()

    with app_engine.connect() as conn:
        conn.execute(text("SELECT set_config('sentinel.org_id', :o, true)"), {"o": str(org_id)})
        conn.execute(text("SELECT set_config('sentinel.role', 'instructor', true)"))
        with pytest.raises(ProgrammingError) as exc:
            conn.execute(
                text("INSERT INTO exam (org_id, title, owner_id) VALUES (:o, 'smuggled', :u)"),
                {"o": second_org["org_id"], "u": owner},
            )
        assert "row-level security" in str(exc.value).lower()


def test_set_local_does_not_leak_across_transactions(app_engine: Engine) -> None:
    """The PgBouncer trap.

    Tenant context is set with set_config(..., is_local => true), which dies
    with the transaction. A plain SET would persist on the pooled backend and be
    inherited by the next request — which may belong to a different
    organization. That is a cross-tenant breach and it is invisible in code
    review, so it is asserted here.
    """
    org_id = demo_org_id(app_engine)

    with app_engine.connect() as conn:
        with conn.begin():
            conn.execute(text("SELECT set_config('sentinel.org_id', :o, true)"), {"o": str(org_id)})
            inside = conn.execute(text("SELECT current_setting('sentinel.org_id', true)")).scalar()
            assert inside == str(org_id)

        # New transaction on the SAME connection: context must be gone.
        with conn.begin():
            after = conn.execute(text("SELECT current_setting('sentinel.org_id', true)")).scalar()
        assert after in (None, ""), f"Tenant context leaked across transactions: {after!r}"


def test_candidate_cannot_read_another_candidates_session(app_engine: Engine) -> None:
    """Same-tenant isolation. The org boundary is not the only boundary."""
    org_id = demo_org_id(app_engine)
    with app_engine.connect() as conn:
        conn.execute(text("SELECT set_config('sentinel.org_id', :o, true)"), {"o": str(org_id)})
        conn.execute(text("SELECT set_config('sentinel.role', 'org_admin', true)"))
        candidates = [
            r[0]
            for r in conn.execute(
                text(
                    "SELECT u.id FROM app_user u JOIN membership m ON m.user_id = u.id "
                    "WHERE m.org_id = :o AND m.role = 'candidate' ORDER BY u.email LIMIT 2"
                ),
                {"o": org_id},
            )
        ]
    assert len(candidates) == 2

    a, b = candidates
    assignment = _assignment_for(app_engine, org_id, b)
    session_id = uuid.uuid4()

    with app_engine.begin() as conn:
        conn.execute(text("SELECT set_config('sentinel.org_id', :o, true)"), {"o": str(org_id)})
        conn.execute(text("SELECT set_config('sentinel.user_id', :u, true)"), {"u": str(b)})
        conn.execute(text("SELECT set_config('sentinel.role', 'candidate', true)"))
        attempt = conn.execute(
            text(
                "SELECT coalesce(max(attempt_no), 0) + 1 FROM exam_session WHERE assignment_id = :a"
            ),
            {"a": assignment},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO exam_session (id, org_id, assignment_id, exam_version_id, "
                "candidate_user_id, attempt_no, paper_seed) "
                "SELECT :i, :o, :a, exam_version_id, :u, :n, '\\x00'::bytea "
                "FROM exam_assignment WHERE id = :a"
            ),
            {"i": session_id, "o": org_id, "a": assignment, "u": b, "n": attempt},
        )

    try:
        with app_engine.connect() as conn:
            conn.execute(text("SELECT set_config('sentinel.org_id', :o, true)"), {"o": str(org_id)})
            conn.execute(text("SELECT set_config('sentinel.user_id', :u, true)"), {"u": str(a)})
            conn.execute(text("SELECT set_config('sentinel.role', 'candidate', true)"))
            visible = conn.execute(
                text("SELECT count(*) FROM exam_session WHERE id = :i"), {"i": session_id}
            ).scalar()
        assert visible == 0, "A candidate could read another candidate's session."

        # ...and the owning candidate can still see their own.
        with app_engine.connect() as conn:
            conn.execute(text("SELECT set_config('sentinel.org_id', :o, true)"), {"o": str(org_id)})
            conn.execute(text("SELECT set_config('sentinel.user_id', :u, true)"), {"u": str(b)})
            conn.execute(text("SELECT set_config('sentinel.role', 'candidate', true)"))
            own = conn.execute(
                text("SELECT count(*) FROM exam_session WHERE id = :i"), {"i": session_id}
            ).scalar()
        assert own == 1
    finally:
        with app_engine.begin() as conn:
            conn.execute(text("SELECT set_config('sentinel.org_id', :o, true)"), {"o": str(org_id)})
            conn.execute(text("SELECT set_config('sentinel.role', 'org_admin', true)"))
            conn.execute(text("DELETE FROM exam_session WHERE id = :i"), {"i": session_id})


def _assignment_for(engine: Engine, org_id: uuid.UUID, user_id: uuid.UUID) -> uuid.UUID:
    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('sentinel.org_id', :o, true)"), {"o": str(org_id)})
        conn.execute(text("SELECT set_config('sentinel.role', 'org_admin', true)"))
        return conn.execute(
            text("SELECT id FROM exam_assignment WHERE candidate_user_id = :u LIMIT 1"),
            {"u": user_id},
        ).scalar_one()
