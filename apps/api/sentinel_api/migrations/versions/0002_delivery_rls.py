"""Candidate-scoped row-level security for session-owned delivery tables.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-19

Migration 0001 generated a blanket ``org_id = current_org_id()`` policy for
every tenant table, and gave ``exam_session`` a custom one so that a candidate
sees only their own sessions. The tables that *hang off* a session did not get
the same treatment.

The consequence is concrete: `session_paper_item`, `answer`, `answer_revision`,
`question_score` and `session_result` carry `session_id`, not
`candidate_user_id`, so the blanket policy let any authenticated member of an
organization read every other candidate's answers and marks. Phase 1 never
noticed because nothing wrote to those tables; Phase 2 writes to all of them.

Two things happen here.

1. **Candidate scoping.** Each session-owned table gets a staff/system policy
   plus a narrow candidate policy keyed on session ownership. The API filters by
   session as well — but an authorization bug in a handler should cost nothing,
   which is only true if the database is also refusing.

2. **A `system` role name.** Auto-grading writes `question_score` and
   `session_result` during the candidate's own submit request. A candidate must
   never be able to write their own marks, so grading runs in its own
   transaction that declares `sentinel.role = 'system'` instead of inheriting
   the caller's role. `Principal.primary_role` can only ever produce one of the
   four membership roles, so no request can obtain this context by asking.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


# Roles that see a whole organization. 'system' is not a membership role; see
# the module docstring.
_STAFF_SQL = "current_role_name() IN ('org_admin','instructor','reviewer','system')"

_OWNS_SESSION = "session_belongs_to_current_user(session_id)"

#: table -> whether a candidate may also INSERT/UPDATE their own rows.
_SESSION_OWNED: dict[str, bool] = {
    "session_paper_item": False,  # the server materializes the paper
    "answer": True,  # the candidate autosaves their own answers
    "answer_revision": True,  # ... and its append-only history
    "calibration_record": True,  # written by the candidate's own client
    "session_integrity_config": False,  # frozen by the server at session start
    "question_score": False,  # marks are never candidate-writable
    "session_result": False,
}


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION session_belongs_to_current_user(sid uuid)
            RETURNS boolean LANGUAGE sql STABLE AS $$
              SELECT EXISTS (
                SELECT 1 FROM exam_session s
                WHERE s.id = sid AND s.candidate_user_id = current_user_id()
              )
            $$;
            """
        )
    )

    # exam_session's own policy predates the 'system' role name.
    conn.execute(sa.text("DROP POLICY IF EXISTS exam_session_tenant ON exam_session;"))
    conn.execute(
        sa.text(
            f"""
            CREATE POLICY exam_session_tenant ON exam_session
              USING (
                org_id = current_org_id()
                AND ({_STAFF_SQL} OR candidate_user_id = current_user_id())
              )
              WITH CHECK (org_id = current_org_id());
            """
        )
    )

    for table, candidate_writes in _SESSION_OWNED.items():
        conn.execute(sa.text(f'DROP POLICY IF EXISTS "{table}_tenant" ON "{table}";'))
        conn.execute(
            sa.text(
                f'CREATE POLICY "{table}_staff" ON "{table}" FOR ALL '
                f"USING (org_id = current_org_id() AND {_STAFF_SQL}) "
                f"WITH CHECK (org_id = current_org_id() AND {_STAFF_SQL});"
            )
        )
        conn.execute(
            sa.text(
                f'CREATE POLICY "{table}_candidate_read" ON "{table}" FOR SELECT '
                f"USING (org_id = current_org_id() AND {_OWNS_SESSION});"
            )
        )
        if candidate_writes:
            conn.execute(
                sa.text(
                    f'CREATE POLICY "{table}_candidate_insert" ON "{table}" FOR INSERT '
                    f"WITH CHECK (org_id = current_org_id() AND {_OWNS_SESSION});"
                )
            )
            conn.execute(
                sa.text(
                    f'CREATE POLICY "{table}_candidate_update" ON "{table}" FOR UPDATE '
                    f"USING (org_id = current_org_id() AND {_OWNS_SESSION}) "
                    f"WITH CHECK (org_id = current_org_id() AND {_OWNS_SESSION});"
                )
            )

    _assert_every_tenant_table_protected(conn)


def downgrade() -> None:
    conn = op.get_bind()

    for table in _SESSION_OWNED:
        for suffix in ("staff", "candidate_read", "candidate_insert", "candidate_update"):
            conn.execute(sa.text(f'DROP POLICY IF EXISTS "{table}_{suffix}" ON "{table}";'))
        conn.execute(
            sa.text(
                f'CREATE POLICY "{table}_tenant" ON "{table}" '
                "USING (org_id = current_org_id()) "
                "WITH CHECK (org_id = current_org_id());"
            )
        )

    conn.execute(sa.text("DROP POLICY IF EXISTS exam_session_tenant ON exam_session;"))
    conn.execute(
        sa.text(
            """
            CREATE POLICY exam_session_tenant ON exam_session
              USING (
                org_id = current_org_id()
                AND (
                  current_role_name() IN ('org_admin','instructor','reviewer')
                  OR candidate_user_id = current_user_id()
                )
              )
              WITH CHECK (org_id = current_org_id());
            """
        )
    )
    conn.execute(sa.text("DROP FUNCTION IF EXISTS session_belongs_to_current_user(uuid);"))

    _assert_every_tenant_table_protected(conn)


def _assert_every_tenant_table_protected(conn: sa.Connection) -> None:
    """Same invariant 0001 enforces, re-checked after policies are rewritten.

    A migration that drops a policy and fails to recreate it silently opens the
    tenant boundary. Re-asserting is four lines; discovering it in production is
    not.
    """
    rows = conn.execute(
        sa.text(
            """
            SELECT c.relname,
                   c.relrowsecurity,
                   c.relforcerowsecurity,
                   (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) AS policies
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
        )
    ).all()
    broken = [r[0] for r in rows if not r[1] or not r[2] or r[3] == 0]
    if broken:
        raise RuntimeError(f"Tenant tables left unprotected by this migration: {sorted(broken)}")
