"""The judge sandbox: run untrusted code in a container and survive it.

This is the most security-sensitive module in Sentinel. Everything it does is a
control, and the controls are listed here in one place so a reviewer can read
them without reading the code:

| Control | Flag | Stops |
|---|---|---|
| No network namespace | ``--network=none`` | egress, callbacks, reaching the API |
| No socket syscall | seccomp deny-list | egress if the namespace were misconfigured |
| Read-only root | ``--read-only`` | tampering with the image, planting binaries |
| Size-capped scratch | ``--tmpfs /box`` | filling the host disk |
| Process cap | ``--pids-limit`` | fork bombs |
| Memory cap, no swap | ``--memory``/``--memory-swap`` | memory bombs, host OOM |
| CPU cap | ``--cpus`` | starving other submissions |
| Non-root | ``--user 65534`` | most privilege escalation paths |
| No capabilities | ``--cap-drop=ALL`` | raw sockets, mount, ptrace, module loading |
| No setuid gain | ``--security-opt no-new-privileges`` | setuid binaries in the image |
| No container log | ``--log-driver=none`` | disk exhaustion through stdout |
| Bounded reads | in this module | RAM exhaustion in the *worker* |
| Wall-clock kill | in this module | infinite loops, sleeps, blocked syscalls |
| Empty environment | ``--env`` not inherited | leaking the worker's DATABASE_URL |

**The runtime is a configuration value.** ``--runtime`` defaults to ``runc``,
which shares the host kernel: a kernel exploit escapes, and that is the residual
risk D4 accepted. Setting ``JUDGE_RUNTIME=runsc`` switches to gVisor without a
code change, which was the point of making it a setting rather than a constant.

Two lessons are baked in here because both were learned by breaking a machine:

1. **Killing the client does not kill the container.** ``timeout 60 docker run``
   kills the CLI and leaves the container running. Every exit path in this
   module removes the container by id, and ``test_the_container_is_gone_after_a_timeout``
   asserts it.
2. **Docker's default log driver is unbounded.** A submission writing to stdout
   in a loop wrote 15 GB in 80 seconds and filled the build host's disk. Output
   goes through a pipe with a byte cap, and the log driver is off.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SECCOMP = REPO_ROOT / "infrastructure" / "security" / "seccomp-judge.json"

#: Nobody. Present in every mainstream base image, owns nothing.
SANDBOX_UID = 65534
SANDBOX_GID = 65534

#: Where the submission's files are copied to and run from.
WORKDIR = "/box"
#: Where the caller's files are mounted, read-only.
SRCDIR = "/src"
#: Exit code and stderr marker the wrapper uses to report that it could not
#: stage the submission's files. 121 is outside the range a normal program is
#: likely to choose, and the marker disambiguates the rest.
STAGING_FAILED_EXIT = 121
STAGING_FAILED_MARKER = (
    "sentinel-judge: could not stage sources into the work directory"
)

#: Where build artefacts are written so they can outlive the container.
#: Only mounted when the caller asks for it — see `Sandbox.run(export_dir=...)`.
OUTDIR = "/out"


class SandboxStatus(str, Enum):
    COMPLETED = "completed"
    TIMEOUT = "timeout"
    MEMORY_EXCEEDED = "memory_exceeded"
    OUTPUT_EXCEEDED = "output_exceeded"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class SandboxLimits:
    """Per-run resource limits.

    Defaults are deliberately modest. A limit that is generous "just in case" is
    a limit that does not limit, and the case it was generous for is usually the
    hostile one.
    """

    wall_ms: int = 2_000
    memory_mb: int = 256
    pids: int = 64
    #: Bytes of stdout/stderr kept before the run is killed as a flood.
    stdout_kb: int = 64
    #: Size of the writable tmpfs at /box.
    workdir_mb: int = 32
    cpus: float = 1.0

    def __post_init__(self) -> None:
        if self.wall_ms <= 0 or self.memory_mb <= 0 or self.pids <= 0:
            raise ValueError("sandbox limits must be positive")


@dataclass
class ExecResult:
    status: SandboxStatus
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    truncated: bool = False
    container_id: str | None = None
    #: Populated when `status is INTERNAL_ERROR` — a judge problem, not the
    #: candidate's. Never shown to a candidate as if it were their fault.
    internal_detail: str | None = None


class SandboxError(RuntimeError):
    pass


def docker_bridge_gateway(default: str = "172.17.0.1") -> str:
    """The host address a normally-networked container can reach.

    Only used by the security tests, as the positive control for "the network is
    blocked". It lives here rather than in the test so that the test cannot
    quietly disagree with the runner about what "reachable" means.
    """
    try:
        out = subprocess.run(
            [
                "docker",
                "network",
                "inspect",
                "bridge",
                "-f",
                "{{(index .IPAM.Config 0).Gateway}}",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        return out.stdout.strip() or default
    except (subprocess.SubprocessError, OSError):
        return default


class _BoundedReader(threading.Thread):
    """Read a pipe until it closes or `cap` bytes arrive, then stop reading.

    Stopping matters. Reading everything a hostile program writes is how the
    *worker* runs out of memory instead of the sandbox, which converts a
    contained problem into an uncontained one.
    """

    def __init__(self, stream, cap: int) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._cap = cap
        self.data = bytearray()
        self.exceeded = threading.Event()

    def run(self) -> None:
        try:
            while True:
                chunk = self._stream.read(16 * 1024)
                if not chunk:
                    return
                if len(self.data) < self._cap:
                    self.data.extend(chunk[: self._cap - len(self.data)])
                    if len(self.data) >= self._cap:
                        self.exceeded.set()
                # Keep draining after the cap: a full pipe blocks the container,
                # and a blocked container cannot be killed cleanly.
        except (ValueError, OSError):
            return


@dataclass
class Sandbox:
    image: str
    runtime: str = field(default_factory=lambda: os.getenv("JUDGE_RUNTIME", "runc"))
    seccomp_profile: Path | None = field(
        default_factory=lambda: Path(
            os.getenv("JUDGE_SECCOMP_PROFILE", str(DEFAULT_SECCOMP))
        )
    )
    docker: str = "docker"

    # ---------------------------------------------------------------- public

    def run(
        self,
        argv: list[str],
        *,
        limits: SandboxLimits | None = None,
        stdin: bytes = b"",
        files: dict[str, bytes] | None = None,
        export_dir: Path | None = None,
        _unsafe_allow_network: bool = False,
        _unsafe_no_seccomp: bool = False,
    ) -> ExecResult:
        """Execute `argv` inside a fresh container and return what it did.

        The two `_unsafe_` flags exist for exactly one caller each: the security
        tests that establish positive controls. Asserting that a submission
        cannot reach the network proves nothing unless something demonstrably
        can, and asserting that seccomp blocks `socket()` proves nothing unless
        `socket()` works with the profile removed.

        They are named to be unpleasant to type, they are keyword-only, and
        `test_the_worker_never_disables_isolation` greps the worker for them.
        """
        limits = limits or SandboxLimits()
        cap = limits.stdout_kb * 1024

        try:
            return self._run(
                argv,
                limits,
                cap,
                stdin,
                files,
                export_dir,
                _unsafe_allow_network,
                _unsafe_no_seccomp,
            )
        except (SandboxError, OSError, subprocess.SubprocessError) as exc:
            # A missing image, an unreachable daemon, a full disk. None of these
            # are the candidate's doing, and none of them should take the worker
            # down — a crashed worker stops judging every other submission too.
            return ExecResult(
                status=SandboxStatus.INTERNAL_ERROR,
                exit_code=None,
                stdout="",
                stderr="",
                duration_ms=0,
                internal_detail=f"{type(exc).__name__}: {exc}",
            )

    def _run(
        self,
        argv: list[str],
        limits: SandboxLimits,
        cap: int,
        stdin: bytes,
        files: dict[str, bytes] | None,
        export_dir: Path | None,
        _unsafe_allow_network: bool,
        _unsafe_no_seccomp: bool,
    ) -> ExecResult:
        with tempfile.TemporaryDirectory(prefix="sentinel-judge-") as staging:
            self._stage(Path(staging), files or {})
            container_id = self._create(
                argv,
                limits,
                staging,
                _unsafe_allow_network,
                _unsafe_no_seccomp,
                export_dir=export_dir,
            )
            try:
                return self._start_and_wait(container_id, stdin, cap, limits)
            finally:
                self._remove(container_id)

    def container_exists(self, container_id: str) -> bool:
        result = subprocess.run(
            [self.docker, "container", "inspect", container_id],
            capture_output=True,
            timeout=30,
            check=False,
        )
        return result.returncode == 0

    # --------------------------------------------------------------- private

    @staticmethod
    def _stage(staging: Path, files: dict[str, bytes]) -> None:
        for name, content in files.items():
            # Reject anything that would escape the staging directory. The
            # caller is our own language adapter today, but a path from a
            # question's `starter` block is one refactor away from reaching here.
            candidate = (staging / name).resolve()
            if not str(candidate).startswith(str(staging.resolve()) + os.sep):
                raise SandboxError(
                    f"refusing to stage a file outside the work dir: {name!r}"
                )
            candidate.parent.mkdir(parents=True, exist_ok=True)
            candidate.write_bytes(content)
            # 0o755, not 0o644. Staged files include compiled binaries exported
            # from the compile container, and 0o644 silently stripped the
            # execute bit — `./program` then failed as a runtime error on every
            # test case, with an empty compile log and nothing to explain it.
            #
            # Granting execute to source files as well costs nothing: /box is a
            # copy the submission owns, so it could chmod them itself.
            candidate.chmod(0o755)
        staging.chmod(0o755)

    def _create(
        self,
        argv: list[str],
        limits: SandboxLimits,
        staging: str,
        allow_network: bool,
        no_seccomp: bool = False,
        export_dir: Path | None = None,
    ) -> str:
        name = f"sentinel-judge-{uuid.uuid4().hex[:16]}"
        # `cp` rather than mounting /src writable: the submission gets a private
        # copy on a size-capped tmpfs, so it cannot modify what the next run
        # will read, and compilers get somewhere to put object files.
        #
        # The copy failure is NOT swallowed. An earlier version ended this line
        # with `2>/dev/null || true`, and when the image turned out to have no
        # `cp` the submission ran against an empty directory and the candidate
        # got "No such file or directory: main.py" — a judge misconfiguration
        # presented as their bug. Staging is infrastructure: if it fails, that
        # is `internal_error`, and `STAGING_FAILED_EXIT` is how the parent
        # process finds out.
        # `-R`, not `-a`. `-a` implies `-p`, which tries to preserve timestamps
        # on the destination directory — and the sandbox user does not own
        # /box (it is root-owned, mode 1777), so `cp -a` fails with
        # "preserving times for '/box/.': Operation not permitted". Preserving
        # metadata is not wanted here anyway: the copy should belong to the
        # runtime user.
        wrapped = (
            f"cp -RL {SRCDIR}/. {WORKDIR}/ || "
            f'{{ echo "{STAGING_FAILED_MARKER}" >&2; exit {STAGING_FAILED_EXIT}; }}; '
            f"cd {WORKDIR} || exit {STAGING_FAILED_EXIT}; exec {shlex.join(argv)}"
        )

        cmd = [
            self.docker,
            "create",
            "--name",
            name,
            "--interactive",
            # Not a preference: Docker's default json-file driver is unbounded,
            # and a submission that writes forever fills the host's disk.
            "--log-driver=none",
            f"--runtime={self.runtime}",
            "--read-only",
            f"--tmpfs={WORKDIR}:rw,exec,size={limits.workdir_mb}m,mode=1777,nosuid,nodev",
            # /tmp is separate and tiny: plenty of toolchains insist on it, and
            # nothing should be able to use it as a spare disk.
            "--tmpfs=/tmp:rw,exec,size=16m,mode=1777,nosuid,nodev",
            f"--volume={staging}:{SRCDIR}:ro",
            f"--workdir={WORKDIR}",
            f"--user={SANDBOX_UID}:{SANDBOX_GID}",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            f"--pids-limit={limits.pids}",
            f"--memory={limits.memory_mb}m",
            # Equal to --memory, so the container cannot swap its way past the
            # cap and take the host's I/O down with it.
            f"--memory-swap={limits.memory_mb}m",
            "--memory-swappiness=0",
            f"--cpus={limits.cpus}",
            # A second line of defence against a runaway writer, below the tmpfs
            # cap, and one that also covers writes to /tmp.
            f"--ulimit=fsize={limits.workdir_mb * 1024 * 1024}",
            "--ulimit=nofile=256:256",
            "--ulimit=core=0",
            # Inherit nothing. The worker's environment holds DATABASE_URL and
            # REDIS_URL; a submission that could read them owns the deployment.
            "--env",
            "HOME=" + WORKDIR,
            "--env",
            "PATH=/usr/local/bin:/usr/bin:/bin",
            "--env",
            "LANG=C.UTF-8",
        ]

        if export_dir is not None:
            # A per-run, freshly created host directory, writable *only* here.
            #
            # Compiled languages need this: /box is a tmpfs that dies with the
            # container, so a binary built during the compile step would not
            # exist for the run step. The alternative — compiling on the host —
            # would leave the compiler outside the sandbox, and a compiler is a
            # program that runs candidate-supplied input.
            #
            # The exposure is bounded: the directory is empty at the start of
            # every run, is removed after it, and the `fsize` ulimit still caps
            # how much can be written into it. It is mounted `nosuid,nodev` and
            # the submission still cannot reach anything above it.
            export_dir.mkdir(parents=True, exist_ok=True)
            export_dir.chmod(0o777)
            cmd.append(f"--volume={export_dir}:{OUTDIR}:rw")
            cmd.append("--env")
            cmd.append(f"SENTINEL_OUT={OUTDIR}")

        if not allow_network:
            cmd.append("--network=none")

        if not no_seccomp and self.seccomp_profile and self.seccomp_profile.is_file():
            cmd.append(f"--security-opt=seccomp={self.seccomp_profile}")

        cmd += [self.image, "/bin/sh", "-c", wrapped]

        created = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120, check=False
        )
        if created.returncode != 0:
            raise SandboxError(f"docker create failed: {created.stderr.strip()}")
        return created.stdout.strip()

    def _start_and_wait(
        self, container_id: str, stdin: bytes, cap: int, limits: SandboxLimits
    ) -> ExecResult:
        started = time.monotonic()
        process = subprocess.Popen(
            [self.docker, "start", "--attach", "--interactive", container_id],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        out_reader = _BoundedReader(process.stdout, cap)
        err_reader = _BoundedReader(process.stderr, cap)
        out_reader.start()
        err_reader.start()

        def feed() -> None:
            try:
                if process.stdin:
                    process.stdin.write(stdin)
                    process.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        threading.Thread(target=feed, daemon=True).start()

        deadline = started + (limits.wall_ms / 1000)
        killed_for: str | None = None

        while True:
            if process.poll() is not None:
                break
            now = time.monotonic()
            if now >= deadline:
                killed_for = "timeout"
                break
            if out_reader.exceeded.is_set() or err_reader.exceeded.is_set():
                killed_for = "output"
                break
            time.sleep(0.02)

        if killed_for:
            self._kill(container_id)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()

        duration_ms = int((time.monotonic() - started) * 1000)
        out_reader.join(timeout=5)
        err_reader.join(timeout=5)

        state = self._inspect(container_id)
        exit_code = state.get("ExitCode")
        oom = bool(state.get("OOMKilled"))

        stdout = out_reader.data.decode("utf-8", "replace")
        stderr = err_reader.data.decode("utf-8", "replace")
        truncated = out_reader.exceeded.is_set() or err_reader.exceeded.is_set()

        # Order matters. We know why *we* killed it; Docker reports 137 for both
        # a timeout kill and an OOM kill, so the reason we recorded wins.
        if killed_for == "output":
            status = SandboxStatus.OUTPUT_EXCEEDED
        elif killed_for == "timeout":
            status = SandboxStatus.TIMEOUT
        elif exit_code == STAGING_FAILED_EXIT and STAGING_FAILED_MARKER in stderr:
            # The judge's fault, not the candidate's. Returning this as a normal
            # failure would score a working submission zero.
            return ExecResult(
                status=SandboxStatus.INTERNAL_ERROR,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                duration_ms=duration_ms,
                truncated=truncated,
                container_id=container_id,
                # Report what actually happened rather than guessing. The
                # first guess written here was "the image lacks cp", and the
                # real cause was a permission error on the destination — a
                # misleading diagnostic sends the next person somewhere useless.
                internal_detail=(
                    "could not stage the submission into the work directory: "
                    + " / ".join(
                        line
                        for line in stderr.splitlines()
                        if line and STAGING_FAILED_MARKER not in line
                    )
                    or "no further detail"
                ),
            )
        elif oom or (exit_code == 137 and not killed_for) or "MemoryError" in stderr:
            status = SandboxStatus.MEMORY_EXCEEDED
        else:
            status = SandboxStatus.COMPLETED

        return ExecResult(
            status=status,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            truncated=truncated,
            container_id=container_id,
        )

    def _inspect(self, container_id: str) -> dict:
        result = subprocess.run(
            [
                self.docker,
                "container",
                "inspect",
                container_id,
                "-f",
                "{{json .State}}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            return {}
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {}

    def _kill(self, container_id: str) -> None:
        subprocess.run(
            [self.docker, "kill", "--signal=KILL", container_id],
            capture_output=True,
            timeout=60,
            check=False,
        )

    def _remove(self, container_id: str) -> None:
        """Always, on every path. A leaked container keeps its CPU share, its
        memory, and — with the wrong log driver — the host's disk."""
        subprocess.run(
            [self.docker, "rm", "--force", "--volumes", container_id],
            capture_output=True,
            timeout=60,
            check=False,
        )


def docker_available(docker: str = "docker") -> bool:
    if shutil.which(docker) is None:
        return False
    return (
        subprocess.run(
            [docker, "info"], capture_output=True, timeout=30, check=False
        ).returncode
        == 0
    )
