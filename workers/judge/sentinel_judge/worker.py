"""The judge worker: claim a run, judge it, write the verdict.

Runs as its own process, outside the API. That separation is the point — this
is the only component that talks to a container runtime, and in development it
holds the Docker socket, which is root-equivalent on the host. Nothing that
serves an HTTP request should be able to reach it.

Everything this module writes, it writes as the `system` role
(`core.db.SERVER_ROLE`). A candidate cannot write their own verdict; migration
0003 enforces that at the database, and this is the code path that respects it.
"""

from __future__ import annotations

import hashlib
import logging
import os
import signal
import sys
import time
import uuid
from dataclasses import dataclass

from sentinel_judge import languages
from sentinel_judge.queue import JudgeJob, JudgeQueue, worker_id
from sentinel_judge.runner import (
    CANCELLED,
    INTERNAL_ERROR,
    RUNNING,
    JudgeRunner,
    TestCase,
)
from sentinel_judge.sandbox import Sandbox

log = logging.getLogger("sentinel.judge")


@dataclass
class WorkerConfig:
    image_for: dict[str, str]
    poll_timeout: int = 5
    default_time_ms: int = 2000
    default_memory_mb: int = 256

    @classmethod
    def from_env(cls) -> WorkerConfig:
        return cls(
            image_for={
                "python311": os.getenv(
                    "JUDGE_IMAGE_PYTHON311", "sentinel-judge-python311:latest"
                ),
                "cpp20": os.getenv("JUDGE_IMAGE_CPP20", "sentinel-judge-cpp20:latest"),
                "java17": os.getenv(
                    "JUDGE_IMAGE_JAVA17", "sentinel-judge-java17:latest"
                ),
            },
            default_time_ms=int(os.getenv("JUDGE_DEFAULT_TIME_LIMIT_MS", "2000")),
            default_memory_mb=int(os.getenv("JUDGE_DEFAULT_MEMORY_MB", "256")),
        )


def source_digest(source: str) -> bytes:
    return hashlib.sha256(source.encode("utf-8")).digest()


class JudgeWorker:
    """One worker. Concurrency is more processes, not threads.

    Threads would share a Docker client and a database pool for work whose whole
    purpose is to be isolated from everything else. Processes cost more memory
    and are much easier to reason about when one of them is killed.
    """

    def __init__(
        self, queue: JudgeQueue, session_factory, config: WorkerConfig
    ) -> None:
        self._queue = queue
        self._session_factory = session_factory
        self._config = config
        self._id = worker_id()
        self._running = True

    def stop(self, *_: object) -> None:
        """Finish the current run, then exit.

        Not an immediate exit: a submission abandoned halfway leaves a candidate
        watching a spinner, and a container behind.
        """
        log.info("judge worker %s draining", self._id)
        self._running = False

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        log.info("judge worker %s started", self._id)

        last_sweep = 0.0
        while self._running:
            now = time.time()
            if now - last_sweep > 60:
                recovered = self._queue.requeue_stale()
                if recovered:
                    log.warning("requeued %d abandoned run(s)", recovered)
                last_sweep = now

            job = self._queue.claim(self._id, timeout=self._config.poll_timeout)
            if job is None:
                continue
            try:
                self.process(job)
            except Exception:
                log.exception("run %s failed unexpectedly", job.run_id)
                self._fail(job, "the judge worker raised an unhandled exception")
            finally:
                self._queue.acknowledge(self._id, job)

        log.info("judge worker %s stopped", self._id)

    # ------------------------------------------------------------- one job

    def process(self, job: JudgeJob) -> None:
        from sentinel_api.models import (
            CodeSubmission,
            JudgeRun,
            JudgeTestResult,
            SessionPaperItem,
        )
        from sentinel_api.models import (
            TestCase as TestCaseRow,
        )
        from sqlalchemy import select, update

        mode = "sample"
        fraction = 0.0
        scored = False

        with self._session_factory(org_id=str(job.org_id)) as session:
            # Claim by conditional UPDATE. The queue can deliver a job twice on
            # purpose (crash recovery), and judging twice would write two sets
            # of results and two scores.
            claimed = session.execute(
                update(JudgeRun)
                .where(JudgeRun.id == job.run_id, JudgeRun.status == "queued")
                .values(status=RUNNING, worker_id=self._id, started_at=_now())
                .returning(JudgeRun.id)
            ).first()
            if claimed is None:
                log.info("run %s was already claimed; skipping", job.run_id)
                return

            submission = session.get(CodeSubmission, job.submission_id)
            if submission is None:
                self._mark(
                    session, job.run_id, INTERNAL_ERROR, "the submission has gone"
                )
                session.commit()
                return
            mode = submission.mode

            cases = session.scalars(
                select(TestCaseRow)
                .join(
                    SessionPaperItem,
                    SessionPaperItem.question_version_id
                    == TestCaseRow.question_version_id,
                )
                .where(SessionPaperItem.id == submission.paper_item_id)
                .order_by(TestCaseRow.ordinal)
            ).all()

            # A `sample` run is the candidate pressing "Run": it executes only
            # the tests they can already see, so it can be fast and can show
            # everything. A `final` run executes the hidden suite too and is the
            # only one that scores.
            wanted = [c for c in cases if mode == "final" or c.is_sample]
            if not wanted:
                self._mark(
                    session,
                    job.run_id,
                    INTERNAL_ERROR,
                    "the question has no runnable test cases",
                )
                session.commit()
                return

            image = self._config.image_for.get(submission.language)
            if not image:
                self._mark(
                    session,
                    job.run_id,
                    INTERNAL_ERROR,
                    f"no sandbox image configured for {submission.language}",
                )
                session.commit()
                return

            runner = JudgeRunner(
                Sandbox(image=image),
                default_time_ms=self._config.default_time_ms,
                default_memory_mb=self._config.default_memory_mb,
            )
            outcome = runner.judge(
                language=languages.get(submission.language),
                source=submission.source,
                tests=[
                    TestCase(
                        ordinal=c.ordinal,
                        is_sample=c.is_sample,
                        weight=float(c.weight),
                        stdin=c.stdin_inline or "",
                        expected=c.expected_inline or "",
                        comparator=c.comparator,
                        comparator_config=c.comparator_config or {},
                        time_limit_ms=c.time_limit_ms,
                        memory_limit_mb=c.memory_limit_mb,
                    )
                    for c in wanted
                ],
                stop_on_first_failure=mode == "sample",
            )

            by_ordinal = {c.ordinal: c for c in wanted}
            for result in outcome.outcomes:
                case = by_ordinal.get(result.ordinal)
                if case is None:  # pragma: no cover - defensive
                    continue
                session.add(
                    JudgeTestResult(
                        org_id=job.org_id,
                        run_id=job.run_id,
                        test_case_id=case.id,
                        ordinal=result.ordinal,
                        passed=result.passed,
                        status=result.status,
                        duration_ms=result.duration_ms,
                        # `runner._visible` already decided what may be shown;
                        # this only persists what it allowed through, so a
                        # hidden test's output never reaches the database.
                        stdout_excerpt=result.stdout,
                        stderr_excerpt=(result.detail or {}).get("stderr"),
                    )
                )

            session.execute(
                update(JudgeRun)
                .where(JudgeRun.id == job.run_id)
                .values(
                    status=outcome.status,
                    finished_at=_now(),
                    queue_delay_ms=job.queue_delay_ms,
                    execute_ms=outcome.max_duration_ms,
                    compile_output=outcome.compile_output or None,
                    error_detail=outcome.internal_detail,
                    tests_total=len(outcome.outcomes),
                    tests_passed=sum(1 for t in outcome.outcomes if t.passed),
                    score=round(outcome.fraction, 3),
                    image_digest=image,
                )
            )
            session.commit()
            fraction = outcome.fraction
            scored = outcome.status not in (INTERNAL_ERROR, CANCELLED)

        # Only a `final` run marks the paper, and only if the judge itself was
        # healthy: an infrastructure failure must not overwrite a good mark with
        # a zero.
        if mode == "final" and scored:
            self._apply_score(job, fraction)

    # --------------------------------------------------------------- utils

    def _apply_score(self, job: JudgeJob, fraction: float) -> None:
        """Write the mark for the coding item.

        Kept in its own transaction, and deliberately an upsert: a re-judge
        (a fixed test case, a re-run after an infrastructure failure) must
        replace the mark rather than add to it.
        """
        from sentinel_api.models import CodeSubmission, QuestionScore, SessionPaperItem
        from sqlalchemy.dialects.postgresql import insert

        with self._session_factory(org_id=str(job.org_id)) as session:
            submission = session.get(CodeSubmission, job.submission_id)
            if submission is None:  # pragma: no cover - defensive
                return
            item = session.get(SessionPaperItem, submission.paper_item_id)
            if item is None:  # pragma: no cover - defensive
                return
            awarded = round(float(item.marks) * max(0.0, min(1.0, fraction)), 3)
            session.execute(
                insert(QuestionScore)
                .values(
                    org_id=job.org_id,
                    session_id=submission.session_id,
                    paper_item_id=item.id,
                    awarded=awarded,
                    max_marks=float(item.marks),
                    grader="judge",
                    detail={"fraction": round(fraction, 3), "run_id": str(job.run_id)},
                    graded_at=_now(),
                )
                .on_conflict_do_update(
                    index_elements=[
                        QuestionScore.session_id,
                        QuestionScore.paper_item_id,
                    ],
                    set_={
                        "awarded": awarded,
                        "max_marks": float(item.marks),
                        "grader": "judge",
                        "detail": {
                            "fraction": round(fraction, 3),
                            "run_id": str(job.run_id),
                        },
                        "graded_at": _now(),
                    },
                )
            )
            _refresh_session_total(session, submission.session_id, job.org_id)
            session.commit()

    def _fail(self, job: JudgeJob, detail: str) -> None:
        with self._session_factory(org_id=str(job.org_id)) as session:
            self._mark(session, job.run_id, INTERNAL_ERROR, detail)
            session.commit()

    @staticmethod
    def _mark(session, run_id: uuid.UUID, status: str, detail: str) -> None:
        from sentinel_api.models import JudgeRun
        from sqlalchemy import update

        session.execute(
            update(JudgeRun)
            .where(JudgeRun.id == run_id)
            .values(status=status, finished_at=_now(), error_detail=detail)
        )


def _refresh_session_total(session, session_id: uuid.UUID, org_id: uuid.UUID) -> None:
    """Recompute `session_result` from `question_score`.

    The judge scores one item, asynchronously, long after the session was
    submitted and auto-graded. Adding to the stored total would drift; deriving
    it from the per-question rows cannot.
    """
    from sentinel_api.models import SessionResult
    from sqlalchemy import func as sa_func
    from sqlalchemy import select, update

    totals = session.execute(
        select(
            sa_func.coalesce(sa_func.sum(_qs().awarded), 0),
            sa_func.coalesce(sa_func.sum(_qs().max_marks), 0),
        ).where(_qs().session_id == session_id)
    ).first()
    if totals is None:  # pragma: no cover - defensive
        return
    total, maximum = (max(0.0, float(totals[0])), float(totals[1]))
    session.execute(
        update(SessionResult)
        .where(SessionResult.session_id == session_id)
        .values(total_marks=round(total, 3), max_marks=round(maximum, 3))
    )


def _qs():
    from sentinel_api.models import QuestionScore

    return QuestionScore


def _now():
    import datetime as _dt

    return _dt.datetime.now(_dt.UTC)


def main() -> int:  # pragma: no cover - process entry point
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), stream=sys.stderr)
    sys.path.insert(0, os.getenv("SENTINEL_API_PATH", "/app/apps/api"))

    import redis as redis_lib
    from sentinel_api.core.db import server_session

    queue = JudgeQueue(
        redis_lib.from_url(os.environ["REDIS_URL"], decode_responses=True)
    )
    worker = JudgeWorker(queue, server_session, WorkerConfig.from_env())
    worker.run_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
