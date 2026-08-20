# Sentinel — Phase Reports

Reports use the format required by the brief (§55), including the last question.

---

# PHASE 1 — FOUNDATION

**Date:** 2026-08-18
**Verified against:** PostgreSQL 16.13, Redis 7.0.15, Python 3.11.15, Node 22.22.2,
Chromium 1194, all running live. No mocks, no SQLite, no stubbed database.

---

## Decisions taken to unblock this phase

The product owner approved D7 and said to proceed. D1, D3, D4, D12 and D13 were still
open, so the following defaults were applied. Each is stated here so it can be reversed
cheaply if it was the wrong call.

| Ref | Question | Applied default | Cost of reversing |
|---|---|---|---|
| **D7** | Live monitoring feedback to candidates? | **No live feedback** (owner's decision). Recorded as ADR-0016 | — |
| **D2** | Identity across organizations | Global `app_user` + tenant-scoped `membership`, as designed | Schema change; do it before real data exists |
| **D12** | Phase 1 build order | Vertical slice — sign in → see your assessment → sign out | None |
| **D13** | Repository | `git init` locally, three commits, **no remote** | None; add a remote any time |
| D1, D3, D4 | Market, native client, judge isolation | Not needed for Phase 1; still open | — |

---

## IMPLEMENTED

Everything below has a passing test.

**Database and tenancy**
- Alembic migration `0001` applies the full 40-table schema and **generates** RLS from the
  catalog: every table with an `org_id` gets `ENABLE` + `FORCE ROW LEVEL SECURITY` and a
  tenant policy. 35 tenant tables, 36 policies, zero unprotected.
- The migration fails itself if any tenant table comes out of that loop unprotected.
- Custom policies where the blanket rule is wrong: `membership` (a user must read their
  own memberships before an organization is known), `exam_session` (candidates see only
  their own), `audit_event` (SELECT + INSERT only, no candidate writes), `activity_log`
  (org-less auth events).
- Downgrade tested: `0001 → base → 0001` round-trips cleanly.
- Two database roles: `sentinel_migrate` owns the schema, `sentinel_app` owns nothing and
  is `NOBYPASSRLS`.

**Authentication**
- Argon2id (m=64 MiB, t=3, p=4). Length + breach-list policy, no composition rules.
- EdDSA access tokens, 15 min, carrying identity and roles but **no permissions** —
  permissions resolve per request, so a revoked role takes effect within one token
  lifetime without a blacklist.
- Opaque 256-bit refresh tokens, stored only as SHA-256 digests, rotating, with family
  reuse detection.
- Exponential lockout (60s → 1h), deliberately not permanent: a permanent lock is a
  denial-of-service anyone can trigger against a candidate on exam day.
- Identical error text, status and shape for unknown and known accounts, plus a dummy
  Argon2 verification on the missing-user path so timing does not enumerate accounts.
- `httpOnly` cookies; the refresh cookie is scoped to `/api/v1/auth`; a separate readable
  CSRF cookie for double-submit.
- MFA **required** for `org_admin` and `reviewer`, and login **fails closed** when the
  role requires it and the account has not enrolled.

**Authorization**
- Declarative (role × endpoint) policy table; a route missing from it fails CI.
- The reverse check too: a stale policy entry with no live route also fails.
- Cross-tenant access returns 404, never 403.

**API**
- RFC 9457 `problem+json` errors with per-field JSON Pointers on validation failures.
- `/health` (liveness, touches nothing) and `/ready` (Postgres + Redis, reports which is
  unhappy). Object storage is honestly reported as `NOT IMPLEMENTED (Phase 6)` rather than
  faked green.
- `/api/v1/public-keys` unauthenticated by design.
- Security headers on every response; structured JSON logs with a redaction backstop.

**Configuration guards** — the API refuses to start when:
- `SENTINEL_ENV=production` and `SIGNING_KEY_PROVIDER=file`
- production without a KMS reference, without `COOKIE_SECURE`, or with a plaintext
  `http://` CORS origin
- `DATABASE_URL` points at the `postgres` superuser, **in any environment** (RLS is
  bypassed for superusers)

**Frontend** — Next.js 15 App Router, TypeScript strict, Tailwind, Zustand. Sign-in with
MFA step, assessment list, integrity disclosure, sign-out. Production build clean.

**Seed** — one organization, one instructor, one reviewer, five candidates, one published
exam: 3 single-choice, 2 multiple-choice, 1 short answer, 1 numeric, 1 coding with 2
sample + 4 hidden test cases, 2 sections, `k of n` pools. Idempotent. Refuses to run
outside `SENTINEL_ENV=development`.

---

## TESTS

```
API suite                56 passed   (35 marked `security`)
Browser suite            17 passed   (real Chromium against the real stack)
ruff                     clean
ruff format              clean
mypy (auth, core)        clean, 14 files
next build               clean
tsc --noEmit             clean
```

Run twice: once against a **freshly created empty database**, once against a warm one.
Both green — the suite does not depend on a disposable database or on test ordering.

**What the security tests actually prove**

| Assertion | Result |
|---|---|
| App role is not a superuser and lacks `BYPASSRLS` | PASS — every other RLS test rests on this |
| All 35 tenant tables have forced RLS and a policy | PASS |
| Missing tenant context returns **zero rows**, not all rows | PASS |
| Cross-tenant read by primary key returns nothing | PASS |
| Cross-tenant write rejected by `WITH CHECK` | PASS |
| Tenant context does not survive a transaction (the PgBouncer trap) | PASS |
| A candidate cannot read another candidate's session *in the same org* | PASS |
| `UPDATE`/`DELETE` on `audit_event` rejected by trigger | PASS |
| RLS exposes **no** `UPDATE`/`DELETE` path for `audit_event` at all | PASS — a second, independent layer |
| Duplicate `client_seq` rejected (replay) | PASS |
| Malformed signature length rejected | PASS |
| Review decision with a 5-character justification rejected | PASS |
| Forged JWT signature rejected | PASS |
| `alg: none` JWT rejected | PASS |
| MFA challenge token cannot be used as an access token | PASS |
| Refresh rotation + reuse detection revokes the family | PASS |
| Refresh tokens stored as 32-byte digests, not plaintext | PASS |
| Unknown vs known account produce identical errors | PASS |
| No `cheating` / `guilty` / `misconduct` column anywhere | PASS |
| No foreign key between risk and scoring | PASS |
| Access token never in `localStorage`, `sessionStorage`, or `document.cookie` | PASS (browser) |

---

## SECURITY — what was found while testing

Four real defects, all found by tests rather than by reading the code. Recording them
because "we wrote tests and they all passed first time" would be a suspicious claim.

1. **RLS blocked login.** The generated policy `org_id = current_org_id()` evaluates to
   NULL for `activity_log` rows with a NULL `org_id` — which is every authentication
   event, since they happen before an organization is known. Login was impossible.
   Fixed with a custom policy that permits org-less writes and scopes reads to
   "your organization, plus org-less rows about you". The naive fix (allow all NULL-org
   reads) would have let one tenant read another's login history.

2. **Refresh-reuse revocation was being rolled back.** On detecting a replayed refresh
   token the service revoked the whole token family, then raised — and the exception
   rolled back the transaction that contained the revocation. The security action was
   silently undone by the error that reported it, and the stolen token kept working.
   Fixed by committing before raising. **This is the most serious bug found in Phase 1**
   and it would have been invisible without a test that checked the *legitimate* holder
   was also locked out afterwards.

3. **Cookies silently outranked Bearer tokens.** A test sending a deliberately forged
   Bearer token got a 200, because the request also carried a valid cookie. Explicit
   credentials now win over ambient ones (ADR-0017).

4. **Two route-coverage tests were passing vacuously.** This FastAPI version wraps
   included routers in `_IncludedRouter` objects, so walking `app.routes` naively found
   three docs endpoints and nothing else. "Every route has an authorization rule" was
   checking a list of zero routes. Fixed with a flattening helper, plus an assertion that
   the discovered route count is plausible — a coverage test that finds nothing to cover
   must fail, not pass.

Also fixed: a flaky browser assertion that read page content before the exam fetch
resolved. It passed on the first run by luck. A flaky test is worse than a failing one
because it erodes trust in the whole suite.

---

## LIMITATIONS — what is imperfect

- **Docker Compose has never been run.** No Docker daemon was available in the build
  environment. The stack was verified by running the same components natively. The
  compose file, Dockerfiles, and bootstrap scripts are written and internally consistent,
  but `docker compose up` is **unverified** and is the first thing to try on a machine
  with Docker.
- **MinIO was never started.** `/ready` reports object storage as `NOT IMPLEMENTED` rather
  than pretending to check it.
- **MFA has no enrolment endpoint.** Verification works and is tested. Nothing can enrol,
  so the seeded reviewer account **cannot sign in at all** — which is the correct
  fail-closed behaviour, and also means the reviewer role is currently unusable.
- **The breach-password list is 13 hard-coded entries**, not a real corpus. The loader
  for a real one raises `NotImplementedError` rather than pretending.
- **TOTP secrets use AES-256-GCM with a locally held key**, not real envelope encryption.
  A database administrator with host access can decrypt them.
- **KMS key custody does not exist.** `KmsKeyProvider` raises by design. Sentinel cannot
  legitimately run in production until this is real — the tamper-evidence claim reduces
  to it.
- **ORM models cover the Phase 1/2 subset only.** All 40 tables exist in the database with
  their constraints and triggers; `audit_event`, `evidence_object`, `review`,
  `risk_assessment` and others have no ORM mapping yet.
- **RLS query-plan overhead has not been measured.** ADR-0001 said it must be; it has not
  been.
- **No load testing, no accessibility audit, no dependency scan** yet.

---

## FILES CHANGED

```
docker-compose.yml, .env.example, .gitignore
infrastructure/docker/{api,web,judge}.Dockerfile
infrastructure/postgres/01-roles.sh
infrastructure/minio/bootstrap.sh

apps/api/pyproject.toml, alembic.ini, pytest.ini
apps/api/sentinel_api/main.py, models.py
apps/api/sentinel_api/core/{config,db,errors,logging,runtime,health}.py
apps/api/sentinel_api/auth/{passwords,keys,tokens,secrets_box,service,router}.py
apps/api/sentinel_api/tenancy/{policies,deps}.py
apps/api/sentinel_api/reporting/router.py
apps/api/sentinel_api/migrations/{env.py,script.py.mako}
apps/api/sentinel_api/migrations/versions/0001_initial_schema.py
apps/api/sentinel_api/migrations/sql/0001_schema.sql
apps/api/tests/{conftest,test_auth,test_tenancy_rls,test_append_only,
                test_authz_matrix,test_config_guards}.py

apps/web/{package.json,tsconfig.json,next.config.mjs,tailwind.config.ts,postcss.config.mjs}
apps/web/lib/{api.ts,session.ts}
apps/web/app/{layout.tsx,page.tsx,globals.css,icon.svg}
apps/web/app/login/page.tsx, apps/web/app/exams/page.tsx

scripts/{keygen.py,seed.py}
tests/browser/test_login_flow.py
docs/PHASE_REPORTS.md   (this file)
DECISIONS.md            (ADR-0016, ADR-0017)
README.md               (status)
```

---

## DATABASE CHANGES

Migration `0001_initial_schema`: 40 tables, 17 enum types, 4 functions, 6 append-only
triggers, 57 CHECK constraints, 103 foreign keys, 113 indexes, and generated RLS on
35 tenant tables (36 policies). Downgrade implemented and tested.

---

## API CHANGES

```
GET    /health                        GET    /ready
GET    /api/v1/public-keys            (unauthenticated by design)
POST   /api/v1/auth/login             POST   /api/v1/auth/mfa/verify
POST   /api/v1/auth/refresh           POST   /api/v1/auth/logout
POST   /api/v1/auth/switch-org        GET    /api/v1/auth/me
GET    /api/v1/org                    GET    /api/v1/org/members
GET    /api/v1/org/activity           GET    /api/v1/me/exams
GET    /api/v1/exams                  (STUB — headers only, Phase 2)
```

---

## WHAT DID I IMPLEMENT THAT DOES NOT ACTUALLY WORK AS CLAIMED?

Five things. In descending order of how likely they are to mislead someone reading the
code or the commit log:

1. **`docker compose up` is untested.** The README's original promise was that it works
   from a clean clone. It has never been executed. I verified the same components
   natively instead. Treat the compose file as unproven until someone runs it.

2. **The reviewer role is unusable.** MFA is mandatory for it, verification is
   implemented and tested, but enrolment is not — so the seeded reviewer can never sign
   in. The security posture is right; the feature is incomplete, and a demo of "the
   reviewer workflow" is not possible today.

3. **`GET /api/v1/exams` is a stub.** It returns exam titles because the staff UI needs
   something to render. There is no exam authoring, no sections, no pools, no publishing.
   It is labelled STUB in the docstring but it returns 200 and looks functional.

4. **Nothing in the audit-chain package is implemented.** `sentinel_api/audit/` is an
   empty package. The schema, constraints, and triggers that *support* the chain are real
   and tested; the canonical JSON, hashing, signing, and verification are Phase 5. The
   `/public-keys` endpoint returns a real key that currently signs nothing.

5. **`SecretBox` and `FileKeyProvider` are not production key management** despite having
   the shape of it. They work, they are tested, and they would be a serious mistake to
   deploy.

One more, which is about method rather than code: **the security tests were written
alongside the implementation, not before it.** Phase 3 requires the reverse for the judge
sandbox, and that ordering is a hard gate there.

---

## NEXT PHASE

**Phase 2 — Assessment engine.** Question bank CRUD and versioning, JSON import/export
with per-field validation errors, exam authoring with sections and `k of n` pools,
candidate assignment, deterministic paper generation from the seed, server-authoritative
timer with clock sync, autosave, reload/crash/reconnect recovery, submission with
server-side deadline enforcement, and auto-grading for the four non-coding question types.

Exit criteria (from `MVP_SCOPE.md`): a candidate completes an exam end-to-end; a forced
reload mid-exam loses nothing; the server rejects a late submission; the deterministic
paper reproduces exactly on recovery.

Before starting, two things are worth doing first:

- **Try `docker compose up` on a machine with Docker** and fix whatever it reveals. That
  is the one claim in Phase 1 with no evidence behind it.
- **Add the MFA enrolment endpoint** — it is small, and it unblocks the reviewer role,
  which Phase 8 depends on entirely.

Still open and not blocking Phase 2: **D1** (first market), **D3** (native client),
**D4** (judge isolation — blocks Phase 3, not Phase 2). See
[`OPEN_DECISIONS.md`](OPEN_DECISIONS.md).

---

# PHASE 2 — ASSESSMENT ENGINE

**Date:** 2026-08-19
**Verified against:** PostgreSQL 16.13, Redis 7.0.15, Python 3.11.15, Node 22.22.2,
Chromium 1194, all running live. No mocks, no SQLite, no stubbed database.

---

## The blocker this phase opened with

Phase 1's report named `docker compose up` as its one unverified claim. Running it
produced, on the first migration:

```
psycopg.errors.InsufficientPrivilege: permission denied to create extension "pgcrypto"
HINT:  Must have CREATE privilege on current database to create this extension.
```

`0001_schema.sql` opens with `CREATE EXTENSION IF NOT EXISTS pgcrypto` / `citext`, and
`sentinel_migrate` owns the `public` schema but has no `CREATE` on the *database* — which
is deliberate, and should stay that way. Granting it would let the schema owner create
schemas outside the RLS-generation loop in migration 0001, which is precisely the hole
that loop exists to close. Managed Postgres (RDS, Cloud SQL, Azure) refuses
`CREATE EXTENSION` from a non-superuser regardless of grants, so a migration that installs
its own extensions cannot be deployed to any of them.

Fixed by moving both extensions into `infrastructure/postgres/01-roles.sh`, which runs as
the superuser at cluster init. The schema's `IF NOT EXISTS` then becomes a clean no-op —
the privilege check is not reached, which was verified directly rather than assumed.

Migration 0001 also gained a preflight that fails with an actionable message instead of a
hundred-line SQLAlchemy traceback, because `docker-entrypoint-initdb.d` only runs on an
*empty* data directory: anyone whose volume predates this change still needs the one-line
fix, and the error now tells them exactly what to run.

---

## Decisions taken to unblock this phase

| Ref | Question | Applied default | Cost of reversing |
|---|---|---|---|
| — | Navigation modes | `free` only. `sequential` / `one_way` are refused at authoring time with a NOT IMPLEMENTED message | None; implement and delete the guard |
| — | Marks within a `k of n` pool | Must be equal, enforced at publish | Relax the check; no schema change |
| — | Grading trigger | Inline on submit, in a separate transaction under a new `system` role | Move to a queue worker; no schema change |
| D1, D3, D4 | Market, native client, judge isolation | Still open. D4 blocks Phase 3 | — |

---

## IMPLEMENTED

Everything below has a passing test.

**Question bank**
- Banks, questions, and **immutable published versions**. Editing a question creates
  version *n+1*; a version referenced by a pool or a delivered paper never changes, so a
  candidate who sat an exam in March can still be shown the question they answered with
  the marks it carried.
- A question's `kind` cannot change between versions — changing it changes what a stored
  answer *means*, and a new question is the honest representation of that.
- Per-kind body validation (`assessment/schemas.py`): option ids unique and referenced,
  `single_choice` exactly one key, `multiple_choice` at least two, regexes compiled at
  authoring time, relative tolerance around zero rejected, coding questions require test
  cases.
- JSON import/export, `question.v1`. **Every bad row is reported at once**, each with an
  index, an `external_key` and a JSON Pointer; a file with any error imports nothing.
  Re-importing the same `external_key` upserts to a new version.
- Export is ordered deterministically and round-trips: the export of a bank, with a
  `bank_id` added, is a valid import body. Asserted, because an export that cannot be
  re-imported is a backup that cannot be restored.

**Exam authoring**
- Exams, versions, sections, `k of n` pools, candidate assignment by id or e-mail.
- Publishing validates and then freezes: pool size vs `select_count`, no draft question
  versions, equal marks inside a pool, no coding questions when `judge_enabled` is false.
  All problems are returned together with pointers.
- Assignment reports `not_a_candidate` and `unknown` rather than silently doing something
  surprising, and is idempotent.

**Delivery**
- **Deterministic papers.** `seed = SHA-256(exam_version_id ‖ candidate_user_id ‖
  attempt_no)`; selection, question order and option order all derive from labelled
  sub-streams of that seed. `random.Random` is deliberately not used — its seeding is not
  a documented cross-version contract, and a candidate resuming after a rolling deploy
  must not get a different paper. The shuffle is Fisher-Yates over a SHA-256 counter
  stream with rejection sampling, specified entirely in `assessment/paper.py`.
- Selection and *ordering* are separate: a pool decides which questions, the section's
  `shuffle_questions` decides the order. (Found by a test — the first implementation
  shuffled inside `sample()`, silently overriding an author who had turned shuffling off.)
- **Server-authoritative clock.** `deadline_at` is computed once at start; the browser is
  told what remains and what time the server thinks it is, and is never asked. Extra time
  cannot push a deadline past the exam's own `closes_at`.
- **Idempotent start.** `POST /sessions` on a live session returns the same session and
  paper with 200 instead of 201. A crashed client does not have to know whether it is
  starting or resuming, so it cannot get that wrong.
- **Autosave** with a monotonic revision per item and an append-only `answer_revision`
  history written in the same transaction. A save carrying a revision older than the
  server's is refused with 409 *and the server's current value*, so a reconnecting tab
  reconciles rather than overwriting.
- **Submission** enforced on the server clock, with a grace period that absorbs an
  in-flight request rather than extending the exam. A late submit closes the session as
  `expired` and still grades what was autosaved.
- Sessions past their deadline are expired lazily on next touch, so no request ever sees a
  state that is a lie. (A sweeper is still Phase 4.)

**Auto-grading** — `single_choice`, `multiple_choice`, `short_answer`, `numeric`.
- Blank is always exactly 0, never a penalty. Malformed client input is 0, never a penalty.
- Partial credit on multiple-choice uses symmetric per-option credit, so selecting every
  option scores 0 rather than full marks.
- Every score carries a `detail` explaining how it was reached — and short-answer detail
  deliberately omits the accepted answers, because the candidate can see it.
- Coding raises `NotAutoGradable` and is recorded with `grader='judge'` and 0 awarded, so
  the maximum stays honest about what has not been scored yet.

**MFA enrolment** — the Phase 1 gap. Login still fails closed for an unenrolled required
role, but the 403 now carries a 15-minute `mfa_enrol` token that can do nothing except
enrol an authenticator. The secret is returned but not stored until a code derived from it
verifies, so a failed QR scan cannot enable an account nobody can sign into.
**The reviewer role is usable for the first time.**

**Candidate exam runner (Next.js)** — paper rendering, per-kind inputs, visible save
state, countdown anchored to `performance.now()` and the server's remaining seconds,
debounced autosave with a retry loop, submit confirmation that names the cost of blanks,
and a read-only view after submission. No `localStorage`, no `sessionStorage`: recovery is
"ask the server", which is the only version that also works when the candidate changes
machine.

---

## TESTS

```
API suite                144 passed  (51 marked `security`)
  test_paper_determinism  12   pure, no database
  test_grading            28   pure, no database
  test_assessment_flow    11   live Postgres, end-to-end
  test_authoring          21   live Postgres
  test_delivery_security   7   live Postgres, as the NOBYPASSRLS app role
  test_mfa_enrolment       9   live Postgres
  (Phase 1 suites)        56   unchanged, still passing
Browser suite             32 passed  (17 login flow + 15 exam runner, real Chromium)
ruff                     clean
ruff format              clean
mypy                     clean, 27 files (auth, core, tenancy, assessment, grading)
next build               clean
tsc --noEmit             clean
alembic 0002 -> 0001 -> 0002   round-trips
```

Run twice against a **freshly created empty database** and then against the same one warm.
Both green.

**The four Phase 2 exit criteria, and where each is proved**

| Exit criterion | Test |
|---|---|
| A candidate completes an exam end-to-end | `test_candidate_completes_an_exam_end_to_end` |
| A forced reload mid-exam loses nothing | `test_forced_reload_loses_nothing` (API) + `tests/browser/test_exam_runner.py` (real reload in Chromium) |
| The server rejects a late submission | `test_server_rejects_a_late_submission` |
| The deterministic paper reproduces exactly on recovery | `test_paper_reproduces_exactly_on_recovery`, which *re-derives* the paper from the seed and compares it to the stored rows |

**What the new security tests actually prove**

| Assertion | Result |
|---|---|
| A candidate cannot read another candidate's answers **in the same organization** | PASS |
| ...nor their paper, nor their answer history | PASS |
| The `candidate` role cannot INSERT into `question_score` at all | PASS — refused by RLS, not by a handler |
| No session-owned table still carries the blanket org-only policy | PASS — structural, so a new table cannot regress it |
| Starting another candidate's assignment returns 404, not 403 | PASS |
| A candidate cannot reach a question bank, publish, or assign | PASS |
| No answer key (`correct` / `accepted` / `tolerance`) in a served paper | PASS (API and browser) |
| An `mfa_enrol` token opens no other endpoint | PASS |
| A wrong TOTP code stores no secret | PASS |
| The TOTP secret is not stored in the clear | PASS |
| Login still fails closed for an unenrolled required role | PASS |
| The countdown does not move when the browser clock jumps two hours | PASS (browser) |

---

## SECURITY — what was found while testing

Five real defects. Four were found by tests failing, one by running the stack.

1. **Every candidate could read every other candidate's answers.** Migration 0001
   generated `org_id = current_org_id()` for all 35 tenant tables and gave `exam_session`
   a candidate-scoped policy — but `session_paper_item`, `answer`, `answer_revision`,
   `question_score` and `session_result` carry `session_id`, not `candidate_user_id`, so
   they kept the blanket policy. Phase 1 never noticed because nothing wrote to them;
   Phase 2 writes to all of them. **This is the most serious defect found in Phase 2.**
   Fixed by migration 0002, which also re-asserts the "every tenant table is protected"
   invariant after rewriting policies.

2. **Auto-grading ran as the candidate.** Grading happens during the candidate's own
   submit request, so the first implementation wrote `question_score` in the candidate's
   RLS context — meaning the database would have permitted a candidate-authenticated code
   path to write its own marks. Fixed with a `system` role name that no login can produce
   (`Principal.primary_role` only ever returns a membership role) and a separate
   transaction, and `question_score` is now candidate-unwritable at the policy level.

3. **CSRF enforcement broke every Bearer-token client.** `enforce_csrf` triggered whenever
   an access *cookie* was present, even when the request authenticated with an
   `Authorization` header — which, per ADR-0017, is the credential that actually wins. An
   API client that had ever logged in through the browser got 403 on every write. Found
   by the first authoring test. Fixed by skipping CSRF when a Bearer token is presented,
   which is sound: a browser does not attach that header cross-site.

4. **A blank answer could earn a penalty.** `is_blank` inspected the raw payload, so
   `{"selected": []}` — a candidate deselecting their answer — read as a non-empty dict
   and fell through to the "incorrect" branch with negative marking applied. Found by a
   parametrized test written specifically to probe the blank rule.

5. **The login form submitted the password in a URL if the page had not hydrated.** A
   stale build chunk during browser testing produced a native form submit; the default
   method is GET, so the password went into the query string, the history, and every
   access log in between. Fixed with `method="post"`, which fails visibly instead.

Also worth recording: `answer_revision`'s append-only trigger blocks **cascade** deletes,
so a session with answers cannot be removed even by deleting its parent. That is correct
for an evidence-first product and wrong for retention purging — Phase 6 needs an explicit,
audited purge path rather than a DELETE.

---

## LIMITATIONS — what is imperfect

- **`docker compose up` is still not verified end-to-end.** The pgcrypto failure was
  reproduced and fixed against a natively-run PostgreSQL 16.13 with the same roles and
  grants the compose stack creates, and the fix was confirmed by executing the same
  statements as the same roles. No Docker daemon was available to run the full stack.
- **Only `free` navigation is enforced.** `sequential` and `one_way` are in the schema and
  are refused at authoring time rather than accepted and ignored.
- **Coding questions are stored, served and autosaved, but cannot be run or scored.** They
  are recorded with `grader='judge'` and 0 awarded. Phase 3.
- **Grading is inline and synchronous.** A large paper makes submit slower than it should
  be. It is idempotent and upsert-keyed, so moving it to a worker later is mechanical.
- **Pools are `explicit` only.** `source_kind='filter'` exists in the schema and is not
  implemented.
- **Short-answer matching does not fold typographic apostrophes.** NFKC normalisation does
  not map U+2019 to U+0027; a test records this rather than pretending otherwise.
- **No manual grading, no result release workflow, no CSV export.** `results_release_at`
  is enforced on read, but nothing sets it through the API yet.
- **There is no staff UI for authoring.** Everything in the authoring section is API-only;
  the only UI shipped this phase is the candidate runner.
- **Expiry is lazy.** A session whose deadline passed is closed on the next request that
  touches it. A sweeper is Phase 4.
- **Still true from Phase 1:** KMS key custody does not exist, the breach-password list is
  13 entries, TOTP secrets use a locally held AES key, RLS query-plan overhead has not
  been measured, and there is no load testing, accessibility audit, or dependency scan.

---

## FILES CHANGED

```
infrastructure/postgres/01-roles.sh                    (extensions created as superuser)
scripts/seed.py                                        (org_admin account; enrolment note)

apps/api/sentinel_api/migrations/versions/0001_initial_schema.py   (extension preflight)
apps/api/sentinel_api/migrations/versions/0002_delivery_rls.py     (new)
apps/api/sentinel_api/models.py                        (+5 delivery models)
apps/api/sentinel_api/core/db.py                       (server_session, elevated, SERVER_ROLE)
apps/api/sentinel_api/tenancy/{deps,policies}.py       (CSRF fix; 15 new policy entries)
apps/api/sentinel_api/auth/router.py                   (MFA enrolment; enrolment token)
apps/api/sentinel_api/reporting/router.py              (Phase 1 /exams stub removed)
apps/api/sentinel_api/main.py                          (assessment router; description)
apps/api/sentinel_api/assessment/{schemas,paper,bank,authoring,delivery,router}.py   (new)
apps/api/sentinel_api/grading/{graders,service}.py     (new)
apps/api/tests/{test_paper_determinism,test_grading,test_assessment_flow,
                test_authoring,test_delivery_security,test_mfa_enrolment}.py   (new)
apps/api/tests/{conftest,test_auth,test_authz_matrix}.py   (order-independence, new fixtures)

apps/web/lib/api.ts                                    (delivery types and calls)
apps/web/lib/exam/useExamSession.ts                    (new — clock, autosave, recovery)
apps/web/app/exams/[assignmentId]/take/page.tsx        (new — the exam runner)
apps/web/app/exams/page.tsx                            ("Start or continue")
apps/web/app/login/page.tsx                            (method="post")
tests/browser/test_exam_runner.py                      (new)
docs/PHASE_REPORTS.md, README.md
```

---

## DATABASE CHANGES

Migration `0002_delivery_rls`. No new tables, no new columns — Phase 1's migration already
created all 40 tables. What changes is the policy layer:

- `session_belongs_to_current_user(uuid)`, a STABLE SQL function.
- Seven session-owned tables lose the blanket `<table>_tenant` policy and gain a
  `<table>_staff` policy plus a candidate policy scoped to session ownership. `answer`,
  `answer_revision` and `calibration_record` additionally get candidate INSERT/UPDATE
  policies; `question_score`, `session_result`, `session_paper_item` and
  `session_integrity_config` deliberately do not.
- `exam_session`'s policy is rewritten to recognise the `system` role name.
- The migration re-asserts that every tenant table still has forced RLS and at least one
  policy, and fails itself otherwise.

Downgrade implemented and tested; `0002 → 0001 → 0002` round-trips.

---

## API CHANGES

```
POST   /api/v1/auth/mfa/enrol             POST  /api/v1/auth/mfa/enrol/confirm

POST   /api/v1/banks                      GET   /api/v1/banks
POST   /api/v1/banks/{id}/questions       GET   /api/v1/banks/{id}/questions
POST   /api/v1/questions/import           GET   /api/v1/banks/{id}/export

POST   /api/v1/exams                      GET   /api/v1/exams        (was a STUB)
POST   /api/v1/exams/{id}/versions        GET   /api/v1/exams/{id}/versions
POST   /api/v1/exam-versions/{id}/publish
POST   /api/v1/exam-versions/{id}/assignments

POST   /api/v1/sessions                   (start or resume; 201 vs 200)
GET    /api/v1/sessions/{id}              GET   /api/v1/assignments/{id}/session
PUT    /api/v1/sessions/{id}/answers/{paper_item_id}
POST   /api/v1/sessions/{id}/submit       GET   /api/v1/sessions/{id}/result
```

`POST /api/v1/auth/login` now includes `enrolment_token` in its 403 problem document when
a role requires MFA and the account has none. The status, the shape and the wording of the
refusal are unchanged.

---

## WHAT DID I IMPLEMENT THAT DOES NOT ACTUALLY WORK AS CLAIMED?

Six things, in descending order of how likely they are to mislead someone.

1. **"The pgcrypto blocker is fixed" is narrower than it sounds.** The *cause* was found
   and the fix was verified by executing the same statements as the same roles against
   PostgreSQL 16.13. `docker compose up` itself has still never run to completion here.
   The compose file remains the one claim in this project with no direct evidence.

2. **Coding questions look delivered and are not.** They render, the candidate can type,
   and the answer autosaves — but nothing runs it, and the score is 0 with
   `grader='judge'`. A demo would look like a working code exam right up to the results
   screen. `judge_enabled=false` refuses to publish them, which limits the blast radius,
   but the seeded exam sets `judge_enabled=true`.

3. **`section.time_limit_seconds` is stored and ignored.** Authoring accepts a per-section
   limit; the delivery layer enforces only the whole-exam deadline. Unlike `navigation`,
   this one is *not* rejected at authoring time — it should be, and is not.

4. **The `system` role is a convention, not a database role.** It is a value in
   `sentinel.role`, and the protection rests on nothing else ever setting it. That is true
   today and enforced only by the fact that `Principal.primary_role` cannot produce it —
   there is no test asserting that no other code path can.

5. **`GET /sessions/{id}/result` enforces `results_release_at`, which nothing sets.**
   The check is real and tested against a value written directly to the database. No API
   endpoint writes that column, so in practice results are visible as soon as they exist.

6. **The exam runner's offline story is a retry loop, not an offline queue.** A save that
   fails retries every five seconds while the page is open. Close the tab during an outage
   and the last unsaved edit is gone. The real IndexedDB queue is Phase 4 (P0-10 is only
   partly met by this phase, and `LIMITATIONS.md` should say so).

One more about method: the security tests for Phase 2 were again written alongside the
implementation rather than before it. Phase 3 requires the reverse for the judge sandbox
and that ordering is a hard gate there.

---

## NEXT PHASE

**Phase 3 — the code judge.** Security tests first, and that is a gate rather than a
preference: fork bomb, memory bomb, network egress, filesystem traversal, `/proc` access
and stdout flood must all be demonstrably contained *before* the runner is called
complete. Then the sandbox, the Redis queue, compile and execute per language, resource
limits, hidden test cases and partial scoring.

**D4 (judge isolation — container vs gVisor vs Firecracker) blocks this phase and is still
open.** It is the one decision Phase 3 cannot start without.

Worth doing first, in this order:

- **Run `docker compose up` on a machine with Docker.** The extension fix should clear the
  failure that was actually hit; whatever it reveals next is unknown.
- **Reject `section.time_limit_seconds` at authoring time**, as `navigation` already is.
  It is a five-line guard and it removes a silent lie.
- **Add a session-expiry sweeper**, so abandoned sessions close without waiting for a
  request that may never come.

Still open and not blocking Phase 3: **D1** (first market), **D3** (native client).

---

# PHASE 3 — THE CODE JUDGE

**Date:** 2026-08-20
**Verified against:** Docker 29.4.3 (runc, cgroup v1, seccomp enabled),
PostgreSQL 16.13, Redis 7.0.15, Python 3.11.15, Node 22.22.2, Chromium 1194 —
all live. Every sandbox test ran a real container.

---

## The gate, and whether it was met

MVP_SCOPE.md §5 makes Phase 3 conditional: *"All sandbox security tests pass
before the runner is called complete. Fork bomb, memory bomb, network egress,
FS traversal, /proc, stdout flood all contained."* Phase 1's report also set an
ordering requirement — security tests written **before** the implementation, not
alongside it.

Both were met. `tests/security/test_judge_sandbox.py` was written first and every
test in it failed on `ImportError: No module named 'sentinel_judge'` until the
runner existed. **68 security tests now pass against real containers.**

The rule the suite is built on: *every containment test has a positive control.*
Asserting that a submission cannot reach the network proves nothing in an
environment where nothing can. So each control is tested twice — once with the
control on and once with it off — and the "off" run must succeed. Two of these
controls caught the problem immediately:

- The first network test passed **vacuously**: this build host has no outbound
  egress, so "blocked" was true regardless. The suite now stands up a TCP
  listener on the Docker bridge gateway, proves a normally-networked container
  reaches it, and only then asserts that `--network=none` does not.
- The seccomp test could not establish its control at all, because the profile
  blocks `socket()` whether or not the network namespace is removed. There are
  now three tests: control (both off) reaches; namespace only blocks; full
  configuration blocks and `socket()` itself fails.

---

## Decisions taken

| Ref | Question | Decision | Recorded |
|---|---|---|---|
| **D4** | Judge isolation | **Hardened containers on runc, with the runtime as a config value.** `JUDGE_RUNTIME=runsc` switches to gVisor with no code change | ADR-0022 |
| — | Seccomp shape | Default-**allow** with an explicit deny-list, not a default-deny allowlist | ADR-0023 |
| — | Scoring | Weighted partial credit — the fraction of test *weight* passed | ADR-0024 |
| — | Queue | Redis `BLMOVE` with a per-worker processing list; identifiers only, never source | ADR-0025 |

**D4's residual risk, stated plainly:** runc shares the host kernel. A kernel
exploit escapes the sandbox, and no combination of flags in `sandbox.py` changes
that. The mitigations are the ones listed in that module's docstring plus an
isolated host pool in production; the escape hatch is a one-line runtime change.

---

## IMPLEMENTED

**The sandbox** (`workers/judge/sentinel_judge/sandbox.py`) — one container per
execution, with `--network=none`, `--read-only`, a size-capped `tmpfs` work
directory, `--pids-limit`, `--memory`/`--memory-swap` equal (no swap escape),
`--cpus`, `--user 65534`, `--cap-drop=ALL`, `no-new-privileges`, a seccomp
deny-list, `--log-driver=none`, an `fsize` ulimit, and an empty environment.
Output is read through a byte cap; the wall clock is enforced by killing the
container by id; every exit path removes it.

**The judge** (`runner.py`) — compile once, run every test case, score by
weight. Compilation happens *inside* the sandbox with its own limits, because a
compiler is a program that runs candidate-supplied input. Compiled artefacts are
exported through a per-run host directory and staged into the run container.

**Languages** — `python311` and `cpp20` verified end to end (compile, run,
syntax error, timeout, memory, flood). `java17` is written and **not verified**.

**Comparators** — `trim_exact`, `token`, `float_eps`, with 20 tests. An unknown
comparator name raises rather than falling back to a default, so a typo in a
question cannot silently change how it is marked.

**Queue and worker** — Redis `BLMOVE` with crash recovery, idempotent claim by
conditional `UPDATE`, stale-job requeue, graceful drain on SIGTERM.

**API** — `run` (samples only, debugging, no mark) and `submit` (hidden suite,
scores) as deliberately separate verbs, plus polling and run history. The API
**never executes anything**; it enqueues.

**Scoring integration** — a `final` run writes `question_score` with
`grader='judge'` and recomputes `session_result` from the per-question rows.

**Candidate UI** — editor, language selector, Run / Submit with the difference
spelled out, live verdict, per-test results, and an explicit "this is not your
fault" message on `internal_error`.

**Migration 0003** — candidate-scoped RLS on `code_submission`, `judge_run`,
`judge_test_result`.

---

## TESTS

```
Security suite (real containers)   68 passed
  sandbox containment               27
  judge behaviour                   21
  comparators                       20
API suite                         156 passed  (9 new judge integration tests)
Browser suite                      32 passed
ruff / ruff format / mypy         clean (43 API files + the judge package)
next build / tsc --noEmit         clean
alembic 0003 -> 0002 -> 0003      round-trips
```

**What the sandbox tests prove**

| Assertion | Result |
|---|---|
| Fork bomb bounded by `--pids-limit` (31 of 5000 attempted) | PASS |
| ...and raising the limit lets it fork further (positive control) | PASS |
| Memory bomb is OOM-killed, not merely slowed | PASS |
| A program inside the limit is untouched (positive control) | PASS |
| A listener on the bridge **is** reachable with isolation off (control) | PASS |
| `--network=none` alone blocks it, with seccomp removed | PASS |
| Full configuration blocks it, and `socket()` fails | PASS |
| Root filesystem is read-only; the work directory is not (control) | PASS |
| Path traversal finds nothing writable | PASS |
| The host Docker socket is not visible | PASS |
| A 512 MB write stops at the 16 MiB tmpfs | PASS |
| Only 2 processes visible; highest PID < 100 (host is invisible) | PASS |
| `/proc/kcore`, `/proc/sysrq-trigger`, `/sys/kernel/security` unreadable | PASS |
| Submission runs as uid 65534; `setuid(0)` refused | PASS |
| Every process in the namespace is unprivileged | PASS |
| stdout flood truncated at the cap and killed | PASS |
| Infinite loop and `sleep(600)` both hit the wall clock | PASS |
| The container is **gone** after a timeout | PASS |
| Nothing survives from one submission to the next | PASS |
| No `DATABASE_URL` / `REDIS_URL` in the sandbox environment | PASS |
| No production code path passes `_unsafe_*` (grep) | PASS |
| The runner still passes all ten security flags (structural) | PASS |

---

## SECURITY — what was found while testing

Seven real defects. Two of them broke the build machine, which is the most
convincing kind of evidence.

1. **A flood container filled the host's disk — 15 GB in 80 seconds.** Docker's
   default `json-file` log driver is unbounded, and container stdout is written
   to it whether anyone reads it or not. Any submission could have exhausted a
   judge host's disk with a three-line program. Fixed with `--log-driver=none`
   plus a byte-capped read in the worker.

2. **`timeout 60 docker run ...` does not stop the container.** It kills the
   CLI; the container keeps running and keeps writing. This is how the disk
   filled *twice*. The runner now creates the container by id, kills it by id,
   and removes it by id on every exit path — with a test that asserts the
   container is gone after a timeout.

3. **A default-deny seccomp allowlist prevented containers from starting at
   all.** Passing a profile *replaces* Docker's builtin, and runc needs syscalls
   during init that a hand-written allowlist will not think of (`fstatfs` on
   `/proc/thread-self/fd` surfaced first). Reimplementing Docker's ~350-syscall
   allowlist would mean maintaining it against every runc release, and a stale
   allowlist fails **open**. Now a deny-list of 65 syscalls, explicitly a second
   layer, with the primary controls named as such (ADR-0023).

4. **The staging failure was swallowed.** The wrapper ended with
   `cp ... 2>/dev/null || true`, so when the image turned out to lack `cp` the
   submission ran against an empty directory and the candidate saw
   `FileNotFoundError: main.py` — a judge misconfiguration presented as their
   bug. Staging failure is now `internal_error`, which scores nothing and is
   appealable.

5. **`|| true` on the compile command hid every compile error.** A Python file
   with a syntax error "compiled" successfully and then failed all four tests as
   runtime errors, with an empty compile log. The candidate would have had four
   failures and nothing to act on.

6. **Staged files were written 0644, stripping the execute bit.** Every C++
   submission failed as a runtime error on every test, with no compiler output —
   an unfalsifiable-looking failure.

7. **The API wrote a table it forbids candidates to write.** `judge_run` is
   candidate-unwritable by design (a role that can write its own verdict has no
   verdict), but the endpoint created the row in the candidate's transaction.
   RLS refused it. Now created under a narrow, documented `elevated()` block —
   a `queued` row carrying no result is not a verdict.

Two test-quality defects found in the suite itself, both worth recording because
a security test that lies is worse than one that is missing:

- `open(path, "wb").write(b"x")` reported a successful write to
  `/proc/self/mem`. It had not succeeded — Python buffers, and the failing
  syscall happened at garbage-collection time, outside the `except`. Rewritten
  with `os.open`/`os.write`.
- A test asserted `/proc/1/mem` could not be opened. It can, and that is not a
  finding: PID 1 in the namespace is the submission's own shell at the same uid.
  Replaced with the claim that actually has content — no process in the
  namespace runs as anyone more privileged.

---

## LIMITATIONS — what is imperfect

- **`java17` is written and never executed.** The adapter and JVM flags are
  chosen deliberately (`MaxRAMPercentage` so the JVM respects the container cap
  rather than the host's), but no Java submission has run. Treat it as
  NOT VERIFIED.
- **The sandbox images used for verification were assembled locally**, because
  Docker Hub is unreachable from this build environment. The shipped
  Dockerfiles use `python:3.11-slim-bookworm` / `gcc:13-bookworm` /
  `eclipse-temurin:17-jdk-jammy`. The controls under test are runtime flags, not
  properties of the base image, so the evidence transfers — but the *shipped
  images themselves* have never been built.
- **`docker compose --profile judge up` has never been run**, for the same
  reason. The worker was driven directly in-process by the integration tests.
- **The seccomp profile is default-allow.** Weaker in principle than
  default-deny; the reasoning is in ADR-0023 and in the profile's own comments.
- **runc shares the host kernel.** D4's accepted residual risk.
- **cgroup v1 on this host.** `--memory` and `--pids-limit` were verified on v1;
  v2 behaviour is expected to be equivalent and was not tested.
- **No peak-memory measurement.** `judge_run.peak_memory_kb` exists and is
  always NULL — cgroup peak accounting is not read.
- **`compile_ms` is never populated** either; only total execute time is.
- **No judge load testing.** P0-14's evaluation study (Phase 10) has not run, so
  there is no measured throughput or queue-latency figure.
- **Custom comparators are unimplemented** and raise, by design.
- **No re-judge endpoint.** Fixing a broken test case and re-marking a cohort is
  a database operation today.

---

## FILES CHANGED

```
workers/judge/sentinel_judge/{sandbox,runner,languages,comparators,queue,worker}.py  (new)
infrastructure/security/seccomp-judge.json                                          (new)
infrastructure/docker/judge-{python311,cpp20,java17}.Dockerfile                      (new)
infrastructure/docker/judge.Dockerfile                                    (STUB -> real)
docker-compose.yml                                          (sandbox images, judge profile)

apps/api/sentinel_api/models.py                     (+CodeSubmission, JudgeRun, JudgeTestResult)
apps/api/sentinel_api/migrations/versions/0003_judge_rls.py                          (new)
apps/api/sentinel_api/assessment/judge_router.py                                     (new)
apps/api/sentinel_api/core/runtime.py                                    (optional Redis)
apps/api/sentinel_api/{main,tenancy/policies}.py                          (3 new routes)

tests/security/{conftest,test_judge_sandbox,test_judge_runner,test_comparators}.py   (new)
apps/api/tests/{conftest,test_delivery_security,test_judge_integration}.py
apps/web/lib/api.ts, apps/web/app/exams/[assignmentId]/take/{page.tsx,CodeQuestion.tsx}
docs/PHASE_REPORTS.md, DECISIONS.md, LIMITATIONS.md, README.md
```

---

## DATABASE CHANGES

Migration `0003_judge_rls`. No new tables — migration 0001 created all 40. What
changes is the policy layer on `code_submission`, `judge_run` and
`judge_test_result`, each of which came out of 0001 with the blanket
`org_id = current_org_id()` policy. **That meant any authenticated member of an
organization could read any other candidate's submitted source code** — which is
not a privacy footnote, it is the cheating this product exists to detect, handed
over by the database.

Two SQL helpers (`submission_belongs_to_current_user`,
`run_belongs_to_current_user`) walk the extra hop to a session. Candidates may
INSERT a `code_submission` for their own live session and read their own results;
they may never write `judge_run` or `judge_test_result`. Downgrade tested.

---

## API CHANGES

```
POST   /api/v1/sessions/{id}/items/{paper_item_id}/run       (202; mode=sample|final)
GET    /api/v1/sessions/{id}/items/{paper_item_id}/runs
GET    /api/v1/sessions/{id}/runs/{run_id}
```

---

## WHAT DID I IMPLEMENT THAT DOES NOT ACTUALLY WORK AS CLAIMED?

Six, in descending order of how likely they are to mislead.

1. **The shipped judge images have never been built.** Every security claim in
   this report was verified against images assembled from the local filesystem,
   because Docker Hub is blocked here. The *controls* are runtime flags and the
   evidence transfers; the Dockerfiles themselves are unproven, and
   `judge-java17.Dockerfile` has never produced a running container.

2. **`java17` appears in the language list, the UI dropdown, and the compose
   file, and has never executed a line of Java.** A demo that picks Java from the
   dropdown will fail in a way I cannot predict.

3. **"Compilation is sandboxed" is true, but the compile container and the run
   container do not share a filesystem.** Artefacts move through a host
   directory that is bind-mounted read-write into the compile container. That
   directory is fresh per run and discarded after, and the `fsize` ulimit still
   applies — but it is the one writable host path any sandbox sees, and it is
   not a tmpfs, so the disk-fill argument for `/box` does not cover it.

4. **`peak_memory_kb` and `compile_ms` are columns that are always NULL.** The
   API returns them and the UI has room for them. Nothing measures either.

5. **The rate limit counts submissions, not runs.** `RUNS_PER_MINUTE` queries
   `code_submission` within the last minute for the whole *session*, so a
   candidate with two coding questions shares one budget across both. Defensible,
   but not what the constant's name says.

6. **The queue's stale-job recovery has never fired in anger.** `requeue_stale`
   is written and reachable, and no test kills a worker mid-run to prove a job
   comes back. It is the one part of the crash-recovery story that is argued
   rather than demonstrated.

On method: the security tests were written before the implementation this time,
and the ordering did its job — four of the seven defects above were found by a
test failing rather than by reading code.

---

## NEXT PHASE

**Phase 4 — browser integrity signals.** Fullscreen, tab and window focus, large
pastes; the WebSocket session channel; server-assigned sequence numbers and
timestamps; replay protection; late-event handling; hysteresis state machines;
and the offline queue that Phase 2's `LIMITATIONS.md` entry is still waiting for.

Exit criteria: a replayed event is rejected; a forged sequence is rejected;
hysteresis suppresses single-frame noise; the offline queue survives a 60-second
outage.

Worth doing first:

- **Build the three judge images for real** on a machine that can reach Docker
  Hub, and run `docker compose --profile judge up`. That is the largest unproven
  claim in this report.
- **Execute one Java submission** and either promote `java17` to IMPLEMENTED or
  remove it from the language list until it is.
- **Kill a worker mid-run** and prove `requeue_stale` returns the job.

Still open: **D1** (first market), **D3** (native client).

