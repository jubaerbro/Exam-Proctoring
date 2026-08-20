"""SQLAlchemy models.

STATUS: PARTIALLY IMPLEMENTED. Migration 0001 creates all 40 tables from
DATA_MODEL.md. This module maps only the subset Phase 1 and Phase 2 need. The
remaining tables (audit_event, evidence_object, review, risk_assessment, ...)
exist in the database and are enforced by their constraints, but have no ORM
mapping yet. They are added in the phase that first uses them.

Phase 2 added: SessionPaperItem, Answer, AnswerRevision, SessionResult,
QuestionScore.
Phase 3 added: CodeSubmission, JudgeRun, JudgeTestResult.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


TZ = DateTime(timezone=True)

# Enum types are created by the migration; create_type=False stops SQLAlchemy
# from trying to create them again.
org_status = ENUM("active", "suspended", "closed", name="org_status", create_type=False)
member_role = ENUM(
    "org_admin", "instructor", "reviewer", "candidate", name="member_role", create_type=False
)
member_status = ENUM("invited", "active", "disabled", name="member_status", create_type=False)
question_kind = ENUM(
    "single_choice",
    "multiple_choice",
    "short_answer",
    "numeric",
    "coding",
    name="question_kind",
    create_type=False,
)
publish_status = ENUM("draft", "published", "archived", name="publish_status", create_type=False)
exam_delivery = ENUM("scheduled", "window", "on_demand", name="exam_delivery", create_type=False)
session_status = ENUM(
    "created",
    "calibrating",
    "in_progress",
    "paused",
    "submitted",
    "expired",
    "abandoned",
    "voided",
    name="session_status",
    create_type=False,
)
submit_reason = ENUM(
    "candidate", "timer", "proctor", "system", name="submit_reason", create_type=False
)
review_state = ENUM(
    "not_required", "queued", "in_review", "decided", name="review_state", create_type=False
)
chain_state = ENUM("unverified", "verified", "failed", name="chain_state", create_type=False)
judge_status = ENUM(
    "queued",
    "running",
    "completed",
    "compile_error",
    "runtime_error",
    "timeout",
    "memory_exceeded",
    "output_exceeded",
    "internal_error",
    "cancelled",
    name="judge_status",
    create_type=False,
)
judge_language = ENUM("python311", "cpp20", "java17", name="judge_language", create_type=False)
run_mode = ENUM("sample", "final", name="run_mode", create_type=False)


# ---------------------------------------------------------------------------
# Organizations and identity
# ---------------------------------------------------------------------------


class Organization(Base):
    __tablename__ = "organization"

    id: Mapped[uuid.UUID] = _uuid_pk()
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(org_status, nullable=False, default="active")
    plan_code: Mapped[str | None] = mapped_column(Text)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(TZ, nullable=False, server_default=func.now())

    memberships: Mapped[list[Membership]] = relationship(back_populates="organization")


class AppUser(Base):
    """Global identity. Not tenant-scoped: one person, one login, many orgs.

    This is the D2 decision. The cost is that a global-unique email lets one
    organization learn an address is already registered by attempting an
    invitation. Accepted deliberately — see docs/OPEN_DECISIONS.md D2.
    """

    __tablename__ = "app_user"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    email_verified_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str | None] = mapped_column(Text)
    password_changed_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    mfa_secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    mfa_enabled_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    failed_logins: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[dt.datetime | None] = mapped_column(TZ)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    external_subject: Mapped[str | None] = mapped_column(Text)
    external_issuer: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(member_status, nullable=False, default="active")
    created_at: Mapped[dt.datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(TZ, nullable=False, server_default=func.now())

    # membership has two FKs to app_user (user_id and invited_by), so the join
    # has to be spelled out.
    memberships: Mapped[list[Membership]] = relationship(
        back_populates="user", foreign_keys="Membership.user_id"
    )

    @property
    def mfa_enabled(self) -> bool:
        return self.mfa_enabled_at is not None and self.mfa_secret_enc is not None


class Membership(Base):
    __tablename__ = "membership"
    __table_args__ = (UniqueConstraint("org_id", "user_id", "role"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(member_role, nullable=False)
    status: Mapped[str] = mapped_column(member_status, nullable=False, default="active")
    external_ref: Mapped[str | None] = mapped_column(Text)
    invited_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id")
    )
    created_at: Mapped[dt.datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(TZ, nullable=False, server_default=func.now())

    organization: Mapped[Organization] = relationship(back_populates="memberships")
    user: Mapped[AppUser] = relationship(back_populates="memberships", foreign_keys=[user_id])


class RefreshToken(Base):
    __tablename__ = "refresh_token"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    issued_at: Mapped[dt.datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    expires_at: Mapped[dt.datetime] = mapped_column(TZ, nullable=False)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    replaced_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("refresh_token.id")
    )
    user_agent: Mapped[str | None] = mapped_column(Text)
    ip_hash: Mapped[bytes | None] = mapped_column(LargeBinary)


# ---------------------------------------------------------------------------
# Question bank
# ---------------------------------------------------------------------------


class QuestionBank(Base):
    __tablename__ = "question_bank"
    __table_args__ = (UniqueConstraint("org_id", "name"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id"), nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(TZ, nullable=False, server_default=func.now())


class Question(Base):
    __tablename__ = "question"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.id", ondelete="CASCADE"), nullable=False
    )
    bank_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("question_bank.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(question_kind, nullable=False)
    external_key: Mapped[str | None] = mapped_column(Text)
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id"), nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(TZ, nullable=False, server_default=func.now())


class QuestionVersion(Base):
    __tablename__ = "question_version"
    __table_args__ = (UniqueConstraint("question_id", "version"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.id", ondelete="CASCADE"), nullable=False
    )
    question_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("question.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(publish_status, nullable=False, default="draft")
    schema_version: Mapped[str] = mapped_column(Text, nullable=False, default="question.v1")
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_format: Mapped[str] = mapped_column(Text, nullable=False, default="markdown")
    body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    explanation: Mapped[str | None] = mapped_column(Text)
    marks: Mapped[float] = mapped_column(Numeric(8, 3), nullable=False)
    negative_marks: Mapped[float] = mapped_column(Numeric(8, 3), nullable=False, default=0)
    partial_credit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    difficulty: Mapped[int | None] = mapped_column(SmallInteger)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id"), nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    published_at: Mapped[dt.datetime | None] = mapped_column(TZ)


class TestCase(Base):
    __tablename__ = "test_case"
    __table_args__ = (UniqueConstraint("question_version_id", "ordinal"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.id", ondelete="CASCADE"), nullable=False
    )
    question_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("question_version.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    is_sample: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    weight: Mapped[float] = mapped_column(Numeric(8, 3), nullable=False, default=1)
    stdin_ref: Mapped[str | None] = mapped_column(Text)
    stdin_inline: Mapped[str | None] = mapped_column(Text)
    expected_ref: Mapped[str | None] = mapped_column(Text)
    expected_inline: Mapped[str | None] = mapped_column(Text)
    comparator: Mapped[str] = mapped_column(Text, nullable=False, default="trim_exact")
    comparator_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    time_limit_ms: Mapped[int | None] = mapped_column(Integer)
    memory_limit_mb: Mapped[int | None] = mapped_column(Integer)


# ---------------------------------------------------------------------------
# Exams
# ---------------------------------------------------------------------------


class Exam(Base):
    __tablename__ = "exam"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id"), nullable=False
    )
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[dt.datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(TZ, nullable=False, server_default=func.now())


class ExamVersion(Base):
    __tablename__ = "exam_version"
    __table_args__ = (UniqueConstraint("exam_id", "version"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.id", ondelete="CASCADE"), nullable=False
    )
    exam_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("exam.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(publish_status, nullable=False, default="draft")
    delivery: Mapped[str] = mapped_column(exam_delivery, nullable=False, default="window")
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    grace_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    opens_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    closes_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    results_release_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    navigation: Mapped[str] = mapped_column(Text, nullable=False, default="free")
    show_score_on_submit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    integrity_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    integrity_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    judge_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    published_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    published_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id")
    )
    created_at: Mapped[dt.datetime] = mapped_column(TZ, nullable=False, server_default=func.now())

    exam: Mapped[Exam] = relationship()


class ExamSection(Base):
    __tablename__ = "exam_section"
    __table_args__ = (UniqueConstraint("exam_version_id", "ordinal"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.id", ondelete="CASCADE"), nullable=False
    )
    exam_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("exam_version.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    instructions: Mapped[str | None] = mapped_column(Text)
    shuffle_questions: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    time_limit_seconds: Mapped[int | None] = mapped_column(Integer)


class SectionPool(Base):
    __tablename__ = "section_pool"
    __table_args__ = (UniqueConstraint("section_id", "ordinal"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.id", ondelete="CASCADE"), nullable=False
    )
    section_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("exam_section.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str | None] = mapped_column(Text)
    select_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source_kind: Mapped[str] = mapped_column(Text, nullable=False, default="explicit")
    filter: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class SectionPoolItem(Base):
    __tablename__ = "section_pool_item"
    __table_args__ = (
        UniqueConstraint("pool_id", "question_version_id"),
        UniqueConstraint("pool_id", "ordinal"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.id", ondelete="CASCADE"), nullable=False
    )
    pool_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("section_pool.id", ondelete="CASCADE"), nullable=False
    )
    question_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("question_version.id", ondelete="RESTRICT"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)


class AccommodationProfile(Base):
    __tablename__ = "accommodation_profile"
    __table_args__ = (UniqueConstraint("org_id", "name"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    overrides: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(TZ, nullable=False, server_default=func.now())


class ExamAssignment(Base):
    __tablename__ = "exam_assignment"
    __table_args__ = (UniqueConstraint("exam_version_id", "candidate_user_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.id", ondelete="CASCADE"), nullable=False
    )
    exam_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("exam_version.id", ondelete="RESTRICT"), nullable=False
    )
    candidate_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    accommodation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accommodation_profile.id")
    )
    opens_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    closes_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    extra_time_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempts_allowed: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    assigned_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id"), nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(TZ, nullable=False, server_default=func.now())

    exam_version: Mapped[ExamVersion] = relationship()


class ExamSession(Base):
    """STATUS: mapped for Phase 1 read paths only.

    Session lifecycle (start, resume, submit) is Phase 2. Nothing in Phase 1
    writes to this table.
    """

    __tablename__ = "exam_session"
    __table_args__ = (
        UniqueConstraint("assignment_id", "attempt_no"),
        CheckConstraint("evidence_count >= 0", name="session_evidence_count_nonneg"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.id", ondelete="CASCADE"), nullable=False
    )
    assignment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("exam_assignment.id", ondelete="RESTRICT"), nullable=False
    )
    exam_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("exam_version.id", ondelete="RESTRICT"), nullable=False
    )
    candidate_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(session_status, nullable=False, default="created")
    paper_seed: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    started_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    deadline_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    submitted_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    submit_reason: Mapped[str | None] = mapped_column(submit_reason)
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    chain_head_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chain_head_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    signing_key_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    chain_status: Mapped[str] = mapped_column(chain_state, nullable=False, default="unverified")
    chain_verified_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    evidence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    evidence_cap: Mapped[int] = mapped_column(Integer, nullable=False, default=40)
    evidence_capped_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    review_status: Mapped[str] = mapped_column(review_state, nullable=False, default="not_required")
    user_agent: Mapped[str | None] = mapped_column(String)
    ip_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    created_at: Mapped[dt.datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(TZ, nullable=False, server_default=func.now())


# ---------------------------------------------------------------------------
# Delivery: the materialized paper, answers, results
# ---------------------------------------------------------------------------


class SessionPaperItem(Base):
    """One question as it was actually served to one candidate.

    The paper is materialized at session start rather than recomputed on every
    request. Recomputation would mean the candidate's paper depends on the code
    that happens to be deployed when they refresh; materialization means it
    depends on one row written once. ``paper.py`` still keeps generation pure and
    deterministic so the stored paper can be *re-derived and compared*, which is
    what proves recovery is faithful rather than merely quiet.
    """

    __tablename__ = "session_paper_item"
    __table_args__ = (
        UniqueConstraint("session_id", "item_ordinal"),
        UniqueConstraint("session_id", "question_version_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("exam_session.id", ondelete="CASCADE"), nullable=False
    )
    section_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("exam_section.id", ondelete="RESTRICT"), nullable=False
    )
    pool_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("section_pool.id", ondelete="RESTRICT")
    )
    question_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("question_version.id", ondelete="RESTRICT"), nullable=False
    )
    section_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    item_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    option_order: Mapped[list[int] | None] = mapped_column(ARRAY(Integer))
    marks: Mapped[float] = mapped_column(Numeric(8, 3), nullable=False)


class Answer(Base):
    """The current answer for one paper item. One row per item, updated in place.

    History lives in :class:`AnswerRevision`, which is append-only at the
    database level. Keeping "current" and "history" apart means the hot autosave
    path is a single upsert, while the record of what was saved when survives it.
    """

    __tablename__ = "answer"
    __table_args__ = (UniqueConstraint("session_id", "paper_item_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("exam_session.id", ondelete="CASCADE"), nullable=False
    )
    paper_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("session_paper_item.id", ondelete="CASCADE"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    is_flagged_by_candidate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    time_spent_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    client_saved_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    saved_at: Mapped[dt.datetime] = mapped_column(TZ, nullable=False, server_default=func.now())


class AnswerRevision(Base):
    """Append-only autosave history. Protected by a BEFORE UPDATE OR DELETE trigger."""

    __tablename__ = "answer_revision"
    __table_args__ = (UniqueConstraint("session_id", "paper_item_id", "revision"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("exam_session.id", ondelete="CASCADE"), nullable=False
    )
    paper_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("session_paper_item.id", ondelete="CASCADE"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    saved_at: Mapped[dt.datetime] = mapped_column(TZ, nullable=False, server_default=func.now())


class SessionResult(Base):
    """Auto-graded totals.

    There is deliberately no foreign key, column, or code path from
    ``risk_assessment`` to this table. ``test_no_path_from_risk_to_score``
    asserts it, because the whole product claim rests on a detector never
    touching a mark.
    """

    __tablename__ = "session_result"

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("exam_session.id", ondelete="CASCADE"),
        primary_key=True,
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.id", ondelete="CASCADE"), nullable=False
    )
    total_marks: Mapped[float] = mapped_column(Numeric(10, 3), nullable=False)
    max_marks: Mapped[float] = mapped_column(Numeric(10, 3), nullable=False)
    # Generated column; read-only from the ORM's point of view.
    percentage: Mapped[float | None] = mapped_column(
        Numeric(6, 3), server_default=None, nullable=True
    )
    auto_graded_at: Mapped[dt.datetime] = mapped_column(
        TZ, nullable=False, server_default=func.now()
    )
    released_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    released_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id")
    )


class QuestionScore(Base):
    __tablename__ = "question_score"
    __table_args__ = (UniqueConstraint("session_id", "paper_item_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("exam_session.id", ondelete="CASCADE"), nullable=False
    )
    paper_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("session_paper_item.id", ondelete="CASCADE"), nullable=False
    )
    awarded: Mapped[float] = mapped_column(Numeric(8, 3), nullable=False)
    max_marks: Mapped[float] = mapped_column(Numeric(8, 3), nullable=False)
    grader: Mapped[str] = mapped_column(Text, nullable=False, default="auto")
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    graded_at: Mapped[dt.datetime] = mapped_column(TZ, nullable=False, server_default=func.now())


# ---------------------------------------------------------------------------
# The code judge
# ---------------------------------------------------------------------------


class CodeSubmission(Base):
    """One press of "run" or "submit" on a coding question.

    The source is stored verbatim, and its SHA-256 alongside. The hash is what
    lets a later run be recognised as a duplicate of an earlier one without
    comparing 256 KiB of text, and it is what the audit chain will seal in
    Phase 5 — a submission whose source changed after the fact must be
    detectable.
    """

    __tablename__ = "code_submission"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("exam_session.id", ondelete="CASCADE"), nullable=False
    )
    paper_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("session_paper_item.id", ondelete="CASCADE"), nullable=False
    )
    language: Mapped[str] = mapped_column(judge_language, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_sha256: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    source_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    #: `sample` runs only the tests the candidate can already see; `final` runs
    #: the hidden suite and is what scores.
    mode: Mapped[str] = mapped_column(run_mode, nullable=False, default="sample")
    created_at: Mapped[dt.datetime] = mapped_column(TZ, nullable=False, server_default=func.now())


class JudgeRun(Base):
    __tablename__ = "judge_run"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.id", ondelete="CASCADE"), nullable=False
    )
    submission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("code_submission.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(judge_status, nullable=False, default="queued")
    worker_id: Mapped[str | None] = mapped_column(Text)
    #: The image the sandbox actually used, not the one configured. A result
    #: that cannot say which sandbox produced it is not reproducible.
    image_digest: Mapped[str | None] = mapped_column(Text)
    queued_at: Mapped[dt.datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    started_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    finished_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    queue_delay_ms: Mapped[int | None] = mapped_column(Integer)
    compile_ms: Mapped[int | None] = mapped_column(Integer)
    execute_ms: Mapped[int | None] = mapped_column(Integer)
    peak_memory_kb: Mapped[int | None] = mapped_column(Integer)
    compile_output: Mapped[str | None] = mapped_column(Text)
    error_detail: Mapped[str | None] = mapped_column(Text)
    tests_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tests_passed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: The fraction of test weight passed, 0..1. Multiplied by the question's
    #: marks at grading time; deliberately not marks, so a question's weight can
    #: change without rewriting judge history.
    score: Mapped[float | None] = mapped_column(Numeric(8, 3))


class JudgeTestResult(Base):
    __tablename__ = "judge_test_result"
    __table_args__ = (UniqueConstraint("run_id", "test_case_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("judge_run.id", ondelete="CASCADE"), nullable=False
    )
    test_case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("test_case.id", ondelete="RESTRICT"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(judge_status, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    memory_kb: Mapped[int | None] = mapped_column(Integer)
    #: Populated for sample tests only. A hidden test's output is the answer key
    #: and is never written here — see `runner._visible`.
    stdout_excerpt: Mapped[str | None] = mapped_column(Text)
    stderr_excerpt: Mapped[str | None] = mapped_column(Text)


class ActivityLog(Base):
    __tablename__ = "activity_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.id", ondelete="RESTRICT")
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id")
    )
    actor_role: Mapped[str | None] = mapped_column(member_role)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    object_type: Mapped[str | None] = mapped_column(Text)
    object_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    ip_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    occurred_at: Mapped[dt.datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
