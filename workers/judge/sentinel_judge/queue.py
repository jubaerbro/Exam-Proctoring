"""The judge queue.

A Redis list plus a processing list, consumed with `BLMOVE`. Not `BRPOP`:
`BRPOP` removes the job before the worker has done anything with it, so a worker
that dies mid-run loses the submission silently. `BLMOVE` puts the job on a
per-worker processing list atomically, which means a crashed worker leaves
evidence and the job can be recovered.

The queue carries **identifiers only** — never the source. Two reasons:

* the source is up to 256 KiB and can be read from Postgres by the worker, which
  already needs a database connection to write results;
* a Redis instance that holds candidate source code is a second place that has
  to be secured, backed up, and reasoned about for retention. It is not one.

Idempotency is by `run_id`. A job delivered twice — which `BLMOVE` recovery
makes possible on purpose — must not produce two sets of results, so the worker
claims a run with a conditional UPDATE and skips it if the claim fails.
"""

from __future__ import annotations

import json
import socket
import time
import uuid
from dataclasses import dataclass

QUEUE_KEY = "sentinel:judge:queue"
PROCESSING_KEY_PREFIX = "sentinel:judge:processing:"
#: A run that has been in a processing list longer than this is presumed
#: abandoned by a dead worker and is requeued.
STALE_AFTER_SECONDS = 900


@dataclass(frozen=True)
class JudgeJob:
    run_id: uuid.UUID
    submission_id: uuid.UUID
    org_id: uuid.UUID
    #: `sample` or `final`. Carried so the worker does not have to read the
    #: submission before deciding how urgent the job is.
    mode: str
    enqueued_at: float

    def to_json(self) -> str:
        return json.dumps(
            {
                "run_id": str(self.run_id),
                "submission_id": str(self.submission_id),
                "org_id": str(self.org_id),
                "mode": self.mode,
                "enqueued_at": self.enqueued_at,
            }
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> JudgeJob:
        data = json.loads(raw)
        return cls(
            run_id=uuid.UUID(data["run_id"]),
            submission_id=uuid.UUID(data["submission_id"]),
            org_id=uuid.UUID(data["org_id"]),
            mode=data.get("mode", "sample"),
            enqueued_at=float(data.get("enqueued_at", 0.0)),
        )

    @property
    def queue_delay_ms(self) -> int:
        return max(0, int((time.time() - self.enqueued_at) * 1000))


def worker_id() -> str:
    """Stable enough to attribute a run, unique enough to not collide."""
    return f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"


class JudgeQueue:
    def __init__(self, redis, *, key: str = QUEUE_KEY) -> None:
        self._redis = redis
        self._key = key

    # ----------------------------------------------------------- producing

    def enqueue(self, job: JudgeJob) -> None:
        # LPUSH + BLMOVE from the tail gives FIFO. A candidate who pressed run
        # first should be judged first; LIFO would starve them under load,
        # which is exactly when it matters.
        self._redis.lpush(self._key, job.to_json())

    def depth(self) -> int:
        return int(self._redis.llen(self._key))

    # ----------------------------------------------------------- consuming

    def claim(self, worker: str, *, timeout: int = 5) -> JudgeJob | None:
        """Block for a job, moving it to this worker's processing list."""
        raw = self._redis.blmove(
            self._key, self._processing_key(worker), timeout, "RIGHT", "LEFT"
        )
        if raw is None:
            return None
        return JudgeJob.from_json(raw)

    def acknowledge(self, worker: str, job: JudgeJob) -> None:
        """Remove a finished job from the processing list.

        Called after the results are committed, never before. The window
        between "results written" and "acknowledged" can only cause a job to be
        retried, and the worker's `run_id` claim makes a retry a no-op.
        """
        self._redis.lrem(self._processing_key(worker), 1, job.to_json())

    def requeue_stale(self, *, older_than: int = STALE_AFTER_SECONDS) -> int:
        """Return abandoned jobs to the queue.

        A worker that is OOM-killed mid-run leaves its job on a processing list
        forever. Without this the candidate waits for a result that is never
        coming, and the only symptom is a spinner.
        """
        moved = 0
        cutoff = time.time() - older_than
        for key in self._redis.scan_iter(match=f"{PROCESSING_KEY_PREFIX}*"):
            for raw in self._redis.lrange(key, 0, -1):
                try:
                    job = JudgeJob.from_json(raw)
                except (ValueError, KeyError):
                    self._redis.lrem(key, 1, raw)
                    continue
                if job.enqueued_at < cutoff:
                    self._redis.lrem(key, 1, raw)
                    self._redis.lpush(self._key, raw)
                    moved += 1
        return moved

    @staticmethod
    def _processing_key(worker: str) -> str:
        return f"{PROCESSING_KEY_PREFIX}{worker}"
