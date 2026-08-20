"""Security boundaries added by Phase 2.

The blanket RLS policy migration 0001 generated is `org_id = current_org_id()`.
For the tables that hang off a session — the paper, the answers, the marks —
that policy makes every candidate in an organization able to read every other
candidate's work. Phase 1 never noticed because nothing wrote to those tables.
Migration 0002 closes it; these tests are the reason to believe it.

Every test here runs as the `sentinel_app` role, which is `NOBYPASSRLS` and owns
nothing. Running them as the schema owner or as `postgres` would pass while
proving nothing.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from sentinel_api.core.db import tenant_session
from tests.conftest import admin_tx, assign_exam, author_exam, bearer

pytestmark = pytest.mark.security


SESSION_OWNED_TABLES = [
    "session_paper_item",
    "answer",
    "answer_revision",
    "question_score",
    "session_result",
    "calibration_record",
    "session_integrity_config",
]


def _org_id(engine: Engine) -> uuid.UUID:
    # `organization` carries no org_id column, so it needs no tenant context.
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT id FROM organization WHERE slug = 'demo-university'")
        ).scalar_one()


def _staff_query(engine: Engine, sql: str, params: dict | None = None):
    """Read tenant-owned rows as staff.

    A bare `engine.connect()` has no tenant context, so RLS correctly returns
    nothing — which reads as "the fixture data is missing" and silently skips
    the test. Declaring the tenant is required even for the schema owner,
    because every tenant table is FORCE ROW LEVEL SECURITY.
    """
    with admin_tx(engine, org_id=_org_id(engine), role="instructor") as conn:
        return conn.execute(text(sql), params or {}).all()


def test_every_session_owned_table_has_a_candidate_scoped_policy(admin_engine: Engine) -> None:
    """Structural check: the blanket policy must be gone from all of them.

    A future table added with `session_id` and left on the generated policy is
    the same bug again, so this asserts the shape rather than one instance of it.
    """
    with admin_engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT tablename, policyname
                FROM pg_policies
                WHERE schemaname = 'public' AND tablename = ANY(:tables)
                """
            ),
            {"tables": SESSION_OWNED_TABLES},
        ).all()

    by_table: dict[str, set[str]] = {}
    for table, policy in rows:
        by_table.setdefault(table, set()).add(policy)

    for table in SESSION_OWNED_TABLES:
        policies = by_table.get(table, set())
        assert f"{table}_tenant" not in policies, (
            f"{table} still carries the blanket org-only policy from migration 0001"
        )
        assert f"{table}_staff" in policies, f"{table} has no staff policy"
        assert f"{table}_candidate_read" in policies, f"{table} has no candidate policy"


def test_candidate_cannot_read_another_candidates_answers(
    client: TestClient,
    admin_engine: Engine,
    instructor_login: dict,
    candidate_login: dict,
    seed_domain: str,
) -> None:
    """The concrete version of the same claim, through the app's own engine.

    Two real candidates in the *same* organization. Tenant isolation does not
    help here — they are the same tenant — so this is entirely a test of the
    policy migration 0002 adds.

    The exam is authored fresh rather than reusing the seeded one. The seeded
    exam runs 90 minutes; a session started on it expires after that and counts
    against the single permitted attempt, so a test built on it passes for 90
    minutes and then fails forever. Owning its data makes this test independent
    of the clock and of every other test.
    """
    org_id = _org_id(admin_engine)
    staff = bearer(instructor_login)
    mine = bearer(candidate_login)

    exam = author_exam(client, staff)
    assignment = assign_exam(client, staff, exam["version_id"], f"aisha.rahman@{seed_domain}")

    started = client.post("/api/v1/sessions", json={"assignment_id": assignment}, headers=mine)
    assert started.status_code == 201, started.text
    session_id = started.json()["session_id"]
    item_id = started.json()["items"][0]["paper_item_id"]
    saved = client.put(
        f"/api/v1/sessions/{session_id}/answers/{item_id}",
        json={"value": {"selected": ["a"]}},
        headers=mine,
    )
    assert saved.status_code == 200, saved.text

    # Now read as a different candidate in the same organization.
    with admin_engine.connect() as conn:
        other = conn.execute(
            text("SELECT id FROM app_user WHERE email = :e"),
            {"e": f"daniel.okafor@{seed_domain}"},
        ).scalar_one()
        owner = conn.execute(
            text("SELECT id FROM app_user WHERE email = :e"),
            {"e": f"aisha.rahman@{seed_domain}"},
        ).scalar_one()

    with tenant_session(org_id=str(org_id), user_id=str(other), role="candidate") as db:
        for table in ("answer", "session_paper_item", "answer_revision"):
            visible = db.execute(
                text(f"SELECT count(*) FROM {table} WHERE session_id = :s"),  # noqa: S608
                {"s": session_id},
            ).scalar()
            assert visible == 0, f"another candidate can read {table}"

    # ...and the owner still can, or the policy is merely broken rather than tight.
    with tenant_session(org_id=str(org_id), user_id=str(owner), role="candidate") as db:
        assert (
            db.execute(
                text("SELECT count(*) FROM answer WHERE session_id = :s"), {"s": session_id}
            ).scalar()
            == 1
        )


def test_candidate_role_cannot_write_its_own_marks(admin_engine: Engine, seed_domain: str) -> None:
    """Auto-grading runs as `system`, not as the candidate, for exactly this reason.

    If a handler ever graded inside the request transaction, this policy is what
    would stop a candidate-authenticated code path from writing a score.
    """
    org_id = _org_id(admin_engine)
    rows = _staff_query(
        admin_engine,
        "SELECT s.id, s.candidate_user_id, p.id FROM exam_session s "
        "JOIN session_paper_item p ON p.session_id = s.id LIMIT 1",
    )
    row = rows[0] if rows else None
    if row is None:
        pytest.skip("No delivered session yet.")
    session_id, candidate_id, item_id = row

    from sqlalchemy.exc import ProgrammingError

    with pytest.raises(ProgrammingError) as exc:
        with tenant_session(org_id=str(org_id), user_id=str(candidate_id), role="candidate") as db:
            db.execute(
                text(
                    "INSERT INTO question_score (org_id, session_id, paper_item_id, awarded, "
                    "max_marks) VALUES (:o, :s, :p, 999, 999)"
                ),
                {"o": org_id, "s": session_id, "p": item_id},
            )
    assert "row-level security" in str(exc.value).lower()


def test_candidate_cannot_start_another_candidates_assignment(
    client: TestClient, admin_engine: Engine, seed_domain: str, seed_password: str
) -> None:
    """404, not 403. A 403 would confirm that person sits this exam."""
    aisha = client.post(
        "/api/v1/auth/login",
        json={"email": f"aisha.rahman@{seed_domain}", "password": seed_password},
    ).json()
    rows = _staff_query(
        admin_engine,
        "SELECT a.id FROM exam_assignment a JOIN app_user u "
        "ON u.id = a.candidate_user_id WHERE u.email <> :e LIMIT 1",
        {"e": f"aisha.rahman@{seed_domain}"},
    )
    other = rows[0][0] if rows else None
    if other is None:
        pytest.skip("Only one candidate is assigned; nothing to attempt.")

    resp = client.post(
        "/api/v1/sessions",
        json={"assignment_id": str(other)},
        headers={"Authorization": f"Bearer {aisha['access_token']}"},
    )
    assert resp.status_code == 404


def test_candidate_cannot_reach_the_answer_key(
    client: TestClient, candidate_login: dict, seed_domain: str
) -> None:
    """The question bank is staff-only, at the policy layer."""
    headers = {"Authorization": f"Bearer {candidate_login['access_token']}"}
    assert client.get("/api/v1/banks", headers=headers).status_code == 403
    assert client.post("/api/v1/banks", json={"name": "mine"}, headers=headers).status_code == 403
    assert client.post("/api/v1/exams", json={"title": "mine"}, headers=headers).status_code == 403


def test_candidate_cannot_publish_or_assign(
    client: TestClient, candidate_login: dict, admin_engine: Engine
) -> None:
    headers = {"Authorization": f"Bearer {candidate_login['access_token']}"}
    version_id = _staff_query(admin_engine, "SELECT id FROM exam_version LIMIT 1")[0][0]
    assert (
        client.post(f"/api/v1/exam-versions/{version_id}/publish", headers=headers).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/v1/exam-versions/{version_id}/assignments",
            json={"emails": ["someone@example.org"]},
            headers=headers,
        ).status_code
        == 403
    )


def test_paper_served_to_a_candidate_contains_no_answer_key(
    client: TestClient, admin_engine: Engine, seed_domain: str, seed_password: str
) -> None:
    """Stripping happens where the row is read, not in a response model.

    A response model that happens to omit `correct` today is one `dict(...)`
    away from including it tomorrow.
    """
    aisha = client.post(
        "/api/v1/auth/login",
        json={"email": f"aisha.rahman@{seed_domain}", "password": seed_password},
    ).json()
    headers = {"Authorization": f"Bearer {aisha['access_token']}"}
    rows = _staff_query(
        admin_engine,
        "SELECT a.id FROM exam_assignment a JOIN app_user u "
        "ON u.id = a.candidate_user_id WHERE u.email = :e LIMIT 1",
        {"e": f"aisha.rahman@{seed_domain}"},
    )
    if not rows:
        pytest.skip("No seeded assignment.")
    assignment = rows[0][0]

    started = client.post(
        "/api/v1/sessions", json={"assignment_id": str(assignment)}, headers=headers
    )
    payload = started.text.lower()
    for forbidden in ('"correct"', '"accepted"', '"tolerance"', '"expected_inline"'):
        assert forbidden not in payload, f"{forbidden} leaked into the candidate's paper"
