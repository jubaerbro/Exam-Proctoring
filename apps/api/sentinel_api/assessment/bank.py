"""Question bank: creation, versioning, and bulk import/export.

The versioning rule is the important part. A `question_version` that has been
published is **immutable**: editing a question creates version *n+1* and leaves
*n* alone. This is not tidiness. A pool references a `question_version_id`, and a
session's paper references the same id — so a candidate who sat an exam in March
can be shown exactly the question they answered, with exactly the marks it
carried, even if the instructor rewrote it in April. Mutating a published
version in place would rewrite history for every session that used it, silently.

Draft versions are editable in place, because nothing can reference them: a pool
that lists a draft version is rejected at publish time.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sentinel_api.assessment.schemas import (
    BodyError,
    ImportItemError,
    ImportRequest,
    ImportResponse,
    QuestionDraft,
    parse_body,
)
from sentinel_api.core.errors import Conflict, NotFound, ValidationFailed
from sentinel_api.models import Question, QuestionBank, QuestionVersion, TestCase


def create_bank(
    session: Session, *, org_id: uuid.UUID, actor_id: uuid.UUID, name: str, description: str | None
) -> QuestionBank:
    existing = session.scalar(select(QuestionBank).where(QuestionBank.name == name))
    if existing is not None:
        raise Conflict(f"A question bank named {name!r} already exists.")
    bank = QuestionBank(org_id=org_id, name=name, description=description, created_by=actor_id)
    session.add(bank)
    session.flush()
    return bank


def get_bank(session: Session, bank_id: uuid.UUID) -> QuestionBank:
    bank = session.get(QuestionBank, bank_id)
    if bank is None:
        # RLS already hid another tenant's bank; 404 rather than 403 so the
        # response does not confirm that the id exists somewhere.
        raise NotFound("Question bank not found.")
    return bank


def question_counts(session: Session) -> dict[uuid.UUID, int]:
    rows = session.execute(
        select(Question.bank_id, func.count(Question.id)).group_by(Question.bank_id)
    ).all()
    return dict(rows)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Versioning
# ---------------------------------------------------------------------------


def add_version(
    session: Session,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    bank_id: uuid.UUID,
    draft: QuestionDraft,
    question: Question | None = None,
    publish: bool = True,
) -> QuestionVersion:
    """Create question version *n+1*, or version 1 with a new question.

    Raises :class:`BodyError` if the body does not match the kind. The caller
    decides whether that aborts the request (single create) or is collected into
    a per-row report (bulk import).
    """
    parse_body(draft.kind, draft.body)  # raises BodyError with /body/... pointers

    if question is None:
        question = Question(
            org_id=org_id,
            bank_id=bank_id,
            kind=draft.kind,
            external_key=draft.external_key,
            created_by=actor_id,
        )
        session.add(question)
        session.flush()
        next_version = 1
    else:
        if question.kind != draft.kind:
            # Changing the kind changes what a stored answer *means*. A new
            # question is the honest representation of that, not a new version.
            raise BodyError(
                [
                    {
                        "pointer": "/kind",
                        "message": f"question already exists with kind {question.kind!r}; "
                        "a question's kind cannot change between versions",
                    }
                ]
            )
        current = session.scalar(
            select(func.max(QuestionVersion.version)).where(
                QuestionVersion.question_id == question.id
            )
        )
        next_version = (current or 0) + 1

    now = dt.datetime.now(dt.UTC)
    version = QuestionVersion(
        org_id=org_id,
        question_id=question.id,
        version=next_version,
        status="published" if publish else "draft",
        prompt=draft.prompt,
        prompt_format=draft.prompt_format,
        body=draft.body,
        explanation=draft.explanation,
        marks=draft.marks,
        negative_marks=draft.negative_marks,
        partial_credit=draft.partial_credit,
        difficulty=draft.difficulty,
        tags=draft.tags,
        meta=draft.metadata,
        created_by=actor_id,
        published_at=now if publish else None,
    )
    session.add(version)
    session.flush()

    for tc in draft.test_cases:
        session.add(
            TestCase(
                org_id=org_id,
                question_version_id=version.id,
                ordinal=tc.ordinal,
                is_sample=tc.is_sample,
                weight=tc.weight,
                stdin_inline=tc.stdin_inline,
                expected_inline=tc.expected_inline,
                comparator=tc.comparator,
                comparator_config=tc.comparator_config,
                time_limit_ms=tc.time_limit_ms,
                memory_limit_mb=tc.memory_limit_mb,
            )
        )

    question.current_version_id = version.id
    question.updated_at = now
    session.flush()
    return version


def find_by_external_key(
    session: Session, *, bank_id: uuid.UUID, external_key: str
) -> Question | None:
    return session.scalar(
        select(Question).where(Question.bank_id == bank_id, Question.external_key == external_key)
    )


# ---------------------------------------------------------------------------
# Bulk import
# ---------------------------------------------------------------------------


def import_questions(
    session: Session, *, org_id: uuid.UUID, actor_id: uuid.UUID, request: ImportRequest
) -> ImportResponse:
    """Import a batch, reporting every bad row rather than the first one.

    Nobody types 500 questions into a form (MVP_SCOPE P0-05), and nobody wants
    to fix a 500-question file one error per round trip. Rows are validated
    independently and reported with `index`, `external_key` and a JSON Pointer,
    so a single response is enough to fix the whole file.

    The transaction is all-or-nothing: if any row fails, nothing is written. A
    half-imported bank is worse than a rejected file, because the operator has
    no way to tell which half.
    """
    get_bank(session, request.bank_id)  # 404s for another tenant's bank

    errors: list[ImportItemError] = []
    version_ids: list[uuid.UUID] = []
    created = updated = skipped = 0

    seen_keys: dict[str, int] = {}
    for index, draft in enumerate(request.questions):
        if draft.external_key:
            if draft.external_key in seen_keys:
                errors.append(
                    ImportItemError(
                        index=index,
                        external_key=draft.external_key,
                        pointer="/external_key",
                        message=f"duplicated within this file (first seen at index "
                        f"{seen_keys[draft.external_key]})",
                    )
                )
                continue
            seen_keys[draft.external_key] = index

        existing = (
            find_by_external_key(session, bank_id=request.bank_id, external_key=draft.external_key)
            if draft.external_key
            else None
        )
        if existing is not None and not request.upsert:
            skipped += 1
            continue

        try:
            version = add_version(
                session,
                org_id=org_id,
                actor_id=actor_id,
                bank_id=request.bank_id,
                draft=draft,
                question=existing,
                publish=request.publish,
            )
        except BodyError as exc:
            errors.extend(
                ImportItemError(
                    index=index,
                    external_key=draft.external_key,
                    pointer=e["pointer"],
                    message=e["message"],
                )
                for e in exc.errors
            )
            continue

        version_ids.append(version.id)
        if existing is None:
            created += 1
        else:
            updated += 1

    if errors:
        session.rollback()
        raise ValidationFailed(
            f"{len(errors)} problem(s) in {len({e.index for e in errors})} question(s); "
            "nothing was imported.",
            errors=[e.model_dump() for e in errors],
        )

    return ImportResponse(
        created=created,
        updated=updated,
        skipped=skipped,
        version_ids=version_ids,
        errors=[],
    )


def export_bank(session: Session, *, bank_id: uuid.UUID) -> dict[str, Any]:
    """Export the current published version of every question in a bank.

    Round-trips through :class:`ImportRequest`: the output of this function,
    with a `bank_id` added, is a valid import body. `test_import_export_round_trip`
    asserts it, because an export format that cannot be re-imported is a backup
    that cannot be restored.
    """
    get_bank(session, bank_id)

    rows = session.execute(
        select(Question, QuestionVersion)
        .join(QuestionVersion, QuestionVersion.id == Question.current_version_id)
        .where(Question.bank_id == bank_id)
        # Ordered by external_key first, and only then by insertion order.
        # Rows created in one transaction share a `created_at` (it is the
        # transaction timestamp), so ordering by time alone falls back to a
        # random uuid and shuffles the file on every export. A stable order is
        # what makes an exported bank diff cleanly in version control, which is
        # most of why anyone exports one.
        .order_by(Question.external_key.nulls_last(), Question.created_at, Question.id)
    ).all()

    questions = []
    for question, version in rows:
        cases = session.scalars(
            select(TestCase)
            .where(TestCase.question_version_id == version.id)
            .order_by(TestCase.ordinal)
        ).all()
        questions.append(
            {
                "external_key": question.external_key,
                "kind": question.kind,
                "prompt": version.prompt,
                "prompt_format": version.prompt_format,
                "body": version.body,
                "explanation": version.explanation,
                "marks": float(version.marks),
                "negative_marks": float(version.negative_marks),
                "partial_credit": version.partial_credit,
                "difficulty": version.difficulty,
                "tags": list(version.tags),
                "metadata": version.meta,
                "test_cases": [
                    {
                        "ordinal": c.ordinal,
                        "is_sample": c.is_sample,
                        "weight": float(c.weight),
                        "stdin_inline": c.stdin_inline,
                        "expected_inline": c.expected_inline or "",
                        "comparator": c.comparator,
                        "comparator_config": c.comparator_config,
                        "time_limit_ms": c.time_limit_ms,
                        "memory_limit_mb": c.memory_limit_mb,
                    }
                    for c in cases
                ],
            }
        )

    return {"schema_version": "question.v1", "questions": questions}
