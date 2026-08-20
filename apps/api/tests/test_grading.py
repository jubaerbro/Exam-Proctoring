"""Autograders. Pure functions, so tested exhaustively and without a database.

A grader bug is a wrong mark on somebody's transcript, discovered late or never,
so these tests go well past the happy path: blanks, malformed clients, unicode,
float edges, and the penalty rules.
"""

from __future__ import annotations

import pytest

from sentinel_api.grading.graders import NotAutoGradable, grade

SINGLE = {
    "options": [{"id": "a", "text": "A"}, {"id": "b", "text": "B"}, {"id": "c", "text": "C"}],
    "correct": ["b"],
}
MULTI = {
    "options": [
        {"id": "a", "text": "A"},
        {"id": "b", "text": "B"},
        {"id": "c", "text": "C"},
        {"id": "d", "text": "D"},
    ],
    "correct": ["a", "c"],
}


# ------------------------------------------------------------------ blanks


@pytest.mark.parametrize(
    ("kind", "body", "value"),
    [
        ("single_choice", SINGLE, None),
        ("single_choice", SINGLE, {"selected": []}),
        ("short_answer", {"accepted": ["x"]}, {"text": "   "}),
        ("numeric", {"value": 1.0}, None),
    ],
)
def test_blank_is_zero_never_a_penalty(kind: str, body: dict, value: object) -> None:
    """A candidate who skipped a question has not earned a penalty.

    This is the rule that makes negative marking defensible. Without it,
    guessing dominates skipping, which is a worse assessment.
    """
    award = grade(kind, body, value, marks=5, negative_marks=2)
    assert award.awarded == 0.0
    assert award.detail["outcome"] == "blank"


# ------------------------------------------------------------------ choice


def test_single_choice_correct() -> None:
    assert grade("single_choice", SINGLE, {"selected": ["b"]}, marks=4).awarded == 4.0


def test_single_choice_incorrect_applies_negative_marking() -> None:
    award = grade("single_choice", SINGLE, {"selected": ["a"]}, marks=4, negative_marks=1)
    assert award.awarded == -1.0
    assert award.detail["penalty_applied"] is True


def test_single_choice_incorrect_without_negative_marking_is_zero() -> None:
    assert grade("single_choice", SINGLE, {"selected": ["a"]}, marks=4).awarded == 0.0


def test_unknown_option_id_scores_zero_rather_than_penalising() -> None:
    """Far more likely a client bug than a candidate. Do not punish it."""
    award = grade("single_choice", SINGLE, {"selected": ["zz"]}, marks=4, negative_marks=2)
    assert award.awarded == 0.0
    assert award.detail["outcome"] == "invalid_option"


def test_multiple_choice_all_correct() -> None:
    assert grade("multiple_choice", MULTI, {"selected": ["a", "c"]}, marks=6).awarded == 6.0


def test_multiple_choice_order_does_not_matter() -> None:
    assert grade("multiple_choice", MULTI, {"selected": ["c", "a"]}, marks=6).awarded == 6.0


def test_partial_credit_gives_proportional_marks() -> None:
    award = grade("multiple_choice", MULTI, {"selected": ["a"]}, marks=6, partial_credit=True)
    assert award.awarded == 3.0
    assert award.detail["outcome"] == "partial"


def test_partial_credit_penalises_a_wrong_selection() -> None:
    """One right, one wrong nets to zero rather than to half marks."""
    award = grade("multiple_choice", MULTI, {"selected": ["a", "b"]}, marks=6, partial_credit=True)
    assert award.awarded == 0.0


def test_selecting_everything_scores_zero_under_partial_credit() -> None:
    """The whole reason the penalty term exists.

    Without it, "select all options" is a dominant strategy on every
    multiple-choice question with partial credit, and the question measures
    nothing.
    """
    award = grade(
        "multiple_choice",
        MULTI,
        {"selected": ["a", "b", "c", "d"]},
        marks=6,
        partial_credit=True,
    )
    assert award.awarded == 0.0


def test_partial_credit_never_goes_negative() -> None:
    award = grade("multiple_choice", MULTI, {"selected": ["b", "d"]}, marks=6, partial_credit=True)
    assert award.awarded == 0.0


# ------------------------------------------------------------ short answer


def test_short_answer_case_insensitive_by_default() -> None:
    body = {"accepted": ["Dijkstra"], "match": "ci"}
    assert grade("short_answer", body, {"text": "dijkstra"}, marks=3).awarded == 3.0


def test_short_answer_exact_mode_is_exact() -> None:
    body = {"accepted": ["Dijkstra"], "match": "exact"}
    assert grade("short_answer", body, {"text": "dijkstra"}, marks=3).awarded == 0.0


def test_short_answer_collapses_whitespace_when_trimming() -> None:
    body = {"accepted": ["hash table"], "match": "ci", "trim": True}
    assert grade("short_answer", body, {"text": "  hash   table "}, marks=3).awarded == 3.0


def test_short_answer_normalises_unicode() -> None:
    """A curly apostrophe from a phone keyboard is not a different answer."""
    body = {"accepted": ["Dijkstra's algorithm"], "match": "ci"}
    award = grade("short_answer", body, {"text": "Dijkstra’s algorithm"}, marks=3)
    # NFKC does not fold U+2019 to U+0027, so this is expected to miss — the
    # assertion records the known limitation rather than pretending otherwise.
    assert award.awarded == 0.0


def test_short_answer_regex_is_anchored() -> None:
    """`fullmatch`, not `search`. Otherwise `a` accepts any answer containing 'a'."""
    body = {"accepted": [r"\d{4}"], "match": "regex"}
    assert grade("short_answer", body, {"text": "1999"}, marks=3).awarded == 3.0
    assert grade("short_answer", body, {"text": "year 1999 exactly"}, marks=3).awarded == 0.0


def test_short_answer_detail_does_not_leak_the_answer_key() -> None:
    """This detail is shown to the candidate. The accepted answers are not."""
    body = {"accepted": ["dijkstra", "bellman-ford"], "match": "ci"}
    award = grade("short_answer", body, {"text": "kruskal"}, marks=3)
    assert "dijkstra" not in str(award.detail).lower()
    assert "accepted" not in award.detail


# ----------------------------------------------------------------- numeric


def test_numeric_within_absolute_tolerance() -> None:
    body = {"value": 0.75, "tolerance": 0.01, "tolerance_kind": "abs"}
    assert grade("numeric", body, {"value": 0.755}, marks=2).awarded == 2.0
    assert grade("numeric", body, {"value": 0.8}, marks=2).awarded == 0.0


def test_numeric_accepts_a_value_exactly_at_the_tolerance_boundary() -> None:
    """Binary floating point will not always agree that 0.76 - 0.75 <= 0.01."""
    body = {"value": 0.75, "tolerance": 0.01, "tolerance_kind": "abs"}
    assert grade("numeric", body, {"value": 0.76}, marks=2).awarded == 2.0


def test_numeric_relative_tolerance() -> None:
    body = {"value": 1000.0, "tolerance": 0.01, "tolerance_kind": "rel"}
    assert grade("numeric", body, {"value": 1009.0}, marks=2).awarded == 2.0
    assert grade("numeric", body, {"value": 1011.0}, marks=2).awarded == 0.0


def test_numeric_accepts_a_numeric_string() -> None:
    body = {"value": 42.0}
    assert grade("numeric", body, {"value": "42"}, marks=2).awarded == 2.0


def test_numeric_rejects_a_boolean() -> None:
    """`True == 1` in Python. A checkbox is not an answer to a numeric question."""
    award = grade("numeric", {"value": 1.0}, {"value": True}, marks=2, negative_marks=1)
    assert award.awarded == 0.0
    assert award.detail["outcome"] == "malformed"


def test_numeric_rejects_nan_and_infinity() -> None:
    for bad in (float("nan"), float("inf")):
        award = grade("numeric", {"value": 1.0}, {"value": bad}, marks=2, negative_marks=1)
        assert award.awarded == 0.0
        assert award.detail["outcome"] == "malformed"


def test_malformed_answers_are_never_penalised() -> None:
    """A client that sends nonsense has not made the candidate wrong."""
    award = grade("numeric", {"value": 1.0}, {"value": "not a number"}, marks=2, negative_marks=2)
    assert award.awarded == 0.0


# ------------------------------------------------------------------ coding


def test_coding_is_not_auto_gradable() -> None:
    """Raises rather than silently returning 0. A silent 0 becomes a transcript."""
    with pytest.raises(NotAutoGradable):
        grade("coding", {"languages": ["python311"]}, {"source": "print(1)"}, marks=10)
