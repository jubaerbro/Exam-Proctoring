# Sentinel

**Evidence-first assessment infrastructure for trustworthy remote examinations.**

Sentinel helps organizations conduct remote assessments while producing a transparent,
tamper-evident, human-reviewable evidence trail.


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

## License and data responsibility

Sentinel processes biometric-adjacent data (facial images, face embeddings) in some
configurations. Deploying organizations are responsible for their own lawful basis,
disclosure, retention, and data-subject-rights obligations. Sentinel provides
configuration and documentation to support that; it does not confer compliance. See
[`SECURITY.md`](SECURITY.md) and [`LIMITATIONS.md`](LIMITATIONS.md).
