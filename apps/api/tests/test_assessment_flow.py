"""Phase 2 end-to-end, against a live PostgreSQL with RLS enforced.

The four exit criteria from MVP_SCOPE.md §5 each have a test here, named so the
mapping is obvious:

* a candidate completes an exam end-to-end   -> test_candidate_completes_an_exam_end_to_end
* a forced reload mid-exam loses nothing     -> test_forced_reload_loses_nothing
* the server rejects a late submission       -> test_server_rejects_a_late_submission
* the paper reproduces exactly on recovery   -> test_paper_reproduces_exactly_on_recovery
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.conftest import admin_tx

pytestmark = pytest.mark.usefixtures("_seeded")


# --------------------------------------------------------------------- helpers


def _auth(login: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {login['access_token']}"}


@pytest.fixture
def instructor(instructor_login: dict) -> dict[str, str]:
    return _auth(instructor_login)


@pytest.fixture
def candidate(candidate_login: dict) -> dict[str, str]:
    return _auth(candidate_login)


def _choice(prefix: str, correct: str = "b") -> dict:
    return {
        "options": [
            {"id": "a", "text": f"{prefix} A"},
            {"id": "b", "text": f"{prefix} B"},
            {"id": "c", "text": f"{prefix} C"},
        ],
        "correct": [correct],
        "shuffle_options": True,
    }


@pytest.fixture
def authored_exam(client: TestClient, instructor: dict[str, str]) -> dict:
    """Build a small exam through the public API, exactly as an instructor would.

    Deliberately not built by inserting rows: the point is to exercise the
    authoring endpoints, and a fixture that bypasses them would leave the
    end-to-end test proving less than its name claims.
    """
    tag = uuid.uuid4().hex[:8]

    bank = client.post("/api/v1/banks", json={"name": f"Phase 2 bank {tag}"}, headers=instructor)
    assert bank.status_code == 201, bank.text
    bank_id = bank.json()["id"]

    payload = {
        "schema_version": "question.v1",
        "bank_id": bank_id,
        "publish": True,
        "questions": [
            {
                "external_key": f"{tag}-q1",
                "kind": "single_choice",
                "prompt": "Pick B.",
                "body": _choice("Q1"),
                "marks": 4,
            },
            {
                "external_key": f"{tag}-q2",
                "kind": "single_choice",
                "prompt": "Pick B again.",
                "body": _choice("Q2"),
                "marks": 4,
            },
            {
                "external_key": f"{tag}-q3",
                "kind": "single_choice",
                "prompt": "And again.",
                "body": _choice("Q3"),
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
    }
    imported = client.post("/api/v1/questions/import", json=payload, headers=instructor)
    assert imported.status_code == 200, imported.text
    version_ids = imported.json()["version_ids"]

    exam = client.post("/api/v1/exams", json={"title": f"Phase 2 exam {tag}"}, headers=instructor)
    assert exam.status_code == 201, exam.text
    exam_id = exam.json()["id"]

    version = client.post(
        f"/api/v1/exams/{exam_id}/versions",
        json={
            "duration_seconds": 3600,
            "grace_seconds": 60,
            "show_score_on_submit": True,
            "sections": [
                {
                    "ordinal": 1,
                    "title": "Choice",
                    "shuffle_questions": True,
                    # 2 of 3: the pool that makes papers differ between candidates.
                    "pools": [
                        {
                            "ordinal": 1,
                            "select_count": 2,
                            "question_version_ids": version_ids[:3],
                        }
                    ],
                },
                {
                    "ordinal": 2,
                    "title": "Written",
                    "shuffle_questions": False,
                    "pools": [
                        {
                            "ordinal": 1,
                            "select_count": 1,
                            "question_version_ids": [version_ids[3]],
                        },
                        {
                            "ordinal": 2,
                            "select_count": 1,
                            "question_version_ids": [version_ids[4]],
                        },
                    ],
                },
            ],
        },
        headers=instructor,
    )
    assert version.status_code == 201, version.text
    version_id = version.json()["version_id"]

    published = client.post(f"/api/v1/exam-versions/{version_id}/publish", headers=instructor)
    assert published.status_code == 200, published.text

    return {
        "bank_id": bank_id,
        "exam_id": exam_id,
        "version_id": version_id,
        "question_version_ids": version_ids,
        "tag": tag,
    }


@pytest.fixture
def assigned(
    client: TestClient, instructor: dict[str, str], authored_exam: dict, seed_domain: str
) -> dict:
    resp = client.post(
        f"/api/v1/exam-versions/{authored_exam['version_id']}/assignments",
        json={"emails": [f"aisha.rahman@{seed_domain}"]},
        headers=instructor,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["assigned"] == 1
    return {**authored_exam, "assignment_id": resp.json()["results"][0]["assignment_id"]}


# ================================================================ exit criteria


def test_candidate_completes_an_exam_end_to_end(
    client: TestClient, candidate: dict[str, str], assigned: dict
) -> None:
    """MVP_SCOPE §5 phase 2 exit criterion 1."""
    started = client.post(
        "/api/v1/sessions", json={"assignment_id": assigned["assignment_id"]}, headers=candidate
    )
    assert started.status_code == 201, started.text
    state = started.json()
    session_id = state["session_id"]

    assert state["question_count"] == 4  # 2 from the pool + 1 + 1
    assert state["remaining_seconds"] > 3500
    assert state["status"] == "in_progress"

    # The answer key must not be in what the candidate is served.
    assert "correct" not in str(state["items"])
    assert "accepted" not in str(state["items"])
    assert "tolerance" not in str(state["items"])

    for item in state["items"]:
        if item["kind"] == "single_choice":
            value = {"selected": ["b"]}
        elif item["kind"] == "short_answer":
            value = {"text": "Dijkstra"}
        else:
            value = {"value": 0.75}
        saved = client.put(
            f"/api/v1/sessions/{session_id}/answers/{item['paper_item_id']}",
            json={"value": value},
            headers=candidate,
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["revision"] == 1

    submitted = client.post(
        f"/api/v1/sessions/{session_id}/submit", json={"reason": "candidate"}, headers=candidate
    )
    assert submitted.status_code == 200, submitted.text
    body = submitted.json()
    assert body["status"] == "submitted"
    assert body["answered"] == 4
    # 2 choice questions at 4 + short answer 3 + numeric 3
    assert body["max_marks"] == 14.0
    assert body["total_marks"] == 14.0

    result = client.get(f"/api/v1/sessions/{session_id}/result", headers=candidate)
    assert result.status_code == 200, result.text
    assert result.json()["percentage"] == 100.0
    assert len(result.json()["items"]) == 4


def test_forced_reload_loses_nothing(
    client: TestClient, candidate: dict[str, str], assigned: dict
) -> None:
    """MVP_SCOPE §5 phase 2 exit criterion 2.

    Simulates the browser being killed: the client keeps no state, POSTs to
    /sessions again as if starting fresh, and must get its own session back with
    every answer intact.
    """
    started = client.post(
        "/api/v1/sessions", json={"assignment_id": assigned["assignment_id"]}, headers=candidate
    )
    assert started.status_code == 201
    state = started.json()
    session_id = state["session_id"]
    first_item = state["items"][0]

    client.put(
        f"/api/v1/sessions/{session_id}/answers/{first_item['paper_item_id']}",
        json={"value": {"selected": ["c"]}},
        headers=candidate,
    )

    # The crash. A second start must resume, not create a second attempt.
    resumed = client.post(
        "/api/v1/sessions", json={"assignment_id": assigned["assignment_id"]}, headers=candidate
    )
    assert resumed.status_code == 200, "resume must not report 201 Created"
    resumed_state = resumed.json()
    assert resumed_state["session_id"] == session_id
    assert resumed_state["attempt_no"] == 1

    recovered = next(
        i for i in resumed_state["items"] if i["paper_item_id"] == first_item["paper_item_id"]
    )
    assert recovered["answer"] == {"selected": ["c"]}
    assert recovered["revision"] == 1

    # GET is the other recovery path — a reconnecting tab that still holds the id.
    fetched = client.get(f"/api/v1/sessions/{session_id}", headers=candidate)
    assert fetched.status_code == 200
    assert fetched.json()["items"] == resumed_state["items"]


def test_paper_reproduces_exactly_on_recovery(
    client: TestClient, candidate: dict[str, str], assigned: dict, app_engine: Engine
) -> None:
    """MVP_SCOPE §5 phase 2 exit criterion 4.

    Not "the stored rows are returned again" — that would only prove the
    database works. The paper is *re-derived* from the seed and compared to what
    was stored, which is what makes determinism a checked property.
    """
    from sentinel_api.assessment.delivery import verify_paper_matches_seed
    from sentinel_api.core.db import tenant_session
    from sentinel_api.models import ExamSession

    started = client.post(
        "/api/v1/sessions", json={"assignment_id": assigned["assignment_id"]}, headers=candidate
    )
    session_id = uuid.UUID(started.json()["session_id"])
    ordering = [i["paper_item_id"] for i in started.json()["items"]]
    options = [i["body"].get("options") for i in started.json()["items"]]

    with tenant_session(
        org_id=str(_org_of(app_engine)),
        user_id=str(started.json()["items"] and _candidate_of(app_engine, session_id)),
        role="instructor",
    ) as session:
        exam_session = session.get(ExamSession, session_id)
        assert exam_session is not None
        assert verify_paper_matches_seed(session, exam_session) is True

    again = client.get(f"/api/v1/sessions/{session_id}", headers=candidate)
    assert [i["paper_item_id"] for i in again.json()["items"]] == ordering
    assert [i["body"].get("options") for i in again.json()["items"]] == options


def test_server_rejects_a_late_submission(
    client: TestClient, candidate: dict[str, str], assigned: dict, admin_engine: Engine
) -> None:
    """MVP_SCOPE §5 phase 2 exit criterion 3.

    The deadline is moved into the past directly in the database rather than by
    waiting an hour. The client is never consulted about the time, which is the
    property under test: nothing the browser sends can change the answer.
    """
    started = client.post(
        "/api/v1/sessions", json={"assignment_id": assigned["assignment_id"]}, headers=candidate
    )
    session_id = started.json()["session_id"]

    past = dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)
    with admin_tx(admin_engine, org_id=_org_of(admin_engine), role="instructor") as conn:
        conn.execute(
            text("UPDATE exam_session SET started_at = :s, deadline_at = :d WHERE id = :i"),
            {"s": past - dt.timedelta(hours=1), "d": past, "i": session_id},
        )

    late = client.post(
        f"/api/v1/sessions/{session_id}/submit", json={"reason": "candidate"}, headers=candidate
    )
    assert late.status_code == 409, late.text
    assert "deadline" in late.json()["detail"].lower()

    # And the session is closed, not left dangling as in_progress forever.
    state = client.get(f"/api/v1/sessions/{session_id}", headers=candidate)
    assert state.json()["status"] == "expired"
    assert state.json()["remaining_seconds"] == 0

    # A late autosave is refused too — the exam is over, not merely un-submittable.
    item = state.json()["items"][0]
    refused = client.put(
        f"/api/v1/sessions/{session_id}/answers/{item['paper_item_id']}",
        json={"value": {"selected": ["a"]}},
        headers=candidate,
    )
    assert refused.status_code == 409


# =============================================================== autosave rules


def test_stale_save_is_rejected_and_returns_the_server_state(
    client: TestClient, candidate: dict[str, str], assigned: dict
) -> None:
    """A reconnecting tab must not overwrite newer work with what it cached."""
    started = client.post(
        "/api/v1/sessions", json={"assignment_id": assigned["assignment_id"]}, headers=candidate
    )
    session_id = started.json()["session_id"]
    item = started.json()["items"][0]
    url = f"/api/v1/sessions/{session_id}/answers/{item['paper_item_id']}"

    client.put(url, json={"value": {"selected": ["a"]}}, headers=candidate)
    second = client.put(
        url, json={"value": {"selected": ["b"]}, "client_revision": 1}, headers=candidate
    )
    assert second.status_code == 200
    assert second.json()["revision"] == 2

    stale = client.put(
        url, json={"value": {"selected": ["c"]}, "client_revision": 1}, headers=candidate
    )
    assert stale.status_code == 409
    body = stale.json()
    assert body["server_revision"] == 2
    assert body["server_value"] == {"selected": ["b"]}

    state = client.get(f"/api/v1/sessions/{session_id}", headers=candidate)
    kept = next(i for i in state.json()["items"] if i["paper_item_id"] == item["paper_item_id"])
    assert kept["answer"] == {"selected": ["b"]}


def test_every_save_is_kept_in_the_append_only_history(
    client: TestClient, candidate: dict[str, str], assigned: dict, admin_engine: Engine
) -> None:
    started = client.post(
        "/api/v1/sessions", json={"assignment_id": assigned["assignment_id"]}, headers=candidate
    )
    session_id = started.json()["session_id"]
    item = started.json()["items"][0]
    url = f"/api/v1/sessions/{session_id}/answers/{item['paper_item_id']}"

    for option in ("a", "b", "c"):
        client.put(url, json={"value": {"selected": [option]}}, headers=candidate)

    with admin_tx(admin_engine, org_id=_org_of(admin_engine), role="instructor") as conn:
        rows = conn.execute(
            text(
                "SELECT revision, value FROM answer_revision "
                "WHERE session_id = :s AND paper_item_id = :p ORDER BY revision"
            ),
            {"s": session_id, "p": item["paper_item_id"]},
        ).all()
    assert [r[0] for r in rows] == [1, 2, 3]
    assert [r[1]["selected"][0] for r in rows] == ["a", "b", "c"]


def test_answer_shape_is_validated_at_save_time(
    client: TestClient, candidate: dict[str, str], assigned: dict
) -> None:
    """Tell the candidate now, while they can still fix it."""
    started = client.post(
        "/api/v1/sessions", json={"assignment_id": assigned["assignment_id"]}, headers=candidate
    )
    session_id = started.json()["session_id"]
    item = next(i for i in started.json()["items"] if i["kind"] == "single_choice")

    bad = client.put(
        f"/api/v1/sessions/{session_id}/answers/{item['paper_item_id']}",
        json={"value": {"selected": ["not-an-option"]}},
        headers=candidate,
    )
    assert bad.status_code == 422
    assert "unknown option" in bad.json()["detail"].lower()

    too_many = client.put(
        f"/api/v1/sessions/{session_id}/answers/{item['paper_item_id']}",
        json={"value": {"selected": ["a", "b"]}},
        headers=candidate,
    )
    assert too_many.status_code == 422


def test_unanswered_questions_are_graded_as_zero_not_skipped(
    client: TestClient, candidate: dict[str, str], assigned: dict
) -> None:
    """max_marks must count the whole paper, or a percentage is meaningless."""
    started = client.post(
        "/api/v1/sessions", json={"assignment_id": assigned["assignment_id"]}, headers=candidate
    )
    session_id = started.json()["session_id"]
    item = started.json()["items"][0]
    if item["kind"] == "single_choice":
        client.put(
            f"/api/v1/sessions/{session_id}/answers/{item['paper_item_id']}",
            json={"value": {"selected": ["b"]}},
            headers=candidate,
        )

    submitted = client.post(f"/api/v1/sessions/{session_id}/submit", json={}, headers=candidate)
    assert submitted.json()["max_marks"] == 14.0
    assert submitted.json()["total_marks"] < 14.0

    result = client.get(f"/api/v1/sessions/{session_id}/result", headers=candidate)
    assert len(result.json()["items"]) == 4
    blanks = [i for i in result.json()["items"] if i["detail"].get("outcome") == "blank"]
    assert len(blanks) == 3


# ================================================================== lifecycle


def test_second_submit_is_refused(
    client: TestClient, candidate: dict[str, str], assigned: dict
) -> None:
    started = client.post(
        "/api/v1/sessions", json={"assignment_id": assigned["assignment_id"]}, headers=candidate
    )
    session_id = started.json()["session_id"]
    assert (
        client.post(f"/api/v1/sessions/{session_id}/submit", json={}, headers=candidate).status_code
        == 200
    )
    again = client.post(f"/api/v1/sessions/{session_id}/submit", json={}, headers=candidate)
    assert again.status_code == 409


def test_attempt_limit_is_enforced(
    client: TestClient, candidate: dict[str, str], assigned: dict
) -> None:
    started = client.post(
        "/api/v1/sessions", json={"assignment_id": assigned["assignment_id"]}, headers=candidate
    )
    session_id = started.json()["session_id"]
    client.post(f"/api/v1/sessions/{session_id}/submit", json={}, headers=candidate)

    second = client.post(
        "/api/v1/sessions", json={"assignment_id": assigned["assignment_id"]}, headers=candidate
    )
    assert second.status_code == 409
    assert "attempt" in second.json()["detail"].lower()


def test_answers_are_still_readable_after_submission(
    client: TestClient, candidate: dict[str, str], assigned: dict
) -> None:
    """Reviewing what you submitted is legitimate; changing it is not."""
    started = client.post(
        "/api/v1/sessions", json={"assignment_id": assigned["assignment_id"]}, headers=candidate
    )
    session_id = started.json()["session_id"]
    item = started.json()["items"][0]
    client.put(
        f"/api/v1/sessions/{session_id}/answers/{item['paper_item_id']}",
        json={"value": {"selected": ["b"]}},
        headers=candidate,
    )
    client.post(f"/api/v1/sessions/{session_id}/submit", json={}, headers=candidate)

    state = client.get(f"/api/v1/sessions/{session_id}", headers=candidate)
    assert state.status_code == 200
    assert state.json()["status"] == "submitted"

    edit = client.put(
        f"/api/v1/sessions/{session_id}/answers/{item['paper_item_id']}",
        json={"value": {"selected": ["a"]}},
        headers=candidate,
    )
    assert edit.status_code == 409


# ------------------------------------------------------------------- utilities


def _org_of(engine: Engine) -> uuid.UUID:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT id FROM organization WHERE slug = 'demo-university'")
        ).scalar_one()


def _candidate_of(engine: Engine, session_id: uuid.UUID) -> uuid.UUID:
    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('sentinel.role', 'system', true)"))
        conn.execute(
            text("SELECT set_config('sentinel.org_id', :o, true)"),
            {"o": str(_org_of(engine))},
        )
        return conn.execute(
            text("SELECT candidate_user_id FROM exam_session WHERE id = :i"), {"i": session_id}
        ).scalar_one()
