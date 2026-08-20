"""Grade a submitted session.

Runs in its own transaction with `sentinel.role = 'system'` (see
`core/db.server_session`), not in the candidate's request context. The candidate
triggers grading by submitting, but they must not be able to write their own
marks even if a handler somewhere forgets to check — migration 0002's RLS
policies make `question_score` and `session_result` unwritable by the
`candidate` role, and this is the code path that respects that.

Grading is idempotent. A retry after a crash between "scores written" and
"result written" must converge, not double-count, so every write is an upsert
keyed on (session, paper item).
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from sentinel_api.grading.graders import NotAutoGradable, grade
from sentinel_api.models import (
    Answer,
    Question,
    QuestionScore,
    QuestionVersion,
    SessionPaperItem,
    SessionResult,
)


def grade_session(session: Session, *, org_id: uuid.UUID, session_id: uuid.UUID) -> SessionResult:
    """Score every auto-gradable item on a paper and write the totals.

    Coding items are recorded with `grader='judge'` and 0 awarded, so the
    maximum is honest about what has not been scored yet rather than quietly
    excluding it. Phase 3 overwrites those rows.
    """
    rows = session.execute(
        select(SessionPaperItem, QuestionVersion, Question, Answer)
        .join(QuestionVersion, QuestionVersion.id == SessionPaperItem.question_version_id)
        .join(Question, Question.id == QuestionVersion.question_id)
        .outerjoin(
            Answer,
            (Answer.paper_item_id == SessionPaperItem.id)
            & (Answer.session_id == SessionPaperItem.session_id),
        )
        .where(SessionPaperItem.session_id == session_id)
        .order_by(SessionPaperItem.item_ordinal)
    ).all()

    now = dt.datetime.now(dt.UTC)
    total = 0.0
    maximum = 0.0

    for item, version, question, answer in rows:
        item_max = float(item.marks)
        maximum += item_max

        try:
            award = grade(
                question.kind,
                version.body,
                answer.value if answer is not None else None,
                marks=item_max,
                negative_marks=float(version.negative_marks),
                partial_credit=version.partial_credit,
            )
            grader_name = "auto"
            awarded = award.awarded
            detail = award.detail
        except NotAutoGradable as exc:
            grader_name = "judge"
            awarded = 0.0
            detail = {"outcome": "pending", "reason": str(exc)}

        total += awarded
        session.execute(
            insert(QuestionScore)
            .values(
                org_id=org_id,
                session_id=session_id,
                paper_item_id=item.id,
                awarded=awarded,
                max_marks=item_max,
                grader=grader_name,
                detail=detail,
                graded_at=now,
            )
            .on_conflict_do_update(
                index_elements=[QuestionScore.session_id, QuestionScore.paper_item_id],
                set_={
                    "awarded": awarded,
                    "max_marks": item_max,
                    "grader": grader_name,
                    "detail": detail,
                    "graded_at": now,
                },
            )
        )

    # A negative total is possible with negative marking, and is not what a
    # transcript should say. The floor is applied to the paper, not to each
    # question, so a penalty still costs marks earned elsewhere.
    total = max(0.0, round(total, 3))
    maximum = round(maximum, 3)

    session.execute(
        insert(SessionResult)
        .values(
            session_id=session_id,
            org_id=org_id,
            total_marks=total,
            max_marks=maximum,
            auto_graded_at=now,
        )
        .on_conflict_do_update(
            index_elements=[SessionResult.session_id],
            set_={"total_marks": total, "max_marks": maximum, "auto_graded_at": now},
        )
    )
    session.flush()

    result = session.get(SessionResult, session_id)
    assert result is not None  # noqa: S101 - just written in this transaction
    return result
