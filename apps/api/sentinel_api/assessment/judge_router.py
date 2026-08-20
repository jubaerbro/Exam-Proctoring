"""Candidate-facing endpoints for running code.

Two verbs, deliberately distinct:

* **run** executes only the sample tests the candidate already has. It is
  debugging, it is fast, it shows everything, and it does not score.
* **submit** executes the hidden suite as well and is the only thing that
  produces a mark.

Keeping them separate matters for cost and for fairness. If every keystroke-
driven "run" executed the hidden suite, a candidate could binary-search the
hidden tests from the pass/fail pattern and reconstruct the answer key.

Nothing here executes anything. The API enqueues; the worker — a separate
process, the only one that can reach a container runtime — executes. An API that
could start a container would be an API whose compromise is root on the judge
host.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sentinel_api.assessment import delivery
from sentinel_api.core.db import elevated
from sentinel_api.core.errors import Conflict, NotFound, RateLimited, ValidationFailed
from sentinel_api.core.runtime import get_runtime
from sentinel_api.models import (
    CodeSubmission,
    ExamSession,
    JudgeRun,
    JudgeTestResult,
    Question,
    QuestionVersion,
    SessionPaperItem,
)
from sentinel_api.tenancy.deps import Principal, authorize, db

router = APIRouter(prefix="/api/v1", tags=["judge"])

#: Matches `submission_size_cap` in the schema. Checked here too so the caller
#: gets a 422 that says what is wrong rather than a constraint violation.
MAX_SOURCE_BYTES = 262_144

#: A candidate pressing "run" repeatedly is normal; a script doing it is a
#: denial of service against every other candidate's judge capacity.
RUNS_PER_MINUTE = 12


class RunRequest(BaseModel):
    language: str
    source: str = Field(min_length=1)
    #: `sample` debugs, `final` scores. Named rather than boolean so a client
    #: cannot flip it by accident.
    mode: str = Field(default="sample", pattern="^(sample|final)$")


class RunAccepted(BaseModel):
    run_id: uuid.UUID
    submission_id: uuid.UUID
    status: str
    mode: str
    queue_depth: int


class TestResultView(BaseModel):
    ordinal: int
    passed: bool
    status: str
    duration_ms: int | None
    #: Sample tests only; `null` for hidden ones, always.
    stdout: str | None = None
    stderr: str | None = None


class RunView(BaseModel):
    run_id: uuid.UUID
    status: str
    mode: str
    language: str
    queued_at: dt.datetime
    finished_at: dt.datetime | None
    tests_total: int
    tests_passed: int
    #: Fraction of test weight passed, 0..1. Not marks — the question's weight
    #: is applied at grading time.
    score: float | None
    compile_output: str | None
    error_detail: str | None
    results: list[TestResultView]


def _own_live_session(session: Session, principal: Principal, session_id: uuid.UUID) -> ExamSession:
    exam_session = session.get(ExamSession, session_id)
    if exam_session is None or exam_session.candidate_user_id != principal.user_id:
        raise NotFound("Session not found.")
    return exam_session


def _coding_item(
    session: Session, exam_session: ExamSession, paper_item_id: uuid.UUID
) -> tuple[SessionPaperItem, QuestionVersion, Question]:
    item = session.get(SessionPaperItem, paper_item_id)
    if item is None or item.session_id != exam_session.id:
        raise NotFound("That question is not on this paper.")
    version = session.get(QuestionVersion, item.question_version_id)
    question = session.get(Question, version.question_id) if version else None
    if version is None or question is None:  # pragma: no cover - FK guarantees it
        raise NotFound("That question is not on this paper.")
    if question.kind != "coding":
        raise ValidationFailed("That question does not accept code.")
    return item, version, question


@router.post(
    "/sessions/{session_id}/items/{paper_item_id}/run",
    response_model=RunAccepted,
    status_code=202,
)
def run_code(
    session_id: uuid.UUID,
    paper_item_id: uuid.UUID,
    body: RunRequest,
    principal: Principal = Depends(authorize),
    session: Session = Depends(db),
) -> RunAccepted:
    import hashlib

    assert principal.org_id is not None  # noqa: S101 - authorize() guarantees it
    now = delivery.Clock.now()
    exam_session = _own_live_session(session, principal, session_id)
    delivery.require_live(session, exam_session, now)

    item, version, _ = _coding_item(session, exam_session, paper_item_id)

    allowed = version.body.get("languages", [])
    if body.language not in allowed:
        raise ValidationFailed(
            f"This question accepts {', '.join(allowed) or 'no languages'}; "
            f"{body.language!r} is not one of them."
        )

    encoded = body.source.encode("utf-8")
    if len(encoded) > MAX_SOURCE_BYTES:
        raise ValidationFailed(
            f"Your program is {len(encoded)} bytes; the limit is {MAX_SOURCE_BYTES}."
        )

    recent = session.scalar(
        select(func.count())
        .select_from(CodeSubmission)
        .where(
            CodeSubmission.session_id == exam_session.id,
            CodeSubmission.created_at > now - dt.timedelta(minutes=1),
        )
    )
    if (recent or 0) >= RUNS_PER_MINUTE:
        # 429, not a silent queue. A candidate who is being throttled needs to
        # know it is happening rather than assume the judge is broken.
        raise RateLimited(
            f"You can run code {RUNS_PER_MINUTE} times a minute. Wait a moment and try again."
        )

    submission = CodeSubmission(
        org_id=principal.org_id,
        session_id=exam_session.id,
        paper_item_id=item.id,
        language=body.language,
        source=body.source,
        source_sha256=hashlib.sha256(encoded).digest(),
        source_bytes=len(encoded),
        mode=body.mode,
    )
    session.add(submission)
    session.flush()

    # The run row is created here, not by the worker, so the client has an id to
    # poll from the moment the request returns.
    #
    # It needs `elevated()` because migration 0003 makes `judge_run` unwritable
    # by the candidate role — deliberately: a role that can write its own
    # verdict has no verdict. Creating a `queued` row carrying no result is not
    # writing a verdict, and doing it in the same transaction as the submission
    # means there is never a submission with no run to poll. Every column that
    # *is* a verdict — status, score, tests_passed — is written only by the
    # worker, in its own `system` transaction.
    with elevated(session):
        run = JudgeRun(org_id=principal.org_id, submission_id=submission.id, status="queued")
        session.add(run)
        session.flush()

    session.commit()

    depth = _enqueue(run.id, submission.id, principal.org_id, body.mode)
    return RunAccepted(
        run_id=run.id,
        submission_id=submission.id,
        status="queued",
        mode=body.mode,
        queue_depth=depth,
    )


def _enqueue(run_id: uuid.UUID, submission_id: uuid.UUID, org_id: uuid.UUID, mode: str) -> int:
    """Publish the job. Enqueued *after* the commit, on purpose.

    Enqueuing inside the transaction risks a worker claiming a run that has not
    been committed yet and finding nothing. The opposite ordering can only lose
    a job — recoverable, because a `queued` run with no job is visible and can
    be requeued — rather than corrupt one.
    """
    from sentinel_judge.queue import JudgeJob, JudgeQueue

    rt = get_runtime()
    if rt.redis is None:  # pragma: no cover - only in a degraded deployment
        raise Conflict(
            "The judge queue is unavailable, so this program cannot be run right now. "
            "Your code is saved."
        )
    queue = JudgeQueue(rt.redis)
    import time as _time

    queue.enqueue(
        JudgeJob(
            run_id=run_id,
            submission_id=submission_id,
            org_id=org_id,
            mode=mode,
            enqueued_at=_time.time(),
        )
    )
    return int(queue.depth())


@router.get("/sessions/{session_id}/runs/{run_id}", response_model=RunView)
def get_run(
    session_id: uuid.UUID,
    run_id: uuid.UUID,
    principal: Principal = Depends(authorize),
    session: Session = Depends(db),
) -> RunView:
    """Poll a run.

    Polling rather than a WebSocket: Phase 4 brings the session channel, and
    until then a poll is a smaller thing to get wrong. Judge runs finish in
    seconds, so the cost is a handful of requests.
    """
    exam_session = _own_live_session(session, principal, session_id)

    run = session.get(JudgeRun, run_id)
    if run is None:
        raise NotFound("Run not found.")
    submission = session.get(CodeSubmission, run.submission_id)
    if submission is None or submission.session_id != exam_session.id:
        # RLS already hid another candidate's run; this is the second layer.
        raise NotFound("Run not found.")

    results = session.scalars(
        select(JudgeTestResult)
        .where(JudgeTestResult.run_id == run.id)
        .order_by(JudgeTestResult.ordinal)
    ).all()

    return RunView(
        run_id=run.id,
        status=run.status,
        mode=submission.mode,
        language=submission.language,
        queued_at=run.queued_at,
        finished_at=run.finished_at,
        tests_total=run.tests_total,
        tests_passed=run.tests_passed,
        score=float(run.score) if run.score is not None else None,
        compile_output=run.compile_output,
        error_detail=run.error_detail,
        results=[
            TestResultView(
                ordinal=r.ordinal,
                passed=r.passed,
                status=r.status,
                duration_ms=r.duration_ms,
                stdout=r.stdout_excerpt,
                stderr=r.stderr_excerpt,
            )
            for r in results
        ],
    )


class LatestRuns(BaseModel):
    runs: list[dict[str, Any]]


@router.get("/sessions/{session_id}/items/{paper_item_id}/runs", response_model=LatestRuns)
def list_runs(
    session_id: uuid.UUID,
    paper_item_id: uuid.UUID,
    principal: Principal = Depends(authorize),
    session: Session = Depends(db),
) -> LatestRuns:
    """Every run this candidate made against one question, newest first.

    Exists so a reload does not lose the candidate's place: after a crash the
    exam runner can show the last result rather than an empty panel.
    """
    exam_session = _own_live_session(session, principal, session_id)
    rows = session.execute(
        select(JudgeRun, CodeSubmission)
        .join(CodeSubmission, CodeSubmission.id == JudgeRun.submission_id)
        .where(
            CodeSubmission.session_id == exam_session.id,
            CodeSubmission.paper_item_id == paper_item_id,
        )
        .order_by(JudgeRun.queued_at.desc())
        .limit(20)
    ).all()
    return LatestRuns(
        runs=[
            {
                "run_id": str(run.id),
                "status": run.status,
                "mode": submission.mode,
                "language": submission.language,
                "queued_at": run.queued_at.isoformat(),
                "tests_total": run.tests_total,
                "tests_passed": run.tests_passed,
                "score": float(run.score) if run.score is not None else None,
            }
            for run, submission in rows
        ]
    )
