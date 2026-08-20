"""Per-language toolchain adapters.

Each adapter answers three questions: what file does the source go in, how is it
compiled, and how is it run. Everything else — the container, the limits, the
kill — belongs to `sandbox.py` and is identical for every language.

**Compilation is untrusted too.** A C++ template that expands forever, or a
`#include` of `/dev/urandom`, is an attack on the judge that never reaches the
run step. Compilation therefore happens inside the same sandbox with its own
(more generous, but still finite) limits. A judge that compiles on the host and
only sandboxes execution has a hole the size of its compiler.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sentinel_judge.sandbox import SandboxLimits


@dataclass(frozen=True)
class Language:
    key: str
    display_name: str
    source_filename: str
    #: argv for compilation, or None for interpreted languages.
    compile_argv: list[str] | None
    #: argv to execute the (possibly compiled) program.
    run_argv: list[str]
    #: Compilation gets its own ceiling. Generous relative to execution, because
    #: a legitimate C++ translation unit is genuinely slow, but still bounded.
    compile_limits: SandboxLimits = field(
        default_factory=lambda: SandboxLimits(
            wall_ms=20_000, memory_mb=768, pids=64, stdout_kb=64, workdir_mb=64
        )
    )
    #: Added to whatever the question asks for. A JVM needs headroom the
    #: question author should not have to know about.
    memory_overhead_mb: int = 0
    #: Interpreter or VM startup, excluded from the question's time limit for
    #: the same reason.
    startup_overhead_ms: int = 0

    @property
    def compiled(self) -> bool:
        return self.compile_argv is not None


PYTHON311 = Language(
    key="python311",
    display_name="Python 3.11",
    source_filename="main.py",
    # `py_compile` catches syntax errors before the run, so a typo is reported
    # as a compile error rather than as a failure on every test case.
    compile_argv=["/usr/bin/python3.11", "-m", "py_compile", "main.py"],
    # -I: isolated mode. Ignores PYTHON* environment variables and the user site
    # directory, so nothing outside the work directory can influence the run.
    # -B: no .pyc files, which keeps the tmpfs for the program's own use.
    run_argv=["/usr/bin/python3.11", "-I", "-B", "main.py"],
    memory_overhead_mb=32,
    startup_overhead_ms=300,
)

CPP20 = Language(
    key="cpp20",
    display_name="C++20 (GCC)",
    source_filename="main.cpp",
    compile_argv=[
        "/usr/bin/g++",
        "-std=c++20",
        "-O2",
        "-pipe",
        "-static",
        "-s",
        # A hostile template can expand until the compiler exhausts memory.
        # These make that a compile error instead of an OOM kill.
        "-ftemplate-depth=256",
        "-fconstexpr-ops-limit=33554432",
        "-o",
        "program",
        "main.cpp",
    ],
    run_argv=["./program"],
    startup_overhead_ms=50,
)

JAVA17 = Language(
    key="java17",
    display_name="Java 17",
    # javac requires the filename to match the public class.
    source_filename="Main.java",
    compile_argv=["/usr/bin/javac", "-encoding", "UTF-8", "-d", ".", "Main.java"],
    run_argv=[
        "/usr/bin/java",
        # The JVM sizes its heap from the *host's* memory unless told otherwise,
        # and a container memory cap it does not know about becomes an OOM kill
        # that looks like the candidate's fault.
        "-XX:+UseSerialGC",
        "-Xss64m",
        "-XX:MaxRAMPercentage=75",
        "-Dfile.encoding=UTF-8",
        "Main",
    ],
    memory_overhead_mb=256,
    startup_overhead_ms=600,
)

LANGUAGES: dict[str, Language] = {lang.key: lang for lang in (PYTHON311, CPP20, JAVA17)}


def get(key: str) -> Language:
    try:
        return LANGUAGES[key]
    except KeyError:
        raise ValueError(
            f"Unsupported language {key!r}. Supported: {', '.join(sorted(LANGUAGES))}."
        ) from None


def execution_limits(
    language: Language, *, time_limit_ms: int, memory_limit_mb: int, stdout_kb: int = 64
) -> SandboxLimits:
    """The question's limits, adjusted for what the toolchain itself costs.

    A question that says "2 seconds, 256 MB" means two seconds of the
    candidate's algorithm. Charging them for JVM startup would make the same
    solution pass in C++ and fail in Java, which measures the language rather
    than the student.
    """
    return SandboxLimits(
        wall_ms=time_limit_ms + language.startup_overhead_ms,
        memory_mb=memory_limit_mb + language.memory_overhead_mb,
        pids=64,
        stdout_kb=stdout_kb,
        workdir_mb=64,
        cpus=1.0,
    )
