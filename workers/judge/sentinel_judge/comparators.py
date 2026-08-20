"""Deciding whether a submission's output is right.

Comparison is where a judge quietly becomes unfair. A comparator that is too
strict fails correct solutions over a trailing newline; one that is too loose
passes wrong ones. Each of these is explicit about what it ignores, and the
`detail` it returns says which rule matched, because "wrong answer" with no
explanation is the single most resented thing a judge can say.

Nothing here ever sees the candidate's source. A comparator takes two strings.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Comparison:
    passed: bool
    reason: str
    detail: dict[str, Any] | None = None


def _normalise_trailing(text: str) -> str:
    """Strip trailing whitespace on every line, and trailing blank lines.

    Universally expected. A solution that prints "42\\n" and one that prints
    "42" are the same solution, and no assessment is measuring which one a
    student's language of choice emits.
    """
    lines = [
        line.rstrip()
        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def trim_exact(
    actual: str, expected: str, config: dict[str, Any] | None = None
) -> Comparison:
    """Exact match after trailing-whitespace normalisation. The default."""
    a, e = _normalise_trailing(actual), _normalise_trailing(expected)
    if a == e:
        return Comparison(True, "exact match")
    return Comparison(
        False,
        "output did not match",
        _first_difference(a, e),
    )


def token(
    actual: str, expected: str, config: dict[str, Any] | None = None
) -> Comparison:
    """Whitespace-insensitive token comparison.

    For problems where the answer is a sequence of values and the layout is not
    part of the question. Note this deliberately accepts "1 2 3" for an expected
    "1\\n2\\n3" — if layout matters, the question should use `trim_exact`.
    """
    a, e = actual.split(), expected.split()
    if a == e:
        return Comparison(True, "tokens match")
    return Comparison(
        False,
        "tokens did not match",
        {
            "expected_tokens": len(e),
            "actual_tokens": len(a),
            "first_mismatch_index": _first_token_mismatch(a, e),
        },
    )


def float_eps(
    actual: str, expected: str, config: dict[str, Any] | None = None
) -> Comparison:
    """Token comparison where numeric tokens compare within a tolerance.

    `config` takes `epsilon` (absolute, default 1e-6) and `relative` (bool). Any
    token that is not a number on both sides falls back to an exact comparison,
    so a problem that prints "Case 1: 3.14159" still works.
    """
    config = config or {}
    epsilon = float(config.get("epsilon", 1e-6))
    relative = bool(config.get("relative", False))

    a, e = actual.split(), expected.split()
    if len(a) != len(e):
        return Comparison(
            False,
            "wrong number of values",
            {"expected_tokens": len(e), "actual_tokens": len(a)},
        )

    for index, (got, want) in enumerate(zip(a, e, strict=True)):
        try:
            got_f, want_f = float(got), float(want)
        except ValueError:
            if got != want:
                return Comparison(
                    False,
                    "text token did not match",
                    {"index": index, "expected": want},
                )
            continue

        if math.isnan(got_f) or math.isinf(got_f):
            return Comparison(
                False, "non-finite value", {"index": index, "actual": got}
            )

        allowed = epsilon * max(1.0, abs(want_f)) if relative else epsilon
        if abs(got_f - want_f) > allowed:
            return Comparison(
                False,
                "value outside tolerance",
                {
                    "index": index,
                    "expected": want_f,
                    "actual": got_f,
                    "allowed_difference": allowed,
                },
            )

    return Comparison(True, f"values match within {epsilon}")


COMPARATORS = {
    "trim_exact": trim_exact,
    "token": token,
    "float_eps": float_eps,
}


def compare(
    name: str, actual: str, expected: str, config: dict[str, Any] | None = None
) -> Comparison:
    comparator = COMPARATORS.get(name)
    if comparator is None:
        # Fail closed and loudly. Silently falling back to `trim_exact` would
        # mean a typo in a question's comparator name changes how it is marked
        # without anyone noticing.
        raise ValueError(
            f"Unknown comparator {name!r}. Known: {', '.join(sorted(COMPARATORS))}. "
            "`custom` comparators are NOT IMPLEMENTED."
        )
    return comparator(actual, expected, config)


def _first_difference(actual: str, expected: str) -> dict[str, Any]:
    """Where the outputs diverge, in a form safe to show a candidate.

    The expected output of a *hidden* test is an answer key, so the caller
    decides whether to surface this. It returns line and column rather than the
    expected text so that even if it leaks, it leaks a position and not a key.
    """
    a_lines, e_lines = actual.split("\n"), expected.split("\n")
    for line_no, (got, want) in enumerate(zip(a_lines, e_lines, strict=False), start=1):
        if got != want:
            column = next(
                (
                    i
                    for i, (g, w) in enumerate(zip(got, want, strict=False), start=1)
                    if g != w
                ),
                min(len(got), len(want)) + 1,
            )
            return {"line": line_no, "column": column}
    if len(a_lines) != len(e_lines):
        return {
            "line": min(len(a_lines), len(e_lines)) + 1,
            "reason": "output was too short"
            if len(a_lines) < len(e_lines)
            else "output was too long",
        }
    return {}


def _first_token_mismatch(actual: list[str], expected: list[str]) -> int | None:
    for index, (got, want) in enumerate(zip(actual, expected, strict=False)):
        if got != want:
            return index
    if len(actual) != len(expected):
        return min(len(actual), len(expected))
    return None
