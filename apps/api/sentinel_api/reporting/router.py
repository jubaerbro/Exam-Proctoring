"""Organization and candidate endpoints.

The candidate-facing ``GET /api/v1/me/exams`` is the Phase 1 vertical slice: it
exercises authentication, tenant binding, RLS, and a real join, end to end.
"""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from sentinel_api.core.errors import NotFound
from sentinel_api.models import (
    ActivityLog,
    AppUser,
    Exam,
    ExamAssignment,
    ExamVersion,
    Membership,
    Organization,
)
from sentinel_api.tenancy.deps import Principal, authorize, db

router = APIRouter(prefix="/api/v1", tags=["organization"])


class OrgResponse(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    status: str
    created_at: dt.datetime


class MemberResponse(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str
    roles: list[str]
    status: str
    external_ref: str | None


class ActivityResponse(BaseModel):
    action: str
    actor_user_id: uuid.UUID | None
    object_type: str | None
    occurred_at: dt.datetime


class AssignedExam(BaseModel):
    assignment_id: uuid.UUID
    exam_id: uuid.UUID
    exam_version_id: uuid.UUID
    title: str
    description: str | None
    duration_seconds: int
    opens_at: dt.datetime | None
    closes_at: dt.datetime | None
    integrity_enabled: bool
    judge_enabled: bool
    attempts_allowed: int
    window_state: str


@router.get("/org", response_model=OrgResponse)
def get_org(
    principal: Principal = Depends(authorize), session: Session = Depends(db)
) -> OrgResponse:
    org = session.get(Organization, principal.org_id)
    if org is None:
        # Unreachable through a valid token, but if RLS ever hid the row this
        # must be a 404 rather than a 500.
        raise NotFound("Organization not found.")
    return OrgResponse(
        id=org.id, slug=org.slug, name=org.name, status=org.status, created_at=org.created_at
    )


@router.get("/org/members", response_model=list[MemberResponse])
def list_members(
    principal: Principal = Depends(authorize), session: Session = Depends(db)
) -> list[MemberResponse]:
    rows = session.execute(
        select(Membership, AppUser)
        .join(AppUser, AppUser.id == Membership.user_id)
        .order_by(AppUser.display_name)
    ).all()

    grouped: dict[uuid.UUID, MemberResponse] = {}
    for membership, user in rows:
        existing = grouped.get(user.id)
        if existing:
            existing.roles.append(membership.role)
        else:
            grouped[user.id] = MemberResponse(
                user_id=user.id,
                email=user.email,
                display_name=user.display_name,
                roles=[membership.role],
                status=membership.status,
                external_ref=membership.external_ref,
            )
    for member in grouped.values():
        member.roles.sort()
    return list(grouped.values())


@router.get("/org/activity", response_model=list[ActivityResponse])
def list_activity(
    limit: int = 100,
    principal: Principal = Depends(authorize),
    session: Session = Depends(db),
) -> list[ActivityResponse]:
    rows = session.scalars(
        select(ActivityLog).order_by(ActivityLog.occurred_at.desc()).limit(min(limit, 500))
    ).all()
    return [
        ActivityResponse(
            action=r.action,
            actor_user_id=r.actor_user_id,
            object_type=r.object_type,
            occurred_at=r.occurred_at,
        )
        for r in rows
    ]


@router.get("/me/exams", response_model=list[AssignedExam])
def my_exams(
    principal: Principal = Depends(authorize), session: Session = Depends(db)
) -> list[AssignedExam]:
    """Exams assigned to the calling candidate.

    The ``candidate_user_id`` filter is belt-and-braces: RLS already restricts
    the rows to this organization, and a candidate seeing another candidate's
    assignment would be an authorization bug rather than a tenancy one. Both
    layers are cheap; neither is sufficient alone.
    """
    now = dt.datetime.now(dt.UTC)

    rows = session.execute(
        select(ExamAssignment, ExamVersion, Exam)
        .join(ExamVersion, ExamVersion.id == ExamAssignment.exam_version_id)
        .join(Exam, Exam.id == ExamVersion.exam_id)
        .where(
            ExamAssignment.candidate_user_id == principal.user_id,
            ExamVersion.status == "published",
        )
        .order_by(ExamVersion.opens_at.nulls_last(), Exam.title)
    ).all()

    out: list[AssignedExam] = []
    for assignment, version, exam in rows:
        opens = assignment.opens_at or version.opens_at
        closes = assignment.closes_at or version.closes_at
        out.append(
            AssignedExam(
                assignment_id=assignment.id,
                exam_id=exam.id,
                exam_version_id=version.id,
                title=exam.title,
                description=exam.description,
                duration_seconds=version.duration_seconds + assignment.extra_time_seconds,
                opens_at=opens,
                closes_at=closes,
                integrity_enabled=version.integrity_enabled,
                judge_enabled=version.judge_enabled,
                attempts_allowed=assignment.attempts_allowed,
                window_state=_window_state(now, opens, closes),
            )
        )
    return out


def _window_state(now: dt.datetime, opens: dt.datetime | None, closes: dt.datetime | None) -> str:
    if opens and now < opens:
        return "not_yet_open"
    if closes and now > closes:
        return "closed"
    return "open"


# `GET /api/v1/exams` was a Phase 1 stub here. It is now implemented for real in
# `assessment/router.py` alongside the rest of authoring, and has been removed
# rather than left behind as a second, divergent definition of the same route.
