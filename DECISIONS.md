# Sentinel — Architectural Decision Record

Format per entry: Decision · Context · Alternatives considered · Reason · Trade-offs · Date.

Decisions marked **[APPROVED]** were confirmed by the product owner. Decisions marked
**[PROPOSED]** are my recommendation and are open to challenge —
see [`docs/OPEN_DECISIONS.md`](docs/OPEN_DECISIONS.md) for the ones that block Phase 1.

---

## ADR-0001 — Shared schema with `org_id` and PostgreSQL RLS **[APPROVED]**

**Decision.** All tenants share one schema. Every tenant-owned table carries
`org_id NOT NULL`. Isolation is enforced by Row-Level Security with `FORCE`, under a
non-owner, `NOBYPASSRLS` application role, using `SET LOCAL sentinel.org_id` per
transaction.

**Context.** Sentinel is intended to become SaaS serving many organizations. Tenant
isolation is the hardest thing to retrofit and the most damaging thing to get wrong —
a cross-tenant leak here means one university seeing another's students' webcam footage.

**Alternatives considered.**
- *Schema-per-tenant:* stronger blast-radius isolation, easier per-tenant export and
  deletion, but N× migrations, `search_path` complexity, and painful past a few hundred
  tenants.
- *Database-per-tenant:* strongest isolation, unworkable operational cost at SMB pricing.
- *App-enforced `org_id` only:* simplest and fastest, but one forgotten `WHERE` clause is
  a breach. Unacceptable for this data class.

**Reason.** RLS gives a database-level backstop that cannot be forgotten by application
code, at near-zero operational cost. The layered model (endpoint policy → repository
scoping → RLS) means two independent mistakes are required to leak data.

**Trade-offs.** RLS adds query-planning overhead (usually small, occasionally significant
on complex joins — must be measured). `SET LOCAL` is mandatory under PgBouncer transaction
pooling; getting this wrong leaks context between requests, so it is asserted by test. A
new table without an RLS policy is a silent hole, which is why the
`information_schema` sweep is a CI gate rather than a code-review convention.

**Date.** 2026-08-18

---

## ADR-0002 — Local authentication now, OIDC-ready seams **[APPROVED]**

**Decision.** Email/password with Argon2id, short-lived `EdDSA` access tokens plus
rotating opaque refresh tokens in `httpOnly` cookies, TOTP MFA mandatory for `org_admin`
and `reviewer`. `app_user.external_issuer`/`external_subject` and an `IdentityProvider`
interface exist from day one so OIDC/SAML can be added per-org without schema change.

**Context.** Universities and enterprises will eventually demand SSO, but building against
an IdP in Phase 1 adds a hard dependency to local development and delays a working P0.

**Alternatives considered.**
- *OIDC/SSO first-class immediately:* correct destination, wrong starting point; adds
  Keycloak or a paid IdP to `docker compose up`.
- *Candidate one-time links, no candidate accounts:* lower friction for hiring
  assessments, but the candidate-transparency and appeal features assume a durable
  candidate identity, which is core to the product thesis.

**Reason.** Fastest path to the end-to-end flow working, with the expensive part (the
schema and the abstraction) done up front so the later addition is additive.

**Trade-offs.** Sentinel owns password security in the interim — breach lists, lockout,
reset flows, MFA enrolment. That is real work and real risk that an IdP would have
absorbed.

**Date.** 2026-08-18

---

## ADR-0003 — Cloud-agnostic containers **[APPROVED]**

**Decision.** All infrastructure dependencies are accessed through generic interfaces:
S3-compatible object storage, PostgreSQL, Redis, OCI containers. No cloud-vendor SDK in
application code. MinIO locally, any S3-compatible service in production.

**Context.** Universities frequently require self-hosting or in-region data residency.
Locking to one cloud closes off a substantial part of the target market.

**Alternatives considered.**
- *AWS-first:* cleanest production story and the best key management (KMS), but weakens
  the self-hosted option many universities require.
- *Kubernetes-first:* good for scaling judge workers independently, heavy operational
  overhead for an early product, slower local iteration.

**Reason.** Preserves both the SaaS and self-hosted paths from one codebase. Deployment
targets become a configuration concern rather than a rewrite.

**Trade-offs.** Forgoes managed conveniences. Most notably, **KMS-backed signing must be
behind an interface with a file-based local implementation** — and the file-based one must
never reach production. That is a documented sharp edge (see
[`SECURITY.md`](SECURITY.md#3-key-management)) and the most likely operational mistake in the
whole design.

**Date.** 2026-08-18

---

## ADR-0004 — Per-session hash chain, not a global chain **[PROPOSED]**

**Decision.** The audit chain is scoped to one `exam_session`. `server_seq` restarts at 1
per session and is allocated under `SELECT ... FOR UPDATE` on the session row.

**Context.** A hash chain requires a strict total order, which requires serialized appends.

**Alternatives considered.**
- *Global chain across all sessions:* stronger ordering guarantees between sessions, but
  serializes every event write across every tenant — a throughput ceiling and an
  availability risk, and cross-tenant hash linkage is itself an information leak.
- *Merkle tree with periodic roots:* better batching, more complex verification, harder to
  explain to a non-cryptographer reviewer. Explainability matters here.

**Reason.** Sessions are the natural unit of review, export, retention, and dispute. A
per-session chain parallelizes perfectly and produces self-contained export bundles.

**Trade-offs.** No cryptographic ordering *between* sessions. If that is ever needed
(e.g. proving session A's chain existed before session B's), periodic anchoring of chain
heads into a daily Merkle root would be the extension.

**Date.** 2026-08-18

---

## ADR-0005 — RFC 8785 canonical JSON, integers only in the signed envelope **[PROPOSED]**

**Decision.** Canonicalization follows RFC 8785 (JCS). Floating-point numbers are
**forbidden** in the signed envelope; `confidence` is serialized as an integer in
thousandths.

**Context.** The verifier must be an independent implementation, likely in a different
language than the API, and must reproduce byte-identical serialization.

**Alternatives considered.**
- *Sign the raw JSON bytes as transmitted:* trivially correct, but forbids any
  re-serialization anywhere in the pipeline and makes the stored form and the signed form
  diverge.
- *Protobuf or CBOR:* deterministic encodings exist, but the export bundle stops being
  human-readable, which undermines the candidate-transparency goal.
- *Allow floats with a specified formatting rule:* IEEE-754 shortest-round-trip formatting
  differs in enough edge cases between Python and JavaScript that an independent verifier
  would eventually produce spurious failures.

**Reason.** JCS is a published standard with implementations in several languages. Banning
floats removes the one class of divergence that would produce false tamper alarms — and a
false tamper alarm is worse than no verifier, because it destroys trust in the true ones.

**Trade-offs.** Millisecond timestamps and thousandths-precision confidence are fixed
forever in `v1` of the envelope. Higher precision requires a version bump.

**Date.** 2026-08-18

---

## ADR-0006 — Event types in a reference table, not a Postgres enum **[PROPOSED]**

**Decision.** `audit_event.event_type` is `text REFERENCES event_type(code)`.

**Context.** Detector taxonomies will change often across P1 and P2.

**Alternatives considered.** A Postgres `ENUM` gives compact storage and type safety, but
`ALTER TYPE ... ADD VALUE` cannot be combined with other DDL in one transaction, making
migrations awkward, and values cannot be removed at all.

**Reason.** Same referential integrity, better evolution, plus the table carries useful
metadata (`default_severity`, `warrants_evidence`, `evidence_kind`) that an enum could not.

**Trade-offs.** Slightly larger rows and an extra join for metadata.

**Date.** 2026-08-18

---

## ADR-0007 — Evidence bound by server-recomputed digest **[PROPOSED]**

**Decision.** On upload confirmation the API recomputes SHA-256 from the stored object and
seals that digest in a signed `EVIDENCE_BOUND` event. The client-supplied hash is a claim
to compare against, never the authoritative value.

**Context.** Evidence is the most attackable artifact: it is large, stored outside the
database, and its substitution is the obvious attack on an integrity product.

**Alternatives considered.**
- *Trust the client digest:* free, and defeats the exact adversary the mechanism exists
  for.
- *Store evidence bytes in Postgres:* trivially consistent, but ruinous for storage cost
  and backup size at scale.

**Reason.** An attacker who later replaces the object in storage cannot change a digest
already sealed in a signed chain. The verifier reports the mismatch precisely.

**Trade-offs.** The API must read back every uploaded object to hash it — bandwidth and
latency cost on the confirm path. Acceptable at ≤40 small images per session; it would
need revisiting for video clips at scale.

**Date.** 2026-08-18

---

## ADR-0008 — `requestVideoFrameCallback` rolling buffer instead of timers **[PROPOSED]**

**Decision.** Frame capture is driven by `requestVideoFrameCallback` into a ring buffer,
with a feature-detected timer fallback. The capture path actually used is recorded per
session.

**Context.** Background tabs throttle `setInterval` to ≥1 s and may freeze it entirely —
precisely when tab-switch evidence matters most.

**Alternatives considered.** `setInterval` (throttled, unusable for this purpose);
continuous `MediaRecorder` (large, privacy-hostile, expensive, and records everything to
catch a few seconds).

**Reason.** Event-triggered capture from a rolling buffer gets the pre-event context frame
that makes evidence reviewable, at a fraction of the storage and privacy cost of
continuous recording.

**Trade-offs.** `requestVideoFrameCallback` is not universally supported; where it is
absent, fidelity degrades and Sentinel must say so rather than silently producing worse
evidence. See [`LIMITATIONS.md §2.5`](LIMITATIONS.md).

**Date.** 2026-08-18

---

## ADR-0009 — Interpretable baseline risk before any learned model **[PROPOSED]**

**Decision.** Phase 9 ships a weighted, time-decayed, reliability-adjusted linear scorer
with per-signal contributions. An HMM or learned model ships only if it measurably beats
the baseline on calibration and false-positive rate against the evaluation harness.

**Context.** Machine learning is the most tempting and most dangerous addition to a
proctoring product.

**Alternatives considered.** HMM first (sophisticated, opaque, unvalidated without labelled
data); supervised classification (requires ground-truth cheating labels, which are not
obtainable at scale without exactly the accusations this product refuses to make).

**Reason.** A reviewer must be able to see *why* a session surfaced. An interpretable model
is defensible in an academic-integrity hearing; a black box is not. Added complexity must
earn its place with measured improvement.

**Trade-offs.** The baseline will miss patterns a sequence model would catch. Accepted:
missed detections route to the same human review that everything else does, whereas an
unexplainable flag actively harms a candidate.

**Date.** 2026-08-18

---

## ADR-0010 — Two logs: chained per-session audit, unchained org activity **[PROPOSED]**

**Decision.** `audit_event` is per-session, signed and hash-chained. `activity_log` is
org-level, append-only, unchained.

**Context.** Not everything worth logging needs to survive adversarial scrutiny.

**Alternatives considered.** Chain everything (signing cost and write serialization on
high-volume administrative events, for no defensible claim); chain nothing (loses the
product's core differentiator).

**Reason.** Chain where the claim matters — the session record a candidate or a tribunal
may need to trust. Log plainly where it does not.

**Trade-offs.** Administrative actions are tamper-evident only via database controls, not
cryptographically. An `EVIDENCE_ACCESSED` event is written to *both*, because insider
access to candidate evidence does need the strong guarantee.

**Date.** 2026-08-18

---

## ADR-0011 — Exams and questions are versioned and immutable once published **[PROPOSED]**

**Decision.** `exam_version` and `question_version` are frozen at publish. Sessions
reference a version. Editing creates a new version.

**Context.** An in-flight session must be pinned to the paper it started; a result released
last term must remain explainable.

**Alternatives considered.** Mutable exams with an audit log of edits (cheaper, but an
in-flight session's paper could change under the candidate); copy-on-assign snapshots
(storage-heavy, duplicates content per candidate).

**Reason.** Versioning is the standard answer and it also makes `paper_seed` stable —
`hash(exam_id, candidate_id, exam_version)` reproduces the identical paper on recovery.

**Trade-offs.** More rows, and an authoring UX that must make versioning legible to a
non-technical instructor. That UX is a real product risk, not just an implementation
detail.

**Date.** 2026-08-18

---

## ADR-0012 — Hardened Docker for the judge in Phase 3 **[PROPOSED]**

**Decision.** One ephemeral, network-less, read-only, capability-dropped, seccomp-filtered
container per submission, on a rootless daemon, on a dedicated host pool.

**Context.** The judge executes attacker-supplied code by design.

**Alternatives considered.** gVisor (stronger syscall isolation, some performance and
compatibility cost); Firecracker microVMs (strongest, highest operational complexity);
a hosted execution API (offloads the risk, adds a vendor dependency and a data-egress
question for a product selling on data minimization).

**Reason.** Hardened Docker is deployable everywhere including self-hosted, and its limits
are well understood. It is a defensible *baseline*, not the strongest option.

**Trade-offs.** Container isolation shares a kernel and is not a hard security boundary.
This residual risk is stated in [`THREAT_MODEL.md §T5.9`](THREAT_MODEL.md) rather than
papered over. Upgrading to gVisor or Firecracker is an
[open decision](docs/OPEN_DECISIONS.md).

**Date.** 2026-08-18

---

## ADR-0013 — Verifier shares no code with the API **[PROPOSED]**

**Decision.** `tools/verifier/verify_chain.py` has its own `pyproject.toml`, no import
path into `apps/api`, no network access, and depends only on stock Python 3.11 plus
`cryptography`.

**Context.** The verifier exists to be trusted by people who do not trust Sentinel.

**Alternatives considered.** Share the canonicalization module (guarantees agreement — and
guarantees that a bug in it is invisible, because both sides would make the same mistake).

**Reason.** Independent implementation from the written spec is what makes the verifier
meaningful. A shared implementation verifies self-consistency, which is not the claim.

**Trade-offs.** Duplicated logic and a real risk of divergence. Mitigated by shared
test vectors: a fixture set of envelopes with expected canonical bytes and digests that
both implementations must reproduce.

**Date.** 2026-08-18

---

## ADR-0014 — Accommodations adjust thresholds without visible labels **[PROPOSED]**

**Decision.** Accommodation profiles alter detector thresholds and evidence policy. The
reviewer roster and detail views show no accommodation badge; the applied profile appears
only in the configuration/audit detail view, and its hash is sealed in the chain.

**Context.** Accommodated candidates are disproportionately disabled candidates.

**Alternatives considered.** Show the accommodation to reviewers for context (helps
interpretation, but primes reviewers with disability information and risks discriminatory
adjudication); hide it entirely (auditability lost).

**Reason.** Reviewers should adjudicate the evidence, not the candidate's medical status.
Auditability is preserved without putting a label in the decision-maker's eye line.

**Trade-offs.** A reviewer may misread an accommodation-driven threshold as a system
inconsistency. Documentation and reviewer training must cover this.

**Date.** 2026-08-18

---

## ADR-0016 — No live integrity feedback to the candidate **[APPROVED]**

**Decision.** During an integrity-monitored exam the candidate sees a single calm
status indicator ("monitoring active") and nothing else. No flag count, no severity, no
risk score, no live warnings. Full disclosure happens *before* the exam; the evidence
mirror happens *after* it.

**Context.** D7 in `docs/OPEN_DECISIONS.md`. I flagged this as the decision where I was
least confident in my own recommendation.

**Alternatives considered.**
- *Live warnings ("please stay in frame"):* arguably fairer — it lets an honest candidate
  fix a genuine problem, like having drifted out of frame, instead of being flagged for
  something they would have corrected instantly.
- *Nudge-only for recoverable technical conditions* (poor lighting, camera lost) with no
  flag counts: the middle option.

**Reason.** Product owner's decision. Live feedback turns a timed exam into an anxiety
loop, and it tells anyone inclined to cheat exactly which behaviours evade detection —
the system would be training its own adversaries.

**Trade-offs.** A candidate whose camera drifts out of frame will not know until after
the exam. Calibration (Phase 1 design, Phase 7 implementation) is the compensating
control: environment problems must surface *before* the clock starts, because the
alternative is punishing someone for a setup failure they were never told about. If
false-positive rates from Study 2 turn out to be driven mainly by recoverable
environmental conditions, the nudge-only option should be revisited.

**Date.** 2026-08-18

---

## ADR-0017 — Explicit credentials take precedence over ambient ones **[PROPOSED]**

**Decision.** When a request carries both an `Authorization: Bearer` header and a session
cookie, the header wins.

**Context.** Found by a test, not by design: a test sending a deliberately forged Bearer
token got a 200, because the request also carried a valid cookie that silently took
precedence.

**Alternatives considered.** Cookie-first (the original implementation) — but that means
a caller who presents a token can act as whoever the cookie belongs to, which is
surprising and a real hazard for API clients running inside a browser context.

**Reason.** A header is something the caller deliberately attached. A cookie is sent
automatically by the browser. Honouring the deliberate one is the least surprising
behaviour and makes forged-token tests meaningful.

**Trade-offs.** A client that sends a stale header alongside a fresh cookie now gets a
401 instead of silently succeeding. That is the correct outcome, and it is a louder
failure than before.

**Date.** 2026-08-18

---

## ADR-0015 — No `cheating` field anywhere in the schema **[PROPOSED]**

**Decision.** No table has a boolean or enum expressing a system determination of
misconduct. `risk_assessment` holds `anomaly_probability` with a confidence interval and
nothing else. There is no foreign key or code path from `risk_assessment` to
`session_result`, and a static test asserts this.

**Context.** The product's core promise is that automated software does not determine guilt.

**Alternatives considered.** A `flagged` boolean for query convenience — which is exactly
how "evidence" silently becomes "verdict" over a few sprints of well-intentioned feature
work.

**Reason.** Make the wrong thing structurally impossible rather than merely discouraged.
If the field does not exist, no future engineer can accidentally join it to a grade, and
no customer can ask for it to be surfaced "just as a filter".

**Trade-offs.** Some reporting is slightly more awkward. That is the point.

**Date.** 2026-08-18

---

## ADR-0018 — Extensions are installed by the superuser, not by the migration **[ACCEPTED]**

**Context.** `0001_schema.sql` opened with `CREATE EXTENSION IF NOT EXISTS pgcrypto`. The
migrate role owns the `public` schema but has no `CREATE` on the database, so the first
real `docker compose up` failed with `InsufficientPrivilege`.

**Decision.** Extensions are created by the superuser in
`infrastructure/postgres/01-roles.sh`. The migration keeps its `IF NOT EXISTS`, which is a
clean no-op once they exist, and gains a preflight that names the fix.

**Rejected: grant `CREATE ON DATABASE` to the migrate role.** It would let the schema owner
create schemas outside the RLS-generation loop in migration 0001 — the exact hole that loop
exists to close. It also would not work on RDS, Cloud SQL or Azure, which refuse
`CREATE EXTENSION` from a non-superuser regardless of grants.

**Consequence.** A database whose volume predates this change still needs one manual
statement, because `docker-entrypoint-initdb.d` runs only on an empty data directory. The
preflight error says exactly which.

---

## ADR-0019 — A `system` role name for server-initiated work **[ACCEPTED]**

**Context.** Auto-grading writes `question_score` and `session_result` during the
candidate's own submit request. Reusing the request's transaction would mean the database
permitted a candidate-authenticated code path to write its own marks.

**Decision.** `sentinel.role` may take the value `system`, which the RLS policies treat as
organization-wide. Grading runs in its own transaction with that context
(`core.db.server_session`); paper materialization briefly adopts it inside the request
transaction (`core.db.elevated`) so that the session row and its paper are written
atomically. No login can produce it: `Principal.primary_role` returns only membership
roles.

**Consequence.** The protection is a convention enforced by the absence of any other code
path that sets the value, not by a database role. That is weaker than a real role and is
recorded as such; making `sentinel_grader` an actual PostgreSQL role would be stronger and
would cost a second connection pool.

---

## ADR-0020 — Selection and ordering are separate concerns in paper generation **[ACCEPTED]**

**Context.** A `k of n` pool decides *which* questions a candidate gets. A section's
`shuffle_questions` flag decides what *order* they appear in. The first implementation
shuffled inside the sampler, so an author who turned question shuffling off still got a
random order.

**Decision.** `_Stream.sample` returns its selection in pool order; only the section-level
shuffle reorders. Every random draw comes from a labelled sub-stream of the session seed,
so adding a question to one pool cannot reshuffle another pool in a paper already in
flight.

**Consequence.** Questions that build on each other stay in the author's order, which is
usually why the flag was turned off.

---

## ADR-0021 — A Bearer token suppresses the CSRF check **[ACCEPTED]**

**Context.** ADR-0017 made explicit credentials beat ambient ones for *authentication*.
`enforce_csrf` was not updated to match: it fired whenever an access cookie was present,
so an API client that had ever logged in through the browser got 403 on every write.

**Decision.** When a request carries `Authorization: Bearer`, CSRF enforcement is skipped —
that header is the credential the request authenticated with, and a browser does not attach
it to a cross-site request.

**Consequence.** Cookie-authenticated writes are unchanged and still require the
double-submit header. An attacker who can set an `Authorization` header already has script
execution in the origin, at which point CSRF is not the control that matters.

---

## ADR-0022 — Judge isolation: hardened containers, runtime as configuration **[ACCEPTED — resolves D4]**

**Context.** D4 asked whether the judge sandbox should be containers, gVisor, or
Firecracker microVMs. It blocked Phase 3.

**Decision.** runc containers with every hardening flag the runtime offers
(`--network=none`, `--read-only`, size-capped tmpfs, `--pids-limit`, memory cap
with no swap, `--cpus`, non-root, `--cap-drop=ALL`, `no-new-privileges`, a
seccomp deny-list, `--log-driver=none`, `fsize` ulimit, empty environment), plus
a wall-clock kill and byte-capped output enforced by the runner. `--runtime` is
read from `JUDGE_RUNTIME` and defaults to `runc`.

**Rejected: gVisor as a requirement.** Stronger, but it must be installed on
every judge host, costs 15–30% runtime, and breaks some syscalls. Making it a
configuration value gets the option without the deployment mandate.

**Rejected: Firecracker.** Strongest, and it needs `/dev/kvm`, VM images, the
jailer, and ~125 ms of boot per submission. Disproportionate for Phase 3.

**Consequence, stated plainly.** runc shares the host kernel: a kernel exploit
escapes, and no flag here prevents that. Production runs judge workers on an
isolated host pool for exactly this reason. Switching to gVisor is
`JUDGE_RUNTIME=runsc` and a base-image change — no code.

---

## ADR-0023 — The judge seccomp profile is default-allow with a deny-list **[ACCEPTED]**

**Context.** Passing `--security-opt seccomp=` **replaces** Docker's builtin
default-deny profile; the two do not compose. The first attempt here was a
default-deny allowlist, and containers would not start: runc needs syscalls
during init that a hand-written allowlist does not anticipate (`fstatfs` on
`/proc/thread-self/fd` was the first to surface).

**Decision.** A default-allow profile that denies 65 syscalls across six groups:
networking, namespace and mount manipulation, cross-process access, kernel
surface (eBPF, perf, module loading, kexec), host state, and the keyring.

**Rejected: reimplementing Docker's ~350-syscall allowlist.** It would have to
be maintained against every runc release, and the failure modes are asymmetric —
a broken allowlist fails closed and visibly, a *stale* one fails open and
silently.

**Consequence.** Weaker in principle than default-deny, and it is explicitly not
the primary control. The primary controls are the namespace, capability and
cgroup restrictions in ADR-0022. This profile is the layer that still holds if
one of those is misconfigured. Its effect is verified in both directions: the
tests assert the denied syscalls fail under it *and* succeed without it.

---

## ADR-0024 — Coding questions score by weighted partial credit **[ACCEPTED]**

**Context.** A submission passes some subset of a question's test cases. The
question is what mark that earns.

**Decision.** The fraction of test *weight* passed, multiplied by the question's
marks. Weights are per test case, so an author can make the hard hidden cases
worth more.

**Rejected: all-or-nothing.** It tells a candidate who solved 90% of the problem
exactly as much as one who solved none, and tells the examiner nothing either.

**Rejected: counting tests rather than weight.** It silently overrides the
author's judgement about which cases matter.

**Consequence.** `judge_run.score` stores the fraction, not marks, so a
question's weight can change without rewriting judge history. Grading multiplies
at write time.

---

## ADR-0025 — The judge queue carries identifiers, never source **[ACCEPTED]**

**Context.** The API enqueues work; the worker executes it. The submission is up
to 256 KiB of candidate-written code.

**Decision.** Redis holds `{run_id, submission_id, org_id, mode, enqueued_at}`.
The worker reads the source from Postgres, which it already connects to in order
to write results. Consumption is `BLMOVE` onto a per-worker processing list, not
`BRPOP`.

**Rejected: putting the source on the queue.** It would make Redis a second
store of candidate work — one more thing to secure, back up, and reason about
for retention — for no gain.

**Rejected: `BRPOP`.** It removes the job before the worker has done anything
with it, so a worker that dies mid-run loses the submission silently. `BLMOVE`
leaves evidence and makes recovery possible; the worker's conditional-`UPDATE`
claim makes redelivery a no-op.

