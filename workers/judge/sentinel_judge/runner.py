"""Compile once, run every test case, score the result.

The scoring rule is weighted-partial: a submission earns the fraction of test
weight it passes. That is a product decision, not an arbitrary one — all-or-
nothing scoring on a hidden test suite tells a candidate who solved 90% of the
problem exactly as much as one who solved none of it, and tells the examiner
nothing either.

Two things this module refuses to do:

* **It never returns a hidden test's expected output.** Sample tests are shown
  in full because the candidate has already seen them in the question. Hidden
  tests return a verdict and a position, never the key. `visible_detail` is the
  single place that distinction is enforced.
* **It never lets a judge failure look like a candidate failure.** If the
  sandbox itself breaks, the run is `internal_error` and scores nothing, rather
  than a zero the candidate would have to appeal.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sentinel_judge.comparators import compare
from sentinel_judge.languages import Language, execution_limits
from sentinel_judge.sandbox import ExecResult, Sandbox, SandboxLimits, SandboxStatus


@dataclass(frozen=True)
class TestCase:
    # pytest collects any class named Test*; this is a data class, not a suite.
    __test__ = False

    ordinal: int
    is_sample: bool
    weight: float
    stdin: str
    expected: str
    comparator: str = "trim_exact"
    comparator_config: dict[str, Any] = field(default_factory=dict)
    time_limit_ms: int | None = None
    memory_limit_mb: int | None = None


@dataclass
class TestOutcome:
    __test__ = False

    ordinal: int
    is_sample: bool
    weight: float
    status: str
    passed: bool
    duration_ms: int
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    #: Only ever populated for sample tests.
    stdout: str | None = None
    expected: str | None = None
    stdin: str | None = None


@dataclass
class JudgeOutcome:
    status: str
    outcomes: list[TestOutcome] = field(default_factory=list)
    compile_output: str = ""
    total_weight: float = 0.0
    passed_weight: float = 0.0
    max_duration_ms: int = 0
    internal_detail: str | None = None

    @property
    def fraction(self) -> float:
        """The share of test weight passed, in [0, 1]."""
        if self.total_weight <= 0:
            return 0.0
        return max(0.0, min(1.0, self.passed_weight / self.total_weight))


# `judge_status` in the schema. Kept as strings so the worker can write them
# straight to the enum column without a translation table drifting out of date.
QUEUED = "queued"
RUNNING = "running"
COMPLETED = "completed"
COMPILE_ERROR = "compile_error"
RUNTIME_ERROR = "runtime_error"
TIMEOUT = "timeout"
MEMORY_EXCEEDED = "memory_exceeded"
OUTPUT_EXCEEDED = "output_exceeded"
INTERNAL_ERROR = "internal_error"
CANCELLED = "cancelled"

_SANDBOX_TO_STATUS = {
    SandboxStatus.TIMEOUT: TIMEOUT,
    SandboxStatus.MEMORY_EXCEEDED: MEMORY_EXCEEDED,
    SandboxStatus.OUTPUT_EXCEEDED: OUTPUT_EXCEEDED,
    SandboxStatus.INTERNAL_ERROR: INTERNAL_ERROR,
}


class JudgeRunner:
    def __init__(
        self,
        sandbox: Sandbox,
        *,
        default_time_ms: int = 2000,
        default_memory_mb: int = 256,
    ):
        self._sandbox = sandbox
        self._default_time_ms = default_time_ms
        self._default_memory_mb = default_memory_mb

    def judge(
        self,
        *,
        language: Language,
        source: str,
        tests: list[TestCase],
        time_limit_ms: int | None = None,
        memory_limit_mb: int | None = None,
        stop_on_first_failure: bool = False,
    ) -> JudgeOutcome:
        time_limit_ms = time_limit_ms or self._default_time_ms
        memory_limit_mb = memory_limit_mb or self._default_memory_mb

        files = {language.source_filename: source.encode("utf-8")}
        outcome = JudgeOutcome(status=RUNNING)

        build_root = Path(tempfile.mkdtemp(prefix="sentinel-build-"))
        try:
            return self._judge_with_build_dir(
                language=language,
                files=files,
                tests=tests,
                time_limit_ms=time_limit_ms,
                memory_limit_mb=memory_limit_mb,
                stop_on_first_failure=stop_on_first_failure,
                outcome=outcome,
                build_root=build_root,
            )
        finally:
            shutil.rmtree(build_root, ignore_errors=True)

    def _judge_with_build_dir(
        self,
        *,
        language: Language,
        files: dict[str, bytes],
        tests: list[TestCase],
        time_limit_ms: int,
        memory_limit_mb: int,
        stop_on_first_failure: bool,
        outcome: JudgeOutcome,
        build_root: Path,
    ) -> JudgeOutcome:
        # ---- compile ------------------------------------------------------
        artefacts = dict(files)
        if language.compiled:
            compiled = self._compile(language, files, build_root)
            if compiled is None:
                outcome.status = INTERNAL_ERROR
                outcome.internal_detail = "the sandbox could not run the compiler"
                return outcome
            if compiled[0].status is SandboxStatus.INTERNAL_ERROR:
                outcome.status = INTERNAL_ERROR
                outcome.internal_detail = compiled[0].internal_detail
                return outcome
            result, artefacts = compiled
            outcome.compile_output = _trim(result.stderr or result.stdout, 8000)
            if result.status is not SandboxStatus.COMPLETED or result.exit_code != 0:
                outcome.status = (
                    TIMEOUT
                    if result.status is SandboxStatus.TIMEOUT
                    else MEMORY_EXCEEDED
                    if result.status is SandboxStatus.MEMORY_EXCEEDED
                    else COMPILE_ERROR
                )
                return outcome
            if not artefacts:
                outcome.status = COMPILE_ERROR
                outcome.compile_output = (
                    outcome.compile_output
                    or "the compiler reported success but produced no output file"
                )
                return outcome

        # ---- run every test ----------------------------------------------
        limits = execution_limits(
            language, time_limit_ms=time_limit_ms, memory_limit_mb=memory_limit_mb
        )
        worst = COMPLETED

        for test in sorted(tests, key=lambda t: t.ordinal):
            per_test = _limits_for(limits, language, test)
            started = time.monotonic()
            result = self._sandbox.run(
                language.run_argv,
                limits=per_test,
                stdin=test.stdin.encode("utf-8"),
                files=artefacts,
            )
            elapsed = int((time.monotonic() - started) * 1000)
            outcome.max_duration_ms = max(
                outcome.max_duration_ms, result.duration_ms or elapsed
            )

            if result.status is SandboxStatus.INTERNAL_ERROR:
                # Stop immediately. Running the rest would produce a plausible
                # looking score built on a broken judge.
                outcome.status = INTERNAL_ERROR
                outcome.internal_detail = result.internal_detail
                return outcome

            test_outcome = self._score_one(test, result)
            outcome.outcomes.append(test_outcome)
            outcome.total_weight += test.weight
            if test_outcome.passed:
                outcome.passed_weight += test.weight
            elif worst == COMPLETED and test_outcome.status != COMPLETED:
                worst = test_outcome.status

            if stop_on_first_failure and not test_outcome.passed:
                break

        # A run where every test executed but some produced wrong answers is
        # `completed` — the judge did its job. `runtime_error` and friends
        # describe the *program*, and the first one seen is the most useful
        # thing to show.
        outcome.status = worst
        return outcome

    # ------------------------------------------------------------------ util

    def _compile(
        self, language: Language, files: dict[str, bytes], build_root: Path
    ) -> tuple[ExecResult, dict[str, bytes]] | None:
        """Compile inside the sandbox and carry the artefacts out.

        `/box` is a tmpfs that dies with the container, so a binary built there
        would not exist for the run step. The compile container therefore gets a
        writable `/out` (a fresh host directory, discarded afterwards) and the
        compile command is wrapped to copy whatever it produced into it.

        The alternative — compiling on the host — would put a compiler, running
        candidate-supplied input, outside every control in `sandbox.py`.
        """
        assert language.compile_argv is not None
        export = build_root / "out"

        # Compile in /box as usual, then publish the artefacts. Compiling
        # straight into /out would let a hostile source tree write object files
        # into the export directory and have them staged into the run step.
        # No `|| true`. An earlier version ended this command with
        # `2>/dev/null || true`, which made the shell exit 0 even when the
        # compiler had failed — so a Python file with a syntax error compiled
        # "successfully" and then failed every single test case as a runtime
        # error. The candidate saw four failures and no compiler message.
        # `cp -RL` rather than `cp -a` for the same permissions reason as in
        # `sandbox.py`.
        wrapped = list(language.compile_argv)
        result = self._sandbox.run(
            [
                "/bin/sh",
                "-c",
                _shell(wrapped) + f" && cp -RL {_artefact_glob(language)} /out/",
            ],
            limits=language.compile_limits,
            files=files,
            export_dir=export,
        )
        if result.status is SandboxStatus.INTERNAL_ERROR:
            return None

        artefacts: dict[str, bytes] = {}
        if export.is_dir():
            for path in sorted(export.rglob("*")):
                if path.is_file():
                    artefacts[str(path.relative_to(export))] = path.read_bytes()
        return result, artefacts

    def _score_one(self, test: TestCase, result: ExecResult) -> TestOutcome:
        mapped = _SANDBOX_TO_STATUS.get(result.status)
        if mapped is not None:
            return TestOutcome(
                ordinal=test.ordinal,
                is_sample=test.is_sample,
                weight=test.weight,
                status=mapped,
                passed=False,
                duration_ms=result.duration_ms,
                reason=mapped.replace("_", " "),
                **_visible(test, result, include_output=False),
            )

        if result.exit_code not in (0, None):
            return TestOutcome(
                ordinal=test.ordinal,
                is_sample=test.is_sample,
                weight=test.weight,
                status=RUNTIME_ERROR,
                passed=False,
                duration_ms=result.duration_ms,
                reason=f"exited with status {result.exit_code}",
                detail={"stderr": _trim(result.stderr, 2000)} if test.is_sample else {},
                **_visible(test, result, include_output=test.is_sample),
            )

        verdict = compare(
            test.comparator, result.stdout, test.expected, test.comparator_config
        )
        return TestOutcome(
            ordinal=test.ordinal,
            is_sample=test.is_sample,
            weight=test.weight,
            status=COMPLETED,
            passed=verdict.passed,
            duration_ms=result.duration_ms,
            reason=verdict.reason,
            detail=verdict.detail or {},
            **_visible(test, result, include_output=test.is_sample),
        )


def _visible(
    test: TestCase, result: ExecResult, *, include_output: bool
) -> dict[str, Any]:
    """The single gate on what a candidate may see.

    Sample tests are already in the question, so echoing them back costs
    nothing. A hidden test's input and expected output are the answer key, and
    leaking them turns the hidden suite into a public one on the next sitting.
    """
    if not (test.is_sample and include_output):
        return {"stdout": None, "expected": None, "stdin": None}
    return {
        "stdout": _trim(result.stdout, 4000),
        "expected": _trim(test.expected, 4000),
        "stdin": _trim(test.stdin, 4000),
    }


def _limits_for(
    base: SandboxLimits, language: Language, test: TestCase
) -> SandboxLimits:
    if test.time_limit_ms is None and test.memory_limit_mb is None:
        return base
    return SandboxLimits(
        wall_ms=(test.time_limit_ms or base.wall_ms) + language.startup_overhead_ms,
        memory_mb=(test.memory_limit_mb or base.memory_mb)
        + language.memory_overhead_mb,
        pids=base.pids,
        stdout_kb=base.stdout_kb,
        workdir_mb=base.workdir_mb,
        cpus=base.cpus,
    )


def _shell(argv: list[str]) -> str:
    import shlex

    return shlex.join(argv)


def _artefact_glob(language: Language) -> str:
    """What to carry out of the compile container.

    Named per language rather than "everything", so a submission cannot bloat
    the run stage by emitting a thousand files next to its source.
    """
    if language.key == "cpp20":
        return "program"
    if language.key == "java17":
        return "*.class"
    return language.source_filename


def _trim(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… truncated, {len(text) - limit} more characters"
