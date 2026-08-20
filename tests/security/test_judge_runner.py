"""The judge's behaviour: verdicts, scoring, and what a candidate is allowed to see.

These run real containers, like the sandbox suite. The pure-function parts
(comparators) are tested separately in `test_comparators.py`, without Docker.

Several of these exist because the first implementation got them wrong:

* a syntax error was reported as four runtime errors with no compiler message,
  because the compile command ended in `|| true`;
* a compiled language's binary did not survive the compile container, because
  `/box` is a tmpfs;
* a staging failure inside the container was swallowed and surfaced as the
  candidate's `FileNotFoundError`.
"""

from __future__ import annotations

import pytest
from sentinel_judge import languages
from sentinel_judge.runner import (
    COMPILE_ERROR,
    COMPLETED,
    INTERNAL_ERROR,
    MEMORY_EXCEEDED,
    OUTPUT_EXCEEDED,
    RUNTIME_ERROR,
    TIMEOUT,
    JudgeRunner,
    TestCase,
)

pytestmark = pytest.mark.security


ADDER = "a, b = input().split()\nprint(int(a) + int(b))\n"


def cases() -> list[TestCase]:
    return [
        TestCase(1, True, 1.0, "2 3\n", "5\n"),
        TestCase(2, True, 1.0, "10 5\n", "15\n"),
        TestCase(3, False, 2.0, "100 200\n", "300\n"),
        TestCase(4, False, 2.0, "-1 1\n", "0\n"),
    ]


@pytest.fixture
def judge(sandbox) -> JudgeRunner:
    return JudgeRunner(sandbox)


# ------------------------------------------------------------------ verdicts


def test_a_correct_solution_passes_every_test(judge: JudgeRunner) -> None:
    outcome = judge.judge(language=languages.PYTHON311, source=ADDER, tests=cases())
    assert outcome.status == COMPLETED
    assert outcome.fraction == 1.0
    assert all(t.passed for t in outcome.outcomes)


def test_partial_credit_is_weighted_not_counted(judge: JudgeRunner) -> None:
    """Two of four tests pass, but they carry 1.0 of 6.0 weight each.

    Counting tests rather than weight would report 50%; the weights say 33%.
    A question author who marks the hard hidden cases as worth more means it.
    """
    source = "a, b = input().split()\nprint(int(a) + int(b) if int(a) > 0 else 999)\n"
    outcome = judge.judge(language=languages.PYTHON311, source=source, tests=cases())
    assert outcome.status == COMPLETED
    assert outcome.total_weight == 6.0
    assert outcome.passed_weight == 4.0
    assert round(outcome.fraction, 3) == 0.667


def test_a_wrong_solution_scores_zero_but_still_completes(judge: JudgeRunner) -> None:
    # `print(0)` would accidentally pass the "-1 1 -> 0" case, which is a
    # reminder that a "clearly wrong" fixture can still be right by luck.
    outcome = judge.judge(
        language=languages.PYTHON311, source="input()\nprint(-12345)\n", tests=cases()
    )
    assert outcome.status == COMPLETED, "the judge did its job; the program was wrong"
    assert outcome.fraction == 0.0


def test_a_syntax_error_is_a_compile_error_with_the_compiler_message(
    judge: JudgeRunner,
) -> None:
    """Not four runtime errors. The candidate needs the compiler's words."""
    outcome = judge.judge(
        language=languages.PYTHON311, source="def broken(:\n    pass\n", tests=cases()
    )
    assert outcome.status == COMPILE_ERROR
    assert "SyntaxError" in outcome.compile_output
    assert outcome.outcomes == [], "no test should run after a failed compile"


def test_an_import_error_is_a_runtime_error_not_a_compile_error(
    judge: JudgeRunner,
) -> None:
    """It compiles. It fails when run. Those are different things to a candidate."""
    outcome = judge.judge(
        language=languages.PYTHON311,
        source="import nonexistent_module_xyz\n",
        tests=cases()[:1],
    )
    assert outcome.status == RUNTIME_ERROR
    assert outcome.outcomes[0].status == RUNTIME_ERROR


def test_an_infinite_loop_is_a_timeout(judge: JudgeRunner) -> None:
    outcome = judge.judge(
        language=languages.PYTHON311,
        source="while True:\n    pass\n",
        tests=cases()[:1],
        time_limit_ms=1_000,
    )
    assert outcome.status == TIMEOUT


def test_a_memory_bomb_is_reported_as_memory_exceeded(judge: JudgeRunner) -> None:
    outcome = judge.judge(
        language=languages.PYTHON311,
        source="b = []\nwhile True:\n    b.append(bytearray(4 * 1024 * 1024))\n",
        tests=cases()[:1],
        memory_limit_mb=64,
    )
    assert outcome.status == MEMORY_EXCEEDED


def test_an_output_flood_is_reported_as_output_exceeded(judge: JudgeRunner) -> None:
    outcome = judge.judge(
        language=languages.PYTHON311,
        source="import sys\nwhile True:\n    sys.stdout.write('A' * 4096)\n",
        tests=cases()[:1],
        time_limit_ms=5_000,
    )
    assert outcome.status == OUTPUT_EXCEEDED


# ------------------------------------------------------- the answer key


def test_hidden_test_input_and_expected_output_never_leave_the_judge(
    judge: JudgeRunner,
) -> None:
    """The hidden suite is the answer key. Leaking it once ruins the question
    for every future sitting, and the leak would be permanent and silent."""
    outcome = judge.judge(
        language=languages.PYTHON311, source="input()\nprint(0)\n", tests=cases()
    )
    hidden = [t for t in outcome.outcomes if not t.is_sample]
    assert hidden, "the fixture must contain hidden tests or this proves nothing"
    for t in hidden:
        assert t.expected is None, f"test {t.ordinal} leaked its expected output"
        assert t.stdin is None, f"test {t.ordinal} leaked its input"
        assert t.stdout is None, f"test {t.ordinal} leaked the program's output"


def test_sample_tests_do_show_their_detail(judge: JudgeRunner) -> None:
    """The positive control. The candidate already has these from the question,
    so withholding them would be pointless secrecy that helps nobody debug."""
    outcome = judge.judge(
        language=languages.PYTHON311, source="input()\nprint(0)\n", tests=cases()
    )
    samples = [t for t in outcome.outcomes if t.is_sample]
    assert samples
    assert all(t.expected is not None and t.stdin is not None for t in samples)


def test_a_failing_hidden_test_still_explains_itself_without_the_key(
    judge: JudgeRunner,
) -> None:
    """A verdict with no explanation is the most resented thing a judge says.

    Hidden tests get a reason and a position — "output did not match at line 1,
    column 1" — which is actionable without being the answer.
    """
    outcome = judge.judge(
        language=languages.PYTHON311, source="input()\nprint(0)\n", tests=cases()
    )
    hidden = next(t for t in outcome.outcomes if not t.is_sample)
    assert hidden.reason
    assert hidden.expected is None


# ------------------------------------------------------------- robustness


def test_a_judge_failure_is_never_charged_to_the_candidate(sandbox) -> None:
    """Point the runner at an image that cannot stage sources.

    The result must be `internal_error` — which scores nothing and is appealable
    — rather than a zero that looks like the candidate's fault.
    """
    from sentinel_judge.sandbox import Sandbox

    broken = Sandbox(image="scratch-nonexistent-image:definitely-not-here")
    outcome = JudgeRunner(broken).judge(
        language=languages.PYTHON311, source=ADDER, tests=cases()[:1]
    )
    assert outcome.status == INTERNAL_ERROR
    assert outcome.fraction == 0.0


def test_stopping_early_short_circuits_the_remaining_tests(judge: JudgeRunner) -> None:
    """Used for the candidate's own sample runs, where waiting for a full hidden
    suite would be a waste of the judge and of their exam time."""
    outcome = judge.judge(
        language=languages.PYTHON311,
        source="input()\nprint(0)\n",
        tests=cases(),
        stop_on_first_failure=True,
    )
    assert len(outcome.outcomes) == 1


def test_two_submissions_do_not_see_each_other(judge: JudgeRunner) -> None:
    """A submission that writes to disk must not influence the next one — the
    next one is very often a different candidate."""
    first = judge.judge(
        language=languages.PYTHON311,
        source="open('/tmp/planted', 'w').write('x')\ninput()\nprint(5)\n",
        tests=cases()[:1],
    )
    assert first.fraction == 1.0

    second = judge.judge(
        language=languages.PYTHON311,
        source="import os\ninput()\nprint(5 if not os.path.exists('/tmp/planted') else 99)\n",
        tests=cases()[:1],
    )
    assert second.fraction == 1.0, "the second submission saw the first one's file"


# --------------------------------------------------- compiled languages


@pytest.fixture(scope="session")
def cpp_judge(cpp_image: str) -> JudgeRunner:
    from sentinel_judge.sandbox import Sandbox

    return JudgeRunner(Sandbox(image=cpp_image))


ADDER_CPP = (
    "#include <iostream>\n"
    "int main() { long a, b; std::cin >> a >> b; std::cout << a + b << std::endl; return 0; }\n"
)


def test_a_compiled_language_runs_end_to_end(cpp_judge: JudgeRunner) -> None:
    """The compiled path is genuinely different from the interpreted one.

    `/box` is a tmpfs that dies with the compile container, so the binary has to
    be exported to a host directory and staged back into the run container. The
    first implementation did neither and would have run `./program` against a
    directory that never contained it.
    """
    outcome = cpp_judge.judge(language=languages.CPP20, source=ADDER_CPP, tests=cases())
    assert outcome.status == COMPLETED, outcome.compile_output
    assert outcome.fraction == 1.0


def test_a_compiled_binary_keeps_its_execute_bit(cpp_judge: JudgeRunner) -> None:
    """Staging wrote artefacts as 0644 at first, which stripped `+x`.

    The symptom was a runtime error on every test with an empty compile log —
    an unfalsifiable-looking failure that told the candidate nothing.
    """
    outcome = cpp_judge.judge(
        language=languages.CPP20, source=ADDER_CPP, tests=cases()[:1]
    )
    assert outcome.outcomes[0].status == COMPLETED
    assert outcome.outcomes[0].passed


def test_a_cpp_syntax_error_is_a_compile_error(cpp_judge: JudgeRunner) -> None:
    outcome = cpp_judge.judge(
        language=languages.CPP20,
        source="#include <iostream>\nint main() { this is not c++ }\n",
        tests=cases(),
    )
    assert outcome.status == COMPILE_ERROR
    assert outcome.compile_output
    assert outcome.outcomes == []


def test_a_cpp_infinite_loop_is_still_a_timeout(cpp_judge: JudgeRunner) -> None:
    """Compiled code is not exempt from the wall clock."""
    outcome = cpp_judge.judge(
        language=languages.CPP20,
        source="int main() { for (;;) ; }\n",
        tests=cases()[:1],
        time_limit_ms=1_000,
    )
    assert outcome.status == TIMEOUT
