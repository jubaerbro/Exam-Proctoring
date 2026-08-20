# Sentinel — API Architecture

**Status: PHASE 0 design. No endpoint is implemented. This is the contract to build to.**

---

## 1. Shape and conventions

- **REST over HTTPS**, JSON bodies, OpenAPI 3.1 generated from Pydantic v2 models.
- **Base path `/api/v1`.** The version is in the path, not a header — path versioning is
  legible in logs and in support conversations, which matters more here than purity.
- **WebSocket** at `/ws/v1/session/{session_id}` for live session traffic only.
- Every mutating endpoint accepts `Idempotency-Key`; replays return the original response.
- Cursor pagination (`?cursor=&limit=`). No offset pagination — rosters are large and
  offset pagination is both slow and inconsistent under concurrent writes.
- Errors are RFC 9457 `application/problem+json`:

```json
{
  "type": "https://sentinel.dev/errors/exam-window-closed",
  "title": "Exam window closed",
  "status": 409,
  "detail": "The exam window closed at 2026-08-18T10:00:00Z.",
  "instance": "/api/v1/sessions/0f3c.../submit",
  "request_id": "01J9..."
}
```

Error bodies never leak whether an object exists in another tenant. Cross-tenant access
returns **404, not 403** — 403 confirms existence, and existence is itself information.

---

## 2. Authentication

**Decision (approved): local auth now, OIDC-ready seams.** See ADR-0002.

| Element | Choice | Reason |
|---|---|---|
| Password hashing | Argon2id (m=64MiB, t=3, p=4) | Memory-hard; current OWASP guidance |
| Access token | JWT, 15 min, `EdDSA` | Short-lived, stateless, cheap to verify |
| Refresh token | Opaque 256-bit, 14 days, rotating | Revocable; rotation gives reuse detection |
| Transport | `httpOnly; Secure; SameSite=Lax` cookies | Immune to XSS token theft |
| CSRF | Double-submit token + `Origin` check | Required because auth is cookie-borne |
| MFA | TOTP, **mandatory for `org_admin` and `reviewer`** | These roles can read candidate webcam evidence |

Refresh rotation uses a `family_id`. Presenting an already-rotated token revokes the whole
family and writes an `activity_log` entry — that is the signal that a token was stolen,
and it is the main reason to prefer opaque rotating refresh tokens over long-lived JWTs.

The access token carries `sub`, `org_id`, `roles[]`, `exp`, `jti`. **It does not carry
permissions.** Permissions are resolved server-side per request, so revoking a role takes
effect within one access-token lifetime rather than requiring a token blacklist.

### Org switching

A user in several organizations calls `POST /api/v1/auth/switch-org` and receives a new
access token scoped to that org. There is no "all orgs" token. Every request is bound to
exactly one tenant, which is what makes the `SET LOCAL sentinel.org_id` contract in
[`DATA_MODEL.md`](DATA_MODEL.md#1-multi-tenancy-model) safe.

### The OIDC seam

`auth/` exposes an `IdentityProvider` interface with `authenticate()` and
`resolve_user()`. The local implementation checks a password hash; a future OIDC
implementation validates an ID token and maps `iss`/`sub` to `app_user.external_issuer`
/`external_subject`, which already exist in the schema. No table changes required.

---

## 3. Authorization

Three checks per sensitive request, all server-side:

```
1. Authenticated?              → 401
2. Role permits the action?    → 403 (policy table, not scattered if-statements)
3. Object in the caller's org? → 404 (RLS enforces this even if step 3 is forgotten)
```

Policy is a declarative table in `tenancy/policies.py`, exercised by a matrix test that
enumerates every (role × endpoint) pair. A new endpoint absent from the matrix fails CI —
otherwise "we forgot to add the authz check" is a matter of luck.

### Role capability matrix (abridged)

| Action | Candidate | Instructor | Reviewer | Org Admin |
|---|---|---|---|---|
| Take own exam | ✓ | — | — | — |
| Read own session state | ✓ | own exams | flagged only | ✓ |
| Read other candidates' sessions | ✗ | own exams | assigned queue | ✓ |
| Create/edit exams, questions | ✗ | ✓ | ✗ | ✓ |
| Assign candidates | ✗ | ✓ | ✗ | ✓ |
| Read evidence | own, after disclosure point | own exams, after window closes | assigned queue | ✓ |
| Write/modify evidence | ✗ | ✗ | ✗ | ✗ |
| Delete evidence | ✗ | ✗ | ✗ | via retention policy only |
| Modify audit events | ✗ | ✗ | ✗ | ✗ (no endpoint exists) |
| Make review decisions | ✗ | ✗ | ✓ | ✓ |
| Submit a statement | own session | ✗ | ✗ | ✗ |
| Manage members, retention | ✗ | ✗ | ✗ | ✓ |

There is **no endpoint anywhere in the API that updates or deletes an `audit_event`**.
Not gated, not admin-only — absent. Combined with the database trigger, that is two
independent reasons the chain cannot be rewritten through the product.

### Evidence visibility gating

`GET /evidence/{id}/url` returns a signed URL only when **all** of:

1. caller's role permits evidence access,
2. the object is in the caller's org,
3. **and** the temporal gate is open:
   - Reviewer/Instructor: exam window closed (`exam_version.closes_at < now()`)
   - Candidate: only their own session, and only after `results_release_at`
   - Org Admin: any time, but every access is logged

Every issuance writes `evidence_access_log` **and** appends an `EVIDENCE_ACCESSED` event
to the session chain. A reviewer cannot look at a candidate's webcam frames without
leaving a signed, immutable trace. This is deliberately uncomfortable for the operator; it
is the price of asking candidates to trust the system.

---

## 4. Endpoint surface

### Auth
```
POST   /api/v1/auth/login                     → tokens (+ MFA challenge if required)
POST   /api/v1/auth/mfa/verify
POST   /api/v1/auth/refresh                   → rotate; reuse revokes the family
POST   /api/v1/auth/logout
POST   /api/v1/auth/switch-org
GET    /api/v1/auth/me                        → identity, orgs, roles
POST   /api/v1/auth/password/forgot | /reset
```

### Organization (org_admin)
```
GET    /api/v1/org
PATCH  /api/v1/org
GET    /api/v1/org/members
POST   /api/v1/org/members/invite
PATCH  /api/v1/org/members/{id}               → role, status
DELETE /api/v1/org/members/{id}               → soft disable, never hard delete
GET    /api/v1/org/retention-policies
POST   /api/v1/org/retention-policies
GET    /api/v1/org/accommodation-profiles
POST   /api/v1/org/accommodation-profiles
GET    /api/v1/org/activity                   → administrative audit trail
GET    /api/v1/org/usage                      → metering facts, no pricing
```

### Question bank (instructor)
```
GET    /api/v1/banks
POST   /api/v1/banks
GET    /api/v1/banks/{id}/questions           → filter by kind, tags, difficulty
POST   /api/v1/banks/{id}/questions
GET    /api/v1/questions/{id}
POST   /api/v1/questions/{id}/versions        → new version; published ones are frozen
POST   /api/v1/questions/{id}/versions/{v}/publish
GET    /api/v1/questions/{id}/versions/{v}/test-cases
PUT    /api/v1/questions/{id}/versions/{v}/test-cases
POST   /api/v1/banks/{id}/import              → JSON import, validate-only supported
GET    /api/v1/banks/{id}/export
```

`POST /import` accepts `?dry_run=true` and returns per-item validation errors with a JSON
Pointer to the offending field:

```json
{
  "schema_version": "question-bank.v1",
  "valid": 47, "invalid": 3,
  "errors": [
    {"index": 12, "pointer": "/body/correct/0",
     "message": "references option id 'e' which is not defined in /body/options"},
    {"index": 31, "pointer": "/marks",
     "message": "must be > 0, got -2"}
  ]
}
```

An import that reports "3 errors" without saying which items and which fields is useless
to an instructor with a 500-question bank. This is a product feature, not an error path.

### Exams (instructor)
```
GET    /api/v1/exams
POST   /api/v1/exams
GET    /api/v1/exams/{id}
POST   /api/v1/exams/{id}/versions
PUT    /api/v1/exams/{id}/versions/{v}/sections
PUT    /api/v1/exams/{id}/versions/{v}/sections/{sid}/pools
PUT    /api/v1/exams/{id}/versions/{v}/integrity-config
POST   /api/v1/exams/{id}/versions/{v}/publish     → validates pools are satisfiable
POST   /api/v1/exams/{id}/versions/{v}/assignments → bulk assign, CSV accepted
GET    /api/v1/exams/{id}/versions/{v}/preview     → renders a paper for a seed
```

Publish validation is a hard gate: every pool must have `n >= k`, every coding question
must have ≥1 hidden test case, and `duration_seconds` must be > 0. Discovering
`k=10, n=7` when a candidate clicks Start is an outage during someone's final exam.

### Candidate — exam delivery
```
GET    /api/v1/me/exams                       → assigned, with window state
POST   /api/v1/sessions                       → start or resume; idempotent per assignment
GET    /api/v1/sessions/{id}                  → paper, answers, remaining time, config
GET    /api/v1/sessions/{id}/sync             → authoritative clock, cheap, uncached
PUT    /api/v1/sessions/{id}/answers/{item}   → autosave
POST   /api/v1/sessions/{id}/answers:batch    → offline queue flush
POST   /api/v1/sessions/{id}/calibration
POST   /api/v1/sessions/{id}/submit
GET    /api/v1/sessions/{id}/result           → 404 until results_release_at
```

`POST /sessions` is idempotent: calling it twice returns the same session. A double-click
on "Start Exam" must not consume a second attempt.

`GET /sync` returns:
```json
{"server_time":"2026-08-18T09:14:22.317Z",
 "deadline_at":"2026-08-18T10:00:00.000Z",
 "remaining_ms":2737683,
 "status":"in_progress"}
```
The client computes RTT-adjusted offset. The server independently rejects
`POST /submit` after `deadline_at + grace_seconds`, so a manipulated client clock changes
only what the candidate sees, never what counts.

### Code judge
```
POST   /api/v1/sessions/{id}/code/run         → sample tests only, rate limited
POST   /api/v1/sessions/{id}/code/submit      → full suite, counts toward grade
GET    /api/v1/runs/{run_id}                  → status + per-test results
```

`code/run` is rate limited per session (default 1 per 10 s, burst 3). Sample runs are
candidate-triggered compute and are the cheapest thing in the product to abuse — both
accidentally, by a candidate hammering the button, and deliberately.

Hidden test outputs are **never** returned by either endpoint. `judge_test_result` stores
`stdout_excerpt` only for sample cases; returning hidden outputs would let a candidate
reconstruct the expected answers by submitting probes.

### Integrity events
```
POST   /api/v1/sessions/{id}/events           → batch REST fallback
WSS    /ws/v1/session/{id}                    → primary path
GET    /api/v1/sessions/{id}/events           → staff/reviewer read
```

### Evidence
```
POST   /api/v1/sessions/{id}/evidence/intent  → {upload_url, evidence_id, expires_in}
POST   /api/v1/evidence/{id}/confirm          → server recomputes SHA-256, binds to chain
GET    /api/v1/evidence/{id}/url              → short-lived signed GET (logged)
GET    /api/v1/sessions/{id}/evidence         → metadata list, no bytes
```

Intent returns `{"accepted": false, "reason": "session_cap_reached"}` once the cap is hit,
and the server has already appended `CAPTURE_LIMIT_REACHED` to the chain. The client stops
uploading but keeps sending events.

`confirm` recomputes the digest server-side from the stored object. The client-supplied
hash is treated as a claim to compare against, never as the authoritative value — trusting
it would make evidence binding meaningless against exactly the attacker it defends
against.

### Audit and verification
```
GET    /api/v1/sessions/{id}/audit/head       → {seq, hash, key_fingerprint}
GET    /api/v1/sessions/{id}/audit/export     → staff export bundle (zip)
GET    /api/v1/me/sessions/{id}/audit/export  → candidate bundle (zip)
POST   /api/v1/sessions/{id}/audit/verify     → server-side re-verification
GET    /api/v1/public-keys                    → unauthenticated, by design
```

`/public-keys` is deliberately public and unauthenticated. Verification that requires
Sentinel's permission is not verification.

### Risk and review
```
GET    /api/v1/reviews                        → queue; filter unreviewed|flagged|high-risk|
                                                clean|technical-issues
GET    /api/v1/reviews/{id}
POST   /api/v1/reviews/{id}/claim
POST   /api/v1/reviews/{id}/decision          → outcome + mandatory justification
GET    /api/v1/sessions/{id}/risk             → probability, CI, contributions
GET    /api/v1/sessions/{id}/timeline         → merged events, evidence refs, answers meta
POST   /api/v1/me/sessions/{id}/statement     → candidate's response to a flag
GET    /api/v1/me/sessions/{id}/evidence      → candidate mirror
```

`POST /decision` rejects a justification under 20 characters with 422, matching the
database constraint. Two layers because a mandatory-justification field that can be
satisfied with `"."` is theatre.

### Reporting
```
GET    /api/v1/exams/{id}/versions/{v}/roster        → paginated
GET    /api/v1/exams/{id}/versions/{v}/export?format=csv|xlsx
POST   /api/v1/reports/jobs                          → async for large exports
GET    /api/v1/reports/jobs/{id}
GET    /api/v1/metrics/product|integrity|reviewer|infrastructure
```

### Health
```
GET    /health    → liveness only, no dependency checks, always fast
GET    /ready     → Postgres + Redis + object store reachable
GET    /metrics   → Prometheus, network-restricted
```

`/health` must not touch the database. A slow query answering a liveness probe causes an
orchestrator to kill healthy pods and turn a degradation into an outage.

---

## 5. WebSocket protocol

Connect: `wss://.../ws/v1/session/{id}` with the access token via a short-lived
subprotocol ticket (`GET /api/v1/sessions/{id}/ws-ticket`), not a query parameter — query
strings land in access logs.

### Client → server

```json
{"t":"event.batch","batch_id":"01J9...","events":[
  {"client_seq":141,"client_ts":"2026-08-18T09:14:22.317Z",
   "type":"TAB_HIDDEN","detector":"browser","confidence_milli":1000,
   "payload":{"duration_ms":3400},
   "position":{"section":1,"item":7,"elapsed_ms":812340},
   "mac":"base64url(HMAC-SHA256(ingest_secret, canonical(event)))"}]}
{"t":"heartbeat","client_ts":"..."}
{"t":"answer.saved","item":7,"revision":3}
```

### Server → client

```json
{"t":"event.ack","batch_id":"01J9...",
 "accepted":[{"client_seq":141,"server_seq":88,"is_late":false}],
 "rejected":[{"client_seq":140,"reason":"duplicate"}]}
{"t":"clock","server_time":"...","remaining_ms":2737683}
{"t":"directive","action":"stop_evidence_upload","reason":"session_cap_reached"}
{"t":"judge.result","run_id":"...","status":"completed","passed":3,"total":6}
{"t":"session.terminated","reason":"deadline_reached"}
```

Rejection reasons are explicit: `duplicate`, `seq_regression`, `schema_invalid`,
`mac_invalid`, `rate_limited`, `session_not_active`. The client logs rejections locally so
a support case can distinguish "the detector never fired" from "the server refused it".

**The WebSocket is never the sole path for anything durable.** If it drops, events queue
in IndexedDB and flush via `POST /events`, answers save via REST, and the exam continues.
A candidate must not fail an exam because a proxy killed a WebSocket.

---

## 6. Event ingest

### The per-session ingest secret

Issued at session start, delivered once over HTTPS, 32 random bytes, held in memory (not
`localStorage`), rotated hourly. Events carry an HMAC over the canonical event body.

**What this actually buys, stated precisely:** it stops event injection by anything that
does not hold the secret — a stale replayed capture, another tab, a third party who
obtained a session id. It does **not** stop an attacker who controls the browser, because
that attacker can read the secret out of memory. There is no browser mechanism that
would. See [`../THREAT_MODEL.md`](../THREAT_MODEL.md#t2--compromised-browser).

Claiming otherwise would be the single easiest way for this product to be embarrassed by
a competent reviewer, so the documentation says it plainly and the sales material must
too.

### Server-side validation order

```
1. Session exists, is in_progress, belongs to caller        → 404 / 409
2. HMAC valid against the current or previous secret        → 401
3. Envelope matches the JSON Schema for its event type      → 422
4. client_seq strictly greater than the last accepted       → reject seq_regression
5. (session_id, client_seq) not already present             → reject duplicate
6. Rate limit: 60 events/min/session, burst 20              → 429
7. Allocate server_seq under SELECT ... FOR UPDATE
8. server_ts = clock_timestamp()
9. is_late = (server_ts - client_ts) > 30s
10. Canonicalize → SHA-256 → prev_hash link → Ed25519 sign → INSERT
11. If event_type.warrants_evidence and cap not reached → issue evidence intent
```

Steps 7–10 run in one transaction. A failure anywhere rolls back, and the chain is never
left with a gap. Gaplessness is not cosmetic — the verifier reports a missing sequence
number as a failure, so a rolled-back insert that consumed a number would produce false
tamper alarms.

### Rate limiting

Redis token bucket keyed by `session_id`. Exceeding it does not terminate the session; it
returns 429 and appends a single `DETECTOR_ERROR` event noting suppression. Cutting off a
candidate's exam because their browser was chatty would be a self-inflicted wound.

---

## 7. Idempotency

`Idempotency-Key` on `POST /sessions`, `/submit`, `/code/submit`, `/reviews/{id}/decision`
and `/evidence/{id}/confirm`. Key + request-body hash + response stored in Redis for 24 h.
Same key with a different body → 409, which catches client bugs rather than silently
serving a stale response.

---

## 8. Rate limits (defaults)

| Endpoint group | Limit | Key |
|---|---|---|
| `auth/login` | 5 / 15 min, then backoff | IP + email |
| `auth/refresh` | 30 / hour | user |
| `code/run` | 1 / 10 s, burst 3 | session |
| `code/submit` | 10 / exam | session |
| events (WS + REST) | 60 / min, burst 20 | session |
| evidence intent | 20 / min | session |
| `evidence/{id}/url` | 100 / hour | user |
| reports/export | 5 concurrent jobs | org |

Login limiting is keyed on IP **and** email so that neither a single attacker IP nor a
distributed attempt against one account slips through, while a shared university NAT does
not lock out a whole cohort.

---

## 9. Standalone verifier CLI

`tools/verifier/verify_chain.py` — no import from `apps/api`, no network, no database.

```
$ python verify_chain.py --bundle session-0f3c-export.zip --keys public_keys.json

Sentinel chain verifier 1.0
Session : 0f3c1a2b-...
Key     : Jx9k... (ed25519)

  [1/5] sequence continuity ........ OK   142 events, seq 1..142, no gaps
  [2/5] canonical re-serialization . OK   142/142 reproduced
  [3/5] hash links ................. OK   141 links
  [4/5] signatures ................. OK   142 Ed25519 verifications
  [5/5] evidence digests ........... OK   31 objects, 0 purged

PASS
142 events verified
31 evidence objects verified
chain head valid: a3f1...  seq 142
```

Failures are specific and actionable:

```
FAIL: Event 47 signature invalid
      expected key Jx9k..., signature does not verify over canonical digest
      canonical sha256: 9c2f...  stored sha256: 9c2f...
      → the event body matches its stored digest, but the signature does not.
        Consistent with a forged signature or the wrong public key.

FAIL: Event 83 previous hash does not match event 82
      event 82 hash: 4b1e...   event 83 prev_hash: 0000...
      → chain broken between 82 and 83. Events after 83 cannot be trusted
        to be in their original order.

FAIL: Evidence object hash mismatch for event 91
      expected 7a2c... (sealed in signed event 91)
      actual   e40b... (bytes in evidence/ea31....jpg)
      → the image in this bundle is not the image that was captured.

WARN: Event 104 references evidence purged on 2026-09-18 under policy 'default-30d'
      → not a failure; deletion is recorded in signed event 118.
```

Exit codes: `0` pass, `1` verification failure, `2` bundle malformed, `3` key not found.
Machine-readable output via `--json` for CI and institutional audit pipelines.

Three properties are non-negotiable:

1. **No shared code with the API.** A canonicalization bug that exists in both would make
   the verifier confirm the API's own mistakes. Independent implementation from the
   written spec in [`DATA_MODEL.md §5`](DATA_MODEL.md#5-canonical-json-contract) is the
   whole point.
2. **Offline.** An auditor must be able to verify on an air-gapped machine.
3. **Runs on stock Python 3.11 + `cryptography`.** Nothing exotic; a university IT
   department must be able to run it without approving a dependency tree.

### What the verifier proves, and what it does not

**Proves:** the events in this bundle are exactly what Sentinel's server signed, in the
order it signed them, and the evidence files are byte-identical to what was captured.

**Does not prove:** that what the server was told was true. A modified browser can feed
Sentinel a virtual camera and a fabricated event stream, and the chain will faithfully,
verifiably record the fabrication.

This distinction belongs in the product's own documentation, not only in a security
appendix. It is the difference between an honest integrity claim and an overclaim, and
it is the reason the review layer exists.
