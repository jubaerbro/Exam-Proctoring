"""Exam authoring: versions, sections, `k of n` pools, publishing, assignment.

Publishing is where the interesting rules live. Once an `exam_version` is
published it is frozen — sections, pools and pool membership cannot change —
because a candidate's paper is generated from that structure, and a structure
that moves underneath an in-flight session makes recovery undefined. Editing a
published exam creates version *n+1*, exactly as with questions.

The publish check is deliberately strict and runs before the freeze, not after:
a published exam that cannot generate a paper is discovered by a candidate, at
the worst possible moment.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sentinel_api.assessment.paper import ExamBlueprint, PoolSpec, SectionSpec
from sentinel_api.assessment.schemas import ExamVersionDraft
from sentinel_api.core.errors import Conflict, NotFound, ValidationFailed
from sentinel_api.models import (
    AppUser,
    Exam,
    ExamAssignment,
    ExamSection,
    ExamVersion,
    Membership,
    Question,
    QuestionVersion,
    SectionPool,
    SectionPoolItem,
)


def create_exam(
    session: Session,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    title: str,
    description: str | None,
) -> Exam:
    exam = Exam(org_id=org_id, title=title, description=description, owner_id=actor_id)
    session.add(exam)
    session.flush()
    return exam


def get_exam(session: Session, exam_id: uuid.UUID) -> Exam:
    exam = session.get(Exam, exam_id)
    if exam is None:
        raise NotFound("Exam not found.")
    return exam


def get_version(session: Session, version_id: uuid.UUID) -> ExamVersion:
    version = session.get(ExamVersion, version_id)
    if version is None:
        raise NotFound("Exam version not found.")
    return version


def add_version(
    session: Session,
    *,
    org_id: uuid.UUID,
    exam: Exam,
    draft: ExamVersionDraft,
) -> ExamVersion:
    """Create the next draft version of an exam, with its sections and pools."""
    if draft.navigation != "free":
        # The schema permits 'sequential' and 'one_way'; the delivery layer only
        # enforces 'free'. Accepting a value the server will not honour would be
        # a lie told at authoring time and discovered by a candidate.
        raise ValidationFailed(
            f"navigation={draft.navigation!r} is NOT IMPLEMENTED. Only 'free' navigation is "
            "enforced by the delivery layer today; publishing an exam whose navigation rule "
            "the server ignores would misrepresent the exam to candidates."
        )

    current = session.scalar(
        select(func.max(ExamVersion.version)).where(ExamVersion.exam_id == exam.id)
    )
    version = ExamVersion(
        org_id=org_id,
        exam_id=exam.id,
        version=(current or 0) + 1,
        status="draft",
        delivery=draft.delivery,
        duration_seconds=draft.duration_seconds,
        grace_seconds=draft.grace_seconds,
        opens_at=draft.opens_at,
        closes_at=draft.closes_at,
        results_release_at=draft.results_release_at,
        max_attempts=draft.max_attempts,
        navigation=draft.navigation,
        show_score_on_submit=draft.show_score_on_submit,
        integrity_enabled=draft.integrity_enabled,
        integrity_config=draft.integrity_config,
        judge_enabled=draft.judge_enabled,
    )
    session.add(version)
    session.flush()

    for section_in in draft.sections:
        section = ExamSection(
            org_id=org_id,
            exam_version_id=version.id,
            ordinal=section_in.ordinal,
            title=section_in.title,
            instructions=section_in.instructions,
            shuffle_questions=section_in.shuffle_questions,
            time_limit_seconds=section_in.time_limit_seconds,
        )
        session.add(section)
        session.flush()

        for pool_in in section_in.pools:
            pool = SectionPool(
                org_id=org_id,
                section_id=section.id,
                ordinal=pool_in.ordinal,
                label=pool_in.label,
                select_count=pool_in.select_count,
                source_kind="explicit",
            )
            session.add(pool)
            session.flush()
            for ordinal, qv_id in enumerate(pool_in.question_version_ids, start=1):
                session.add(
                    SectionPoolItem(
                        org_id=org_id,
                        pool_id=pool.id,
                        question_version_id=qv_id,
                        ordinal=ordinal,
                    )
                )

    session.flush()
    exam.current_version_id = version.id
    exam.updated_at = dt.datetime.now(dt.UTC)
    return version


def publish_version(session: Session, *, version: ExamVersion, actor_id: uuid.UUID) -> ExamVersion:
    """Validate and freeze. Everything that can be checked, is checked here."""
    if version.status == "published":
        raise Conflict("This exam version is already published.")
    if version.status == "archived":
        raise Conflict("An archived exam version cannot be published.")

    problems = _publish_problems(session, version)
    if problems:
        raise ValidationFailed(
            f"This exam version cannot be published: {len(problems)} problem(s).",
            errors=problems,
        )

    version.status = "published"
    version.published_at = dt.datetime.now(dt.UTC)
    version.published_by = actor_id
    session.flush()
    return version


def _publish_problems(session: Session, version: ExamVersion) -> list[dict[str, str]]:
    problems: list[dict[str, str]] = []

    sections = session.scalars(
        select(ExamSection)
        .where(ExamSection.exam_version_id == version.id)
        .order_by(ExamSection.ordinal)
    ).all()
    if not sections:
        problems.append({"pointer": "/sections", "message": "an exam needs at least one section"})

    total_questions = 0
    for section in sections:
        pools = session.scalars(
            select(SectionPool)
            .where(SectionPool.section_id == section.id)
            .order_by(SectionPool.ordinal)
        ).all()
        if not pools:
            problems.append(
                {
                    "pointer": f"/sections/{section.ordinal}/pools",
                    "message": "a section needs at least one pool",
                }
            )
        for pool in pools:
            rows = session.execute(
                select(SectionPoolItem, QuestionVersion)
                .join(QuestionVersion, QuestionVersion.id == SectionPoolItem.question_version_id)
                .where(SectionPoolItem.pool_id == pool.id)
            ).all()
            pointer = f"/sections/{section.ordinal}/pools/{pool.ordinal}"

            if len(rows) < pool.select_count:
                problems.append(
                    {
                        "pointer": f"{pointer}/select_count",
                        "message": f"selects {pool.select_count} question(s) from a pool of "
                        f"{len(rows)}",
                    }
                )
            unpublished = [qv.id for _, qv in rows if qv.status != "published"]
            if unpublished:
                problems.append(
                    {
                        "pointer": f"{pointer}/question_version_ids",
                        "message": f"{len(unpublished)} question version(s) are not published; "
                        "a draft question cannot appear on a live paper",
                    }
                )
            # Every candidate must be able to score the same maximum. Mixed
            # marks inside one pool mean the paper you are dealt decides your
            # ceiling, which is an unfair exam rather than a varied one.
            distinct_marks = {float(qv.marks) for _, qv in rows}
            if len(distinct_marks) > 1:
                problems.append(
                    {
                        "pointer": f"{pointer}/question_version_ids",
                        "message": "questions in one pool must carry equal marks, otherwise the "
                        f"paper a candidate is dealt changes their maximum score (found "
                        f"{sorted(distinct_marks)})",
                    }
                )
            total_questions += pool.select_count

            if version.judge_enabled is False:
                coding = [
                    qv.id
                    for _, qv in rows
                    if session.get(Question, qv.question_id)
                    and session.get(Question, qv.question_id).kind == "coding"  # type: ignore[union-attr]
                ]
                if coding:
                    problems.append(
                        {
                            "pointer": f"{pointer}/question_version_ids",
                            "message": "contains coding question(s) but judge_enabled is false; "
                            "candidates would be unable to run or score them",
                        }
                    )

    if total_questions == 0 and not problems:
        problems.append({"pointer": "/sections", "message": "the exam would have no questions"})

    return problems


def load_blueprint(session: Session, version: ExamVersion) -> ExamBlueprint:
    """Read an exam version's structure into the pure form `paper.py` consumes.

    Everything is ordered explicitly. Relying on the database's natural row
    order here would make paper generation depend on vacuum timing, which is a
    long way from deterministic.
    """
    sections = session.scalars(
        select(ExamSection)
        .where(ExamSection.exam_version_id == version.id)
        .order_by(ExamSection.ordinal)
    ).all()

    section_specs: list[SectionSpec] = []
    for section in sections:
        pools = session.scalars(
            select(SectionPool)
            .where(SectionPool.section_id == section.id)
            .order_by(SectionPool.ordinal)
        ).all()
        pool_specs: list[PoolSpec] = []
        for pool in pools:
            rows = session.execute(
                select(SectionPoolItem, QuestionVersion, Question)
                .join(QuestionVersion, QuestionVersion.id == SectionPoolItem.question_version_id)
                .join(Question, Question.id == QuestionVersion.question_id)
                .where(SectionPoolItem.pool_id == pool.id)
                .order_by(SectionPoolItem.ordinal)
            ).all()
            items = tuple(
                (qv.id, float(qv.marks), _shufflable_option_count(question.kind, qv.body))
                for _, qv, question in rows
            )
            pool_specs.append(
                PoolSpec(
                    pool_id=pool.id,
                    ordinal=pool.ordinal,
                    select_count=pool.select_count,
                    items=items,
                )
            )
        section_specs.append(
            SectionSpec(
                section_id=section.id,
                ordinal=section.ordinal,
                shuffle_questions=section.shuffle_questions,
                pools=tuple(pool_specs),
            )
        )

    return ExamBlueprint(exam_version_id=version.id, sections=tuple(section_specs))


def _shufflable_option_count(kind: str, body: dict[str, Any]) -> int:
    """0 when there is nothing to shuffle — including when the author said not to."""
    if kind not in ("single_choice", "multiple_choice"):
        return 0
    if not body.get("shuffle_options", True):
        return 0
    return len(body.get("options", []))


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------


def resolve_candidates(
    session: Session, *, user_ids: list[uuid.UUID], emails: list[str]
) -> tuple[dict[str, uuid.UUID], list[tuple[str, str]]]:
    """Map identifiers to candidate user ids within the caller's organization.

    Returns (resolved, rejected). A person who is not a candidate *in this
    organization* is rejected rather than assigned — assigning an instructor to
    sit their own exam is almost always a typo, and the failure mode if it is
    not caught is an exam session nobody expected.
    """
    resolved: dict[str, uuid.UUID] = {}
    rejected: list[tuple[str, str]] = []

    wanted_ids = set(user_ids)
    wanted_emails = {e.strip().lower() for e in emails if e.strip()}
    if not wanted_ids and not wanted_emails:
        return resolved, rejected

    # RLS confines `membership` to this organization, so this join cannot leak
    # the existence of a user who is only a member elsewhere.
    rows = session.execute(
        select(AppUser, Membership)
        .join(Membership, Membership.user_id == AppUser.id)
        .where(Membership.status == "active")
    ).all()

    by_id: dict[uuid.UUID, set[str]] = {}
    by_email: dict[str, uuid.UUID] = {}
    for user, membership in rows:
        by_id.setdefault(user.id, set()).add(membership.role)
        by_email[user.email.lower()] = user.id

    for user_id in wanted_ids:
        roles = by_id.get(user_id)
        if roles is None:
            rejected.append((str(user_id), "unknown"))
        elif "candidate" not in roles:
            rejected.append((str(user_id), "not_a_candidate"))
        else:
            resolved[str(user_id)] = user_id

    for email in wanted_emails:
        found = by_email.get(email)
        if found is None:
            rejected.append((email, "unknown"))
        elif "candidate" not in by_id.get(found, set()):
            rejected.append((email, "not_a_candidate"))
        else:
            resolved[email] = found

    return resolved, rejected


def existing_assignments(
    session: Session, *, exam_version_id: uuid.UUID
) -> dict[uuid.UUID, ExamAssignment]:
    rows = session.scalars(
        select(ExamAssignment).where(ExamAssignment.exam_version_id == exam_version_id)
    ).all()
    return {a.candidate_user_id: a for a in rows}
