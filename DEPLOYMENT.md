# Sentinel — Deployment Architecture

**Status: PHASE 0 design. No `docker-compose.yml` exists yet. Nothing here has been run.**

Per ADR-0003, deployment targets cloud-agnostic containers: the same images run under
`docker compose` on a laptop, on a single VM, or on a managed container platform.

---

## 1. Local development

```
                    docker compose up
                            │
   ┌──────────┬─────────────┼──────────────┬──────────────┬──────────────┐
   │          │             │              │              │              │
┌──▼─────┐ ┌──▼──────┐ ┌────▼─────┐ ┌──────▼────┐ ┌───────▼───┐ ┌────────▼────┐
│  web   │ │   api   │ │ postgres │ │   redis   │ │   minio   │ │ judge-worker│
│ :3000  │ │  :8000  │ │  :5432   │ │   :6379   │ │   :9000   │ │  (no port)  │
│ Next.js│ │ FastAPI │ │    16    │ │     7     │ │           │ │ docker.sock │
└────────┘ └─────────┘ └──────────┘ └───────────┘ └───────────┘ └─────────────┘
                            │
                    ┌───────▼────────┐   ┌──────────────────┐
                    │ migrate (once) │   │ seed (once)      │
                    │ alembic upgrade│   │ demo org + exam  │
                    └────────────────┘   └──────────────────┘
```

### Services

| Service | Image | Notes |
|---|---|---|
| `web` | built from `infrastructure/docker/web.Dockerfile` | Next.js dev server with hot reload |
| `api` | `api.Dockerfile` | uvicorn `--reload`; waits on `postgres` and `redis` health |
| `postgres` | `postgres:16-alpine` | init script creates `sentinel_migrate` and `sentinel_app` (`NOBYPASSRLS`) |
| `redis` | `redis:7-alpine` | AOF on so a restart does not lose queued jobs |
| `minio` | `minio/minio` | console on 9001; bootstrap creates `evidence` and `testcases` buckets, private |
| `migrate` | api image | `alembic upgrade head`, runs once |
| `seed` | api image | idempotent demo data, gated behind `SENTINEL_ENV=development` |
| `judge-worker` | `judge.Dockerfile` | mounts the Docker socket **in dev only** — see the warning below |
| `keygen` | api image | generates a dev Ed25519 keypair into a local volume if absent |

### Bootstrap

```bash
cp .env.example .env
docker compose up --build
# web      http://localhost:3000
# api docs http://localhost:8000/docs
# minio    http://localhost:9001
```

> **First run on an existing checkout:** if you ran this project before
> `infrastructure/postgres/01-roles.sh` learned to install `pgcrypto` and
> `citext`, your Postgres volume predates the fix and the migration will fail
> with `permission denied to create extension "pgcrypto"`. The init script only
> runs on an empty data directory, so `docker compose down -v` once. See
> [`RUNNING.md`](RUNNING.md) for the alternative that keeps the volume, for
> running the API and web app natively under a debugger, and for the Windows
> line-ending and port-conflict failures.

Seeded: `demo-university`, one instructor, one reviewer, five candidates, one published
exam (3 single-choice, 2 multi-choice, 1 short answer, 1 numeric, 1 coding with 2 sample
and 4 hidden tests; two sections, one using a `k of n` pool). **Credentials come from
`.env`; none are committed.**

### Two things that are dev-only and must never reach production

> **1. The judge worker mounts `/var/run/docker.sock`.** This is equivalent to root on the
> host. It is acceptable on a developer laptop and is a critical vulnerability anywhere
> else. Production uses a rootless daemon or a broker on an isolated host pool.
>
> **2. The signing key is a file in a Docker volume.** Anyone with host access can sign
> arbitrary events, which voids every tamper-evidence claim Sentinel makes. Production
> requires KMS/HSM custody.

Both are called out again in [`SECURITY.md`](SECURITY.md#3-key-management) because they are
the two most likely things to be copied into production by accident.

---

## 2. Production architecture

```
                            ┌──────────────┐
                            │   CDN / WAF  │  TLS, DDoS, rate limiting
                            └──────┬───────┘
                                   │
                            ┌──────▼───────┐
                            │Load balancer │  HTTPS + WSS, sticky not required
                            └──┬────────┬──┘
                               │        │
                   ┌───────────▼──┐  ┌──▼───────────────┐
                   │  web (N)     │  │   api (N)        │  stateless, autoscaled
                   │  Next.js     │  │   FastAPI        │
                   └──────────────┘  └──┬────┬──────┬───┘
                                        │    │      │
        ┌───────────────────────────────┘    │      └──────────────┐
        │                                    │                     │
┌───────▼─────────┐              ┌───────────▼──────┐   ┌──────────▼─────────┐
│ Managed Postgres│              │  Managed Redis   │   │ S3-compatible store│
│ primary+replica │              │  queues, limits  │   │ private, versioned,│
│ PITR backups    │              │  presence        │   │ object lock, SSE   │
│ encryption      │              └───────────┬──────┘   └────────────────────┘
└─────────────────┘                          │
                                  ┌──────────▼───────────────┐
                                  │  JUDGE POOL              │
                                  │  dedicated hosts         │
                                  │  rootless runtime        │
                                  │  no inbound network      │
                                  │  autoscaled on queue depth│
                                  └──────────────────────────┘
                                             │
                                  ┌──────────▼───────────────┐
                                  │  retention worker (cron) │
                                  └──────────────────────────┘

        ┌────────────────────────────────────────────────────┐
        │  KMS / HSM — Ed25519 signing key                   │
        │  API service account may sign. No human may export.│
        │  Not administrable by the DBA. (THREAT_MODEL T4.3) │
        └────────────────────────────────────────────────────┘
```

### Why judge workers get their own host pool

They execute attacker-supplied code. Container isolation is not a hard security boundary
([`THREAT_MODEL.md §T5.9`](THREAT_MODEL.md)). If a submission escapes its container, it
should land on a host with no database credentials, no signing key, no candidate data, and
no inbound network — not next to the API.

### Scaling characteristics

| Component | Scaling | Bottleneck |
|---|---|---|
| `web` | Horizontal, stateless | CPU on SSR |
| `api` | Horizontal, stateless | DB connections → PgBouncer in **transaction** mode |
| WebSocket | Horizontal; presence in Redis | Connections per instance |
| Event ingest | Per-session serialized by `FOR UPDATE`; sessions parallel | Row lock per session (by design, ADR-0004) |
| Postgres | Vertical + read replicas for reporting | Write throughput on `audit_event` |
| Judge | Horizontal on queue depth | CPU; one container per submission |
| Object storage | Provider-managed | Bandwidth on evidence confirm-and-hash |

**PgBouncer transaction mode is mandatory, not optional**, and it is why tenant context
uses `SET LOCAL` rather than `SET` — session-scoped settings would leak between tenants on
a pooled connection. That is a cross-tenant data breach, and it is subtle enough to reach
production unnoticed, so it gets an explicit test.

---

## 3. Configuration

All configuration via environment. `.env.example` is committed; `.env` never is.

```bash
SENTINEL_ENV=production            # development | staging | production
DATABASE_URL=postgresql+psycopg://sentinel_app:...@host:5432/sentinel
REDIS_URL=rediss://...
S3_ENDPOINT=https://...            # MinIO or any S3-compatible service
S3_BUCKET_EVIDENCE=sentinel-evidence
S3_REGION=...
SIGNING_KEY_PROVIDER=kms           # kms | file  ("file" is rejected when ENV=production)
SIGNING_KEY_REF=arn:...            # KMS key reference
JWT_PUBLIC_KEY / JWT_PRIVATE_KEY_REF
CORS_ORIGINS=https://app.example.edu
EVIDENCE_MAX_PER_SESSION=40
EVIDENCE_COOLDOWN_SECONDS=20
EVENT_LATE_THRESHOLD_SECONDS=30
JUDGE_MAX_CONCURRENT=8
RETENTION_DEFAULT_DAYS=30
```

**The API refuses to start when `SENTINEL_ENV=production` and
`SIGNING_KEY_PROVIDER=file`.** Refusing to boot is the correct response to a configuration
that silently voids the product's central claim.

---

## 4. Migrations

Alembic, forward-only, run by a job before the new version serves traffic.

- Additive first: add nullable column → backfill → add constraint → drop old. Never a
  destructive change in the same release as the code that depends on it.
- Zero-downtime: N and N+1 of the API must both work against the intermediate schema.
- Every migration includes a downgrade, and every downgrade is tested in CI even though
  production rolls forward.
- `audit_event`, `evidence_access_log`, `activity_log`, `review_decision`, and
  `purge_record` are append-only by trigger. **A migration that alters historical rows in
  these tables is a red-line change** and requires explicit sign-off — it is exactly the
  operation the triggers exist to prevent.

---

## 5. Backups and recovery

| Asset | Method | Target |
|---|---|---|
| Postgres | Continuous WAL + daily full, PITR | RPO 5 min, RTO 1 h |
| Object storage | Versioning + cross-region replication | RPO 15 min |
| Signing keys | KMS-managed durability; public keys additionally in Git | No loss tolerated |
| Redis | AOF; queue loss is recoverable by resubmission | RPO best-effort |

**Restore drills are quarterly and non-optional.** An untested backup is a hypothesis.

Losing a signing *private* key means no new events can be signed under it — recoverable by
rotation. Losing the *public* key means past chains cannot be verified — which is why
public keys are kept in version control as well as in the database.

---

## 6. Production readiness checklist (Phase 11 gate)

Nothing ships as production-ready until every line is checked and evidenced.

**Security**
- [ ] MFA enforced for `org_admin` and `reviewer`
- [ ] Authz matrix test covers every endpoint
- [ ] RLS enabled and **forced** on all 35 tenant tables; invariant test green
- [ ] App DB role is non-owner and `NOBYPASSRLS`
- [ ] Signing key in KMS; file provider rejected in production
- [ ] Judge sandbox security tests green on the production runtime
- [ ] Evidence buckets deny anonymous access (asserted by test)
- [ ] Presigned URLs short-lived, single-use, user-bound
- [ ] CSP, HSTS, and security headers verified against a live deployment
- [ ] Dependency and image scans clean or triaged with dated exceptions
- [ ] No secrets in the repository; secret scanning enabled

**Reliability**
- [ ] `/health` and `/ready` correct (liveness does not touch the DB)
- [ ] Autoscaling configured; judge scales on queue depth
- [ ] Rate limits tuned against a realistic exam-day load test
- [ ] PgBouncer transaction mode verified with the `SET LOCAL` tenancy test
- [ ] Backups verified by an actual restore
- [ ] Load test at peak concurrency for a real customer cohort

**Correctness**
- [ ] Full test suite green
- [ ] Chain tamper suite detects all eight attack classes
- [ ] Browser tests pass on the supported browser matrix
- [ ] Reload, crash, and disconnect recovery verified under load

**Operational**
- [ ] Structured logging with no secrets, answers, or evidence bytes
- [ ] Metrics and alerts wired
- [ ] Runbooks: key rotation, chain failure, judge outage, evidence upload failure
- [ ] Retention automation running with purge events landing in the chain
- [ ] Status page and incident process

**Product and legal**
- [ ] `LIMITATIONS.md` current and linked from candidate-facing disclosure
- [ ] Candidate disclosure screen reviewed for plain language
- [ ] Retention defaults documented per organization
- [ ] Accessibility audit (WCAG 2.2 AA) on the candidate exam runner
- [ ] Measured false-positive rates published to customers, including unflattering ones
- [ ] Customer-facing documentation of what organizations must configure for their own
      jurisdiction

---

## 7. Browser support

Integrity features are desktop-first. Support is feature-detected at calibration and the
candidate is told before the exam starts which signals are unavailable on their browser.

| Feature | Chrome/Edge | Firefox | Safari |
|---|---|---|---|
| Fullscreen, visibility, focus | Yes | Yes | Yes |
| `getDisplayMedia` | Yes | Yes | Partial (OS permission required) |
| `screen.isExtended` | Yes | No | No |
| `requestVideoFrameCallback` | Yes | Varies by version | Yes |
| MediaPipe / ONNX Runtime Web | Yes | Yes | Varies |

Where a signal is unavailable, its absence is recorded as unavailable rather than as
"nothing detected" — and reviewers see the distinction. Conflating "we could not look" with
"we looked and saw nothing" is how a reviewer draws a wrong conclusion from a correct
record.
