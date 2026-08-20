"""Question bank, import/export, and exam publishing rules."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def instructor(instructor_login: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {instructor_login['access_token']}"}


@pytest.fixture
def bank_id(client: TestClient, instructor: dict[str, str]) -> str:
    resp = client.post(
        "/api/v1/banks",
        json={"name": f"Bank {uuid.uuid4().hex[:8]}"},
        headers=instructor,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _q(key: str, **over) -> dict:
    base = {
        "external_key": key,
        "kind": "single_choice",
        "prompt": "Pick one.",
        "body": {
            "options": [{"id": "a", "text": "A"}, {"id": "b", "text": "B"}],
            "correct": ["a"],
        },
        "marks": 2,
    }
    base.update(over)
    return base


# ------------------------------------------------------------ body validation


def test_single_choice_rejects_two_correct_options(
    client: TestClient, instructor: dict[str, str], bank_id: str
) -> None:
    """`multiple_choice` exists for that. A single_choice with two keys is
    almost always an authoring mistake, and it silently makes the question
    ungradable."""
    resp = client.post(
        f"/api/v1/banks/{bank_id}/questions",
        json=_q(
            "x",
            body={
                "options": [{"id": "a", "text": "A"}, {"id": "b", "text": "B"}],
                "correct": ["a", "b"],
            },
        ),
        headers=instructor,
    )
    assert resp.status_code == 422
    assert any(e["pointer"] == "/body/correct" for e in resp.json()["errors"])


def test_correct_must_reference_a_real_option(
    client: TestClient, instructor: dict[str, str], bank_id: str
) -> None:
    resp = client.post(
        f"/api/v1/banks/{bank_id}/questions",
        json=_q(
            "y",
            body={
                "options": [{"id": "a", "text": "A"}, {"id": "b", "text": "B"}],
                "correct": ["z"],
            },
        ),
        headers=instructor,
    )
    assert resp.status_code == 422
    assert "unknown option" in str(resp.json()["errors"]).lower()


def test_invalid_regex_is_rejected_at_authoring_time(
    client: TestClient, instructor: dict[str, str], bank_id: str
) -> None:
    """Better a 422 now than a grading crash after 200 people have sat the exam."""
    resp = client.post(
        f"/api/v1/banks/{bank_id}/questions",
        json=_q(
            "z",
            kind="short_answer",
            body={"accepted": ["([a-z"], "match": "regex"},
        ),
        headers=instructor,
    )
    assert resp.status_code == 422
    assert "regular expression" in str(resp.json()["errors"]).lower()


def test_relative_tolerance_around_zero_is_rejected(
    client: TestClient, instructor: dict[str, str], bank_id: str
) -> None:
    """`|0| * 0.05 == 0` — a relative tolerance around zero accepts only an
    exact match, which is never what the author meant."""
    resp = client.post(
        f"/api/v1/banks/{bank_id}/questions",
        json=_q(
            "n",
            kind="numeric",
            body={"value": 0, "tolerance": 0.05, "tolerance_kind": "rel"},
        ),
        headers=instructor,
    )
    assert resp.status_code == 422


def test_coding_question_needs_test_cases(
    client: TestClient, instructor: dict[str, str], bank_id: str
) -> None:
    resp = client.post(
        f"/api/v1/banks/{bank_id}/questions",
        json=_q("c", kind="coding", body={"languages": ["python311"]}, test_cases=[]),
        headers=instructor,
    )
    assert resp.status_code == 422


# -------------------------------------------------------------------- versioning


def test_editing_a_question_creates_a_new_version(
    client: TestClient, instructor: dict[str, str], bank_id: str
) -> None:
    """The old version must survive, because a session's paper points at it."""
    key = f"k-{uuid.uuid4().hex[:8]}"
    first = client.post(f"/api/v1/banks/{bank_id}/questions", json=_q(key), headers=instructor)
    assert first.status_code == 201
    assert first.json()["version"] == 1

    second = client.post(
        f"/api/v1/banks/{bank_id}/questions",
        json=_q(key, prompt="Reworded."),
        headers=instructor,
    )
    assert second.status_code == 201
    assert second.json()["version"] == 2
    assert second.json()["question_id"] == first.json()["question_id"]
    assert second.json()["version_id"] != first.json()["version_id"]


def test_a_questions_kind_cannot_change_between_versions(
    client: TestClient, instructor: dict[str, str], bank_id: str
) -> None:
    """Changing the kind changes what a stored answer *means*."""
    key = f"k-{uuid.uuid4().hex[:8]}"
    client.post(f"/api/v1/banks/{bank_id}/questions", json=_q(key), headers=instructor)
    changed = client.post(
        f"/api/v1/banks/{bank_id}/questions",
        json=_q(key, kind="numeric", body={"value": 1.0}),
        headers=instructor,
    )
    assert changed.status_code == 422
    assert "kind" in str(changed.json()["errors"])


# ------------------------------------------------------------------ import


def test_import_reports_every_bad_row_at_once(
    client: TestClient, instructor: dict[str, str], bank_id: str
) -> None:
    """Nobody wants to fix a 500-question file one error per round trip."""
    resp = client.post(
        "/api/v1/questions/import",
        json={
            "bank_id": bank_id,
            "questions": [
                _q("ok-1"),
                _q("bad-1", body={"options": [{"id": "a", "text": "A"}], "correct": ["a"]}),
                _q("ok-2"),
                _q(
                    "bad-2",
                    kind="numeric",
                    body={"value": 0, "tolerance_kind": "rel", "tolerance": 1},
                ),
            ],
        },
        headers=instructor,
    )
    assert resp.status_code == 422
    errors = resp.json()["errors"]
    assert {e["external_key"] for e in errors} == {"bad-1", "bad-2"}
    assert all("pointer" in e and "index" in e for e in errors)


def test_a_failed_import_writes_nothing(
    client: TestClient, instructor: dict[str, str], bank_id: str
) -> None:
    """A half-imported bank is worse than a rejected file."""
    before = client.get(f"/api/v1/banks/{bank_id}/questions", headers=instructor).json()
    client.post(
        "/api/v1/questions/import",
        json={
            "bank_id": bank_id,
            "questions": [_q("good-a"), _q("broken", body={"options": [], "correct": ["a"]})],
        },
        headers=instructor,
    )
    after = client.get(f"/api/v1/banks/{bank_id}/questions", headers=instructor).json()
    assert len(after) == len(before)


def test_duplicate_external_key_within_one_file_is_reported(
    client: TestClient, instructor: dict[str, str], bank_id: str
) -> None:
    resp = client.post(
        "/api/v1/questions/import",
        json={"bank_id": bank_id, "questions": [_q("dupe"), _q("dupe")]},
        headers=instructor,
    )
    assert resp.status_code == 422
    assert "duplicated" in str(resp.json()["errors"])


def test_reimporting_the_same_key_upserts_to_a_new_version(
    client: TestClient, instructor: dict[str, str], bank_id: str
) -> None:
    body = {"bank_id": bank_id, "questions": [_q("stable")]}
    first = client.post("/api/v1/questions/import", json=body, headers=instructor)
    assert first.json()["created"] == 1
    second = client.post("/api/v1/questions/import", json=body, headers=instructor)
    assert second.json()["updated"] == 1
    assert second.json()["created"] == 0


def test_import_export_round_trip(
    client: TestClient, instructor: dict[str, str], bank_id: str, org_admin: dict[str, str]
) -> None:
    """An export format that cannot be re-imported is a backup that cannot be
    restored."""
    client.post(
        "/api/v1/questions/import",
        json={
            "bank_id": bank_id,
            "questions": [
                _q("rt-1"),
                _q("rt-2", kind="numeric", body={"value": 1.5, "tolerance": 0.1}),
                _q(
                    "rt-3",
                    kind="short_answer",
                    body={"accepted": ["yes", "y"], "match": "ci", "trim": True},
                ),
            ],
        },
        headers=instructor,
    )
    exported = client.get(f"/api/v1/banks/{bank_id}/export", headers=org_admin)
    assert exported.status_code == 200, exported.text
    payload = exported.json()

    target = client.post(
        "/api/v1/banks", json={"name": f"Copy {uuid.uuid4().hex[:8]}"}, headers=instructor
    ).json()["id"]

    reimported = client.post(
        "/api/v1/questions/import",
        json={**payload, "bank_id": target},
        headers=instructor,
    )
    assert reimported.status_code == 200, reimported.text
    assert reimported.json()["created"] == 3

    copy = client.get(f"/api/v1/banks/{target}/export", headers=org_admin).json()
    assert copy["questions"] == payload["questions"]


# ------------------------------------------------------------------ publishing


def _exam_with(client: TestClient, headers: dict[str, str], **version_body) -> tuple[str, str]:
    exam_id = client.post(
        "/api/v1/exams", json={"title": f"Exam {uuid.uuid4().hex[:6]}"}, headers=headers
    ).json()["id"]
    version = client.post(f"/api/v1/exams/{exam_id}/versions", json=version_body, headers=headers)
    return exam_id, version.json().get("version_id", "")


def test_a_pool_smaller_than_its_select_count_is_refused_at_authoring_time(
    client: TestClient, instructor: dict[str, str], bank_id: str
) -> None:
    """Otherwise a candidate is dealt a paper with fewer questions than the
    exam claims, and nobody finds out until the results look strange.

    Caught by the request schema, before a draft even exists. `publish` checks
    it again against the stored rows, which is the layer that still holds if a
    pool is ever populated by anything other than this endpoint.
    """
    ids = client.post(
        "/api/v1/questions/import",
        json={"bank_id": bank_id, "questions": [_q("p-1"), _q("p-2")]},
        headers=instructor,
    ).json()["version_ids"]

    exam_id = client.post("/api/v1/exams", json={"title": "Too small"}, headers=instructor).json()[
        "id"
    ]
    resp = client.post(
        f"/api/v1/exams/{exam_id}/versions",
        json={
            "duration_seconds": 600,
            "sections": [
                {
                    "ordinal": 1,
                    "title": "S",
                    "pools": [{"ordinal": 1, "select_count": 5, "question_version_ids": ids}],
                }
            ],
        },
        headers=instructor,
    )
    assert resp.status_code == 422
    assert "exceeds the pool size" in str(resp.json()["errors"])


def test_publish_rejects_draft_question_versions(
    client: TestClient, instructor: dict[str, str], bank_id: str
) -> None:
    """A draft question can still change. It must not appear on a live paper."""
    ids = client.post(
        "/api/v1/questions/import",
        json={"bank_id": bank_id, "publish": False, "questions": [_q("draft-1")]},
        headers=instructor,
    ).json()["version_ids"]

    _, version_id = _exam_with(
        client,
        instructor,
        duration_seconds=600,
        sections=[
            {
                "ordinal": 1,
                "title": "S",
                "pools": [{"ordinal": 1, "select_count": 1, "question_version_ids": ids}],
            }
        ],
    )
    resp = client.post(f"/api/v1/exam-versions/{version_id}/publish", headers=instructor)
    assert resp.status_code == 422
    assert "not published" in str(resp.json()["errors"])


def test_publish_rejects_unequal_marks_within_a_pool(
    client: TestClient, instructor: dict[str, str], bank_id: str
) -> None:
    """The paper you are dealt must not decide your ceiling."""
    ids = client.post(
        "/api/v1/questions/import",
        json={
            "bank_id": bank_id,
            "questions": [_q("m-1", marks=2), _q("m-2", marks=7)],
        },
        headers=instructor,
    ).json()["version_ids"]

    _, version_id = _exam_with(
        client,
        instructor,
        duration_seconds=600,
        sections=[
            {
                "ordinal": 1,
                "title": "S",
                "pools": [{"ordinal": 1, "select_count": 1, "question_version_ids": ids}],
            }
        ],
    )
    resp = client.post(f"/api/v1/exam-versions/{version_id}/publish", headers=instructor)
    assert resp.status_code == 422
    assert "equal marks" in str(resp.json()["errors"])


def test_publish_rejects_coding_questions_when_the_judge_is_off(
    client: TestClient, instructor: dict[str, str], bank_id: str
) -> None:
    """Candidates would be unable to run or score them. Phase 3 turns this on."""
    ids = client.post(
        "/api/v1/questions/import",
        json={
            "bank_id": bank_id,
            "questions": [
                _q(
                    "code-1",
                    kind="coding",
                    body={"languages": ["python311"]},
                    test_cases=[{"ordinal": 1, "expected_inline": "1"}],
                )
            ],
        },
        headers=instructor,
    ).json()["version_ids"]

    _, version_id = _exam_with(
        client,
        instructor,
        duration_seconds=600,
        judge_enabled=False,
        sections=[
            {
                "ordinal": 1,
                "title": "S",
                "pools": [{"ordinal": 1, "select_count": 1, "question_version_ids": ids}],
            }
        ],
    )
    resp = client.post(f"/api/v1/exam-versions/{version_id}/publish", headers=instructor)
    assert resp.status_code == 422
    assert "judge_enabled" in str(resp.json()["errors"])


def test_unimplemented_navigation_modes_are_refused_at_authoring_time(
    client: TestClient, instructor: dict[str, str], bank_id: str
) -> None:
    """The schema allows 'sequential'; the delivery layer does not enforce it.

    Accepting it would misrepresent the exam to candidates, so it is refused
    where the author can still see the message.
    """
    exam_id = client.post("/api/v1/exams", json={"title": "Nav"}, headers=instructor).json()["id"]
    resp = client.post(
        f"/api/v1/exams/{exam_id}/versions",
        json={
            "duration_seconds": 600,
            "navigation": "sequential",
            "sections": [
                {
                    "ordinal": 1,
                    "title": "S",
                    "pools": [
                        {
                            "ordinal": 1,
                            "select_count": 1,
                            "question_version_ids": [str(uuid.uuid4())],
                        }
                    ],
                }
            ],
        },
        headers=instructor,
    )
    assert resp.status_code == 422
    assert "not implemented" in resp.json()["detail"].lower()


def test_cannot_assign_to_an_unpublished_version(
    client: TestClient, instructor: dict[str, str], bank_id: str
) -> None:
    ids = client.post(
        "/api/v1/questions/import",
        json={"bank_id": bank_id, "questions": [_q("a-1")]},
        headers=instructor,
    ).json()["version_ids"]
    _, version_id = _exam_with(
        client,
        instructor,
        duration_seconds=600,
        sections=[
            {
                "ordinal": 1,
                "title": "S",
                "pools": [{"ordinal": 1, "select_count": 1, "question_version_ids": ids}],
            }
        ],
    )
    resp = client.post(
        f"/api/v1/exam-versions/{version_id}/assignments",
        json={"emails": ["nobody@example.org"]},
        headers=instructor,
    )
    assert resp.status_code == 409


def test_publishing_twice_is_a_conflict(
    client: TestClient, instructor: dict[str, str], bank_id: str
) -> None:
    ids = client.post(
        "/api/v1/questions/import",
        json={"bank_id": bank_id, "questions": [_q("pp-1")]},
        headers=instructor,
    ).json()["version_ids"]
    _, version_id = _exam_with(
        client,
        instructor,
        duration_seconds=600,
        sections=[
            {
                "ordinal": 1,
                "title": "S",
                "pools": [{"ordinal": 1, "select_count": 1, "question_version_ids": ids}],
            }
        ],
    )
    assert (
        client.post(f"/api/v1/exam-versions/{version_id}/publish", headers=instructor).status_code
        == 200
    )
    assert (
        client.post(f"/api/v1/exam-versions/{version_id}/publish", headers=instructor).status_code
        == 409
    )


# ----------------------------------------------------------------- assignment


def test_assigning_a_non_candidate_is_reported_not_silently_done(
    client: TestClient, instructor: dict[str, str], bank_id: str, seed_domain: str
) -> None:
    """Assigning an instructor to sit their own exam is almost always a typo."""
    ids = client.post(
        "/api/v1/questions/import",
        json={"bank_id": bank_id, "questions": [_q("as-1")]},
        headers=instructor,
    ).json()["version_ids"]
    _, version_id = _exam_with(
        client,
        instructor,
        duration_seconds=600,
        sections=[
            {
                "ordinal": 1,
                "title": "S",
                "pools": [{"ordinal": 1, "select_count": 1, "question_version_ids": ids}],
            }
        ],
    )
    client.post(f"/api/v1/exam-versions/{version_id}/publish", headers=instructor)

    resp = client.post(
        f"/api/v1/exam-versions/{version_id}/assignments",
        json={
            "emails": [
                f"instructor@{seed_domain}",
                f"aisha.rahman@{seed_domain}",
                "ghost@nowhere.example",
            ]
        },
        headers=instructor,
    )
    assert resp.status_code == 200
    statuses = {r["identifier"]: r["status"] for r in resp.json()["results"]}
    assert statuses[f"instructor@{seed_domain}"] == "not_a_candidate"
    assert statuses["ghost@nowhere.example"] == "unknown"
    assert statuses[f"aisha.rahman@{seed_domain}"] == "assigned"
    assert resp.json()["assigned"] == 1


def test_assigning_twice_is_idempotent(
    client: TestClient, instructor: dict[str, str], bank_id: str, seed_domain: str
) -> None:
    ids = client.post(
        "/api/v1/questions/import",
        json={"bank_id": bank_id, "questions": [_q("as-2")]},
        headers=instructor,
    ).json()["version_ids"]
    _, version_id = _exam_with(
        client,
        instructor,
        duration_seconds=600,
        sections=[
            {
                "ordinal": 1,
                "title": "S",
                "pools": [{"ordinal": 1, "select_count": 1, "question_version_ids": ids}],
            }
        ],
    )
    client.post(f"/api/v1/exam-versions/{version_id}/publish", headers=instructor)
    body = {"emails": [f"mei.tanaka@{seed_domain}"]}
    first = client.post(
        f"/api/v1/exam-versions/{version_id}/assignments", json=body, headers=instructor
    )
    second = client.post(
        f"/api/v1/exam-versions/{version_id}/assignments", json=body, headers=instructor
    )
    assert first.json()["assigned"] == 1
    assert second.json()["assigned"] == 0
    assert second.json()["results"][0]["status"] == "already_assigned"
