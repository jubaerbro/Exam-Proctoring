"""The deterministic paper generator, tested as a pure function.

These tests need no database. That is deliberate: the property "the same
candidate always gets the same paper" must hold across process restarts, Python
upgrades and deployments, and a test that goes through the database would mostly
be testing the database.
"""

from __future__ import annotations

import uuid

import pytest

from sentinel_api.assessment.paper import (
    ExamBlueprint,
    PoolSpec,
    SectionSpec,
    generate_paper,
    paper_seed,
)

EXAM = uuid.UUID("11111111-1111-4111-8111-111111111111")
ALICE = uuid.UUID("22222222-2222-4222-8222-222222222222")
BOB = uuid.UUID("33333333-3333-4333-8333-333333333333")


def _blueprint(*, pool_size: int = 6, select: int = 3, shuffle: bool = True) -> ExamBlueprint:
    section_id = uuid.UUID("44444444-4444-4444-8444-444444444444")
    pool_id = uuid.UUID("55555555-5555-4555-8555-555555555555")
    items = tuple(
        (uuid.UUID(int=1000 + i), 5.0, 4) for i in range(pool_size)
    )  # 4 options each, equal marks
    return ExamBlueprint(
        exam_version_id=EXAM,
        sections=(
            SectionSpec(
                section_id=section_id,
                ordinal=1,
                shuffle_questions=shuffle,
                pools=(PoolSpec(pool_id=pool_id, ordinal=1, select_count=select, items=items),),
            ),
        ),
    )


def test_same_candidate_same_paper() -> None:
    """The property recovery depends on."""
    seed = paper_seed(EXAM, ALICE, 1)
    first = generate_paper(_blueprint(), seed)
    second = generate_paper(_blueprint(), seed)
    assert [i.question_version_id for i in first] == [i.question_version_id for i in second]
    assert [i.option_order for i in first] == [i.option_order for i in second]


def test_different_candidates_get_different_papers() -> None:
    """...and the property that makes `k of n` pools worth having.

    With 3 of 6 there are 20 possible question sets, so a collision between two
    specific candidates is possible and would not be a bug. The assertion is
    over a population: if 40 candidates all receive the identical paper, the
    seed is not reaching the generator.
    """
    papers = {
        tuple(
            i.question_version_id
            for i in generate_paper(_blueprint(), paper_seed(EXAM, uuid.uuid4(), 1))
        )
        for _ in range(40)
    }
    assert len(papers) > 1


def test_attempt_number_changes_the_paper() -> None:
    """A resit must not be the same paper as the first attempt."""
    one = generate_paper(_blueprint(), paper_seed(EXAM, ALICE, 1))
    two = generate_paper(_blueprint(), paper_seed(EXAM, ALICE, 2))
    assert [i.question_version_id for i in one] != [i.question_version_id for i in two] or [
        i.option_order for i in one
    ] != [i.option_order for i in two]


def test_selection_respects_k() -> None:
    items = generate_paper(_blueprint(select=2), paper_seed(EXAM, ALICE, 1))
    assert len(items) == 2
    assert len({i.question_version_id for i in items}) == 2


def test_ordinals_are_dense_and_one_based() -> None:
    items = generate_paper(_blueprint(select=5), paper_seed(EXAM, BOB, 1))
    assert [i.item_ordinal for i in items] == [1, 2, 3, 4, 5]


def test_option_order_is_a_permutation() -> None:
    items = generate_paper(_blueprint(), paper_seed(EXAM, ALICE, 1))
    for item in items:
        assert item.option_order is not None
        assert sorted(item.option_order) == [0, 1, 2, 3]


def test_no_option_shuffle_when_author_disabled_it() -> None:
    """`shufflable_option_count == 0` means the author said not to."""
    section_id = uuid.uuid4()
    pool_id = uuid.uuid4()
    blueprint = ExamBlueprint(
        exam_version_id=EXAM,
        sections=(
            SectionSpec(
                section_id=section_id,
                ordinal=1,
                shuffle_questions=False,
                pools=(
                    PoolSpec(
                        pool_id=pool_id,
                        ordinal=1,
                        select_count=2,
                        items=((uuid.UUID(int=1), 1.0, 0), (uuid.UUID(int=2), 1.0, 0)),
                    ),
                ),
            ),
        ),
    )
    items = generate_paper(blueprint, paper_seed(EXAM, ALICE, 1))
    assert all(i.option_order is None for i in items)
    # shuffle_questions=False and select_count == pool size means the pool order
    # is preserved exactly.
    assert [i.question_version_id for i in items] == [uuid.UUID(int=1), uuid.UUID(int=2)]


def test_editing_one_pool_does_not_reshuffle_another() -> None:
    """Sub-streams are labelled per pool, and this is what that buys.

    Adding a question to pool B must not change which questions pool A deals to
    a candidate whose session is already in flight.
    """
    section_id = uuid.UUID("66666666-6666-4666-8666-666666666666")
    pool_a = uuid.UUID("77777777-7777-4777-8777-777777777777")
    pool_b = uuid.UUID("88888888-8888-4888-8888-888888888888")

    def build(b_size: int) -> ExamBlueprint:
        return ExamBlueprint(
            exam_version_id=EXAM,
            sections=(
                SectionSpec(
                    section_id=section_id,
                    ordinal=1,
                    shuffle_questions=False,
                    pools=(
                        PoolSpec(
                            pool_id=pool_a,
                            ordinal=1,
                            select_count=2,
                            items=tuple((uuid.UUID(int=100 + i), 1.0, 0) for i in range(5)),
                        ),
                        PoolSpec(
                            pool_id=pool_b,
                            ordinal=2,
                            select_count=1,
                            items=tuple((uuid.UUID(int=200 + i), 1.0, 0) for i in range(b_size)),
                        ),
                    ),
                ),
            ),
        )

    seed = paper_seed(EXAM, ALICE, 1)
    before = generate_paper(build(3), seed)
    after = generate_paper(build(9), seed)
    assert [i.question_version_id for i in before[:2]] == [i.question_version_id for i in after[:2]]


def test_selection_is_uniform_enough_to_not_be_broken() -> None:
    """A modulo-biased sampler would starve the last questions in a pool.

    Not a statistical proof — a smoke test that every question in a pool of 6
    can actually appear when 1 is selected. A sampler that never deals question
    6 is a real bug and this catches it.
    """
    seen: set[uuid.UUID] = set()
    for _ in range(300):
        items = generate_paper(_blueprint(select=1), paper_seed(EXAM, uuid.uuid4(), 1))
        seen.update(i.question_version_id for i in items)
    assert len(seen) == 6


@pytest.mark.parametrize("attempt", [1, 2, 7])
def test_seed_is_stable_across_calls(attempt: int) -> None:
    assert paper_seed(EXAM, ALICE, attempt) == paper_seed(EXAM, ALICE, attempt)
    assert len(paper_seed(EXAM, ALICE, attempt)) == 32
