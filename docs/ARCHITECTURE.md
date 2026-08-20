# Sentinel — Architecture

**Status: PHASE 0 design. Nothing in this document is implemented.**

---

## 1. Architectural thesis

Sentinel's defensibility does not come from having better detectors than competitors. It
comes from **the evidence trail being verifiable by someone who does not trust Sentinel.**

Every architectural decision follows from that. Specifically:

- The audit chain is signed server-side and verifiable by a standalone tool that shares
  no code with the application. If Sentinel's own API is compromised or lying, the
  verifier still tells the truth about the chain.
- Detectors are advisory. Their output enters a review queue, never a grade.
- Evidence is bound to audit events by hash, so evidence substitution is detectable
  without trusting the object store.
- Limitations are documented as first-class product artifacts, not buried.

The competitive claim is therefore: *"you can prove what our system recorded"* — not
*"our AI is smarter"*. That claim survives adversarial scrutiny; the other one does not.

---

## 2. System context

```
┌────────────────────────────────────────────────────────────────────────┐
│                            CANDIDATE BROWSER                           │
│                                                                        │
│  Exam Runner (React)      Integrity Collectors      Evidence Pipeline  │
│  ─ question nav           ─ fullscreen/vis/blur     ─ rolling buffer   │
│  ─ server-synced timer    ─ clipboard               ─ pHash dedupe     │
│  ─ autosave               ─ network state           ─ resize/encode    │
│  ─ offline queue          ─ hysteresis FSMs         ─ upload intents   │
│                                                                        │
│  ┌──────────────── Web Worker: CV Engine ────────────────┐             │
│  │ MediaPipe Tasks Vision · ONNX Runtime Web             │             │
│  │ face presence/count · head pose · gaze proxy ·        │             │
│  │ object detection · identity continuity · VAD (opt)    │             │
│  └───────────────────────────────────────────────────────┘             │
└───────────┬────────────────────────┬───────────────────────┬───────────┘
            │ HTTPS (REST)           │ WSS (events)          │ HTTPS PUT
            │                        │                       │ (presigned)
┌───────────▼────────────────────────▼───────────┐   ┌───────▼───────────┐
│                 SENTINEL API                   │   │  OBJECT STORAGE   │
│                  (FastAPI)                     │   │  S3-compatible    │
│                                                │   │  (MinIO local)    │
│  Auth │ Tenancy │ Assessment │ Grading         │   │  private, SSE     │
│  Integrity │ Audit │ Evidence │ Risk           │   └───────────────────┘
│  Review │ Reporting │ WS Hub                   │
└──┬──────────────┬───────────────┬──────────────┘
   │              │               │
┌──▼─────────┐ ┌──▼──────────┐ ┌──▼──────────────────────────────────────┐
│ PostgreSQL │ │    Redis    │ │            JUDGE WORKERS                │
│  RLS-      │ │ queues,     │ │  Redis consumer → ephemeral container   │
│  enforced  │ │ rate limit, │ │  per submission (netless, read-only,    │
│  tenancy   │ │ WS presence │ │  cap-dropped, seccomp, pid/mem capped)  │
└────────────┘ └─────────────┘ └─────────────────────────────────────────┘
   │
┌──▼──────────────────────────────────────────────────────────────────────┐
│                    tools/verifier/verify_chain.py                       │
│  Standalone. No import from apps/api. Consumes an export bundle +       │
│  public key. Verifies sequence, canonicalization, hash links,           │
│  Ed25519 signatures, and evidence digests.                              │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Module boundaries

Four modules with deliberately narrow contracts. The rule that keeps them honest:
**Integrity may read Assessment; Assessment may not read Integrity.**

```
                          SENTINEL
                             │
       ┌─────────────────────┼─────────────────────┐
       │                     │                     │
  ASSESSMENT            INTEGRITY               JUDGE
   ENGINE                ENGINE                ENGINE
       │                     │                     │
  question bank         event ingest          job queue
  exam authoring        validation            sandbox spawn
  sections/pools        hysteresis config     compile
  assignment            evidence intents      execute
  delivery              audit chain           per-test grade
  server timer          risk estimation       result publish
  autosave/recovery     retention
  grading               access audit
       │                     │                     │
       └─────────────────────┼─────────────────────┘
                             │
                       REVIEW LAYER
                   queue · timeline · evidence
                   verification · decisions
                             │
                  ┌──────────┴──────────┐
             Instructor UI         Reviewer UI
                             │
                         Reporting
```

### Contract between Assessment and Integrity

Integrity knows about a session only through a stable identifier and a coarse "exam
position" (section index, question index, elapsed seconds) supplied at event ingest.

Integrity **never** receives: answer content, correctness, marks, or the question text.

This matters commercially as well as ethically. It means:
- integrity monitoring can be sold separately,
- a reviewer viewing evidence cannot infer answer quality from the integrity payload,
- an integrity bug can never corrupt a grade.

### Contract between Assessment and Judge

The Judge receives `{submission_id, language, source, test_bundle_ref, limits}` and
returns `{per_test_results, resource_usage, status}`. It does not know about exams,
candidates, organizations, or integrity. It is a pure, adversarially-isolated function.

---

## 4. The event pipeline

This is the most security-sensitive path in the product.

```
Browser detector
   │  (raw signal, e.g. a video frame classification)
   ▼
Hysteresis state machine          NORMAL → SUSPECTED → FLAGGED
   │  (dwell/recovery/cooldown; no per-frame events)
   ▼
Client event envelope             {client_seq, client_ts, type, payload, confidence}
   │
   ├─ online  ──► WebSocket ──┐
   └─ offline ──► IndexedDB ──┘  (keyed by client_seq, replayed in order on reconnect)
                               │
                               ▼
                    ┌──────────────────────────┐
                    │   API: event ingest      │
                    │  1. authn/authz session  │
                    │  2. type + payload schema│
                    │  3. client_seq monotonic │
                    │  4. dedupe (session,     │
                    │     client_seq) UNIQUE   │
                    │  5. rate limit           │
                    │  6. assign server_seq    │◄── server owns the authoritative
                    │  7. assign server_ts     │    sequence and clock
                    │  8. late-arrival flag    │
                    └────────────┬─────────────┘
                                 ▼
                    ┌──────────────────────────┐
                    │   Audit chain append     │
                    │  canonical JSON (RFC     │
                    │  8785 JCS) → SHA-256     │
                    │  → prev_hash link        │
                    │  → Ed25519 signature     │
                    │  (single writer per      │
                    │   session, serialized)   │
                    └────────────┬─────────────┘
                                 ▼
                    ┌──────────────────────────┐
                    │  Evidence intent (if the │
                    │  event type warrants it) │
                    │  → presigned PUT         │
                    │  → client uploads        │
                    │  → API confirms sha256   │
                    │  → binding event appended│
                    └──────────────────────────┘
```

### Why the server owns the sequence

A compromised browser can lie about *content*. It cannot be allowed to also lie about
*order* or *time*, because ordering is what makes the chain meaningful. `client_seq` is
retained as an untrusted claim for gap detection; `server_seq` is the chain's spine.

### Late events

An event whose `client_ts` is more than `LATE_THRESHOLD` (default 30s) behind its
`server_ts` is flagged `is_late = true` and carries `arrival_delay_ms`. It is **never**
inserted retroactively into the chain — the chain is append-only in `server_seq` order.
Timelines in the reviewer UI render on `client_ts` but visually mark late events, so a
reviewer can see both "when it happened" and "when we learned about it".

This distinction is required by the brief and is also the honest position: the system
knows when it was told something, and only has the client's word for when it occurred.

### Replay protection

- `UNIQUE (session_id, client_seq)` rejects exact replay.
- Per-session HMAC over the envelope using a per-session ingest secret issued at session
  start (short-lived, rotated) — see [`API.md`](API.md#6-event-ingest) — raises the cost of
  forging events from outside the session.
- **Residual risk, stated plainly:** an attacker who controls the browser controls the
  ingest secret. Nothing in a browser resists this. See
  [`../THREAT_MODEL.md`](../THREAT_MODEL.md#t2--compromised-browser).

---

## 5. The evidence pipeline

### Rolling buffer

`setInterval` is throttled in hidden tabs, which is precisely when the most interesting
events occur. Frame capture is therefore driven by
`HTMLVideoElement.requestVideoFrameCallback`, maintaining a ring buffer of recent frames
(default: 3s of frames at the sampled rate, plus a 10s low-rate buffer for clip
evidence).

On a qualifying event, capture is **synchronous** against the buffer:
- frame at/near event time
- frame ≈1.5s prior (contextual "what led to this")

The pre-event frame is what makes evidence reviewable rather than accusatory: a single
frame of an empty chair means little; the second before it often explains it.

### Cost and privacy controls

| Control | Default | Rationale |
|---|---|---|
| Max long edge | 640 px | Sufficient for human review, ~8× cheaper than full frame |
| JPEG quality | 0.7 | Below this, JPEG artifacts start to look like evidence |
| Hard session cap | 40 images | Bounds worst-case storage cost per candidate |
| Per-type cooldown | 20 s | Prevents one flapping detector from consuming the cap |
| Perceptual dedupe | pHash, Hamming ≤ 6 | Near-identical frames reference the existing object |

When the cap is reached: **events keep flowing, evidence uploads stop**, and a
`CAPTURE_LIMIT_REACHED` event is appended to the chain. A reviewer must be able to tell
"nothing else happened" apart from "we stopped recording" — conflating those two would be
a serious integrity failure.

### Binding evidence to the chain

Evidence is bound by content digest, in a two-step protocol, so the binding survives an
object-store compromise:

1. Client computes SHA-256 of the exact bytes, requests a presigned PUT.
2. Client uploads. API independently recomputes SHA-256 server-side on confirm (it does
   **not** trust the client-supplied digest as authoritative).
3. API appends an `EVIDENCE_BOUND` audit event containing `{evidence_id, sha256,
   event_ref}`.

An attacker who replaces the object in storage cannot change the digest already sealed in
a signed chain. The verifier reports the mismatch.

---

## 6. Server-authoritative exam delivery

The browser is a display, never an authority.

| Concern | Authority |
|---|---|
| Exam duration, remaining time | Server |
| Final submission time | Server |
| Eligibility to start/resume | Server |
| Question selection and order | Server (deterministic seed) |
| Grading | Server |
| Countdown rendering | Client (display only) |

### Clock synchronization

On start and every 30 s, the client calls a lightweight `sync` endpoint and computes a
round-trip-adjusted offset. The displayed countdown derives from
`server_deadline - (client_now + offset)`. Clock drift, tab throttling, and device time
changes therefore affect only the *display*; the server independently rejects any
submission past `deadline + grace`.

### Deterministic question selection

```
seed = SHA-256(exam_id || candidate_id || exam_version)
```

Selection and shuffling consume that seed via a specified deterministic PRNG. The same
candidate recovering a crashed session receives the identical paper. `exam_version` in
the seed means editing an exam after publication does not silently reshuffle in-flight
sessions — it is a distinct version, and in-flight sessions stay pinned to the version
they started.

### Recovery

Autosave on change (debounced, plus a periodic flush) with last-write-wins per
`(session, question)` and a monotonic `answer_revision`. On reload the client fetches
authoritative session state — paper, answers, remaining time, integrity config — and
resumes. No exam progress may be lost to a reload, a crash, or a 90-second network
outage.

---

## 7. The judge pipeline

```
Candidate submits
      │
      ▼
API validates ─ rate limit (sample runs are cheap to abuse) ─ persists submission
      │
      ▼
Redis stream job  {submission_id, language, limits, test_bundle_ref}
      │
      ▼
Judge worker (separate container, separate host pool in production)
      │
      ├─ pull toolchain image (pinned digest)
      ├─ compile step   ─ own timeout, own memory cap, no network
      ├─ execute step   ─ per-test timeout, memory cap, stdout cap
      │                   one ephemeral container per submission
      ├─ collect per-test results
      └─ destroy container unconditionally (defensive: reaper for orphans)
      │
      ▼
Result → Postgres → WebSocket push to candidate
```

Hard container constraints (see [`../SECURITY.md`](../SECURITY.md#5-judge-sandbox)):
`--network=none --read-only --memory=256m --cpus=1.0 --pids-limit=64 --cap-drop=ALL
--security-opt=no-new-privileges --security-opt seccomp=<profile>`, writable `tmpfs` at
`/tmp` with `noexec,nosuid,size=64m`, non-root UID, no bind mounts from the host.

**A slow or hostile submission must not degrade the platform.** Judge workers are a
separate autoscaling pool; queue depth is a monitored metric; the API never blocks on
judge execution.

**Phase 3 gate:** the security test suite (fork bomb, memory bomb, network egress,
filesystem traversal, `/proc` abuse, stdout flood, escape attempts) is written and passing
*before* the normal happy-path runner is considered complete. This is a hard gate, not a
preference.

---

## 8. Risk engine

The risk engine is the most likely place for this product to become dishonest, so its
architecture is constrained deliberately.

**Phase 9 starts with an interpretable baseline only:**

```
score = Σ_events  w(type) · r(type) · d(Δt) · c(confidence)
        + co-occurrence bonus for corroborated signals
```

where `w` = configured severity weight, `r` = measured detector reliability, `d` = time
decay, `c` = detector confidence. Every term is inspectable, and the UI shows the
per-signal contribution.

Rules that are structural, not stylistic:

1. **No threshold produces a verdict.** There is no boolean `cheating` field in the
   schema. There is no code path from risk to score.
2. Output is a probability *with an uncertainty interval*, labelled as
   "estimated session anomaly probability", plus contributing signals.
3. An HMM or learned model is added in Phase 9 **only if** it measurably beats the
   baseline on calibration and false-positive rate against the evaluation harness. If it
   does not, it is not shipped. Complexity must earn its place.
4. Unverifiable ≠ guilty. A session whose chain fails verification is labelled
   `UNVERIFIABLE` and is a *technical* status, displayed separately from risk.

---

## 9. Realtime and connection model

One WebSocket per active session carrying: integrity events (client→server), timer sync
hints, judge results, and server directives (e.g. "stop uploading evidence, cap reached").

REST remains the authority for anything durable. The WebSocket is an optimization; every
critical operation (answer save, submission, event ingest) has a REST fallback so a
blocked WebSocket degrades the experience without breaking the exam.

Redis holds presence and per-session ingest state so API instances stay stateless and
horizontally scalable.

---

## 10. Frontend architecture

- **Next.js App Router + TypeScript.** Server components for staff/authoring screens;
  the exam runner is a client component tree because it owns hard realtime state.
- **Zustand** for exam session state (answers, save status, connection, timer offset).
  Deliberately not Redux — the state graph is small and the ceremony would not pay.
- **Web Workers** for CV inference and hashing so the exam UI never janks. A candidate
  fighting a laggy UI during a timed exam is a product failure and an integrity
  confound.
- **IndexedDB** for the offline event/evidence queue, keyed by `client_seq`.
- **Candidate UI complexity target: Google Forms.** Question, navigation, time, save
  status, connection status. Nothing else competes for attention. Integrity status is a
  single calm indicator, not a threatening dashboard.

### Candidate disclosure

Before an integrity-enabled exam starts, the candidate sees, in plain language: what is
monitored, what is recorded, when evidence becomes visible to staff, how long it is
retained, and how to appeal. This is a blocking screen, not a link. Hiding it would
undermine the entire product thesis.

---

## 11. Calibration

Before the exam clock starts: permissions → enrollment capture (if identity continuity is
enabled) → camera check → lighting check → face visibility check → basic pose
calibration → explicit warnings about poor conditions.

Discovering bad lighting *after* the exam began produces false positives that punish the
candidate for the system's own setup failure. Calibration results are recorded so a
reviewer can see that a flagged session began under known-poor conditions.

---

## 12. Accommodations

Accommodation profiles adjust detector thresholds, dwell times, and evidence policy per
candidate. In the reviewer UI, an accommodation is **not** rendered as a badge on the
candidate — it silently adjusts thresholds, and the applied profile is visible only in the
audit/config detail view. Visibly labelling accommodated candidates would be
stigmatizing and, in several jurisdictions, legally hazardous.

The applied profile is recorded in the audit chain so the configuration is reconstructible
after the fact.

---

## 13. Observability

Structured JSON logs with `request_id`, `org_id`, `session_id` (never answer content,
never evidence bytes, never keys). Metrics: request latency, error rate, WS
connect/failure, queue depth, judge execution time and failure rate, evidence upload
failure rate, storage usage, session failure rate, DB errors.

`/health` = process liveness, no dependencies. `/ready` = Postgres, Redis, and object
store reachable. Distinguishing these prevents a slow database from causing a restart
storm.

---

## 14. What this architecture explicitly does not solve

Stated here rather than discovered later — full list in
[`../LIMITATIONS.md`](../LIMITATIONS.md):

- A second physical device (phone on a stand, second laptop) is largely undetectable from
  a browser.
- OS-level screenshots cannot be prevented or reliably detected by a web page.
- A determined attacker with a modified browser or a virtual camera can feed the CV
  engine whatever they like. The chain will faithfully record the lie.
- Gaze estimation from a consumer webcam is a *proxy*, not eye tracking, and degrades
  with glasses, lighting, and head coverings.
- Verification proves *what the server recorded and signed*. It does not prove the input
  to that server was truthful. This is the central honest limitation of the product and
  is stated in the candidate-facing and buyer-facing material alike.
