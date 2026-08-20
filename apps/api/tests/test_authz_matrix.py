"""Authorization matrix and structural assertions.

Two categories here:

1. Every route on the app is declared in the policy table. A new endpoint that
   nobody added to the matrix fails CI rather than shipping unguarded.

2. Structural invariants that make the product's promises impossible to break
   by accident — no risk-to-score path, no endpoint that mutates the audit
   chain, no `cheating` column anywhere.
"""

from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from sentinel_api.tenancy.policies import POLICY, PolicyError, rule_for
from tests.conftest import iter_routes

pytestmark = pytest.mark.security

API_ROOT = pathlib.Path(__file__).resolve().parents[1] / "sentinel_api"

IGNORED_PATHS = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


def test_every_route_has_a_policy(app) -> None:
    routes = iter_routes(app)
    assert len(routes) >= 10, (
        f"Only {len(routes)} routes discovered; this test would pass vacuously. "
        "Route discovery is broken, not the policy table."
    )
    missing: list[str] = []
    for route in routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not methods or path in IGNORED_PATHS:
            continue
        for method in methods - {"HEAD", "OPTIONS"}:
            try:
                rule_for(method, path)
            except PolicyError:
                missing.append(f"{method} {path}")
    assert missing == [], (
        "Routes with no authorization rule: "
        + ", ".join(sorted(missing))
        + ". Declare them in tenancy/policies.py."
    )


def test_no_policy_entry_is_orphaned(app) -> None:
    """The reverse check: a stale policy entry means the matrix is describing an
    endpoint that no longer exists, which makes the table untrustworthy."""
    live = {(m, r.path) for r in iter_routes(app) for m in (r.methods - {"HEAD", "OPTIONS"})}
    orphans = set(POLICY) - live
    assert orphans == set(), f"Policy entries with no matching route: {sorted(orphans)}"


def test_candidate_cannot_list_org_members(client: TestClient, candidate_login: dict) -> None:
    resp = client.get(
        "/api/v1/org/members",
        headers={"authorization": f"Bearer {candidate_login['access_token']}"},
    )
    assert resp.status_code == 403


def test_instructor_can_list_org_members(client: TestClient, instructor_login: dict) -> None:
    resp = client.get(
        "/api/v1/org/members",
        headers={"authorization": f"Bearer {instructor_login['access_token']}"},
    )
    assert resp.status_code == 200
    # The seed creates one org_admin, one instructor, one reviewer and five
    # candidates. Asserting the roster rather than a bare count means a change
    # to the seed shows up as "who appeared/disappeared", not as an off-by-one.
    roles = sorted(r for m in resp.json() for r in m["roles"])
    assert roles == ["candidate"] * 5 + ["instructor", "org_admin", "reviewer"]


def test_instructor_cannot_read_org_activity(client: TestClient, instructor_login: dict) -> None:
    """Administrative audit is org_admin only."""
    resp = client.get(
        "/api/v1/org/activity",
        headers={"authorization": f"Bearer {instructor_login['access_token']}"},
    )
    assert resp.status_code == 403


def test_candidate_sees_only_their_own_exams(client: TestClient, candidate_login: dict) -> None:
    resp = client.get(
        "/api/v1/me/exams",
        headers={"authorization": f"Bearer {candidate_login['access_token']}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    # An exact count would make this test depend on every other test that
    # assigns an exam, and the suite is run deliberately against a warm
    # database. The claim under test is that the seeded assignment is visible
    # and correctly described — not how many other assignments exist.
    seeded = [e for e in body if e["title"] == "Programming Fundamentals — Final"]
    assert len(seeded) == 1
    assert seeded[0]["window_state"] == "open"
    assert seeded[0]["integrity_enabled"] is True


def test_public_keys_endpoint_needs_no_authentication(app) -> None:
    """Verification that requires Sentinel's permission is not verification."""
    fresh = TestClient(app)
    resp = fresh.get("/api/v1/public-keys")
    assert resp.status_code == 200
    key = resp.json()["keys"][0]
    assert key["algorithm"] == "ed25519"
    assert key["key_fingerprint"]


def test_health_does_not_touch_the_database(client: TestClient) -> None:
    """A liveness probe that queries Postgres turns a slow query into a restart
    storm. /health must answer from the process alone."""
    source = (API_ROOT / "core" / "health.py").read_text()
    health_fn = source.split("def health(")[1].split("def ready(")[0]
    for forbidden in ("get_engine", "execute", "select", "redis"):
        assert forbidden not in health_fn, f"/health must not reference {forbidden}."


# ------------------------------------------------------- structural invariants


def test_no_endpoint_mutates_the_audit_chain(app) -> None:
    """There must be no route that updates or deletes an audit event.

    Not gated, not admin-only — absent. Combined with the database trigger,
    that is two independent reasons the chain cannot be rewritten through the
    product.
    """
    offenders = [
        f"{m} {r.path}"
        for r in iter_routes(app)
        for m in (r.methods & {"PUT", "PATCH", "DELETE", "POST"})
        if "audit" in str(getattr(r, "path", "")).lower()
        and not str(r.path).endswith(("/export", "/verify", "/head"))
    ]
    assert offenders == [], f"Endpoints that could mutate the audit chain: {offenders}"


def test_schema_has_no_cheating_field(admin_engine: Engine) -> None:
    """ADR-0015 made structural.

    If the column does not exist, no future engineer can join it to a grade and
    no customer can ask for it to be surfaced "just as a filter". That is how
    evidence quietly becomes verdict.
    """
    with admin_engine.connect() as conn:
        columns = [
            f"{r[0]}.{r[1]}"
            for r in conn.execute(
                text(
                    """
                    SELECT table_name, column_name FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND (column_name ILIKE '%cheat%'
                           OR column_name ILIKE '%guilty%'
                           OR column_name ILIKE '%misconduct%')
                    """
                )
            )
        ]
    assert columns == [], f"Schema contains verdict-shaped columns: {columns}"


def test_risk_assessment_has_no_link_to_scoring(admin_engine: Engine) -> None:
    """No foreign key may connect risk to results.

    There is deliberately no path in the data model from an automated risk
    estimate to a grade.
    """
    with admin_engine.connect() as conn:
        links = conn.execute(
            text(
                """
                SELECT con.conname
                FROM pg_constraint con
                JOIN pg_class src ON src.oid = con.conrelid
                JOIN pg_class tgt ON tgt.oid = con.confrelid
                WHERE con.contype = 'f'
                  AND (
                    (src.relname LIKE 'risk%' AND tgt.relname IN
                        ('session_result','question_score'))
                    OR (src.relname IN ('session_result','question_score')
                        AND tgt.relname LIKE 'risk%')
                  )
                """
            )
        ).all()
    assert links == [], f"Risk is linked to scoring by: {[r[0] for r in links]}"


def test_no_source_module_imports_risk_into_grading() -> None:
    """The static half of the same rule, for code rather than schema."""
    offenders: list[str] = []
    for path in (API_ROOT / "grading").rglob("*.py"):
        text_ = path.read_text()
        if "risk" in text_.lower():
            offenders.append(str(path))
    assert offenders == [], f"Grading code references risk: {offenders}"
