"""Test fixtures.

Tests run against a **real PostgreSQL instance**, never SQLite. Half of what
Phase 1 claims — row-level security, forced RLS, append-only triggers, CHECK
constraints, transaction-local settings — has no SQLite equivalent. A test
suite on SQLite would pass while proving nothing about the controls that
matter.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

REPO_ROOT = Path(__file__).resolve().parents[3]


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        pytest.skip(f"{name} is not set; these tests require a live PostgreSQL instance.")
    return value


@pytest.fixture(scope="session")
def database_url() -> str:
    return _require_env("DATABASE_URL")


@pytest.fixture(scope="session")
def migration_url() -> str:
    return _require_env("MIGRATION_DATABASE_URL")


@pytest.fixture(scope="session")
def admin_engine(migration_url: str) -> Iterator[Engine]:
    """Engine as the schema owner. Used to inspect the catalog, never by the app."""
    engine = create_engine(migration_url, future=True)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def app_engine(database_url: str) -> Iterator[Engine]:
    """Engine as the application role: non-owner, NOBYPASSRLS.

    This is the engine that proves RLS works. Running these tests as the owner
    or as a superuser would silently bypass every policy.
    """
    engine = create_engine(database_url, future=True)
    yield engine
    engine.dispose()


@contextmanager
def admin_tx(engine: Engine, *, org_id=None, user_id=None, role: str = "org_admin"):
    """Transaction as the schema owner, with tenant context declared.

    Note that the owner needs this too. ``FORCE ROW LEVEL SECURITY`` means the
    table owner is subject to its own policies — which is the point, and which
    is why fixtures cannot quietly write cross-tenant rows.
    """
    with engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('sentinel.org_id', :v, true)"),
            {"v": str(org_id) if org_id else ""},
        )
        conn.execute(
            text("SELECT set_config('sentinel.user_id', :v, true)"),
            {"v": str(user_id) if user_id else ""},
        )
        conn.execute(text("SELECT set_config('sentinel.role', :v, true)"), {"v": role})
        yield conn


def iter_routes(app):
    """Flatten the route tree.

    This FastAPI version wraps `include_router` results in `_IncludedRouter`
    objects that expose `original_router` rather than `routes`, so walking
    `app.routes` naively yields only the three docs endpoints. That made the
    route-coverage tests pass while checking nothing — the exact failure this
    helper exists to prevent.
    """
    seen: list = []
    visited: set[int] = set()

    def walk(container) -> None:
        if id(container) in visited:
            return
        visited.add(id(container))
        for route in getattr(container, "routes", []):
            if getattr(route, "path", None) and getattr(route, "methods", None):
                seen.append(route)
                continue
            nested = getattr(route, "original_router", None) or getattr(route, "router", None)
            if nested is not None:
                walk(nested)

    walk(app)
    return seen


def demo_org_id(engine: Engine):
    """The seeded organization. `organization` carries no org_id column, so it
    is readable without tenant context."""
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT id FROM organization WHERE slug = :s"),
            {"s": os.getenv("SEED_ORG_SLUG", "demo-university")},
        ).scalar_one()


@pytest.fixture(scope="session", autouse=True)
def _seeded(migration_url: str) -> None:
    """Ensure the schema is migrated and seed data exists."""
    env = {**os.environ, "MIGRATION_DATABASE_URL": migration_url}
    # sys.executable, not "alembic"/"python": the venv's bin directory is not
    # guaranteed to be on PATH when pytest is invoked by an IDE or CI runner.
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT / "apps" / "api",
        env=env,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [sys.executable, "-m", "scripts.seed"],
        cwd=REPO_ROOT,
        env={**env, "PYTHONPATH": f"{REPO_ROOT / 'apps' / 'api'}:{REPO_ROOT}"},
        check=True,
        capture_output=True,
    )


@pytest.fixture(scope="session")
def app():
    """The FastAPI instance.

    Exposed separately because `TestClient.app` is not guaranteed to expose
    `.routes` on every Starlette version — and a route-coverage test that reads
    an empty route list passes vacuously, which is worse than failing.
    """
    from sentinel_api.main import create_app

    return create_app()


@pytest.fixture(scope="session")
def client(app) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def seed_domain() -> str:
    return f"{os.getenv('SEED_ORG_SLUG', 'demo-university')}.example.edu"


@pytest.fixture(scope="session")
def seed_password() -> str:
    return _require_env("SEED_PASSWORD")


@pytest.fixture
def candidate_login(client: TestClient, seed_domain: str, seed_password: str) -> dict:
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": f"aisha.rahman@{seed_domain}", "password": seed_password},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture
def instructor_login(client: TestClient, seed_domain: str, seed_password: str) -> dict:
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": f"instructor@{seed_domain}", "password": seed_password},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def enrol_and_login(client: TestClient, engine: Engine, email: str, password: str) -> dict:
    """Sign in an account whose role mandates MFA, enrolling on the way through.

    This is the whole Phase 2 MFA-enrolment feature exercised as a fixture: the
    first login is *refused* (fail closed, as in Phase 1), and the refusal
    carries an `enrolment_token` that can do nothing except enrol. Using it here
    rather than inserting an `mfa_secret_enc` row directly means every test that
    needs an org_admin is also a test that the enrolment flow works.

    The secret is cleared first, because the suite is deliberately run against a
    warm database as well as a fresh one: on the second run the account is
    already enrolled, and nothing in this process knows the secret it enrolled
    with. Clearing is the same operation an administrator performs when someone
    loses their phone.
    """
    import pyotp

    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE app_user SET mfa_secret_enc = NULL, mfa_enabled_at = NULL WHERE email = :e"
            ),
            {"e": email.lower()},
        )

    first = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    if first.status_code == 200 and "access_token" in first.json():
        return first.json()  # MFA is not required for this role

    assert first.status_code == 403, first.text
    token = first.json()["enrolment_token"]
    headers = {"Authorization": f"Bearer {token}"}

    begin = client.post("/api/v1/auth/mfa/enrol", headers=headers)
    assert begin.status_code == 200, begin.text
    secret = begin.json()["secret"]

    confirm = client.post(
        "/api/v1/auth/mfa/enrol/confirm",
        json={"secret": secret, "code": pyotp.TOTP(secret).now()},
        headers=headers,
    )
    assert confirm.status_code == 200, confirm.text

    challenge = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert challenge.status_code == 200, challenge.text
    assert challenge.json()["mfa_required"] is True

    verified = client.post(
        "/api/v1/auth/mfa/verify",
        json={
            "challenge_token": challenge.json()["challenge_token"],
            "code": pyotp.TOTP(secret).now(),
        },
    )
    assert verified.status_code == 200, verified.text
    return verified.json()


@pytest.fixture(scope="session")
def org_admin_login(
    client: TestClient, admin_engine: Engine, seed_domain: str, seed_password: str
) -> dict:
    return enrol_and_login(client, admin_engine, f"admin@{seed_domain}", seed_password)


@pytest.fixture
def org_admin(org_admin_login: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {org_admin_login['access_token']}"}


@pytest.fixture(scope="session")
def reviewer_login(
    client: TestClient, admin_engine: Engine, seed_domain: str, seed_password: str
) -> dict:
    return enrol_and_login(client, admin_engine, f"reviewer@{seed_domain}", seed_password)


# ---------------------------------------------------------------------------
# Authoring helpers
#
# Tests that need a *fresh* exam build one through the public API rather than
# reusing the seeded one. Two reasons, both learned the hard way:
#
#   * The seeded exam is 90 minutes long. A test that starts a session on it
#     passes for 90 minutes and then fails with "you have used all 1 permitted
#     attempt(s)", because the session expired and counts as finished. A test
#     whose result depends on how long ago the database was seeded is worse
#     than no test.
#   * The suite is deliberately run twice against the same database. Anything
#     that consumes a single-attempt assignment cannot be run twice.
# ---------------------------------------------------------------------------


def author_exam(
    client: TestClient,
    staff_headers: dict[str, str],
    *,
    duration_seconds: int = 3600,
    show_score_on_submit: bool = True,
) -> dict:
    """A small published exam: one 2-of-3 choice pool, one short answer, one numeric."""
    tag = uuid.uuid4().hex[:8]

    def choice(prefix: str) -> dict:
        return {
            "options": [
                {"id": "a", "text": f"{prefix} A"},
                {"id": "b", "text": f"{prefix} B"},
                {"id": "c", "text": f"{prefix} C"},
            ],
            "correct": ["b"],
            "shuffle_options": True,
        }

    bank_id = client.post(
        "/api/v1/banks", json={"name": f"Bank {tag}"}, headers=staff_headers
    ).json()["id"]

    version_ids = client.post(
        "/api/v1/questions/import",
        json={
            "bank_id": bank_id,
            "publish": True,
            "questions": [
                {
                    "external_key": f"{tag}-q1",
                    "kind": "single_choice",
                    "prompt": "Pick B.",
                    "body": choice("Q1"),
                    "marks": 4,
                },
                {
                    "external_key": f"{tag}-q2",
                    "kind": "single_choice",
                    "prompt": "Pick B again.",
                    "body": choice("Q2"),
                    "marks": 4,
                },
                {
                    "external_key": f"{tag}-q3",
                    "kind": "single_choice",
                    "prompt": "And again.",
                    "body": choice("Q3"),
                    "marks": 4,
                },
                {
                    "external_key": f"{tag}-q4",
                    "kind": "short_answer",
                    "prompt": "Name the shortest-path algorithm.",
                    "body": {"accepted": ["dijkstra"], "match": "ci", "trim": True},
                    "marks": 3,
                },
                {
                    "external_key": f"{tag}-q5",
                    "kind": "numeric",
                    "prompt": "3/4 as a decimal?",
                    "body": {"value": 0.75, "tolerance": 0.01, "tolerance_kind": "abs"},
                    "marks": 3,
                },
            ],
        },
        headers=staff_headers,
    ).json()["version_ids"]

    exam_id = client.post(
        "/api/v1/exams", json={"title": f"Exam {tag}"}, headers=staff_headers
    ).json()["id"]

    version_id = client.post(
        f"/api/v1/exams/{exam_id}/versions",
        json={
            "duration_seconds": duration_seconds,
            "grace_seconds": 60,
            "show_score_on_submit": show_score_on_submit,
            "sections": [
                {
                    "ordinal": 1,
                    "title": "Choice",
                    "shuffle_questions": True,
                    "pools": [
                        {"ordinal": 1, "select_count": 2, "question_version_ids": version_ids[:3]}
                    ],
                },
                {
                    "ordinal": 2,
                    "title": "Written",
                    "shuffle_questions": False,
                    "pools": [
                        {"ordinal": 1, "select_count": 1, "question_version_ids": [version_ids[3]]},
                        {"ordinal": 2, "select_count": 1, "question_version_ids": [version_ids[4]]},
                    ],
                },
            ],
        },
        headers=staff_headers,
    ).json()["version_id"]

    published = client.post(f"/api/v1/exam-versions/{version_id}/publish", headers=staff_headers)
    assert published.status_code == 200, published.text

    return {
        "bank_id": bank_id,
        "exam_id": exam_id,
        "version_id": version_id,
        "question_version_ids": version_ids,
        "tag": tag,
    }


def assign_exam(
    client: TestClient, staff_headers: dict[str, str], version_id: str, email: str
) -> str:
    """Assign one candidate and return the assignment id."""
    resp = client.post(
        f"/api/v1/exam-versions/{version_id}/assignments",
        json={"emails": [email]},
        headers=staff_headers,
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()["results"][0]
    assert result["status"] == "assigned", result
    return result["assignment_id"]


def bearer(login: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {login['access_token']}"}


@pytest.fixture
def second_org(admin_engine: Engine) -> Iterator[dict[str, uuid.UUID]]:
    """A second organization with its own candidate.

    Tenant isolation cannot be tested with one tenant. This creates a genuine
    neighbour so the cross-tenant tests have something real to fail against.
    """
    ids = {
        "org_id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "exam_id": uuid.uuid4(),
        "exam_version_id": uuid.uuid4(),
    }
    # organization and app_user are not tenant-owned, so they are created
    # without context; everything else must declare the tenant it belongs to.
    with admin_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO organization (id, slug, name) VALUES (:i, :s, :n)"),
            {"i": ids["org_id"], "s": f"rival-{ids['org_id'].hex[:8]}", "n": "Rival Institute"},
        )
        conn.execute(
            text(
                "INSERT INTO app_user (id, email, display_name, password_hash) "
                "VALUES (:i, :e, :d, :p)"
            ),
            {
                "i": ids["user_id"],
                "e": f"rival-{ids['user_id'].hex[:8]}@rival.example.org",
                "d": "Rival Candidate",
                "p": "x",
            },
        )

    with admin_tx(admin_engine, org_id=ids["org_id"], user_id=ids["user_id"]) as conn:
        conn.execute(
            text("INSERT INTO membership (org_id, user_id, role) VALUES (:o, :u, 'candidate')"),
            {"o": ids["org_id"], "u": ids["user_id"]},
        )
        conn.execute(
            text("INSERT INTO exam (id, org_id, title, owner_id) VALUES (:i, :o, :t, :u)"),
            {"i": ids["exam_id"], "o": ids["org_id"], "t": "Rival Exam", "u": ids["user_id"]},
        )
        conn.execute(
            text(
                "INSERT INTO exam_version (id, org_id, exam_id, version, status, "
                "duration_seconds, published_at) "
                "VALUES (:i, :o, :e, 1, 'published', 3600, now())"
            ),
            {"i": ids["exam_version_id"], "o": ids["org_id"], "e": ids["exam_id"]},
        )

    yield ids

    with admin_tx(admin_engine, org_id=ids["org_id"], user_id=ids["user_id"]) as conn:
        conn.execute(text("DELETE FROM exam_version WHERE id = :i"), {"i": ids["exam_version_id"]})
        conn.execute(text("DELETE FROM exam WHERE id = :i"), {"i": ids["exam_id"]})
        conn.execute(text("DELETE FROM membership WHERE org_id = :o"), {"o": ids["org_id"]})
    with admin_engine.begin() as conn:
        conn.execute(text("DELETE FROM app_user WHERE id = :i"), {"i": ids["user_id"]})
        conn.execute(text("DELETE FROM organization WHERE id = :i"), {"i": ids["org_id"]})
