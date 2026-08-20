"""Output comparison. Pure functions, no Docker, exhaustive.

A comparator bug is a wrong mark that nobody can see. These are cheap to test
and expensive to get wrong, so the coverage here is deliberately fussy.
"""

from __future__ import annotations

import pytest
from sentinel_judge.comparators import compare

# ------------------------------------------------------------- trim_exact


@pytest.mark.parametrize(
    ("actual", "expected"),
    [
        ("5\n", "5"),
        ("5", "5\n"),
        ("5\n\n\n", "5"),
        ("5   \n", "5"),
        ("a\nb\n", "a\nb"),
        ("a\r\nb\r\n", "a\nb"),
    ],
)
def test_trailing_whitespace_never_decides_a_mark(actual: str, expected: str) -> None:
    """A solution that prints a trailing newline and one that does not are the
    same solution. No assessment is measuring which one a language emits."""
    assert compare("trim_exact", actual, expected).passed


@pytest.mark.parametrize(
    ("actual", "expected"),
    [("5", "6"), ("5\n6", "6\n5"), ("", "5"), ("5", ""), ("a b", "a  b")],
)
def test_real_differences_are_still_failures(actual: str, expected: str) -> None:
    assert not compare("trim_exact", actual, expected).passed


def test_a_failure_says_where_without_saying_what() -> None:
    """The position is safe to show a candidate; the expected text is the key."""
    result = compare("trim_exact", "hello world", "hello there")
    assert not result.passed
    assert result.detail == {"line": 1, "column": 7}
    assert "there" not in str(result.detail)


def test_a_short_output_is_described_as_short() -> None:
    result = compare("trim_exact", "a", "a\nb\nc")
    assert not result.passed
    assert result.detail["reason"] == "output was too short"


# ------------------------------------------------------------------ token


def test_token_ignores_layout() -> None:
    assert compare("token", "1 2 3", "1\n2\n3").passed
    assert compare("token", "  1\t2\n\n3  ", "1 2 3").passed


def test_token_still_cares_about_order_and_count() -> None:
    assert not compare("token", "1 3 2", "1 2 3").passed
    assert not compare("token", "1 2", "1 2 3").passed


def test_token_reports_where_it_diverged() -> None:
    result = compare("token", "1 9 3", "1 2 3")
    assert result.detail["first_mismatch_index"] == 1


# -------------------------------------------------------------- float_eps


def test_floats_compare_within_an_absolute_tolerance() -> None:
    assert compare("float_eps", "0.3333333", "0.3333334", {"epsilon": 1e-6}).passed
    assert not compare("float_eps", "0.33", "0.3333334", {"epsilon": 1e-6}).passed


def test_relative_tolerance_scales_with_magnitude() -> None:
    """1e-6 absolute is meaningless at 1e9. Relative mode is why the option
    exists, and a question that computes large values needs it."""
    config = {"epsilon": 1e-6, "relative": True}
    # allowed = 1e-6 * 1e9 = 1000, so 0.0001 is well inside and 5000 is not.
    # The first version of this test used a difference of exactly 1000, which
    # sits on the boundary and passes — a test that asserts the wrong side of
    # its own tolerance.
    assert compare("float_eps", "1000000000.0001", "1000000000.0", config).passed
    assert not compare("float_eps", "1000005000.0", "1000000000.0", config).passed


def test_mixed_text_and_numbers_compares_text_exactly() -> None:
    assert compare("float_eps", "Case 1: 3.14159", "Case 1: 3.1415900001").passed
    assert not compare("float_eps", "Case 2: 3.14159", "Case 1: 3.14159").passed


def test_a_non_finite_answer_is_never_correct() -> None:
    """`nan` compares unequal to everything including itself, so a naive
    implementation silently accepts or rejects it depending on operator order."""
    for bad in ("nan", "inf", "-inf"):
        assert not compare("float_eps", bad, "1.0").passed


def test_wrong_value_count_is_reported_before_comparing() -> None:
    result = compare("float_eps", "1.0 2.0", "1.0 2.0 3.0")
    assert not result.passed
    assert result.detail == {"expected_tokens": 3, "actual_tokens": 2}


# ------------------------------------------------------------------ misc


def test_an_unknown_comparator_raises_rather_than_defaulting() -> None:
    """A typo in a question's comparator name must not silently change how it
    is marked. Failing closed makes the mistake visible at authoring time."""
    with pytest.raises(ValueError, match="Unknown comparator"):
        compare("trim_exakt", "1", "1")


def test_custom_comparators_are_honestly_unimplemented() -> None:
    """The schema allows `custom`. Nothing implements it, and pretending
    otherwise by falling back to `trim_exact` would mark work incorrectly."""
    with pytest.raises(ValueError, match="NOT IMPLEMENTED"):
        compare("custom", "1", "1")
