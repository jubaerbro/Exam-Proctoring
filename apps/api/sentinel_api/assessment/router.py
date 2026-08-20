"""Assessment endpoints: question bank, exam authoring, and candidate delivery.

Every route here is declared in `tenancy/policies.py`; `test_authz_matrix.py`
fails the build if one is not.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sentinel_api.assessment import authoring, bank, delivery
from sentinel_api.assessment.schemas import (
    AssignItemResult,
    AssignRequest,
    AssignResponse,
    BankCreate,
    BankResponse,
    BodyError,
    ExamCreate,
    ExamVersionDraft,
    ExamVersionResponse,
    ImportRequest,
    ImportResponse,
    QuestionDraft,
    QuestionVersionResponse,
)
from sentinel_api.core.db import server_session
from sentinel_api.core.errors import Conflict, NotFound, ValidationFailed
from sentinel_api.grading.service import grade_session
from sentinel_api.models import (
    ActivityLog,
    Exam,
    ExamAssignment,
    ExamSection,
    ExamSession,
    ExamVersion,
    Question,
    QuestionScore,
    QuestionVersion,
    SectionPool,
    SectionPoolItem,
    SessionPaperItem,
    SessionResult,
)
from sentinel_api.tenancy.deps import Principal, authorize, db

router = APIRouter(prefix="/api/v1", tags=["assessment"])


# --------------------------------------------------------------------- helpers


def _audit(
    session: Session,
    principal: Principal,
    action: str,
    *,
    object_type: str,
    object_id: uuid.UUID,
    detail: dict[str, Any] | None = None,
) -> None:
    """Staff actions go in `activity_log`.

    This is the operational log, not the tamper-evident chain — `audit_event` and
    its Ed25519 signatures are Phase 5. Calling this "the audit trail" would
    overstate what it is: these rows are append-only by trigger, but nothing
    signs them.
    """
    session.add(
        ActivityLog(
            org_id=principal.org_id,
            actor_user_id=principal.user_id,
            actor_role=principal.primary_role,
            action=action,
            object_type=object_type,
            object_id=object_id,
            detail=detail or {},
        )
    )


def _version_response(session: Session, version: ExamVersion) -> ExamVersionResponse:
    exam = session.get(Exam, version.exam_id)
    sections = session.scalars(
        select(ExamSection).where(ExamSection.exam_version_id == version.id)
    ).all()
    pools = (
        session.scalars(
            select(SectionPool).where(SectionPool.section_id.in_([s.id for s in sections]))
        ).all()
        if sections
        else []
    )
    question_count = sum(p.select_count for p in pools)

    total_marks = 0.0
    for pool in pools:
        marks = session.scalar(
            select(func.max(QuestionVersion.marks))
            .join(SectionPoolItem, SectionPoolItem.question_version_id == QuestionVersion.id)
            .where(SectionPoolItem.pool_id == pool.id)
        )
        total_marks += float(marks or 0) * pool.select_count

    return ExamVersionResponse(
        exam_id=version.exam_id,
        version_id=version.id,
        version=version.version,
        status=version.status,
        title=exam.title if exam else "",
        duration_seconds=version.duration_seconds,
        grace_seconds=version.grace_seconds,
        navigation=version.navigation,
        max_attempts=version.max_attempts,
        opens_at=version.opens_at,
        closes_at=version.closes_at,
        integrity_enabled=version.integrity_enabled,
        judge_enabled=version.judge_enabled,
        section_count=len(sections),
        question_count=question_count,
        total_marks=round(total_marks, 3),
        published_at=version.published_at,
    )


# ===================================================================== banks


@router.post("/banks", response_model=BankResponse, status_code=201)
def create_bank(
    body: BankCreate,
    principal: Principal = Depends(authorize),
    session: Session = Depends(db),
) -> BankResponse:
    assert principal.org_id is not None  # noqa: S101 - authorize() guarantees it
    created = bank.create_bank(
        session,
        org_id=principal.org_id,
        actor_id=principal.user_id,
        name=body.name,
        description=body.description,
    )
    _audit(session, principal, "bank.create", object_type="question_bank", object_id=created.id)
    return BankResponse(
        id=created.id,
        name=created.name,
        description=created.description,
        question_count=0,
        created_at=created.created_at,
    )


@router.get("/banks", response_model=list[BankResponse])
def list_banks(
    principal: Principal = Depends(authorize), session: Session = Depends(db)
) -> list[BankResponse]:
    counts = bank.question_counts(session)
    from sentinel_api.models import QuestionBank

    rows = session.scalars(select(QuestionBank).order_by(QuestionBank.name)).all()
    return [
        BankResponse(
            id=b.id,
            name=b.name,
            description=b.description,
            question_count=counts.get(b.id, 0),
            created_at=b.created_at,
        )
        for b in rows
    ]


@router.post("/banks/{bank_id}/questions", response_model=QuestionVersionResponse, status_code=201)
def create_question(
    bank_id: uuid.UUID,
    body: QuestionDraft,
    principal: Principal = Depends(authorize),
    session: Session = Depends(db),
) -> QuestionVersionResponse:
    assert principal.org_id is not None  # noqa: S101
    bank.get_bank(session, bank_id)

    existing = (
        bank.find_by_external_key(session, bank_id=bank_id, external_key=body.external_key)
        if body.external_key
        else None
    )
    try:
        version = bank.add_version(
            session,
            org_id=principal.org_id,
            actor_id=principal.user_id,
            bank_id=bank_id,
            draft=body,
            question=existing,
        )
    except BodyError as exc:
        raise ValidationFailed(str(exc), errors=exc.errors) from exc

    _audit(
        session,
        principal,
        "question.version_create",
        object_type="question_version",
        object_id=version.id,
        detail={"version": version.version, "kind": body.kind},
    )
    return _question_response(session, version)


def _question_response(session: Session, version: QuestionVersion) -> QuestionVersionResponse:
    question = session.get(Question, version.question_id)
    assert question is not None  # noqa: S101
    return QuestionVersionResponse(
        question_id=question.id,
        version_id=version.id,
        version=version.version,
        status=version.status,
        kind=question.kind,
        prompt=version.prompt,
        body=version.body,
        marks=float(version.marks),
        negative_marks=float(version.negative_marks),
        partial_credit=version.partial_credit,
        difficulty=version.difficulty,
        tags=list(version.tags),
        external_key=question.external_key,
        created_at=version.created_at,
    )


@router.get("/banks/{bank_id}/questions", response_model=list[QuestionVersionResponse])
def list_questions(
    bank_id: uuid.UUID,
    principal: Principal = Depends(authorize),
    session: Session = Depends(db),
) -> list[QuestionVersionResponse]:
    bank.get_bank(session, bank_id)
    rows = session.execute(
        select(QuestionVersion)
        .join(Question, Question.current_version_id == QuestionVersion.id)
        .where(Question.bank_id == bank_id)
        .order_by(QuestionVersion.created_at)
    ).scalars()
    return [_question_response(session, v) for v in rows]


@router.post("/questions/import", response_model=ImportResponse)
def import_questions(
    body: ImportRequest,
    principal: Principal = Depends(authorize),
    session: Session = Depends(db),
) -> ImportResponse:
    assert principal.org_id is not None  # noqa: S101
    result = bank.import_questions(
        session, org_id=principal.org_id, actor_id=principal.user_id, request=body
    )
    _audit(
        session,
        principal,
        "question.import",
        object_type="question_bank",
        object_id=body.bank_id,
        detail={"created": result.created, "updated": result.updated},
    )
    return result


@router.get("/banks/{bank_id}/export")
def export_bank(
    bank_id: uuid.UUID,
    principal: Principal = Depends(authorize),
    session: Session = Depends(db),
) -> dict[str, Any]:
    return bank.export_bank(session, bank_id=bank_id)


# ===================================================================== exams


class ExamResponse(BaseModel):
    id: uuid.UUID
    title: str
    description: str | None
    current_version_id: uuid.UUID | None
    created_at: dt.datetime


@router.post("/exams", response_model=ExamResponse, status_code=201)
def create_exam(
    body: ExamCreate,
    principal: Principal = Depends(authorize),
    session: Session = Depends(db),
) -> ExamResponse:
    assert principal.org_id is not None  # noqa: S101
    exam = authoring.create_exam(
        session,
        org_id=principal.org_id,
        actor_id=principal.user_id,
        title=body.title,
        description=body.description,
    )
    _audit(session, principal, "exam.create", object_type="exam", object_id=exam.id)
    return ExamResponse(
        id=exam.id,
        title=exam.title,
        description=exam.description,
        current_version_id=exam.current_version_id,
        created_at=exam.created_at,
    )


@router.get("/exams", response_model=list[ExamResponse])
def list_exams(
    principal: Principal = Depends(authorize), session: Session = Depends(db)
) -> list[ExamResponse]:
    rows = session.scalars(select(Exam).order_by(Exam.created_at.desc())).all()
    return [
        ExamResponse(
            id=e.id,
            title=e.title,
            description=e.description,
            current_version_id=e.current_version_id,
            created_at=e.created_at,
        )
        for e in rows
    ]


@router.post("/exams/{exam_id}/versions", response_model=ExamVersionResponse, status_code=201)
def create_exam_version(
    exam_id: uuid.UUID,
    body: ExamVersionDraft,
    principal: Principal = Depends(authorize),
    session: Session = Depends(db),
) -> ExamVersionResponse:
    assert principal.org_id is not None  # noqa: S101
    exam = authoring.get_exam(session, exam_id)
    version = authoring.add_version(session, org_id=principal.org_id, exam=exam, draft=body)
    _audit(
        session,
        principal,
        "exam.version_create",
        object_type="exam_version",
        object_id=version.id,
        detail={"version": version.version},
    )
    return _version_response(session, version)


@router.get("/exams/{exam_id}/versions", response_model=list[ExamVersionResponse])
def list_exam_versions(
    exam_id: uuid.UUID,
    principal: Principal = Depends(authorize),
    session: Session = Depends(db),
) -> list[ExamVersionResponse]:
    authoring.get_exam(session, exam_id)
    rows = session.scalars(
        select(ExamVersion)
        .where(ExamVersion.exam_id == exam_id)
        .order_by(ExamVersion.version.desc())
    ).all()
    return [_version_response(session, v) for v in rows]


@router.post("/exam-versions/{version_id}/publish", response_model=ExamVersionResponse)
def publish_exam_version(
    version_id: uuid.UUID,
    principal: Principal = Depends(authorize),
    session: Session = Depends(db),
) -> ExamVersionResponse:
    version = authoring.get_version(session, version_id)
    authoring.publish_version(session, version=version, actor_id=principal.user_id)
    _audit(
        session,
        principal,
        "exam.publish",
        object_type="exam_version",
        object_id=version.id,
        detail={"version": version.version},
    )
    return _version_response(session, version)


@router.post("/exam-versions/{version_id}/assignments", response_model=AssignResponse)
def assign_candidates(
    version_id: uuid.UUID,
    body: AssignRequest,
    principal: Principal = Depends(authorize),
    session: Session = Depends(db),
) -> AssignResponse:
    assert principal.org_id is not None  # noqa: S101
    version = authoring.get_version(session, version_id)
    if version.status != "published":
        raise Conflict(
            "Assign candidates to a published exam version. A draft's questions can still "
            "change, so an assignment made now would not describe what they sit."
        )

    resolved, rejected = authoring.resolve_candidates(
        session, user_ids=body.candidate_user_ids, emails=body.emails
    )
    already = authoring.existing_assignments(session, exam_version_id=version.id)

    results: list[AssignItemResult] = [
        AssignItemResult(identifier=ident, status=reason)  # type: ignore[arg-type]
        for ident, reason in rejected
    ]
    assigned = 0
    seen: set[uuid.UUID] = set()

    for identifier, user_id in resolved.items():
        if user_id in already:
            results.append(
                AssignItemResult(
                    identifier=identifier,
                    status="already_assigned",
                    assignment_id=already[user_id].id,
                )
            )
            continue
        if user_id in seen:
            continue
        seen.add(user_id)

        assignment = ExamAssignment(
            org_id=principal.org_id,
            exam_version_id=version.id,
            candidate_user_id=user_id,
            opens_at=body.opens_at,
            closes_at=body.closes_at,
            extra_time_seconds=body.extra_time_seconds,
            attempts_allowed=body.attempts_allowed,
            assigned_by=principal.user_id,
        )
        session.add(assignment)
        session.flush()
        assigned += 1
        results.append(
            AssignItemResult(identifier=identifier, status="assigned", assignment_id=assignment.id)
        )

    _audit(
        session,
        principal,
        "assignment.create",
        object_type="exam_version",
        object_id=version.id,
        detail={"assigned": assigned},
    )
    return AssignResponse(assigned=assigned, skipped=len(results) - assigned, results=results)


# ================================================================== delivery


class SessionState(BaseModel):
    session_id: uuid.UUID
    exam_version_id: uuid.UUID
    attempt_no: int
    status: str
    started_at: dt.datetime | None
    deadline_at: dt.datetime | None
    #: Authoritative. The client renders a countdown from this, not from its own clock.
    remaining_seconds: int
    server_time: dt.datetime
    grace_seconds: int
    integrity_enabled: bool
    question_count: int
    items: list[dict[str, Any]]


class StartSessionRequest(BaseModel):
    assignment_id: uuid.UUID


class SaveAnswerRequest(BaseModel):
    value: dict[str, Any]
    #: The revision the client believes is current. Omit on a first save.
    client_revision: int | None = Field(default=None, ge=0)
    time_spent_ms: int = Field(default=0, ge=0)
    flagged: bool = False
    client_saved_at: dt.datetime | None = None


class SaveAnswerResponse(BaseModel):
    paper_item_id: uuid.UUID
    revision: int
    saved_at: dt.datetime
    server_time: dt.datetime
    remaining_seconds: int


class SubmitRequest(BaseModel):
    #: Set by the client's own timer as a courtesy; the server still decides.
    reason: str = "candidate"


class SubmitResponse(BaseModel):
    session_id: uuid.UUID
    status: str
    submitted_at: dt.datetime
    answered: int
    question_count: int
    score_released: bool
    total_marks: float | None = None
    max_marks: float | None = None


def _load_own_session(session: Session, principal: Principal, session_id: uuid.UUID) -> ExamSession:
    exam_session = session.get(ExamSession, session_id)
    if exam_session is None or exam_session.candidate_user_id != principal.user_id:
        # RLS would already have hidden another candidate's session; the
        # explicit check means an authorization bug here costs nothing either.
        raise NotFound("Session not found.")
    return exam_session


def _state(session: Session, exam_session: ExamSession, now: dt.datetime) -> SessionState:
    version = session.get(ExamVersion, exam_session.exam_version_id)
    items = delivery.load_paper(session, exam_session)
    return SessionState(
        session_id=exam_session.id,
        exam_version_id=exam_session.exam_version_id,
        attempt_no=exam_session.attempt_no,
        status=exam_session.status,
        started_at=exam_session.started_at,
        deadline_at=exam_session.deadline_at,
        remaining_seconds=delivery.remaining_seconds(exam_session, now),
        server_time=now,
        grace_seconds=version.grace_seconds if version else 0,
        integrity_enabled=bool(version and version.integrity_enabled),
        question_count=len(items),
        items=items,
    )


@router.post("/sessions", response_model=SessionState, status_code=201)
def start_session(
    body: StartSessionRequest,
    request: Request,
    response: Response,
    principal: Principal = Depends(authorize),
    session: Session = Depends(db),
) -> SessionState:
    """Start, or resume an already-started attempt.

    Deliberately idempotent. A candidate whose browser crashed will POST here
    again, and the correct answer is their existing session — not a second
    attempt, and not an error they have to interpret while a clock runs.
    """
    assert principal.org_id is not None  # noqa: S101
    now = delivery.Clock.now()
    exam_session, created = delivery.start_session(
        session,
        org_id=principal.org_id,
        candidate_id=principal.user_id,
        assignment_id=body.assignment_id,
        user_agent=request.headers.get("user-agent"),
        now=now,
    )
    if not created:
        response.status_code = 200
    if not delivery.verify_paper_matches_seed(session, exam_session):
        # Fail loudly. A paper that no longer matches its seed means the exam
        # structure moved under a live session, and serving the new one would
        # silently change the exam a candidate is halfway through.
        raise Conflict(
            "This session's paper no longer matches its seed. The exam structure changed "
            "after the session started. Refusing to serve a different paper; contact your "
            "administrator.",
            session_id=str(exam_session.id),
        )
    return _state(session, exam_session, now)


@router.get("/assignments/{assignment_id}/session", response_model=SessionState)
def latest_session_for_assignment(
    assignment_id: uuid.UUID,
    principal: Principal = Depends(authorize),
    session: Session = Depends(db),
) -> SessionState:
    """The candidate's most recent attempt at one assignment.

    Exists because the exam runner has no client-side state to lose and no
    session id in its URL: after a hard reload it knows only which assignment
    it is for. While an attempt is live, `POST /sessions` answers that question
    idempotently. Once the attempts are used up it correctly refuses — and a
    candidate reopening the page then deserves to see the paper they submitted,
    not an error about attempt limits.
    """
    now = delivery.Clock.now()
    exam_session = session.scalars(
        select(ExamSession)
        .where(
            ExamSession.assignment_id == assignment_id,
            ExamSession.candidate_user_id == principal.user_id,
        )
        .order_by(ExamSession.attempt_no.desc())
    ).first()
    if exam_session is None:
        raise NotFound("You have no attempt at this assessment.")
    delivery.refresh_status(session, exam_session, now)
    return _state(session, exam_session, now)


@router.get("/sessions/{session_id}", response_model=SessionState)
def get_session_state(
    session_id: uuid.UUID,
    principal: Principal = Depends(authorize),
    session: Session = Depends(db),
) -> SessionState:
    """The whole recovery story: paper, answers, revisions, and the clock."""
    now = delivery.Clock.now()
    exam_session = _load_own_session(session, principal, session_id)
    # Readable after submission too — a candidate reviewing what they submitted
    # is a legitimate use. Only the *writes* require a live session.
    delivery.refresh_status(session, exam_session, now)
    return _state(session, exam_session, now)


@router.put("/sessions/{session_id}/answers/{paper_item_id}", response_model=SaveAnswerResponse)
def save_answer(
    session_id: uuid.UUID,
    paper_item_id: uuid.UUID,
    body: SaveAnswerRequest,
    principal: Principal = Depends(authorize),
    session: Session = Depends(db),
) -> SaveAnswerResponse:
    assert principal.org_id is not None  # noqa: S101
    now = delivery.Clock.now()
    exam_session = _load_own_session(session, principal, session_id)

    item = session.get(SessionPaperItem, paper_item_id)
    if item is None or item.session_id != exam_session.id:
        raise NotFound("That question is not on this paper.")
    version = session.get(QuestionVersion, item.question_version_id)
    question = session.get(Question, version.question_id) if version else None
    if version is None or question is None:  # pragma: no cover - FK guarantees it
        raise NotFound("That question is not on this paper.")

    delivery.validate_answer_shape(question.kind, version.body, body.value)

    answer = delivery.save_answer(
        session,
        org_id=principal.org_id,
        exam_session=exam_session,
        paper_item_id=paper_item_id,
        value=body.value,
        client_revision=body.client_revision,
        time_spent_ms=body.time_spent_ms,
        flagged=body.flagged,
        client_saved_at=body.client_saved_at,
        now=now,
    )
    return SaveAnswerResponse(
        paper_item_id=paper_item_id,
        revision=answer.revision,
        saved_at=answer.saved_at,
        server_time=now,
        remaining_seconds=delivery.remaining_seconds(exam_session, now),
    )


@router.post("/sessions/{session_id}/submit", response_model=SubmitResponse)
def submit_session(
    session_id: uuid.UUID,
    body: SubmitRequest,
    principal: Principal = Depends(authorize),
    session: Session = Depends(db),
) -> SubmitResponse:
    assert principal.org_id is not None  # noqa: S101
    now = delivery.Clock.now()
    exam_session = _load_own_session(session, principal, session_id)
    delivery.submit_session(session, exam_session=exam_session, reason=body.reason, now=now)

    version = session.get(ExamVersion, exam_session.exam_version_id)
    answered = session.scalar(
        select(func.count())
        .select_from(SessionPaperItem)
        .where(SessionPaperItem.session_id == exam_session.id)
    )
    from sentinel_api.models import Answer as AnswerModel

    answer_count = session.scalar(
        select(func.count())
        .select_from(AnswerModel)
        .where(AnswerModel.session_id == exam_session.id)
    )
    org_id = principal.org_id
    session_id_value = exam_session.id
    show_score = bool(version and version.show_score_on_submit)

    # Commit the submission before grading. Grading runs in its own transaction
    # under the 'system' role, so it cannot see uncommitted work — and a grading
    # failure must not roll back a submission the candidate was told succeeded.
    session.commit()

    total = maximum = None
    with server_session(org_id=str(org_id), actor_user_id=str(principal.user_id)) as gs:
        result = grade_session(gs, org_id=org_id, session_id=session_id_value)
        total, maximum = float(result.total_marks), float(result.max_marks)

    return SubmitResponse(
        session_id=session_id_value,
        status=exam_session.status,
        submitted_at=exam_session.submitted_at or now,
        answered=answer_count or 0,
        question_count=answered or 0,
        score_released=show_score,
        total_marks=total if show_score else None,
        max_marks=maximum if show_score else None,
    )


class ResultResponse(BaseModel):
    session_id: uuid.UUID
    status: str
    total_marks: float
    max_marks: float
    percentage: float | None
    released: bool
    items: list[dict[str, Any]]


@router.get("/sessions/{session_id}/result", response_model=ResultResponse)
def get_result(
    session_id: uuid.UUID,
    principal: Principal = Depends(authorize),
    session: Session = Depends(db),
) -> ResultResponse:
    """A candidate's own result, gated on the exam's release time.

    `results_release_at` is enforced here rather than in the UI, because "the
    page does not show it" and "the candidate cannot get it" are different
    claims and only the second one is true of an API check.
    """
    exam_session = _load_own_session(session, principal, session_id)
    result = session.get(SessionResult, exam_session.id)
    if result is None:
        raise NotFound("This session has not been graded.")

    version = session.get(ExamVersion, exam_session.exam_version_id)
    release = version.results_release_at if version else None
    if release is not None and delivery.Clock.now() < release.replace(
        tzinfo=release.tzinfo or dt.UTC
    ):
        raise Conflict(
            f"Results for this exam are released at {release.isoformat()}.",
            released_at=release.isoformat(),
        )

    scores = session.execute(
        select(QuestionScore, SessionPaperItem)
        .join(SessionPaperItem, SessionPaperItem.id == QuestionScore.paper_item_id)
        .where(QuestionScore.session_id == exam_session.id)
        .order_by(SessionPaperItem.item_ordinal)
    ).all()

    return ResultResponse(
        session_id=exam_session.id,
        status=exam_session.status,
        total_marks=float(result.total_marks),
        max_marks=float(result.max_marks),
        percentage=float(result.percentage) if result.percentage is not None else None,
        released=True,
        items=[
            {
                "item_ordinal": item.item_ordinal,
                "awarded": float(score.awarded),
                "max_marks": float(score.max_marks),
                "grader": score.grader,
                "detail": score.detail,
            }
            for score, item in scores
        ],
    )
