"""Candidate-scoped row-level security for the judge tables.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-20

Exactly the problem migration 0002 fixed for the delivery tables, in the three
tables Phase 3 starts writing to. `code_submission`, `judge_run` and
`judge_test_result` all came out of migration 0001 with the generated
``org_id = current_org_id()`` policy, which means any authenticated member of an
organization could read any other candidate's *source code* and marks.

Reading another candidate's submitted source during an exam is not a privacy
footnote — it is the cheating the product exists to detect, handed over by the
database.

The scoping is one hop longer than in 0002. `code_submission` carries a
`session_id`, so it reuses `session_belongs_to_current_user`. `judge_run` and
`judge_test_result` do not: they reach a session through their parent, so this
migration adds `submission_belongs_to_current_user` and `run_belongs_to_current_user`
rather than repeating a three-table EXISTS in four policies.

Write access is deliberately asymmetric:

* a candidate may INSERT a `code_submission` for their own live session — that
  is what pressing "run" does;
* a candidate may never write `judge_run` or `judge_test_result`. Those are the
  judge's verdict about them, and a role that can write its own verdict has no
  verdict. Only the `system` role (`core.db.SERVER_ROLE`) can, which is what the
  worker uses.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_STAFF_SQL = "current_role_name() IN ('org_admin','instructor','reviewer','system')"

_JUDGE_TABLES = ("code_submission", "judge_run", "judge_test_result")

#: table -> the SQL that decides whether the row belongs to the caller.
_OWNERSHIP = {
    "code_submission": "session_belongs_to_current_user(session_id)",
    "judge_run": "submission_belongs_to_current_user(submission_id)",
    "judge_test_result": "run_belongs_to_current_user(run_id)",
}


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION submission_belongs_to_current_user(sid uuid)
            RETURNS boolean LANGUAGE sql STABLE AS $$
              SELECT EXISTS (
                SELECT 1 FROM code_submission s
                WHERE s.id = sid AND session_belongs_to_current_user(s.session_id)
              )
            $$;
            """
        )
    )
    conn.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION run_belongs_to_current_user(rid uuid)
            RETURNS boolean LANGUAGE sql STABLE AS $$
              SELECT EXISTS (
                SELECT 1 FROM judge_run r
                WHERE r.id = rid AND submission_belongs_to_current_user(r.submission_id)
              )
            $$;
            """
        )
    )

    for table in _JUDGE_TABLES:
        owns = _OWNERSHIP[table]
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
                f"USING (org_id = current_org_id() AND {owns});"
            )
        )

    # The one candidate write. Pressing "run" creates a submission; everything
    # downstream of it is the judge's.
    conn.execute(
        sa.text(
            "CREATE POLICY code_submission_candidate_insert ON code_submission FOR INSERT "
            "WITH CHECK (org_id = current_org_id() "
            "AND session_belongs_to_current_user(session_id));"
        )
    )

    _assert_every_tenant_table_protected(conn)


def downgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        sa.text("DROP POLICY IF EXISTS code_submission_candidate_insert ON code_submission;")
    )
    for table in _JUDGE_TABLES:
        for suffix in ("staff", "candidate_read"):
            conn.execute(sa.text(f'DROP POLICY IF EXISTS "{table}_{suffix}" ON "{table}";'))
        conn.execute(
            sa.text(
                f'CREATE POLICY "{table}_tenant" ON "{table}" '
                "USING (org_id = current_org_id()) "
                "WITH CHECK (org_id = current_org_id());"
            )
        )

    conn.execute(sa.text("DROP FUNCTION IF EXISTS run_belongs_to_current_user(uuid);"))
    conn.execute(sa.text("DROP FUNCTION IF EXISTS submission_belongs_to_current_user(uuid);"))

    _assert_every_tenant_table_protected(conn)


def _assert_every_tenant_table_protected(conn: sa.Connection) -> None:
    rows = conn.execute(
        sa.text(
            """
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
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
