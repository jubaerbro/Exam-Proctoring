"""The judge loop, end to end, through the real API and a real container.

Candidate presses run → the API enqueues → the worker claims, judges in a
sandbox, and writes the verdict → the mark appears on the paper.

The worker is driven directly (`worker.process(job)`) rather than as a daemon.
That keeps the test deterministic without faking anything that matters: it is
the real worker, the real queue payload, the real sandbox, and a real container.

Skips — loudly — when there is no Docker daemon or no judge image. A judge test
that silently passes without executing anything would be worse than absent.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.conftest import assign_exam, bearer

JUDGE_IMAGE = os.getenv("JUDGE_TEST_IMAGE", "sentinel-judge-python311:local")

ADDER = "a, b = input().split()\nprint(int(a) + int(b))\n"
WRONG = "input()\nprint(-1)\n"


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    if subprocess.run(["docker", "info"], capture_output=True, check=False).returncode != 0:
        return False
    return (
        subprocess.run(
            ["docker", "image", "inspect", JUDGE_IMAGE], capture_output=True, check=False
        ).returncode
        == 0
    )


pytestmark = pytest.mark.skipif(
    not _docker_ready(),
    reason=f"needs a Docker daemon and the {JUDGE_IMAGE} image; "
    "run `docker compose build judge-python311` first",
)


# --------------------------------------------------------------------- setup


@pytest.fixture
def coding_exam(client: TestClient, instructor_login: dict) -> dict:
    """An exam whose only question is a coding question with 2 sample + 2 hidden tests."""
    staff = bearer(instructor_login)
    tag = uuid.uuid4().hex[:8]

    bank_id = client.post(
        "/api/v1/banks", json={"name": f"Judge bank {tag}"}, headers=staff
    ).json()["id"]

    imported = client.post(
        "/api/v1/questions/import",
        json={
            "bank_id": bank_id,
            "publish": True,
            "questions": [
                {
                    "external_key": f"{tag}-code",
                    "kind": "coding",
                    "prompt": "Read two integers and print their sum.",
                    "marks": 10,
                    "body": {
                        "languages": ["python311"],
                        "starter": {"python311": "# your code here\n"},
                        "time_limit_ms": 4000,
                        "memory_limit_mb": 128,
                    },
                    "test_cases": [
                        {
                            "ordinal": 1,
                            "is_sample": True,
                            "weight": 1,
                            "stdin_inline": "2 3\n",
                            "expected_inline": "5\n",
                        },
                        {
                            "ordinal": 2,
                            "is_sample": True,
                            "weight": 1,
                            "stdin_inline": "10 5\n",
                            "expected_inline": "15\n",
                        },
                        {
                            "ordinal": 3,
                            "is_sample": False,
                            "weight": 3,
                            "stdin_inline": "100 200\n",
                            "expected_inline": "300\n",
                        },
                        {
                            "ordinal": 4,
                            "is_sample": False,
                            "weight": 5,
                            "stdin_inline": "-7 7\n",
                            "expected_inline": "0\n",
                        },
                    ],
                }
            ],
        },
        headers=staff,
    )
    assert imported.status_code == 200, imported.text
    version_ids = imported.json()["version_ids"]

    exam_id = client.post(
        "/api/v1/exams", json={"title": f"Judge exam {tag}"}, headers=staff
    ).json()["id"]

    created = client.post(
        f"/api/v1/exams/{exam_id}/versions",
        json={
            "duration_seconds": 3600,
            # Without this the publish check refuses a coding question, which is
            # the Phase 2 guard doing its job.
            "judge_enabled": True,
            "show_score_on_submit": True,
            "sections": [
                {
                    "ordinal": 1,
                    "title": "Coding",
                    "pools": [
                        {"ordinal": 1, "select_count": 1, "question_version_ids": version_ids}
                    ],
                }
            ],
        },
        headers=staff,
    )
    assert created.status_code == 201, created.text
    version_id = created.json()["version_id"]

    published = client.post(f"/api/v1/exam-versions/{version_id}/publish", headers=staff)
    assert published.status_code == 200, published.text
    return {"version_id": version_id, "staff": staff}


@pytest.fixture
def live_session(
    client: TestClient, coding_exam: dict, candidate_login: dict, seed_domain: str
) -> dict:
    assignment = assign_exam(
        client, coding_exam["staff"], coding_exam["version_id"], f"aisha.rahman@{seed_domain}"
    )
    headers = bearer(candidate_login)
    started = client.post("/api/v1/sessions", json={"assignment_id": assignment}, headers=headers)
    assert started.status_code == 201, started.text
    state = started.json()
    item = next(i for i in state["items"] if i["kind"] == "coding")
    return {
        "session_id": state["session_id"],
        "paper_item_id": item["paper_item_id"],
        "headers": headers,
        "body": item["body"],
    }


@pytest.fixture
def worker(admin_engine: Engine):
    """The real worker, wired to the real queue, driven by hand."""
    import redis as redis_lib
    from sentinel_judge.queue import JudgeQueue
    from sentinel_judge.worker import JudgeWorker, WorkerConfig

    from sentinel_api.core.db import server_session

    queue = JudgeQueue(redis_lib.from_url(os.environ["REDIS_URL"], decode_responses=True))
    config = WorkerConfig(image_for={"python311": JUDGE_IMAGE})
    return JudgeWorker(queue, server_session, config), queue


def _drain(worker_and_queue, limit: int = 5) -> int:
    worker, queue = worker_and_queue
    handled = 0
    for _ in range(limit):
        job = queue.claim("test-worker", timeout=2)
        if job is None:
            break
        worker.process(job)
        queue.acknowledge("test-worker", job)
        handled += 1
    return handled


# ------------------------------------------------------------------- tests


def test_the_candidate_never_receives_the_starter_with_the_answer_key(
    live_session: dict,
) -> None:
    """The coding body a candidate is served carries languages and starter code
    and nothing else — no test cases, no expected output."""
    body = live_session["body"]
    assert body["languages"] == ["python311"]
    assert "starter" in body
    for forbidden in ("expected_inline", "stdin_inline", "test_cases"):
        assert forbidden not in body


def test_a_sample_run_executes_only_the_visible_tests(
    client: TestClient, live_session: dict, worker
) -> None:
    """Pressing "Run" must not execute the hidden suite.

    If it did, a candidate could read the hidden tests' pass/fail pattern and
    reconstruct the answer key one submission at a time.
    """
    accepted = client.post(
        f"/api/v1/sessions/{live_session['session_id']}/items/{live_session['paper_item_id']}/run",
        json={"language": "python311", "source": ADDER, "mode": "sample"},
        headers=live_session["headers"],
    )
    assert accepted.status_code == 202, accepted.text
    run_id = accepted.json()["run_id"]

    assert _drain(worker) == 1

    view = client.get(
        f"/api/v1/sessions/{live_session['session_id']}/runs/{run_id}",
        headers=live_session["headers"],
    )
    assert view.status_code == 200, view.text
    payload = view.json()
    assert payload["status"] == "completed"
    assert payload["tests_total"] == 2, "a sample run must not touch the hidden tests"
    assert payload["tests_passed"] == 2
    assert all(r["ordinal"] in (1, 2) for r in payload["results"])


def test_a_final_run_scores_the_question_with_weighted_partial_credit(
    client: TestClient, live_session: dict, worker, admin_engine: Engine
) -> None:
    """The Phase 3 product claim, end to end.

    The solution is right for positive inputs and wrong for the negative one, so
    it passes weights 1 + 1 + 3 of 10 — 50%. The question is worth 10 marks, so
    the mark is 5.0. Counting tests instead of weight would say 75%.
    """
    source = "a, b = input().split()\nprint(int(a) + int(b) if int(a) > 0 else 999)\n"
    accepted = client.post(
        f"/api/v1/sessions/{live_session['session_id']}/items/{live_session['paper_item_id']}/run",
        json={"language": "python311", "source": source, "mode": "final"},
        headers=live_session["headers"],
    )
    assert accepted.status_code == 202, accepted.text
    run_id = accepted.json()["run_id"]
    assert _drain(worker) == 1

    payload = client.get(
        f"/api/v1/sessions/{live_session['session_id']}/runs/{run_id}",
        headers=live_session["headers"],
    ).json()
    assert payload["tests_total"] == 4
    assert payload["tests_passed"] == 3
    assert payload["score"] == pytest.approx(0.5, abs=0.001)

    with admin_engine.connect() as conn:
        conn.execute(text("SELECT set_config('sentinel.role', 'system', true)"))
        org_id = conn.execute(
            text("SELECT id FROM organization WHERE slug = 'demo-university'")
        ).scalar_one()
        conn.execute(text("SELECT set_config('sentinel.org_id', :o, true)"), {"o": str(org_id)})
        row = conn.execute(
            text(
                "SELECT awarded, max_marks, grader FROM question_score "
                "WHERE session_id = :s AND paper_item_id = :p"
            ),
            {"s": live_session["session_id"], "p": live_session["paper_item_id"]},
        ).first()
    assert row is not None, "the judge did not write a mark"
    assert float(row[0]) == pytest.approx(5.0, abs=0.001)
    assert float(row[1]) == 10.0
    assert row[2] == "judge"


def test_hidden_test_output_is_never_persisted(
    client: TestClient, live_session: dict, worker, admin_engine: Engine
) -> None:
    """Not merely hidden from the response — never written to the database.

    A column that holds the answer key is one reporting query away from leaking
    it, so the safest place for it is nowhere.
    """
    accepted = client.post(
        f"/api/v1/sessions/{live_session['session_id']}/items/{live_session['paper_item_id']}/run",
        json={"language": "python311", "source": WRONG, "mode": "final"},
        headers=live_session["headers"],
    )
    run_id = accepted.json()["run_id"]
    _drain(worker)

    with admin_engine.connect() as conn:
        conn.execute(text("SELECT set_config('sentinel.role', 'system', true)"))
        org_id = conn.execute(
            text("SELECT id FROM organization WHERE slug = 'demo-university'")
        ).scalar_one()
        conn.execute(text("SELECT set_config('sentinel.org_id', :o, true)"), {"o": str(org_id)})
        rows = conn.execute(
            text(
                "SELECT r.ordinal, r.stdout_excerpt FROM judge_test_result r "
                "WHERE r.run_id = :r ORDER BY r.ordinal"
            ),
            {"r": run_id},
        ).all()

    assert rows, "no results were written"
    for ordinal, stdout in rows:
        if ordinal in (3, 4):
            assert stdout is None, f"hidden test {ordinal} persisted its output"


def test_a_syntax_error_reaches_the_candidate_as_a_compile_error(
    client: TestClient, live_session: dict, worker
) -> None:
    accepted = client.post(
        f"/api/v1/sessions/{live_session['session_id']}/items/{live_session['paper_item_id']}/run",
        json={"language": "python311", "source": "def broken(:\n    pass\n", "mode": "sample"},
        headers=live_session["headers"],
    )
    run_id = accepted.json()["run_id"]
    _drain(worker)

    payload = client.get(
        f"/api/v1/sessions/{live_session['session_id']}/runs/{run_id}",
        headers=live_session["headers"],
    ).json()
    assert payload["status"] == "compile_error"
    assert "SyntaxError" in (payload["compile_output"] or "")


def test_a_language_the_question_does_not_accept_is_refused(
    client: TestClient, live_session: dict
) -> None:
    resp = client.post(
        f"/api/v1/sessions/{live_session['session_id']}/items/{live_session['paper_item_id']}/run",
        json={"language": "java17", "source": "class Main {}", "mode": "sample"},
        headers=live_session["headers"],
    )
    assert resp.status_code == 422
    assert "python311" in resp.json()["detail"]


def test_source_larger_than_the_cap_is_refused_with_a_readable_message(
    client: TestClient, live_session: dict
) -> None:
    resp = client.post(
        f"/api/v1/sessions/{live_session['session_id']}/items/{live_session['paper_item_id']}/run",
        json={"language": "python311", "source": "#" * 300_000, "mode": "sample"},
        headers=live_session["headers"],
    )
    assert resp.status_code == 422
    assert "limit is 262144" in resp.json()["detail"]


def test_running_code_after_the_session_closes_is_refused(
    client: TestClient, live_session: dict
) -> None:
    client.post(
        f"/api/v1/sessions/{live_session['session_id']}/submit",
        json={},
        headers=live_session["headers"],
    )
    resp = client.post(
        f"/api/v1/sessions/{live_session['session_id']}/items/{live_session['paper_item_id']}/run",
        json={"language": "python311", "source": ADDER, "mode": "sample"},
        headers=live_session["headers"],
    )
    assert resp.status_code == 409


def test_a_run_is_judged_once_even_if_the_job_is_delivered_twice(
    client: TestClient, live_session: dict, worker, admin_engine: Engine
) -> None:
    """Crash recovery re-delivers jobs on purpose.

    Judging twice would write a second set of results and a second score, so the
    worker claims a run with a conditional UPDATE and skips one it does not win.
    """
    from sentinel_judge.queue import JudgeJob

    worker_obj, queue = worker
    accepted = client.post(
        f"/api/v1/sessions/{live_session['session_id']}/items/{live_session['paper_item_id']}/run",
        json={"language": "python311", "source": ADDER, "mode": "final"},
        headers=live_session["headers"],
    )
    run_id = accepted.json()["run_id"]
    submission_id = accepted.json()["submission_id"]

    job = queue.claim("test-worker", timeout=2)
    assert job is not None
    worker_obj.process(job)
    queue.acknowledge("test-worker", job)

    duplicate = JudgeJob(
        run_id=uuid.UUID(run_id),
        submission_id=uuid.UUID(submission_id),
        org_id=job.org_id,
        mode="final",
        enqueued_at=time.time(),
    )
    worker_obj.process(duplicate)  # must be a no-op

    with admin_engine.connect() as conn:
        conn.execute(text("SELECT set_config('sentinel.role', 'system', true)"))
        org_id = conn.execute(
            text("SELECT id FROM organization WHERE slug = 'demo-university'")
        ).scalar_one()
        conn.execute(text("SELECT set_config('sentinel.org_id', :o, true)"), {"o": str(org_id)})
        count = conn.execute(
            text("SELECT count(*) FROM judge_test_result WHERE run_id = :r"), {"r": run_id}
        ).scalar()
    assert count == 4, f"expected one set of 4 results, found {count}"
