# Sentinel

**Evidence-first assessment infrastructure for trustworthy remote examinations.**

Sentinel helps organizations conduct remote assessments while producing a transparent,
tamper-evident, human-reviewable evidence trail.

Sentinel is **not** "AI that catches cheaters". It does not decide guilt, it does not
automatically fail candidates, and it does not present an opaque score as fact. It
produces cryptographically verifiable evidence and routes ambiguity to a human.

---

## Current status — PHASE 3 complete

| Component | Status |
|---|---|
| Architecture, schema, threat model, scope (Phase 0) | **IMPLEMENTED** (documents) |
| Migrations, schema, RLS on all 35 tenant tables | **IMPLEMENTED** |
| Candidate-scoped RLS on session and judge tables | **IMPLEMENTED** (migrations 0002, 0003) |
| Authentication: password, tokens, refresh rotation, lockout | **IMPLEMENTED** |
| MFA: TOTP enrolment and verification | **IMPLEMENTED** |
| Organizations, memberships, four roles, authz matrix | **IMPLEMENTED** |
| Question bank: CRUD, immutable versions, JSON import/export | **IMPLEMENTED** |
| Exam authoring: sections, `k of n` pools, publishing, assignment | **IMPLEMENTED** |
| Deterministic per-candidate papers | **IMPLEMENTED** |
| Server-authoritative timer, autosave, recovery, submission | **IMPLEMENTED** |
| Auto-grading: choice, multi-choice, short answer, numeric | **IMPLEMENTED** |
| **Judge sandbox: containment verified against real containers** | **IMPLEMENTED** — 68 security tests |
| Code execution: Python 3.11, C++20, sample + hidden tests, partial scoring | **IMPLEMENTED** |
| Code execution: Java 17 | **NOT VERIFIED** — adapter written, never executed |
| Judge queue, worker, crash recovery | **PARTIALLY IMPLEMENTED** — stale-job requeue is untested |
| Candidate UI: sign in, assessment list, exam runner, code editor | **IMPLEMENTED** |
| Judge sandbox images (`python:3.11-slim` etc.) | **NOT VERIFIED** — Dockerfiles written, never built |
| Navigation modes other than `free` | **NOT IMPLEMENTED** — refused at authoring time |
| Offline answer queue | **NOT IMPLEMENTED** — Phase 4 |
| Staff authoring UI | **NOT IMPLEMENTED** — authoring is API-only |
| Production key custody (KMS) | **STUB** — raises `NotImplementedError` by design |
| Docker Compose stack | **PARTIALLY VERIFIED** — pgcrypto blocker fixed; `docker compose up` never run to completion |
| Integrity events, audit chain, evidence, review, risk | **NOT IMPLEMENTED** |

**156 API tests, 68 security tests and 32 browser tests pass** against a live
PostgreSQL 16, a live Redis, a real Docker daemon and a real Chromium. The
security suite runs actual containers — a fork bomb, a memory bomb, a network
callback, a disk-filling write loop and a stdout flood, each with a positive
control proving the containment is doing the work.

See [`docs/PHASE_REPORTS.md`](docs/PHASE_REPORTS.md) for what was verified, what
was found, and what does not work as it might appear to.

Everything not listed as IMPLEMENTED above is a *design intention*, not a
verified property.

---

## Documentation index

| Document | Purpose |
|---|---|
| [`RUNNING.md`](RUNNING.md) | **Start here to get it running** — Docker, VS Code, and what to do when it fails |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | System architecture, module boundaries, data flows |
| [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md) | Complete database schema, ER model, multi-tenancy |
| [`docs/API.md`](docs/API.md) | API architecture, endpoint surface, WebSocket protocol, canonical JSON |
| [`docs/MVP_SCOPE.md`](docs/MVP_SCOPE.md) | P0 / P1 / P2 feature breakdown and phase plan |
| [`docs/TESTING.md`](docs/TESTING.md) | Test strategy and the definition of done |
| [`docs/PHASE_REPORTS.md`](docs/PHASE_REPORTS.md) | **What each phase actually delivered, and what it did not** |
| [`docs/OPEN_DECISIONS.md`](docs/OPEN_DECISIONS.md) | Decisions still open |
| [`THREAT_MODEL.md`](THREAT_MODEL.md) | Threat actors, attacks, mitigations, residual risk |
| [`SECURITY.md`](SECURITY.md) | Security controls, key management, disclosure policy |
| [`LIMITATIONS.md`](LIMITATIONS.md) | What Sentinel honestly cannot do |
| [`DEPLOYMENT.md`](DEPLOYMENT.md) | Local Docker architecture and production deployment |
| [`DECISIONS.md`](DECISIONS.md) | Architectural decision record (ADR) log |

**Read [`docs/PHASE_REPORTS.md`](docs/PHASE_REPORTS.md) first** for the honest status,
then [`docs/OPEN_DECISIONS.md`](docs/OPEN_DECISIONS.md) for what still needs your call.

---

## Product shape

Sentinel is three engines and a review layer, deliberately decoupled so an organization
can buy a subset:

| Module | Sold as | Depends on |
|---|---|---|
| **Assessment Engine** | Assessment only | — |
| **Judge Engine** | Assessment + code judge | Assessment |
| **Integrity Engine** | Assessment + integrity monitoring | Assessment |
| **Review Layer** | Full Sentinel integrity suite | Integrity |

The Assessment Engine must be fully usable with the Integrity Engine switched off. This
is a hard architectural constraint, not a preference — see ADR-0003.

---

## The end-to-end flow Sentinel must support

```
Create organization → Create exam → Import questions → Assign candidates
    → Candidate takes exam → Answers autosaved → Code executed securely
    → Integrity events recorded → Relevant evidence captured → Audit chain signed
    → Exam submitted → Results calculated → Suspicious sessions enter review queue
    → Reviewer examines evidence → Reviewer decides
    → Candidate inspects their permitted evidence → Organization exports results
```

This flow working end-to-end is the bar for "MVP". Advanced detectors come after.

---

## Repository structure

```
sentinel/
│
├── apps/
│   ├── web/                     Next.js 15 App Router, TypeScript, Tailwind, Zustand
│   │   ├── app/
│   │   │   ├── (auth)/          login, password reset
│   │   │   ├── (candidate)/     exam list, exam runner, results, my-evidence
│   │   │   ├── (staff)/         exam authoring, question bank, roster, reviewer UI
│   │   │   └── api/             BFF route handlers (session cookie exchange only)
│   │   ├── components/
│   │   ├── lib/
│   │   │   ├── exam/            timer sync, autosave, reconnect recovery
│   │   │   ├── integrity/       browser signal collectors, hysteresis state machines
│   │   │   ├── evidence/        rolling frame buffer, capture, pHash, upload queue
│   │   │   ├── offline/         IndexedDB queue, replay-safe resend
│   │   │   └── crypto/          client-side chain verification for candidate export
│   │   └── workers/             Web Workers: cv-worker, hash-worker
│   │
│   └── api/                     FastAPI, Python 3.11+, SQLAlchemy 2.x, Pydantic v2
│       ├── sentinel_api/
│       │   ├── core/            config, logging, errors, dependency wiring
│       │   ├── auth/            password, tokens, MFA, OIDC seam
│       │   ├── tenancy/         org context, RLS session binding, authz policies
│       │   ├── assessment/      exams, sections, questions, assignment, delivery
│       │   ├── grading/         autograders per question type
│       │   ├── integrity/       event ingest, validation, hysteresis config
│       │   ├── audit/           canonical JSON, hash chain, Ed25519 signing
│       │   ├── evidence/        upload intents, object store, retention, access log
│       │   ├── risk/            baseline interpretable scorer
│       │   ├── review/          queue, decisions, justification
│       │   ├── reporting/       exports, analytics, business metrics
│       │   ├── ws/              WebSocket session channel
│       │   └── migrations/      Alembic
│       └── tests/
│
├── workers/
│   ├── judge/                   Redis-consuming judge worker, Docker SDK
│   │   ├── runner/              container lifecycle, limits, seccomp
│   │   ├── languages/           python311, cpp20, java17 toolchain adapters
│   │   └── images/              Dockerfiles for the sandbox images
│   └── retention/               scheduled purge worker, emits purge audit events
│
├── packages/
│   ├── schemas/                 JSON Schema: question bank, event envelope, export bundle
│   │   └── versions/            v1/, v2/ — schemas are versioned, never edited in place
│   └── shared/                  TS types generated from OpenAPI + JSON Schema
│
├── infrastructure/
│   ├── docker/                  Dockerfiles for web, api, judge, verifier
│   ├── postgres/                init SQL, RLS bootstrap, role grants
│   ├── redis/
│   ├── minio/                   local bucket + policy bootstrap
│   └── security/                seccomp profiles, container policies, CSP
│
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── security/                tenant isolation, replay, forgery, sandbox escape
│   ├── browser/                 Playwright
│   └── evaluation/              the four studies (red team, FPR, judge load, reviewer)
│
├── tools/
│   └── verifier/
│       └── verify_chain.py      standalone, zero dependency on the app
│
├── scripts/                     seed, keygen, dev bootstrap, doc checks
├── docs/
│
├── DECISIONS.md
├── LIMITATIONS.md
├── THREAT_MODEL.md
├── SECURITY.md
├── DEPLOYMENT.md
├── README.md
├── .env.example
└── docker-compose.yml
```

### Deviations from the structure in the brief

Three, each justified:

1. **`tools/verifier/` instead of `verify_chain.py` at the repo root.** The verifier must
   be independently distributable — a candidate or an auditor should be able to download
   *only* the verifier and run it against an export bundle. Giving it its own directory
   with its own `pyproject.toml` and no import path into `apps/api` makes that
   independence structural rather than a convention someone will eventually break.

2. **`workers/retention/` added.** Retention purging emits audit events and deletes
   evidence objects. It is a scheduled adversarial-adjacent process and does not belong
   inside the request-serving API.

3. **`packages/schemas/versions/`.** The brief requires versioned schemas. Nesting by
   version makes it impossible to silently mutate a published schema, which is exactly
   the failure mode that breaks old export bundles.

---

## Non-negotiable engineering rules

These are enforced by tests, not by discipline:

1. No detector output ever writes to a grading or pass/fail field. There is no code path
   from `risk_assessment` to `session.score`. A test asserts this.
2. Every tenant-owned table has `org_id NOT NULL` and an enabled RLS policy. A test
   enumerates `information_schema` and fails on any table that doesn't.
3. The audit signing key never enters the web tier, never enters a log, never enters an
   API response. A test greps the OpenAPI schema and log formatter for key fields.
4. Evidence objects are never served from a public URL. A test asserts the bucket policy
   denies anonymous reads.
5. The judge runs with `--network=none`. A security test asserts a submission that tries
   to open a socket fails to reach anything.
6. Nothing is labelled "IMPLEMENTED" without a passing test. See
   [`docs/TESTING.md`](docs/TESTING.md).

---

## License and data responsibility

Sentinel processes biometric-adjacent data (facial images, face embeddings) in some
configurations. Deploying organizations are responsible for their own lawful basis,
disclosure, retention, and data-subject-rights obligations. Sentinel provides
configuration and documentation to support that; it does not confer compliance. See
[`SECURITY.md`](SECURITY.md) and [`LIMITATIONS.md`](LIMITATIONS.md).
