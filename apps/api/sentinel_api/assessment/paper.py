"""Deterministic per-candidate paper generation.

Two properties matter, and they pull in opposite directions:

* **Different candidates get different papers.** That is the point of `k of n`
  pools and option shuffling.
* **The same candidate always gets the same paper.** A candidate whose laptop
  dies mid-exam must come back to the questions they had, in the order they had
  them, with the options in the order they had them. Otherwise recovery is a
  new exam wearing the old one's name.

Both fall out of deriving every random choice from one seed that is a pure
function of (exam version, candidate, attempt):

    seed = SHA-256(exam_version_id || candidate_user_id || attempt_no)

Nothing in the derivation is time-dependent, machine-dependent, or dependent on
the order rows come back from the database — every list is sorted by its
`ordinal` before it is consumed.

`random.Random` is deliberately *not* used. Its Mersenne Twister seeding is
stable in practice but is not a documented, versioned contract, and this
function's output has to survive a Python upgrade: a candidate resuming an exam
after a rolling deploy would otherwise get a different paper. The shuffle here is
a Fisher-Yates driven by a SHA-256 counter stream, which is specified entirely by
this file.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass


def paper_seed(exam_version_id: uuid.UUID, candidate_user_id: uuid.UUID, attempt_no: int) -> bytes:
    """The 32-byte seed for one candidate's attempt at one exam version."""
    h = hashlib.sha256()
    h.update(exam_version_id.bytes)
    h.update(candidate_user_id.bytes)
    h.update(attempt_no.to_bytes(4, "big"))
    return h.digest()


class _Stream:
    """A deterministic, seekable stream of bytes derived from a seed and a label.

    Each use site (pool selection, question order, option order for item *i*)
    takes its own labelled sub-stream, so adding a new use later cannot shift
    the values an existing one sees — which would silently change every paper
    already in flight.
    """

    def __init__(self, seed: bytes, label: str) -> None:
        self._key = hashlib.sha256(seed + b"\x00" + label.encode("utf-8")).digest()
        self._counter = 0
        self._buffer = b""

    def _refill(self) -> None:
        block = hashlib.sha256(self._key + self._counter.to_bytes(8, "big")).digest()
        self._counter += 1
        self._buffer += block

    def _next_bytes(self, n: int) -> bytes:
        while len(self._buffer) < n:
            self._refill()
        out, self._buffer = self._buffer[:n], self._buffer[n:]
        return out

    def below(self, bound: int) -> int:
        """A uniform integer in [0, bound). Rejection-sampled, so no modulo bias."""
        if bound <= 1:
            return 0
        # Smallest byte width that covers `bound`, then reject out-of-range draws.
        width = max(1, (bound - 1).bit_length() + 7 >> 3)
        limit = (1 << (width * 8)) - ((1 << (width * 8)) % bound)
        while True:
            value = int.from_bytes(self._next_bytes(width), "big")
            if value < limit:
                return value % bound

    def shuffled(self, items: Sequence[int]) -> list[int]:
        """Fisher-Yates. Specified here rather than delegated, on purpose."""
        out = list(items)
        for i in range(len(out) - 1, 0, -1):
            j = self.below(i + 1)
            out[i], out[j] = out[j], out[i]
        return out

    def sample(self, population: int, k: int) -> list[int]:
        """`k` distinct indices from `range(population)`, **in pool order**.

        Selection and ordering are separate concerns and are kept separate here.
        A pool decides *which* questions a candidate gets; the section's
        `shuffle_questions` flag decides what order they appear in. Returning
        the sample shuffled would mean an author who turned question shuffling
        off still got a random order — silently overriding a setting they set
        deliberately, usually because the questions build on each other.
        """
        if k >= population:
            return list(range(population))
        return sorted(self.shuffled(range(population))[:k])


# ---------------------------------------------------------------------------
# Blueprint: the exam structure, read once, in a form generation can be pure over
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PoolSpec:
    pool_id: uuid.UUID
    ordinal: int
    select_count: int
    #: (question_version_id, marks, option_count) sorted by the pool's ordinal.
    #: `option_count` is 0 for kinds that have no options to shuffle.
    items: tuple[tuple[uuid.UUID, float, int], ...]


@dataclass(frozen=True)
class SectionSpec:
    section_id: uuid.UUID
    ordinal: int
    shuffle_questions: bool
    pools: tuple[PoolSpec, ...]


@dataclass(frozen=True)
class ExamBlueprint:
    exam_version_id: uuid.UUID
    sections: tuple[SectionSpec, ...]

    @property
    def question_count(self) -> int:
        return sum(p.select_count for s in self.sections for p in s.pools)


@dataclass(frozen=True)
class PaperItem:
    section_id: uuid.UUID
    section_ordinal: int
    pool_id: uuid.UUID
    question_version_id: uuid.UUID
    item_ordinal: int
    marks: float
    #: Permutation of option indices, or None when the kind has no options.
    option_order: list[int] | None


def generate_paper(blueprint: ExamBlueprint, seed: bytes) -> list[PaperItem]:
    """Materialize one candidate's paper. Pure: same inputs, same output, always."""
    items: list[PaperItem] = []
    ordinal = 1

    for section in sorted(blueprint.sections, key=lambda s: s.ordinal):
        section_items: list[tuple[uuid.UUID, uuid.UUID, float, int]] = []

        for pool in sorted(section.pools, key=lambda p: p.ordinal):
            # Labelled per pool, so editing one pool's size cannot reshuffle a
            # different pool in a paper generated from the same seed.
            stream = _Stream(seed, f"pool:{pool.pool_id}")
            chosen = stream.sample(len(pool.items), min(pool.select_count, len(pool.items)))
            for index in chosen:
                qv_id, marks, option_count = pool.items[index]
                section_items.append((pool.pool_id, qv_id, marks, option_count))

        if section.shuffle_questions:
            order = _Stream(seed, f"section:{section.section_id}").shuffled(
                range(len(section_items))
            )
            section_items = [section_items[i] for i in order]

        for pool_id, qv_id, marks, option_count in section_items:
            option_order = (
                _Stream(seed, f"options:{qv_id}").shuffled(range(option_count))
                if option_count > 1
                else None
            )
            items.append(
                PaperItem(
                    section_id=section.section_id,
                    section_ordinal=section.ordinal,
                    pool_id=pool_id,
                    question_version_id=qv_id,
                    item_ordinal=ordinal,
                    marks=marks,
                    option_order=option_order,
                )
            )
            ordinal += 1

    return items
