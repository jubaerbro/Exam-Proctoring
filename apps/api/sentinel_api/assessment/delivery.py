"""Exam delivery: start, resume, autosave, submit.

The clock
---------
`deadline_at` is computed once, server-side, when the session starts, and every
later decision reads it from the row. The browser is told how many seconds
remain and when the server thinks it is, so it can render a countdown; it is
never asked what time it is. A candidate who changes their system clock, or
whose machine is simply wrong, changes the number on their screen and nothing
else.

Recovery
--------
Start is idempotent: calling it again on a live session returns the same paper
rather than making a new one. That is what makes a forced reload safe — the
client does not have to distinguish "starting" from "resuming", so it cannot get
that distinction wrong. The paper is stored, and `verify_paper_matches_seed`
re-derives it from the seed and compares, which is what makes "deterministic"
a checked property rather than a comment.

Autosave
--------
Saves are last-writer-wins per item, with a monotonic revision. A tab that was
offline for two minutes and reconnects with stale content must not overwrite
what the candidate typed since — so a save carrying a revision at or below the
stored one is rejected as stale, and the current server state is returned so the
client can reconcile.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from sentinel_api.assessment.authoring import load_blueprint
from sentinel_api.assessment.paper import generate_paper, paper_seed
from sentinel_api.assessment.schemas import ChoiceBody, parse_body
from sentinel_api.core.db import elevated
from sentinel_api.core.errors import Conflict, Forbidden, NotFound, ValidationFailed
from sentinel_api.models import (
    Answer,
    AnswerRevision,
    ExamAssignment,
    ExamSection,
    ExamSession,
    ExamVersion,
    Question,
    QuestionVersion,
    SessionPaperItem,
)


class Clock:
    """Indirection so tests can move time without sleeping through an exam."""

    @staticmethod
    def now() -> dt.datetime:
        return dt.datetime.now(dt.UTC)


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    """Postgres returns tz-aware timestamps, but a naive one here would compare
    wrong rather than fail, so normalise instead of trusting the driver."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


# ---------------------------------------------------------------------------
# Starting and resuming
# ---------------------------------------------------------------------------


def start_session(
    session: Session,
    *,
    org_id: uuid.UUID,
    candidate_id: uuid.UUID,
    assignment_id: uuid.UUID,
    user_agent: str | None = None,
    now: dt.datetime | None = None,
) -> tuple[ExamSession, bool]:
    """Start or resume the candidate's attempt. Returns (session, created)."""
    now = now or Clock.now()

    assignment = session.get(ExamAssignment, assignment_id)
    if assignment is None or assignment.candidate_user_id != candidate_id:
        # Another candidate's assignment is a 404, not a 403: confirming it
        # exists would confirm that person sits this exam.
        raise NotFound("Assignment not found.")

    version = session.get(ExamVersion, assignment.exam_version_id)
    if version is None or version.status != "published":
        raise NotFound("Assignment not found.")

    existing = session.scalars(
        select(ExamSession)
        .where(ExamSession.assignment_id == assignment.id)
        .order_by(ExamSession.attempt_no.desc())
    ).all()

    open_states = ("created", "calibrating", "in_progress")
    live = next((s for s in existing if s.status in open_states), None)
    if live is not None:
        _expire_if_past_deadline(session, live, now)
        if live.status == "in_progress":
            return live, False

    finished = [s for s in existing if s.status in ("submitted", "expired")]
    allowed = min(assignment.attempts_allowed, version.max_attempts)
    if len(finished) >= allowed:
        raise Conflict(f"You have used all {allowed} permitted attempt(s) at this exam.")

    opens = _aware(assignment.opens_at or version.opens_at)
    closes = _aware(assignment.closes_at or version.closes_at)
    if opens and now < opens:
        raise Forbidden(f"This exam opens at {opens.isoformat()}.")
    if closes and now > closes:
        raise Forbidden(f"This exam closed at {closes.isoformat()}.")

    attempt_no = max((s.attempt_no for s in existing), default=0) + 1
    duration = version.duration_seconds + assignment.extra_time_seconds
    deadline = now + dt.timedelta(seconds=duration)
    # The window is a hard ceiling: extra time cannot run past the moment the
    # exam closes for everyone, or a late starter gains an advantage over a
    # candidate who started on time.
    if closes and deadline > closes:
        deadline = closes

    exam_session = ExamSession(
        org_id=org_id,
        assignment_id=assignment.id,
        exam_version_id=version.id,
        candidate_user_id=candidate_id,
        attempt_no=attempt_no,
        status="in_progress",
        paper_seed=paper_seed(version.id, candidate_id, attempt_no),
        started_at=now,
        deadline_at=deadline,
        last_seen_at=now,
        evidence_cap=40,
        user_agent=(user_agent or "")[:512] or None,
    )
    session.add(exam_session)
    session.flush()

    _materialize_paper(session, org_id=org_id, exam_session=exam_session, version=version)
    return exam_session, True


def _materialize_paper(
    session: Session, *, org_id: uuid.UUID, exam_session: ExamSession, version: ExamVersion
) -> None:
    blueprint = load_blueprint(session, version)
    items = generate_paper(blueprint, exam_session.paper_seed)
    if not items:
        raise ValidationFailed(
            "This exam version generates an empty paper. It should not have been publishable; "
            "refusing to start a session on it."
        )
    # The paper is the server's, not the candidate's. See `core.db.elevated`.
    with elevated(session):
        for item in items:
            session.add(
                SessionPaperItem(
                    org_id=org_id,
                    session_id=exam_session.id,
                    section_id=item.section_id,
                    pool_id=item.pool_id,
                    question_version_id=item.question_version_id,
                    section_ordinal=item.section_ordinal,
                    item_ordinal=item.item_ordinal,
                    option_order=item.option_order,
                    marks=item.marks,
                )
            )
        session.flush()


def verify_paper_matches_seed(session: Session, exam_session: ExamSession) -> bool:
    """Re-derive the paper from the seed and compare it to what was stored.

    This is the check behind the Phase 2 exit criterion "the deterministic paper
    reproduces exactly on recovery". It is exposed as a function rather than
    living only in a test so that the recovery endpoint can assert it on every
    resume: if generation ever stops being deterministic — a Python upgrade, an
    edit to `paper.py`, a pool mutated behind a publish check that failed to
    hold — a candidate finds out through a 500 rather than through a paper that
    quietly changed.
    """
    version = session.get(ExamVersion, exam_session.exam_version_id)
    if version is None:
        return False
    expected = generate_paper(load_blueprint(session, version), exam_session.paper_seed)
    stored = session.scalars(
        select(SessionPaperItem)
        .where(SessionPaperItem.session_id == exam_session.id)
        .order_by(SessionPaperItem.item_ordinal)
    ).all()
    if len(expected) != len(stored):
        return False
    return all(
        e.question_version_id == s.question_version_id
        and e.item_ordinal == s.item_ordinal
        and e.section_id == s.section_id
        and (e.option_order or None) == (list(s.option_order) if s.option_order else None)
        for e, s in zip(expected, stored, strict=True)
    )


# ---------------------------------------------------------------------------
# The clock
# ---------------------------------------------------------------------------


def _expire_if_past_deadline(session: Session, exam_session: ExamSession, now: dt.datetime) -> None:
    """Close out a session whose deadline passed while nobody was looking.

    A candidate who closes the lid at minute 5 of a 90-minute exam leaves a row
    that says `in_progress` forever. Lazy expiry on the next touch is not a
    substitute for a sweeper (Phase 4 adds one), but it means the state a
    request sees is never a lie.
    """
    deadline = _aware(exam_session.deadline_at)
    if exam_session.status != "in_progress" or deadline is None:
        return
    version = session.get(ExamVersion, exam_session.exam_version_id)
    grace = version.grace_seconds if version else 0
    if now > deadline + dt.timedelta(seconds=grace):
        exam_session.status = "expired"
        exam_session.submitted_at = now
        exam_session.submit_reason = "timer"
        session.flush()


def refresh_status(session: Session, exam_session: ExamSession, now: dt.datetime) -> None:
    """Bring a session's status up to date with the clock, without requiring it
    to still be live. Read paths call this; write paths call `require_live`."""
    _expire_if_past_deadline(session, exam_session, now)


def remaining_seconds(exam_session: ExamSession, now: dt.datetime) -> int:
    deadline = _aware(exam_session.deadline_at)
    if deadline is None:
        return 0
    return max(0, int((deadline - now).total_seconds()))


def require_live(session: Session, exam_session: ExamSession, now: dt.datetime) -> None:
    _expire_if_past_deadline(session, exam_session, now)
    if exam_session.status != "in_progress":
        raise Conflict(f"This session is {exam_session.status}; it accepts no further changes.")


# ---------------------------------------------------------------------------
# Autosave
# ---------------------------------------------------------------------------


class StaleSave(Conflict):
    """The client's revision is not newer than the server's."""


def save_answer(
    session: Session,
    *,
    org_id: uuid.UUID,
    exam_session: ExamSession,
    paper_item_id: uuid.UUID,
    value: dict[str, Any],
    client_revision: int | None = None,
    time_spent_ms: int = 0,
    flagged: bool = False,
    client_saved_at: dt.datetime | None = None,
    now: dt.datetime | None = None,
) -> Answer:
    now = now or Clock.now()
    require_live(session, exam_session, now)

    item = session.get(SessionPaperItem, paper_item_id)
    if item is None or item.session_id != exam_session.id:
        raise NotFound("That question is not on this paper.")

    existing = session.scalar(
        select(Answer).where(Answer.session_id == exam_session.id, Answer.paper_item_id == item.id)
    )

    # `client_revision` is the revision the client is editing *from* — an
    # If-Match, not a proposed new number. Equal is the normal case (you saw
    # revision 2, you save, you get 3). Strictly less means the client is
    # working from a snapshot the server has already moved past, which is the
    # reconnecting-stale-tab case this exists to stop.
    stale = (
        existing is not None and client_revision is not None and client_revision < existing.revision
    )
    if stale and existing is not None:
        raise StaleSave(
            f"A newer revision ({existing.revision}) is already saved for this question. "
            "Reload the session state before saving again.",
            server_revision=existing.revision,
            server_value=existing.value,
        )

    revision = (existing.revision + 1) if existing is not None else 1

    if existing is None:
        existing = Answer(
            org_id=org_id,
            session_id=exam_session.id,
            paper_item_id=item.id,
            revision=revision,
            value=value,
            is_flagged_by_candidate=flagged,
            time_spent_ms=max(0, time_spent_ms),
            client_saved_at=client_saved_at,
            saved_at=now,
        )
        session.add(existing)
    else:
        existing.revision = revision
        existing.value = value
        existing.is_flagged_by_candidate = flagged
        existing.time_spent_ms = max(existing.time_spent_ms, max(0, time_spent_ms))
        existing.client_saved_at = client_saved_at
        existing.saved_at = now

    # The append-only trail. Written in the same transaction as the mutable row,
    # so "saved" and "provably saved" cannot diverge.
    session.add(
        AnswerRevision(
            org_id=org_id,
            session_id=exam_session.id,
            paper_item_id=item.id,
            revision=revision,
            value=value,
            saved_at=now,
        )
    )
    exam_session.last_seen_at = now
    session.flush()
    return existing


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------


def submit_session(
    session: Session,
    *,
    exam_session: ExamSession,
    reason: str = "candidate",
    now: dt.datetime | None = None,
) -> ExamSession:
    """Close a session. The server's clock decides whether it was in time.

    The grace period is applied on the server and is not disclosed as slack the
    candidate can plan around: the countdown they see reaches zero at
    `deadline_at`. Grace exists to absorb the request that was in flight when
    the clock ran out, not to extend the exam.
    """
    now = now or Clock.now()

    if exam_session.status in ("submitted", "expired"):
        raise Conflict(f"This session was already {exam_session.status}.")
    if exam_session.status != "in_progress":
        raise Conflict(f"A session with status {exam_session.status!r} cannot be submitted.")

    version = session.get(ExamVersion, exam_session.exam_version_id)
    grace = version.grace_seconds if version else 0
    deadline = _aware(exam_session.deadline_at)

    if deadline is not None and now > deadline + dt.timedelta(seconds=grace):
        exam_session.status = "expired"
        exam_session.submitted_at = now
        exam_session.submit_reason = "timer"
        session.flush()
        raise Conflict(
            "The deadline for this exam has passed; the session was closed by the timer. "
            "Answers already autosaved are kept and graded.",
            deadline_at=deadline.isoformat(),
            server_time=now.isoformat(),
        )

    exam_session.status = "submitted"
    exam_session.submitted_at = now
    exam_session.submit_reason = reason
    exam_session.last_seen_at = now
    session.flush()
    return exam_session


# ---------------------------------------------------------------------------
# Reading a paper back
# ---------------------------------------------------------------------------


def load_paper(session: Session, exam_session: ExamSession) -> list[dict[str, Any]]:
    """The candidate-facing view of the paper.

    Answer keys are stripped here, not in the router. A `body` that reaches a
    response serializer with `correct` still in it is one refactor away from
    being sent, so the removal happens at the point the row is read.
    """
    rows = session.execute(
        select(SessionPaperItem, QuestionVersion, Question, ExamSection, Answer)
        .join(QuestionVersion, QuestionVersion.id == SessionPaperItem.question_version_id)
        .join(Question, Question.id == QuestionVersion.question_id)
        .join(ExamSection, ExamSection.id == SessionPaperItem.section_id)
        .outerjoin(
            Answer,
            (Answer.paper_item_id == SessionPaperItem.id)
            & (Answer.session_id == SessionPaperItem.session_id),
        )
        .where(SessionPaperItem.session_id == exam_session.id)
        .order_by(SessionPaperItem.item_ordinal)
    ).all()

    out: list[dict[str, Any]] = []
    for item, version, question, section, answer in rows:
        out.append(
            {
                "paper_item_id": item.id,
                "item_ordinal": item.item_ordinal,
                "section_id": section.id,
                "section_title": section.title,
                "section_ordinal": section.ordinal,
                "kind": question.kind,
                "prompt": version.prompt,
                "prompt_format": version.prompt_format,
                "marks": float(item.marks),
                "body": candidate_safe_body(question.kind, version.body, item.option_order),
                "answer": None if answer is None else answer.value,
                "revision": 0 if answer is None else answer.revision,
                "flagged": bool(answer and answer.is_flagged_by_candidate),
                "saved_at": None if answer is None else answer.saved_at,
            }
        )
    return out


def candidate_safe_body(
    kind: str, body: dict[str, Any], option_order: list[int] | None
) -> dict[str, Any]:
    """Strip the answer key and apply this candidate's option permutation."""
    if kind in ("single_choice", "multiple_choice"):
        options = list(body.get("options", []))
        if option_order and len(option_order) == len(options):
            options = [options[i] for i in option_order]
        return {"options": options, "multiple": kind == "multiple_choice"}
    if kind == "short_answer":
        return {"multiline": False}
    if kind == "numeric":
        # The unit is part of the question, not the key; the tolerance is not.
        return {"unit": body.get("unit")}
    if kind == "coding":
        return {
            "languages": body.get("languages", []),
            "starter": body.get("starter", {}),
            "time_limit_ms": body.get("time_limit_ms"),
            "memory_limit_mb": body.get("memory_limit_mb"),
        }
    return {}


def validate_answer_shape(kind: str, body: dict[str, Any], value: dict[str, Any]) -> None:
    """Cheap shape check at save time, so a bad client fails loudly and early.

    Grading tolerates malformed answers by scoring 0; that is the right
    behaviour months later at grading time, and the wrong behaviour now, when
    the candidate is still in the room and can be told.
    """
    parsed = parse_body(kind, body)  # the stored body is already valid; cheap re-parse
    if kind in ("single_choice", "multiple_choice"):
        assert isinstance(parsed, ChoiceBody)  # noqa: S101 - guaranteed by `kind`
        selected = value.get("selected")
        if not isinstance(selected, list) or not all(isinstance(s, str) for s in selected):
            raise ValidationFailed("Answer must be {'selected': [option_id, ...]}.")
        valid = {o.id for o in parsed.options}
        unknown = sorted(set(selected) - valid)
        if unknown:
            raise ValidationFailed(f"Unknown option id(s): {unknown}.")
        if kind == "single_choice" and len(selected) > 1:
            raise ValidationFailed("This question accepts a single option.")
    elif kind == "short_answer":
        if not isinstance(value.get("text", ""), str):
            raise ValidationFailed("Answer must be {'text': '...'}.")
    elif kind == "numeric":
        raw = value.get("value")
        if raw is not None and not isinstance(raw, (int, float, str)):
            raise ValidationFailed("Answer must be {'value': <number>}.")
    elif kind == "coding":
        if not isinstance(value.get("source", ""), str):
            raise ValidationFailed("Answer must be {'source': '...', 'language': '...'}.")
