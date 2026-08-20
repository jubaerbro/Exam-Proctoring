"""Seed development data.

Creates exactly what Phase 1 promised: one organization, one instructor, one
reviewer, five candidates, one published exam.

Refuses to run unless SENTINEL_ENV=development. Seeding a staging or production
database with known credentials would be a straightforward way to hand someone
an instructor account.

Idempotent: re-running updates nothing and creates nothing twice.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "api"))

from sqlalchemy import select, text  # noqa: E402

from sentinel_api.auth.passwords import hash_password, validate_password  # noqa: E402
from sentinel_api.core.config import get_settings  # noqa: E402
from sentinel_api.core.db import system_session  # noqa: E402
from sentinel_api.models import (  # noqa: E402
    AppUser,
    Exam,
    ExamAssignment,
    ExamSection,
    ExamVersion,
    Membership,
    Organization,
    Question,
    QuestionBank,
    QuestionVersion,
    SectionPool,
    SectionPoolItem,
    TestCase,
)

CANDIDATE_NAMES = [
    ("aisha.rahman", "Aisha Rahman"),
    ("daniel.okafor", "Daniel Okafor"),
    ("mei.tanaka", "Mei Tanaka"),
    ("luis.ferreira", "Luis Ferreira"),
    ("nadia.hassan", "Nadia Hassan"),
]


def main() -> int:
    settings = get_settings()

    if settings.sentinel_env != "development":
        print(
            f"REFUSING TO SEED: SENTINEL_ENV={settings.sentinel_env}. "
            "Seed data uses known credentials and belongs only in development.",
            file=sys.stderr,
        )
        return 2

    password = settings.seed_password or os.getenv("SEED_PASSWORD", "")
    if not password:
        print("REFUSING TO SEED: SEED_PASSWORD is not set.", file=sys.stderr)
        return 2
    try:
        validate_password(password, min_length=settings.password_min_length)
    except ValueError as exc:
        print(f"REFUSING TO SEED: {exc}", file=sys.stderr)
        return 2

    slug = settings.seed_org_slug
    domain = f"{slug}.example.edu"

    with system_session() as session:
        if session.scalar(select(Organization).where(Organization.slug == slug)):
            print(f"Organization '{slug}' already exists; nothing to do.")
            return 0

        org = Organization(slug=slug, name="Demo University", status="active")
        session.add(org)
        session.flush()

        # RLS is on. The seed writes tenant-owned rows, so it must declare the
        # tenant like any other caller — no exemption for setup scripts.
        session.execute(
            text("SELECT set_config('sentinel.org_id', :o, true)"), {"o": str(org.id)}
        )
        session.execute(text("SELECT set_config('sentinel.role', 'org_admin', true)"))

        pw = hash_password(password)
        now = dt.datetime.now(dt.UTC)

        admin = _user(session, f"admin@{domain}", "Grace Adeyemi", pw)
        instructor = _user(session, f"instructor@{domain}", "Dr Priya Menon", pw)
        reviewer = _user(session, f"reviewer@{domain}", "Tom Whitfield", pw)
        candidates = [
            _user(session, f"{handle}@{domain}", name, pw) for handle, name in CANDIDATE_NAMES
        ]

        session.execute(
            text("SELECT set_config('sentinel.user_id', :u, true)"), {"u": str(instructor.id)}
        )

        _member(session, org.id, admin.id, "org_admin", "STAFF-0000")
        _member(session, org.id, instructor.id, "instructor", "STAFF-0001")
        _member(session, org.id, reviewer.id, "reviewer", "STAFF-0002")
        for i, cand in enumerate(candidates, start=1):
            _member(session, org.id, cand.id, "candidate", f"STU-{i:04d}")

        bank = QuestionBank(
            org_id=org.id,
            name="Introductory Programming",
            description="Seed bank for the demo exam.",
            created_by=instructor.id,
        )
        session.add(bank)
        session.flush()

        versions = _questions(session, org.id, bank.id, instructor.id)

        exam = Exam(
            org_id=org.id,
            title="Programming Fundamentals — Final",
            description="Demo assessment covering the five supported question types.",
            owner_id=instructor.id,
        )
        session.add(exam)
        session.flush()

        version = ExamVersion(
            org_id=org.id,
            exam_id=exam.id,
            version=1,
            status="published",
            delivery="window",
            duration_seconds=5400,
            grace_seconds=60,
            opens_at=now - dt.timedelta(hours=1),
            closes_at=now + dt.timedelta(days=7),
            results_release_at=now + dt.timedelta(days=8),
            max_attempts=1,
            navigation="free",
            integrity_enabled=True,
            integrity_config={
                "browser_signals": True,
                "webcam": False,  # CV detectors are Phase 7 and NOT IMPLEMENTED
                "fullscreen_required": True,
                "large_paste_chars": 200,
            },
            judge_enabled=True,
            published_at=now,
            published_by=instructor.id,
        )
        session.add(version)
        session.flush()
        exam.current_version_id = version.id

        _sections(session, org.id, version.id, versions)

        for cand in candidates:
            session.add(
                ExamAssignment(
                    org_id=org.id,
                    exam_version_id=version.id,
                    candidate_user_id=cand.id,
                    attempts_allowed=1,
                    assigned_by=instructor.id,
                )
            )

    print(
        f"""
Seeded organization '{slug}'.

  org admin   admin@{domain}
  instructor  instructor@{domain}
  reviewer    reviewer@{domain}
  candidates  {", ".join(f"{h}@{domain}" for h, _ in CANDIDATE_NAMES)}

  password    (SEED_PASSWORD from your environment)

Exam: "Programming Fundamentals — Final", published, 90 minutes,
2 sections, 8 questions (one section selects 2 of 3 from a pool),
1 coding question with 2 sample and 4 hidden test cases.

NOTE: the org admin and reviewer accounts require MFA (MFA_REQUIRED_ROLES) and
have no authenticator enrolled. Signing in returns 403 with an `enrolment_token`
in the problem document; POST it to /api/v1/auth/mfa/enrol, then
/api/v1/auth/mfa/enrol/confirm with a code from your authenticator, then sign in
again. Fail-closed is preserved: no session is issued until a factor exists.
"""
    )
    return 0


# ---------------------------------------------------------------------------


def _user(session, email: str, name: str, pw_hash: str) -> AppUser:
    user = AppUser(
        email=email.lower(),
        display_name=name,
        password_hash=pw_hash,
        password_changed_at=dt.datetime.now(dt.UTC),
        email_verified_at=dt.datetime.now(dt.UTC),
        status="active",
    )
    session.add(user)
    session.flush()
    return user


def _member(session, org_id, user_id, role: str, ref: str) -> None:
    session.add(
        Membership(org_id=org_id, user_id=user_id, role=role, status="active", external_ref=ref)
    )


def _qv(session, org_id, bank_id, author, kind, prompt, body, marks, **kw) -> QuestionVersion:
    q = Question(
        org_id=org_id,
        bank_id=bank_id,
        kind=kind,
        external_key=kw.pop("key", None),
        created_by=author,
    )
    session.add(q)
    session.flush()
    qv = QuestionVersion(
        org_id=org_id,
        question_id=q.id,
        version=1,
        status="published",
        prompt=prompt,
        body=body,
        marks=marks,
        created_by=author,
        published_at=dt.datetime.now(dt.UTC),
        **kw,
    )
    session.add(qv)
    session.flush()
    q.current_version_id = qv.id
    return qv


def _questions(session, org_id, bank_id, author) -> dict[str, list[QuestionVersion]]:
    single = [
        _qv(session, org_id, bank_id, author, "single_choice",
            "What is the time complexity of binary search on a sorted array of n elements?",
            {"options": [{"id": "a", "text": "O(1)"}, {"id": "b", "text": "O(log n)"},
                         {"id": "c", "text": "O(n)"}, {"id": "d", "text": "O(n log n)"}],
             "correct": ["b"], "shuffle_options": True},
            2, difficulty=2, tags=["complexity", "search"], key="Q-COMPLEXITY-BSEARCH"),
        _qv(session, org_id, bank_id, author, "single_choice",
            "Which data structure gives O(1) average-case lookup by key?",
            {"options": [{"id": "a", "text": "Linked list"}, {"id": "b", "text": "Hash table"},
                         {"id": "c", "text": "Binary search tree"}, {"id": "d", "text": "Array"}],
             "correct": ["b"], "shuffle_options": True},
            2, difficulty=1, tags=["data-structures"], key="Q-DS-HASH"),
        _qv(session, org_id, bank_id, author, "single_choice",
            "In Python, what does the `is` operator compare?",
            {"options": [{"id": "a", "text": "Value equality"},
                         {"id": "b", "text": "Object identity"},
                         {"id": "c", "text": "Type equality"},
                         {"id": "d", "text": "Hash equality"}],
             "correct": ["b"], "shuffle_options": True},
            2, difficulty=2, tags=["python"], key="Q-PY-IS"),
    ]
    multi = [
        _qv(session, org_id, bank_id, author, "multiple_choice",
            "Which of the following are stable sorting algorithms? Select all that apply.",
            {"options": [{"id": "a", "text": "Merge sort"}, {"id": "b", "text": "Quicksort"},
                         {"id": "c", "text": "Insertion sort"}, {"id": "d", "text": "Heapsort"}],
             "correct": ["a", "c"], "shuffle_options": True},
            3, partial_credit=True, difficulty=3, tags=["sorting"], key="Q-SORT-STABLE"),
        _qv(session, org_id, bank_id, author, "multiple_choice",
            "Which statements about HTTP status codes are correct? Select all that apply.",
            {"options": [{"id": "a", "text": "404 means the resource was not found"},
                         {"id": "b", "text": "500 indicates a client error"},
                         {"id": "c", "text": "201 indicates a resource was created"},
                         {"id": "d", "text": "302 is a permanent redirect"}],
             "correct": ["a", "c"], "shuffle_options": True},
            3, partial_credit=True, difficulty=2, tags=["http"], key="Q-HTTP-CODES"),
    ]
    short = _qv(
        session, org_id, bank_id, author, "short_answer",
        "Name the algorithm that finds the shortest path from a single source in a graph "
        "with non-negative edge weights.",
        {"accepted": ["dijkstra", "dijkstra's algorithm", "dijkstras algorithm"],
         "match": "ci", "trim": True},
        3, difficulty=3, tags=["graphs"], key="Q-GRAPH-DIJKSTRA")

    numeric = _qv(
        session, org_id, bank_id, author, "numeric",
        "A hash table has 1000 slots and holds 750 entries. What is its load factor? "
        "Answer to two decimal places.",
        {"value": 0.75, "tolerance": 0.01, "tolerance_kind": "abs", "unit": None},
        2, difficulty=1, tags=["data-structures"], key="Q-NUM-LOADFACTOR")

    coding = _qv(
        session, org_id, bank_id, author, "coding",
        "Read a line of space-separated integers from stdin and print the second largest "
        "distinct value. If fewer than two distinct values exist, print `NONE`.",
        {"languages": ["python311", "cpp20", "java17"],
         "starter": {"python311": "import sys\n\ndef main():\n    pass\n\nmain()\n"},
         "time_limit_ms": 2000, "memory_limit_mb": 256, "stdout_limit_kb": 64},
        10, partial_credit=True, difficulty=4, tags=["arrays", "implementation"],
        key="Q-CODE-SECONDMAX")

    cases = [
        (1, True, "4 1 7 3 7", "4"),
        (2, True, "5 5 5", "NONE"),
        (3, False, "1 2", "1"),
        (4, False, "9", "NONE"),
        (5, False, "-3 -1 -7 -1", "-3"),
        (6, False, " ".join(str(i) for i in range(1, 2001)), "1999"),
    ]
    for ordinal, is_sample, stdin, expected in cases:
        session.add(
            TestCase(
                org_id=org_id,
                question_version_id=coding.id,
                ordinal=ordinal,
                is_sample=is_sample,
                weight=1,
                stdin_inline=stdin,
                expected_inline=expected,
                comparator="trim_exact",
            )
        )

    return {"single": single, "multi": multi, "short": [short], "numeric": [numeric],
            "coding": [coding]}


def _sections(session, org_id, exam_version_id, qv: dict) -> None:
    # Section 1: fixed questions, one pool selecting 2 of 3 single-choice.
    s1 = ExamSection(
        org_id=org_id, exam_version_id=exam_version_id, ordinal=1,
        title="Concepts", instructions="Answer all questions in this section.",
        shuffle_questions=True,
    )
    session.add(s1)
    session.flush()

    pool_a = SectionPool(
        org_id=org_id, section_id=s1.id, ordinal=1, label="Single choice (2 of 3)",
        select_count=2, source_kind="explicit",
    )
    session.add(pool_a)
    session.flush()
    for i, v in enumerate(qv["single"], start=1):
        session.add(
            SectionPoolItem(org_id=org_id, pool_id=pool_a.id, question_version_id=v.id, ordinal=i)
        )

    pool_b = SectionPool(
        org_id=org_id, section_id=s1.id, ordinal=2, label="Multiple choice (all)",
        select_count=2, source_kind="explicit",
    )
    session.add(pool_b)
    session.flush()
    for i, v in enumerate(qv["multi"], start=1):
        session.add(
            SectionPoolItem(org_id=org_id, pool_id=pool_b.id, question_version_id=v.id, ordinal=i)
        )

    pool_c = SectionPool(
        org_id=org_id, section_id=s1.id, ordinal=3, label="Short and numeric",
        select_count=2, source_kind="explicit",
    )
    session.add(pool_c)
    session.flush()
    for i, v in enumerate(qv["short"] + qv["numeric"], start=1):
        session.add(
            SectionPoolItem(org_id=org_id, pool_id=pool_c.id, question_version_id=v.id, ordinal=i)
        )

    # Section 2: the coding question.
    s2 = ExamSection(
        org_id=org_id, exam_version_id=exam_version_id, ordinal=2,
        title="Programming",
        instructions="Your code runs in an isolated sandbox with no network access.",
        shuffle_questions=False,
    )
    session.add(s2)
    session.flush()

    pool_d = SectionPool(
        org_id=org_id, section_id=s2.id, ordinal=1, label="Coding", select_count=1,
        source_kind="explicit",
    )
    session.add(pool_d)
    session.flush()
    session.add(
        SectionPoolItem(
            org_id=org_id, pool_id=pool_d.id, question_version_id=qv["coding"][0].id, ordinal=1
        )
    )


def paper_seed(exam_id: uuid.UUID, candidate_id: uuid.UUID, version: int) -> bytes:
    """Deterministic per-candidate paper seed (ADR-0011).

    Phase 2 consumes this. Defined here so the seed script and the delivery
    code cannot drift apart on the definition.
    """
    return hashlib.sha256(f"{exam_id}|{candidate_id}|{version}".encode()).digest()


if __name__ == "__main__":
    raise SystemExit(main())
