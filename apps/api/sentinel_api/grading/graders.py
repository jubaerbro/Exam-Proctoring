"""Auto-graders for the four non-coding question kinds.

Each grader is a pure function of (body, answer value) and returns an award plus
a `detail` dict explaining how it got there. The detail is not decoration: a
candidate who disputes a mark is entitled to see why the mark is what it is, and
"the grader said 0" is not an answer. It is stored on `question_score.detail`.

Coding questions are graded by the judge in Phase 3. `grade` raises for them
rather than returning 0, because a silent 0 for an ungradable kind is the sort
of bug that shows up as a student's transcript.

One rule holds across all of them: **negative marking never pushes a question
below `-negative_marks`, and an unanswered question is always exactly 0.** A
candidate who leaves a question blank has not earned a penalty, and a scheme
where skipping is worse than guessing wrong teaches the wrong thing.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Award:
    awarded: float
    detail: dict[str, Any] = field(default_factory=dict)


class NotAutoGradable(Exception):
    """Raised for kinds that need something other than a pure function."""


#: The key each kind's answer envelope carries its payload under. The client
#: always sends `{"selected": [...]}` / `{"text": "..."}` / `{"value": n}`, so
#: the blank check has to look *inside* the envelope: `{"selected": []}` is an
#: empty answer, not a non-empty dict. Missing this is how "candidate deselected
#: their answer" turns into a negative mark.
_PAYLOAD_KEY = {
    "single_choice": "selected",
    "multiple_choice": "selected",
    "short_answer": "text",
    "numeric": "value",
    "coding": "source",
}


def unwrap(kind: str, value: Any) -> Any:
    """Strip the answer envelope, if there is one."""
    key = _PAYLOAD_KEY.get(kind)
    if key and isinstance(value, dict) and key in value:
        return value[key]
    return value


def is_blank(value: Any) -> bool:
    """A blank answer is 0, never a penalty. Every grader routes through this."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


def grade(
    kind: str,
    body: dict[str, Any],
    value: Any,
    *,
    marks: float,
    negative_marks: float = 0.0,
    partial_credit: bool = False,
) -> Award:
    if kind == "coding":
        raise NotAutoGradable(
            "Coding questions are graded by the judge worker (Phase 3), not by an autograder."
        )
    grader = _GRADERS.get(kind)
    if grader is None:
        raise NotAutoGradable(f"No autograder for question kind {kind!r}.")
    if is_blank(unwrap(kind, value)):
        return Award(0.0, {"outcome": "blank"})
    return grader(body, value, marks, negative_marks, partial_credit)


# ---------------------------------------------------------------------------


def _selected_ids(value: Any) -> list[str] | None:
    """Accept `"a"`, `["a"]`, or `{"selected": ["a"]}` — reject anything else.

    The client sends `{"selected": [...]}`; the other shapes exist because an
    import or an integration will eventually send them, and rejecting a
    well-meant answer shape by returning 0 marks is a very expensive way to
    enforce a wire format.
    """
    if isinstance(value, dict):
        value = value.get("selected")
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    return None


def _grade_choice(
    body: dict[str, Any], value: Any, marks: float, negative: float, partial: bool
) -> Award:
    correct = set(body.get("correct", []))
    valid = {o["id"] for o in body.get("options", [])}
    selected_list = _selected_ids(value)
    if selected_list is None:
        return Award(0.0, {"outcome": "malformed", "reason": "answer is not a list of option ids"})

    selected = set(selected_list)
    unknown = selected - valid
    if unknown:
        # An option id that is not on the paper cannot be right, and cannot be a
        # good-faith answer either. Scored 0, not penalised: it is far more
        # likely to be a client bug than a candidate.
        return Award(
            0.0,
            {"outcome": "invalid_option", "unknown_options": sorted(unknown)},
        )

    hits = selected & correct
    misses = correct - selected
    wrong = selected - correct

    if selected == correct:
        return Award(
            round(marks, 3),
            {"outcome": "correct", "selected": sorted(selected), "correct": sorted(correct)},
        )

    if partial and len(correct) > 1:
        # Per-option credit with symmetric penalty: each correct option earns
        # 1/|correct| of the marks, each wrong one loses the same. Selecting
        # everything therefore scores 0, not full marks — which is the whole
        # reason the penalty term is there.
        unit = marks / len(correct)
        raw = unit * (len(hits) - len(wrong))
        awarded = max(0.0, min(marks, raw))
        return Award(
            round(awarded, 3),
            {
                "outcome": "partial",
                "hits": sorted(hits),
                "missed": sorted(misses),
                "incorrect": sorted(wrong),
                "unit_marks": round(unit, 3),
            },
        )

    return Award(
        round(-negative, 3) if negative else 0.0,
        {
            "outcome": "incorrect",
            "selected": sorted(selected),
            "correct": sorted(correct),
            "penalty_applied": bool(negative),
        },
    )


def _normalize(text: str, *, trim: bool) -> str:
    # NFKC first: a candidate who typed a full-width character or a curly
    # apostrophe on a phone keyboard has not given a different answer.
    out = unicodedata.normalize("NFKC", text)
    if trim:
        out = " ".join(out.split())
    return out


def _grade_short_answer(
    body: dict[str, Any], value: Any, marks: float, negative: float, partial: bool
) -> Award:
    if isinstance(value, dict):
        value = value.get("text")
    if not isinstance(value, str):
        return Award(0.0, {"outcome": "malformed", "reason": "answer is not text"})

    mode = body.get("match", "ci")
    trim = body.get("trim", True)
    accepted = body.get("accepted", [])
    given = _normalize(value, trim=trim)

    for pattern in accepted:
        if mode == "regex":
            # fullmatch, not search: `accepted: ["a"]` should not accept
            # "absolutely anything containing an a".
            if re.fullmatch(pattern, given, flags=re.IGNORECASE):
                return Award(round(marks, 3), {"outcome": "correct", "matched": pattern})
        else:
            candidate = _normalize(pattern, trim=trim)
            if mode == "ci":
                if candidate.casefold() == given.casefold():
                    return Award(round(marks, 3), {"outcome": "correct", "matched": pattern})
            elif candidate == given:
                return Award(round(marks, 3), {"outcome": "correct", "matched": pattern})

    return Award(
        round(-negative, 3) if negative else 0.0,
        {
            "outcome": "incorrect",
            "match_mode": mode,
            "penalty_applied": bool(negative),
            # Deliberately not echoing `accepted`: this detail is visible to the
            # candidate, and the answer key is not.
            "note": "no accepted form matched",
        },
    )


def _grade_numeric(
    body: dict[str, Any], value: Any, marks: float, negative: float, partial: bool
) -> Award:
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, bool):  # bool is an int in Python; not a numeric answer
        return Award(0.0, {"outcome": "malformed", "reason": "answer is not a number"})
    if isinstance(value, str):
        try:
            value = float(value.strip().replace(",", ""))
        except ValueError:
            return Award(0.0, {"outcome": "malformed", "reason": "answer is not a number"})
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return Award(0.0, {"outcome": "malformed", "reason": "answer is not a finite number"})

    expected = float(body["value"])
    tolerance = float(body.get("tolerance", 0.0))
    kind = body.get("tolerance_kind", "abs")
    window = tolerance if kind == "abs" else abs(expected) * tolerance

    delta = abs(float(value) - expected)
    # `<=` with a tiny epsilon: a tolerance of 0.01 must accept a value exactly
    # 0.01 away, and binary floating point will not always agree that it is.
    if delta <= window + 1e-12:
        return Award(
            round(marks, 3),
            {"outcome": "correct", "delta": round(delta, 12), "window": window},
        )
    return Award(
        round(-negative, 3) if negative else 0.0,
        {
            "outcome": "incorrect",
            "delta": round(delta, 12),
            "window": window,
            "penalty_applied": bool(negative),
        },
    )


_GRADERS = {
    "single_choice": _grade_choice,
    "multiple_choice": _grade_choice,
    "short_answer": _grade_short_answer,
    "numeric": _grade_numeric,
}
