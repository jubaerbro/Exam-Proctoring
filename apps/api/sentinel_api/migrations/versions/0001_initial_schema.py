"""Initial schema and row-level security.

Revision ID: 0001
Revises:
Create Date: 2026-08-18

Two things happen here:

1. The schema from docs/DATA_MODEL.md §3 is applied verbatim from
   ``sql/0001_schema.sql``.

2. Row-level security is **generated**, not hand-written. The migration queries
   the catalog for every table carrying an ``org_id`` column and enables +
   forces RLS with a tenant policy on each one. Hand-writing 35 policies is how
   a table ends up without one.

``FORCE ROW LEVEL SECURITY`` matters as much as ``ENABLE``: without it,
PostgreSQL skips policies for the table owner, and a deployment where the
application happens to connect as the owner silently loses all tenant
isolation while every functional test still passes.
"""

from __future__ import annotations

import pathlib

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_SQL_DIR = pathlib.Path(__file__).resolve().parents[1] / "sql"

# Tables that need a policy beyond plain "same tenant". See DATA_MODEL.md §4.
_CUSTOM_POLICIES: dict[str, str] = {
    # A user must be able to read their own memberships before an organization
    # is known — that is how login discovers which orgs they belong to. Writes
    # still require an established tenant context.
    "membership": """
        CREATE POLICY membership_tenant ON membership
          USING (org_id = current_org_id() OR user_id = current_user_id())
          WITH CHECK (org_id = current_org_id());
    """,
    # Candidates see only their own sessions; staff see everything in the org.
    "exam_session": """
        CREATE POLICY exam_session_tenant ON exam_session
          USING (
            org_id = current_org_id()
            AND (
              current_role_name() IN ('org_admin','instructor','reviewer')
              OR candidate_user_id = current_user_id()
            )
          )
          WITH CHECK (org_id = current_org_id());
    """,
    # activity_log.org_id is nullable on purpose: authentication events happen
    # before an organization is known (login, refresh, reset). The blanket
    # "org_id = current_org_id()" policy evaluates to NULL for those rows and
    # rejects them, which would make login itself impossible.
    #
    # Reads are still scoped: a caller sees their own organization's rows, plus
    # org-less rows about themselves. One tenant must not be able to read
    # another tenant's login history just because it has no org_id.
    "activity_log": """
        CREATE POLICY activity_log_read ON activity_log
          FOR SELECT
          USING (
            org_id = current_org_id()
            OR (org_id IS NULL AND actor_user_id = current_user_id())
          );
        CREATE POLICY activity_log_write ON activity_log
          FOR INSERT
          WITH CHECK (org_id = current_org_id() OR org_id IS NULL);
    """,
    # A candidate proposes events; the server decides whether to write them.
    # The candidate role may never insert into the chain directly.
    "audit_event": """
        CREATE POLICY audit_event_tenant ON audit_event
          FOR SELECT
          USING (org_id = current_org_id());
        CREATE POLICY audit_event_insert ON audit_event
          FOR INSERT
          WITH CHECK (org_id = current_org_id() AND current_role_name() <> 'candidate');
    """,
}


_REQUIRED_EXTENSIONS = ("pgcrypto", "citext")


def _require_extensions(conn: sa.Connection) -> None:
    """Fail with an actionable message instead of a psycopg privilege traceback.

    The migrate role owns `public` but has no CREATE on the database, so it
    cannot install extensions — by design (see infrastructure/postgres/01-roles.sh).
    On a database whose volume predates that script, or on managed Postgres where
    a human provisions extensions, the schema's `CREATE EXTENSION IF NOT EXISTS`
    raises `InsufficientPrivilege` a hundred lines deep in SQLAlchemy and tells
    the reader nothing about how to fix it.
    """
    present = {
        r[0]
        for r in conn.execute(
            sa.text("SELECT extname FROM pg_extension WHERE extname = ANY(:names)"),
            {"names": list(_REQUIRED_EXTENSIONS)},
        )
    }
    missing = [e for e in _REQUIRED_EXTENSIONS if e not in present]
    if missing:
        names = ", ".join(missing)
        stmts = " ".join(f"CREATE EXTENSION IF NOT EXISTS {e};" for e in missing)
        raise RuntimeError(
            f"Required PostgreSQL extension(s) not installed: {names}.\n"
            "The migrate role cannot install them itself — it has no CREATE on the "
            "database, deliberately.\n"
            "Install them once as a superuser, then re-run the migration:\n"
            f'    docker compose exec postgres psql -U postgres -d sentinel -c "{stmts}"\n'
            "A database created after infrastructure/postgres/01-roles.sh gained the "
            "CREATE EXTENSION lines will already have them; an existing volume will not, "
            "because docker-entrypoint-initdb.d runs only on an empty data directory."
        )


def upgrade() -> None:
    conn = op.get_bind()

    _require_extensions(conn)

    schema_sql = (_SQL_DIR / "0001_schema.sql").read_text()
    conn.execute(sa.text(schema_sql))

    tenant_tables = _tenant_tables(conn)
    if not tenant_tables:  # pragma: no cover - would mean the schema failed
        raise RuntimeError("No tenant-owned tables found; schema did not apply.")

    for table in tenant_tables:
        conn.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY;'))
        conn.execute(sa.text(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY;'))
        if table in _CUSTOM_POLICIES:
            conn.execute(sa.text(_CUSTOM_POLICIES[table]))
        else:
            conn.execute(
                sa.text(
                    f'CREATE POLICY "{table}_tenant" ON "{table}" '
                    "USING (org_id = current_org_id()) "
                    "WITH CHECK (org_id = current_org_id());"
                )
            )

    # Fail the migration rather than the product: if any tenant table came out
    # of this loop without forced RLS, stop now.
    unprotected = _unprotected_tenant_tables(conn)
    if unprotected:  # pragma: no cover - defensive
        raise RuntimeError(f"Tables missing forced RLS after migration: {sorted(unprotected)}")


def downgrade() -> None:
    conn = op.get_bind()
    for table in _tenant_tables(conn):
        conn.execute(sa.text(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY;'))

    conn.execute(sa.text("DROP TABLE IF EXISTS " + ", ".join(_ALL_TABLES) + " CASCADE;"))
    for enum in _ALL_ENUMS:
        conn.execute(sa.text(f"DROP TYPE IF EXISTS {enum} CASCADE;"))
    for fn in ("current_org_id()", "current_user_id()", "current_role_name()", "deny_mutation()"):
        conn.execute(sa.text(f"DROP FUNCTION IF EXISTS {fn} CASCADE;"))


# ---------------------------------------------------------------------------


def _tenant_tables(conn: sa.Connection) -> list[str]:
    rows = conn.execute(
        sa.text(
            """
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
            ORDER BY c.relname
            """
        )
    )
    return [r[0] for r in rows]


def _unprotected_tenant_tables(conn: sa.Connection) -> list[str]:
    rows = conn.execute(
        sa.text(
            """
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
              AND (c.relrowsecurity = false OR c.relforcerowsecurity = false)
            """
        )
    )
    return [r[0] for r in rows]


_ALL_TABLES = [
    "purge_record",
    "evidence_access_log",
    "evidence_object",
    "retention_policy",
    "risk_contribution",
    "risk_assessment",
    "review_decision",
    "review",
    "candidate_statement",
    "question_score",
    "session_result",
    "judge_test_result",
    "judge_run",
    "code_submission",
    "calibration_record",
    "answer_revision",
    "answer",
    "session_paper_item",
    "session_integrity_config",
    "exam_session",
    "exam_assignment",
    "accommodation_profile",
    "section_pool_item",
    "section_pool",
    "exam_section",
    "exam_version",
    "exam",
    "test_case",
    "question_version",
    "question",
    "question_bank",
    "audit_event",
    "signing_key",
    "event_type",
    "usage_meter",
    "activity_log",
    "refresh_token",
    "membership",
    "app_user",
    "organization",
]

_ALL_ENUMS = [
    "org_status",
    "member_role",
    "member_status",
    "question_kind",
    "publish_status",
    "exam_delivery",
    "session_status",
    "submit_reason",
    "judge_status",
    "judge_language",
    "run_mode",
    "evidence_kind",
    "evidence_state",
    "detector_id",
    "review_state",
    "review_outcome",
    "chain_state",
]
