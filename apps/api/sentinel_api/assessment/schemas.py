"""Question bodies and the API request/response shapes for authoring.

The `body` column on `question_version` is `jsonb`, which the database cannot
meaningfully constrain. These models are that constraint. They are used in three
places — the CRUD endpoints, the bulk importer, and the graders — so a body that
reaches a grader has already been proven to have the fields the grader reads.

Validation errors surface as RFC 9457 problem documents with a JSON Pointer per
field (`core/errors.py`), which is why the importer works in terms of these
models rather than hand-rolled dict[str, Any] checks: `pointer` falls out of pydantic's
`loc` for free, and "row 41, field `body/correct`" is the difference between a
usable import error and a support ticket.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "question.v1"

# ---------------------------------------------------------------------------
# Question bodies, one model per kind
# ---------------------------------------------------------------------------


class Option(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=4000)


class ChoiceBody(BaseModel):
    """single_choice and multiple_choice share a body; `kind` decides arity."""

    model_config = ConfigDict(extra="forbid")

    options: list[Option] = Field(min_length=2, max_length=26)
    correct: list[str] = Field(min_length=1)
    shuffle_options: bool = True

    @model_validator(mode="after")
    def _check(self) -> ChoiceBody:
        ids = [o.id for o in self.options]
        if len(set(ids)) != len(ids):
            raise ValueError("option ids must be unique")
        unknown = [c for c in self.correct if c not in ids]
        if unknown:
            raise ValueError(f"correct references unknown option id(s): {sorted(unknown)}")
        if len(set(self.correct)) != len(self.correct):
            raise ValueError("correct must not repeat an option id")
        return self


class ShortAnswerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accepted: list[str] = Field(min_length=1, max_length=64)
    match: Literal["exact", "ci", "regex"] = "ci"
    trim: bool = True

    @model_validator(mode="after")
    def _check(self) -> ShortAnswerBody:
        if self.match == "regex":
            import re

            for pattern in self.accepted:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ValueError(f"invalid regular expression {pattern!r}: {exc}") from exc
                # A catastrophically backtracking pattern in a grader is a
                # denial of service against grading, triggered by content an
                # instructor typed months earlier. Length is a blunt guard, but
                # it is a guard.
                if len(pattern) > 512:
                    raise ValueError("regular expressions are limited to 512 characters")
        return self


class NumericBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: float
    tolerance: float = Field(default=0.0, ge=0)
    tolerance_kind: Literal["abs", "rel"] = "abs"
    unit: str | None = None

    @model_validator(mode="after")
    def _check(self) -> NumericBody:
        if self.tolerance_kind == "rel" and self.value == 0:
            raise ValueError(
                "relative tolerance is undefined for an expected value of 0; use tolerance_kind "
                "'abs'"
            )
        return self


class CodingBody(BaseModel):
    """Phase 3 executes these. Phase 2 only stores and serves them."""

    model_config = ConfigDict(extra="forbid")

    languages: list[Literal["python311", "cpp20", "java17"]] = Field(min_length=1)
    starter: dict[str, str] = Field(default_factory=dict)
    time_limit_ms: int = Field(default=2000, ge=100, le=60_000)
    memory_limit_mb: int = Field(default=256, ge=16, le=4096)
    stdout_limit_kb: int = Field(default=64, ge=1, le=8192)

    @model_validator(mode="after")
    def _check(self) -> CodingBody:
        unknown = set(self.starter) - set(self.languages)
        if unknown:
            raise ValueError(f"starter code for unlisted language(s): {sorted(unknown)}")
        return self


QuestionKind = Literal["single_choice", "multiple_choice", "short_answer", "numeric", "coding"]

_BODY_MODEL: dict[str, type[BaseModel]] = {
    "single_choice": ChoiceBody,
    "multiple_choice": ChoiceBody,
    "short_answer": ShortAnswerBody,
    "numeric": NumericBody,
    "coding": CodingBody,
}


class BodyError(ValueError):
    """Carries per-field pointers so the importer can report them positionally."""

    def __init__(self, errors: list[dict[str, str]]) -> None:
        self.errors = errors
        super().__init__("; ".join(f"{e['pointer']}: {e['message']}" for e in errors))


def _body_pointer(loc: tuple[int | str, ...]) -> str:
    """JSON Pointer for a pydantic error location, rooted at the draft's `body`."""
    return "/body" + ("".join(f"/{p}" for p in loc) if loc else "")


def parse_body(kind: str, body: dict[str, Any]) -> BaseModel:
    """Validate a raw body against the model for `kind`.

    Raises :class:`BodyError` with JSON Pointers relative to ``/body``.
    """
    from pydantic import ValidationError

    model = _BODY_MODEL.get(kind)
    if model is None:
        raise BodyError([{"pointer": "/kind", "message": f"unknown question kind {kind!r}"}])
    try:
        parsed = model.model_validate(body)
    except ValidationError as exc:
        raise BodyError(
            [{"pointer": _body_pointer(e["loc"]), "message": e["msg"]} for e in exc.errors()]
        ) from exc

    if kind == "single_choice":
        assert isinstance(parsed, ChoiceBody)  # noqa: S101 - narrowed by _BODY_MODEL
        if len(parsed.correct) != 1:
            raise BodyError(
                [
                    {
                        "pointer": "/body/correct",
                        "message": "single_choice must have exactly one correct option; "
                        "use multiple_choice for more",
                    }
                ]
            )
    if kind == "multiple_choice":
        assert isinstance(parsed, ChoiceBody)  # noqa: S101
        if len(parsed.correct) < 2:
            raise BodyError(
                [
                    {
                        "pointer": "/body/correct",
                        "message": "multiple_choice must have at least two correct options; "
                        "use single_choice for one",
                    }
                ]
            )
    return parsed


# ---------------------------------------------------------------------------
# Question bank API shapes
# ---------------------------------------------------------------------------


class BankCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)


class BankResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    question_count: int
    created_at: dt.datetime


class TestCaseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ordinal: int = Field(ge=1)
    is_sample: bool = False
    weight: float = Field(default=1.0, gt=0)
    stdin_inline: str | None = None
    expected_inline: str = Field(min_length=0)
    comparator: Literal["trim_exact", "token", "float_eps"] = "trim_exact"
    comparator_config: dict[str, Any] = Field(default_factory=dict)
    time_limit_ms: int | None = Field(default=None, ge=100, le=60_000)
    memory_limit_mb: int | None = Field(default=None, ge=16, le=4096)


class QuestionDraft(BaseModel):
    """One question as authored or imported. The unit of the import file."""

    model_config = ConfigDict(extra="forbid")

    external_key: str | None = Field(default=None, max_length=200)
    kind: QuestionKind
    prompt: str = Field(min_length=1, max_length=20_000)
    prompt_format: Literal["markdown", "text"] = "markdown"
    body: dict[str, Any]
    explanation: str | None = Field(default=None, max_length=20_000)
    marks: float = Field(gt=0, le=1000)
    negative_marks: float = Field(default=0.0, ge=0)
    partial_credit: bool = False
    difficulty: int | None = Field(default=None, ge=1, le=5)
    tags: list[str] = Field(default_factory=list, max_length=50)
    metadata: dict[str, Any] = Field(default_factory=dict)
    test_cases: list[TestCaseIn] = Field(default_factory=list)

    @model_validator(mode="after")
    def _cross_field(self) -> QuestionDraft:
        if self.negative_marks > self.marks:
            raise ValueError("negative_marks must not exceed marks")
        if self.kind == "coding":
            if not self.test_cases:
                raise ValueError("a coding question needs at least one test case")
            ordinals = [t.ordinal for t in self.test_cases]
            if len(set(ordinals)) != len(ordinals):
                raise ValueError("test case ordinals must be unique")
        elif self.test_cases:
            raise ValueError(
                f"test cases are only meaningful for coding questions, not {self.kind}"
            )
        return self


class QuestionVersionResponse(BaseModel):
    question_id: uuid.UUID
    version_id: uuid.UUID
    version: int
    status: str
    kind: str
    prompt: str
    body: dict[str, Any]
    marks: float
    negative_marks: float
    partial_credit: bool
    difficulty: int | None
    tags: list[str]
    external_key: str | None
    created_at: dt.datetime


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["question.v1"] = "question.v1"
    bank_id: uuid.UUID
    #: When true, an existing question with the same `external_key` gets a new
    #: version instead of being rejected as a duplicate.
    upsert: bool = True
    #: Publish each created version immediately. Draft versions cannot be put in
    #: an exam pool, so an import that is not published is not yet usable.
    publish: bool = True
    questions: list[QuestionDraft] = Field(min_length=1, max_length=2000)


class ImportItemError(BaseModel):
    index: int
    external_key: str | None
    pointer: str
    message: str


class ImportResponse(BaseModel):
    created: int
    updated: int
    skipped: int
    version_ids: list[uuid.UUID]
    errors: list[ImportItemError]


# ---------------------------------------------------------------------------
# Exam authoring API shapes
# ---------------------------------------------------------------------------


class ExamCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=8000)


class PoolIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ordinal: int = Field(ge=1)
    label: str | None = Field(default=None, max_length=200)
    #: k, in "select k of n".
    select_count: int = Field(ge=1)
    question_version_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def _check(self) -> PoolIn:
        if len(set(self.question_version_ids)) != len(self.question_version_ids):
            raise ValueError("a pool must not list the same question version twice")
        if self.select_count > len(self.question_version_ids):
            raise ValueError(
                f"select_count {self.select_count} exceeds the pool size "
                f"{len(self.question_version_ids)}"
            )
        return self


class SectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ordinal: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=300)
    instructions: str | None = Field(default=None, max_length=8000)
    shuffle_questions: bool = True
    time_limit_seconds: int | None = Field(default=None, gt=0, le=86_400)
    pools: list[PoolIn] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _check(self) -> SectionIn:
        ordinals = [p.ordinal for p in self.pools]
        if len(set(ordinals)) != len(ordinals):
            raise ValueError("pool ordinals must be unique within a section")
        return self


class ExamVersionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duration_seconds: int = Field(ge=60, le=86_400)
    grace_seconds: int = Field(default=60, ge=0, le=1800)
    delivery: Literal["scheduled", "window", "on_demand"] = "window"
    opens_at: dt.datetime | None = None
    closes_at: dt.datetime | None = None
    results_release_at: dt.datetime | None = None
    max_attempts: int = Field(default=1, ge=1, le=10)
    navigation: Literal["free", "sequential", "one_way"] = "free"
    show_score_on_submit: bool = False
    integrity_enabled: bool = False
    integrity_config: dict[str, Any] = Field(default_factory=dict)
    judge_enabled: bool = False
    sections: Annotated[list[SectionIn], Field(min_length=1, max_length=50)]

    @model_validator(mode="after")
    def _check(self) -> ExamVersionDraft:
        ordinals = [s.ordinal for s in self.sections]
        if len(set(ordinals)) != len(ordinals):
            raise ValueError("section ordinals must be unique")
        if self.opens_at and self.closes_at and self.closes_at <= self.opens_at:
            raise ValueError("closes_at must be after opens_at")
        return self


class ExamVersionResponse(BaseModel):
    exam_id: uuid.UUID
    version_id: uuid.UUID
    version: int
    status: str
    title: str
    duration_seconds: int
    grace_seconds: int
    navigation: str
    max_attempts: int
    opens_at: dt.datetime | None
    closes_at: dt.datetime | None
    integrity_enabled: bool
    judge_enabled: bool
    section_count: int
    question_count: int
    total_marks: float
    published_at: dt.datetime | None


class AssignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Either candidate user ids or their email addresses. Emails are what a
    #: registrar has in a spreadsheet; ids are what an integration has.
    candidate_user_ids: list[uuid.UUID] = Field(default_factory=list, max_length=5000)
    emails: list[str] = Field(default_factory=list, max_length=5000)
    opens_at: dt.datetime | None = None
    closes_at: dt.datetime | None = None
    extra_time_seconds: int = Field(default=0, ge=0, le=86_400)
    attempts_allowed: int = Field(default=1, ge=1, le=10)

    @model_validator(mode="after")
    def _check(self) -> AssignRequest:
        if not self.candidate_user_ids and not self.emails:
            raise ValueError("supply candidate_user_ids, emails, or both")
        return self


class AssignItemResult(BaseModel):
    identifier: str
    status: Literal["assigned", "already_assigned", "not_a_candidate", "unknown"]
    assignment_id: uuid.UUID | None = None


class AssignResponse(BaseModel):
    assigned: int
    skipped: int
    results: list[AssignItemResult]
