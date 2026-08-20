"""The Phase 3 gate: the judge sandbox must contain hostile code.

MVP_SCOPE.md §5 makes this a gate rather than a preference — "All sandbox
security tests pass before the runner is called complete. Fork bomb, memory
bomb, network egress, FS traversal, /proc, stdout flood all contained." This
file was written before `sentinel_judge.sandbox` existed, and every test in it
failed on `ImportError` until it did.

Two rules govern how these tests are written.

**Every containment test needs a positive control.** "The submission could not
reach the network" proves nothing if nothing in the environment can reach the
network. Each test that asserts a control blocks something also asserts that
the same thing succeeds when the control is removed. A security test that would
pass against a broken sandbox is worse than no test, because it is believed.

**Nothing here is a mock.** These run real containers through the real runner.
A sandbox tested against a fake container runtime is a sandbox tested against
nothing.
"""

from __future__ import annotations

import socket
import textwrap
import threading
import time

import pytest
from sentinel_judge.sandbox import Sandbox, SandboxLimits, SandboxStatus

pytestmark = pytest.mark.security


PY = "/usr/bin/python3.11"


def script(source: str) -> list[str]:
    return [PY, "-c", textwrap.dedent(source)]


# ---------------------------------------------------------------------------
# 1. Fork bomb
# ---------------------------------------------------------------------------


def test_fork_bomb_is_contained(sandbox: Sandbox) -> None:
    """A submission must not be able to exhaust the host's process table.

    The assertion is not "it failed" — a fork bomb that brought the host down
    would also make the container fail. It is that the number of processes the
    submission managed to create is bounded by the limit we set.
    """
    result = sandbox.run(
        script(
            """
            import os, sys
            n = 0
            try:
                while n < 5000:
                    if os.fork() == 0:
                        os._exit(0)
                    n += 1
            except OSError:
                pass
            sys.stdout.write(str(n))
            """
        ),
        limits=SandboxLimits(wall_ms=10_000, memory_mb=128, pids=32),
    )
    assert result.status is not SandboxStatus.INTERNAL_ERROR, result.stderr
    forked = int(result.stdout.strip() or 0)
    assert forked < 32, (
        f"the submission created {forked} processes against a limit of 32"
    )


def test_the_pid_limit_is_what_stops_it(sandbox: Sandbox) -> None:
    """The positive control for the test above.

    Raise the limit and the same code forks further. Without this, a sandbox
    that simply failed to run Python at all would pass the fork-bomb test.
    """
    source = script(
        """
        import os, sys
        n = 0
        try:
            while n < 200:
                if os.fork() == 0:
                    os._exit(0)
                n += 1
        except OSError:
            pass
        sys.stdout.write(str(n))
        """
    )
    tight = sandbox.run(
        source, limits=SandboxLimits(wall_ms=15_000, memory_mb=256, pids=16)
    )
    loose = sandbox.run(
        source, limits=SandboxLimits(wall_ms=15_000, memory_mb=256, pids=128)
    )
    assert int(loose.stdout.strip() or 0) > int(tight.stdout.strip() or 0)


# ---------------------------------------------------------------------------
# 2. Memory bomb
# ---------------------------------------------------------------------------


def test_memory_bomb_is_killed_not_merely_slow(sandbox: Sandbox) -> None:
    result = sandbox.run(
        script(
            """
            blocks = []
            while True:
                blocks.append(bytearray(4 * 1024 * 1024))
            """
        ),
        limits=SandboxLimits(wall_ms=20_000, memory_mb=64, pids=32),
    )
    assert result.status is SandboxStatus.MEMORY_EXCEEDED, (
        f"expected MEMORY_EXCEEDED, got {result.status} (exit {result.exit_code})"
    )


def test_a_program_inside_the_memory_limit_is_untouched(sandbox: Sandbox) -> None:
    """Positive control: the limit must not be killing everything."""
    result = sandbox.run(
        script(
            """
            import sys
            block = bytearray(8 * 1024 * 1024)
            sys.stdout.write(str(len(block)))
            """
        ),
        limits=SandboxLimits(wall_ms=10_000, memory_mb=128, pids=32),
    )
    assert result.status is SandboxStatus.COMPLETED, result.stderr
    assert result.stdout.strip() == str(8 * 1024 * 1024)


# ---------------------------------------------------------------------------
# 3. Network egress
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def reachable_listener() -> tuple[str, int]:
    """A TCP listener the *host* can reach, so "blocked" means something.

    This is the positive control for the network tests. Asserting that a
    submission cannot reach the internet is worthless in an environment with no
    internet: the assertion passes for the wrong reason. A listener on the
    Docker bridge gateway is reachable from a normally-networked container and
    not from `--network=none`, which is exactly the difference under test.
    """
    from sentinel_judge.sandbox import docker_bridge_gateway

    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", 0))
    port = server.getsockname()[1]
    server.listen(8)
    stop = threading.Event()

    def serve() -> None:
        server.settimeout(0.5)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except (TimeoutError, OSError):
                continue
            conn.sendall(b"SENTINEL-REACHED")
            conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield docker_bridge_gateway(), port
    stop.set()
    thread.join(timeout=2)
    server.close()


def _connect_script(host: str, port: int) -> list[str]:
    return script(
        f"""
        import socket, sys
        try:
            s = socket.create_connection(({host!r}, {port}), timeout=5)
            sys.stdout.write(s.recv(32).decode())
            s.close()
        except Exception as exc:
            sys.stdout.write("BLOCKED:" + type(exc).__name__)
        """
    )


def test_the_listener_is_actually_reachable_without_the_control(
    sandbox: Sandbox, reachable_listener: tuple[str, int]
) -> None:
    """The positive control itself. If this fails, the network tests below are
    vacuous and must not be trusted."""
    host, port = reachable_listener
    result = sandbox.run(
        _connect_script(host, port),
        limits=SandboxLimits(wall_ms=15_000, memory_mb=128, pids=32),
        # Both controls off. Leaving seccomp on would block `socket()` and the
        # control would fail for a reason that has nothing to do with routing —
        # which is how a positive control quietly stops being one.
        _unsafe_allow_network=True,
        _unsafe_no_seccomp=True,
    )
    assert "SENTINEL-REACHED" in result.stdout, (
        "the control listener was not reachable even with isolation disabled; "
        f"the egress tests would pass vacuously. stdout={result.stdout!r}"
    )


def test_the_network_namespace_alone_blocks_egress(
    sandbox: Sandbox, reachable_listener: tuple[str, int]
) -> None:
    """Isolate one control at a time.

    With seccomp removed, `socket()` succeeds — so anything that still fails is
    the network namespace doing its job, not the syscall filter. Without this,
    the egress result would be attributable to either control and to neither
    specifically.
    """
    host, port = reachable_listener
    result = sandbox.run(
        _connect_script(host, port),
        limits=SandboxLimits(wall_ms=15_000, memory_mb=128, pids=32),
        _unsafe_no_seccomp=True,
    )
    assert "SENTINEL-REACHED" not in result.stdout
    assert result.stdout.startswith("BLOCKED:"), result.stdout


def test_submission_cannot_reach_the_network(
    sandbox: Sandbox, reachable_listener: tuple[str, int]
) -> None:
    host, port = reachable_listener
    result = sandbox.run(
        _connect_script(host, port),
        limits=SandboxLimits(wall_ms=15_000, memory_mb=128, pids=32),
    )
    assert "SENTINEL-REACHED" not in result.stdout
    assert result.stdout.startswith("BLOCKED:"), result.stdout


def test_submission_cannot_even_create_a_socket(sandbox: Sandbox) -> None:
    """Defence in depth: `--network=none` removes the route, seccomp removes the
    syscall. Either alone would do; a bug in one should not be sufficient."""
    result = sandbox.run(
        script(
            """
            import socket, sys
            try:
                socket.socket()
                sys.stdout.write("CREATED")
            except OSError as exc:
                sys.stdout.write("BLOCKED:%d" % exc.errno)
            """
        ),
        limits=SandboxLimits(wall_ms=10_000, memory_mb=128, pids=32),
    )
    assert result.stdout.startswith("BLOCKED:"), result.stdout


# ---------------------------------------------------------------------------
# 4. Filesystem
# ---------------------------------------------------------------------------


def test_root_filesystem_is_read_only(sandbox: Sandbox) -> None:
    result = sandbox.run(
        script(
            """
            import sys
            for path in ("/etc/passwd", "/usr/bin/evil", "/evil"):
                try:
                    open(path, "w").write("x")
                    sys.stdout.write("WROTE:%s " % path)
                except OSError as exc:
                    sys.stdout.write("refused:%d " % exc.errno)
            """
        ),
        limits=SandboxLimits(wall_ms=10_000, memory_mb=128, pids=32),
    )
    assert "WROTE:" not in result.stdout, result.stdout


def test_the_work_directory_is_writable(sandbox: Sandbox) -> None:
    """Positive control, and a real requirement: compilers need scratch space."""
    result = sandbox.run(
        script(
            """
            import sys
            open("scratch.txt", "w").write("hello")
            sys.stdout.write(open("scratch.txt").read())
            """
        ),
        limits=SandboxLimits(wall_ms=10_000, memory_mb=128, pids=32),
    )
    assert result.stdout.strip() == "hello", result.stderr


def test_path_traversal_out_of_the_work_directory_finds_nothing_writable(
    sandbox: Sandbox,
) -> None:
    """Every write is unbuffered and flushed inside the `try`.

    The first version of this test used `open(path, "wb").write(b"x")` and
    reported a write to `/proc/self/mem` as succeeding. It had not succeeded —
    Python buffers, and the failing syscall happened at garbage-collection time,
    outside the `except`. A security test that reports a breach that did not
    happen is as bad as one that misses a breach that did: both destroy trust in
    the result. `os.open` + `os.write` go straight to the kernel.
    """
    result = sandbox.run(
        script(
            """
            import os, sys
            for path in ("../../../etc/shadow", "/../../etc/hosts", "/etc/passwd"):
                try:
                    fd = os.open(path, os.O_WRONLY)
                    os.write(fd, b"x")
                    os.close(fd)
                    sys.stdout.write("WROTE:%s " % path)
                except OSError as exc:
                    sys.stdout.write("refused:%d " % exc.errno)
            """
        ),
        limits=SandboxLimits(wall_ms=10_000, memory_mb=128, pids=32),
    )
    assert "WROTE:" not in result.stdout, result.stdout


def test_there_is_no_higher_privileged_process_to_attack(sandbox: Sandbox) -> None:
    """A documented non-finding, plus the claim that actually matters.

    `/proc/self/mem` and `/proc/1/mem` are both *openable* from a submission,
    and neither is a finding. PID 1 inside the namespace is the submission's own
    shell wrapper, running as the same unprivileged uid: writing to it is
    writing to memory the submission already owns, which it could do with a
    pointer. An earlier version of this test asserted the open would fail, which
    was simply wrong, and "fixing" the sandbox to satisfy it would have bought
    nothing.

    The property with actual security content is this one: inside the namespace
    there is no process running as anyone more privileged, so same-uid memory
    access crosses no boundary. Combined with
    `test_proc_does_not_expose_the_host` — which proves the host's processes are
    not visible at all — that closes the class.
    """
    result = sandbox.run(
        script(
            """
            import os, sys
            uids = set()
            for entry in os.listdir("/proc"):
                if not entry.isdigit():
                    continue
                try:
                    with open("/proc/%s/status" % entry) as fh:
                        for line in fh:
                            if line.startswith("Uid:"):
                                uids.add(int(line.split()[1]))
                                break
                except OSError:
                    pass
            sys.stdout.write(",".join(str(u) for u in sorted(uids)))
            """
        ),
        limits=SandboxLimits(wall_ms=10_000, memory_mb=128, pids=32),
    )
    uids = {int(u) for u in result.stdout.strip().split(",") if u}
    assert uids, f"could not read any process uids: {result.stdout!r} {result.stderr!r}"
    assert 0 not in uids, (
        f"a root-owned process is visible inside the sandbox: uids={uids}"
    )
    assert uids == {65534}, f"unexpected uids inside the sandbox: {uids}"


def test_the_host_docker_socket_is_not_visible(sandbox: Sandbox) -> None:
    """The judge worker mounts the Docker socket; a *submission* must never see
    it. Reaching it would be a full container escape — root on the host."""
    result = sandbox.run(
        script(
            """
            import os, sys
            sys.stdout.write("PRESENT" if os.path.exists("/var/run/docker.sock") else "absent")
            """
        ),
        limits=SandboxLimits(wall_ms=10_000, memory_mb=128, pids=32),
    )
    assert result.stdout.strip() == "absent", (
        "the Docker socket is reachable from a submission"
    )


def test_a_submission_cannot_fill_the_disk(sandbox: Sandbox) -> None:
    """The work directory is a size-capped tmpfs, so a write loop hits ENOSPC
    long before the host notices."""
    result = sandbox.run(
        script(
            """
            import sys
            written = 0
            try:
                with open("big.bin", "wb") as fh:
                    while written < 512 * 1024 * 1024:
                        fh.write(b"\\0" * (1024 * 1024))
                        fh.flush()
                        written += 1024 * 1024
            except OSError as exc:
                sys.stdout.write("stopped:%d:%d" % (written, exc.errno))
                raise SystemExit(0)
            sys.stdout.write("WROTE_EVERYTHING:%d" % written)
            """
        ),
        limits=SandboxLimits(wall_ms=30_000, memory_mb=256, pids=32, workdir_mb=16),
    )
    assert "WROTE_EVERYTHING" not in result.stdout, result.stdout
    assert result.stdout.startswith("stopped:"), result.stdout
    written = int(result.stdout.split(":")[1])
    assert written <= 32 * 1024 * 1024, f"wrote {written} bytes into a 16 MiB tmpfs"


# ---------------------------------------------------------------------------
# 5. /proc and the host
# ---------------------------------------------------------------------------


def test_proc_does_not_expose_the_host(sandbox: Sandbox) -> None:
    """A container's /proc is masked, but the check that matters is that the
    submission sees its own PID namespace and not the host's process list."""
    result = sandbox.run(
        script(
            """
            import os, sys
            pids = sorted(int(p) for p in os.listdir("/proc") if p.isdigit())
            sys.stdout.write("%d:%s" % (len(pids), max(pids)))
            """
        ),
        limits=SandboxLimits(wall_ms=10_000, memory_mb=128, pids=32),
    )
    count, highest = result.stdout.strip().split(":")
    assert int(count) < 10, f"the submission can see {count} processes"
    assert int(highest) < 100, (
        f"highest visible pid {highest} looks like the host's namespace"
    )


def test_sensitive_proc_and_sys_paths_are_not_readable(sandbox: Sandbox) -> None:
    result = sandbox.run(
        script(
            """
            import sys
            for path in ("/proc/kcore", "/proc/sysrq-trigger", "/sys/kernel/security"):
                try:
                    with open(path, "rb") as fh:
                        fh.read(16)
                    sys.stdout.write("READ:%s " % path)
                except OSError:
                    sys.stdout.write("refused ")
            """
        ),
        limits=SandboxLimits(wall_ms=10_000, memory_mb=128, pids=32),
    )
    assert "READ:" not in result.stdout, result.stdout


def test_the_submission_does_not_run_as_root(sandbox: Sandbox) -> None:
    result = sandbox.run(
        script(
            """
            import os, sys
            sys.stdout.write("%d:%d" % (os.getuid(), os.getgid()))
            """
        ),
        limits=SandboxLimits(wall_ms=10_000, memory_mb=128, pids=32),
    )
    uid, gid = (int(x) for x in result.stdout.strip().split(":"))
    assert uid != 0 and gid != 0, f"submission ran as uid={uid} gid={gid}"


def test_privileges_cannot_be_regained(sandbox: Sandbox) -> None:
    result = sandbox.run(
        script(
            """
            import os, sys
            try:
                os.setuid(0)
                sys.stdout.write("ESCALATED")
            except OSError as exc:
                sys.stdout.write("refused:%d" % exc.errno)
            """
        ),
        limits=SandboxLimits(wall_ms=10_000, memory_mb=128, pids=32),
    )
    assert result.stdout.startswith("refused:"), result.stdout


# ---------------------------------------------------------------------------
# 6. Output flood
# ---------------------------------------------------------------------------


def test_stdout_flood_is_truncated_and_the_run_ends(sandbox: Sandbox) -> None:
    """An unbounded writer must not be able to exhaust the host's disk or RAM.

    This is not hypothetical. During Phase 3 development a flood container run
    with Docker's default json-file log driver wrote 15 GB in 80 seconds and
    filled the build machine's disk — which is why the runner sets
    `--log-driver=none` and reads a bounded number of bytes.
    """
    started = time.monotonic()
    result = sandbox.run(
        script(
            """
            import sys
            while True:
                sys.stdout.write("A" * 4096)
            """
        ),
        limits=SandboxLimits(wall_ms=20_000, memory_mb=128, pids=32, stdout_kb=64),
    )
    elapsed = time.monotonic() - started

    assert result.status is SandboxStatus.OUTPUT_EXCEEDED, result.status
    assert result.truncated is True
    assert len(result.stdout) <= 64 * 1024 * 2, f"kept {len(result.stdout)} bytes"
    assert elapsed < 25, f"the flood ran for {elapsed:.1f}s before being stopped"


def test_stderr_flood_is_also_bounded(sandbox: Sandbox) -> None:
    result = sandbox.run(
        script(
            """
            import sys
            while True:
                sys.stderr.write("E" * 4096)
            """
        ),
        limits=SandboxLimits(wall_ms=20_000, memory_mb=128, pids=32, stdout_kb=64),
    )
    assert len(result.stderr) <= 64 * 1024 * 2, (
        f"kept {len(result.stderr)} bytes of stderr"
    )


# ---------------------------------------------------------------------------
# 7. Time
# ---------------------------------------------------------------------------


def test_an_infinite_loop_is_killed_at_the_wall_clock_limit(sandbox: Sandbox) -> None:
    started = time.monotonic()
    result = sandbox.run(
        script("while True:\n    pass\n"),
        limits=SandboxLimits(wall_ms=2_000, memory_mb=128, pids=32),
    )
    elapsed = time.monotonic() - started
    assert result.status is SandboxStatus.TIMEOUT, result.status
    assert elapsed < 20, f"took {elapsed:.1f}s to enforce a 2s limit"


def test_a_sleeping_process_cannot_outlive_the_limit(sandbox: Sandbox) -> None:
    """Blocked-on-syscall is the case a CPU-time limit misses and a wall-clock
    limit catches. A submission that sleeps forever is still a submission that
    never finishes."""
    result = sandbox.run(
        script("import time\ntime.sleep(600)\n"),
        limits=SandboxLimits(wall_ms=2_000, memory_mb=128, pids=32),
    )
    assert result.status is SandboxStatus.TIMEOUT


def test_the_container_is_gone_after_a_timeout(sandbox: Sandbox) -> None:
    """A killed *client* does not stop a container.

    Found the hard way: `timeout 60 docker run ...` kills the CLI and leaves the
    container running, still writing to its log. The runner must stop the
    container itself and must confirm it is gone, or a timed-out submission
    keeps burning CPU on the judge host forever.
    """
    result = sandbox.run(
        script("import time\ntime.sleep(600)\n"),
        limits=SandboxLimits(wall_ms=1_500, memory_mb=128, pids=32),
    )
    assert result.status is SandboxStatus.TIMEOUT
    assert result.container_id is not None
    assert not sandbox.container_exists(result.container_id), (
        "the container survived the run that was supposed to kill it"
    )


# ---------------------------------------------------------------------------
# 8. Isolation between submissions
# ---------------------------------------------------------------------------


def test_one_submission_cannot_leave_anything_for_the_next(sandbox: Sandbox) -> None:
    """Each run gets a fresh container and a fresh tmpfs. A submission that
    writes an answer key to disk must not find it there next time — including
    when the next submission is another candidate's."""
    first = sandbox.run(
        script(
            """
            import sys
            open("/tmp/leak", "w").write("secret")
            open("planted.txt", "w").write("secret")
            sys.stdout.write("planted")
            """
        ),
        limits=SandboxLimits(wall_ms=10_000, memory_mb=128, pids=32),
    )
    assert first.stdout.strip() == "planted", first.stderr

    second = sandbox.run(
        script(
            """
            import os, sys
            found = [p for p in ("/tmp/leak", "planted.txt") if os.path.exists(p)]
            sys.stdout.write(",".join(found) or "clean")
            """
        ),
        limits=SandboxLimits(wall_ms=10_000, memory_mb=128, pids=32),
    )
    assert second.stdout.strip() == "clean", f"leaked: {second.stdout}"


def test_environment_carries_no_host_secrets(sandbox: Sandbox) -> None:
    """The judge worker holds a database URL and a Redis URL. Inheriting the
    worker's environment into the sandbox would hand them to every candidate."""
    result = sandbox.run(
        script(
            """
            import os, sys
            leaked = [k for k in os.environ
                      if any(s in k.upper() for s in
                             ("DATABASE", "REDIS", "SECRET", "KEY", "PASSWORD", "TOKEN", "S3"))]
            sys.stdout.write(",".join(sorted(leaked)) or "clean")
            """
        ),
        limits=SandboxLimits(wall_ms=10_000, memory_mb=128, pids=32),
    )
    assert result.stdout.strip() == "clean", f"leaked env vars: {result.stdout}"


# ---------------------------------------------------------------------------
# 9. The escape hatches stay in the tests
# ---------------------------------------------------------------------------


def test_the_worker_never_disables_isolation() -> None:
    """`_unsafe_allow_network` and `_unsafe_no_seccomp` exist for the positive
    controls above and for nothing else.

    They are keyword-only and unpleasantly named, which stops them being passed
    by accident but not on purpose. This asserts that no production code path
    passes them — a grep, deliberately, because the alternative is trusting that
    nobody ever will.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    production = [
        *(root / "workers" / "judge" / "sentinel_judge").rglob("*.py"),
        *(root / "apps" / "api" / "sentinel_api").rglob("*.py"),
    ]

    offenders = []
    for path in production:
        text = path.read_text(encoding="utf-8")
        for flag in ("_unsafe_allow_network=True", "_unsafe_no_seccomp=True"):
            if flag in text:
                offenders.append(f"{path.relative_to(root)}: {flag}")

    assert not offenders, "production code disables a sandbox control: " + "; ".join(
        offenders
    )


def test_every_sandbox_run_sets_the_controls_that_matter() -> None:
    """A structural check on the argv the runner builds.

    Behavioural tests prove each control works today. This proves nobody deleted
    one during a refactor and left a test passing for an unrelated reason — the
    flags are the security surface, so their absence should fail loudly rather
    than quietly reduce isolation.
    """
    import inspect

    from sentinel_judge import sandbox as sandbox_module

    # The flags are f-strings, so match the prefix rather than the rendered
    # value — asserting `--user=65534` would be asserting against the *source*
    # of a template that never contains it.
    source = inspect.getsource(sandbox_module.Sandbox._create)
    for flag in (
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--log-driver=none",
        "--pids-limit=",
        "--memory=",
        "--memory-swap=",
        "--user=",
        "--ulimit=fsize=",
        "--tmpfs=",
    ):
        assert flag in source, f"the sandbox no longer passes {flag}"

    # And that the rendered values are the ones intended.
    assert sandbox_module.SANDBOX_UID != 0
    assert sandbox_module.SANDBOX_GID != 0
