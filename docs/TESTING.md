# Sentinel — Testing Strategy

**Status: PHASE 0. No test exists. The only thing executed so far is the schema DDL and
nine schema-level behavioural assertions — see
[`DATA_MODEL.md §9`](DATA_MODEL.md#9-verification--what-was-actually-executed).**

---

## Definition of done

A feature is complete only when **all** of the following hold:

```
[ ] implementation exists
[ ] tests exist
[ ] tests pass
[ ] failure cases are handled, not just the happy path
[ ] documentation exists
[ ] security implications considered and recorded
[ ] limitations documented in LIMITATIONS.md
[ ] UI works
[ ] API works
[ ] persistence works
[ ] deployment works where applicable
```

Status vocabulary, used strictly and never loosely:

```
IMPLEMENTED           works, tested, documented, failure cases handled
PARTIALLY IMPLEMENTED main path works; gaps named explicitly
STUB                  interface exists, behaviour does not
NOT IMPLEMENTED       absent
KNOWN LIMITATION      works as designed; the design has a documented limit
```

"Implemented" applied to something partially working is the single most damaging habit in
agent-assisted development. It converts a known gap into an unknown one, and unknown gaps
in a proctoring product get discovered by candidates.

---

## Test categories

### Unit

Grading (each question type, boundary and tolerance cases, negative marking), deterministic
question selection (same seed → same paper; different candidate → different paper),
timer arithmetic and grace handling, permission resolution, canonical JSON, hash chaining,
Ed25519 sign/verify, event validation, hysteresis state machines, risk calculation,
perceptual hashing.

### Integration

Full exam lifecycle (create → publish → assign → take → submit → grade → export),
answer autosave and recovery, reconnect with offline queue flush, evidence upload
intent → confirm → chain binding, judge submission → result, review flow → decision →
chain event, retention purge → purge event.

### Security

This is where the product's claims are either proved or exposed.

| Suite | Attacks |
|---|---|
| **Tenant isolation** | Every endpoint attempted cross-org; direct object references by ID; RLS enforcement with app role; `SET LOCAL` leakage across a pooled connection |
| **Chain tamper** | modify a payload · delete an event · reorder events · re-sign with a wrong key · truncate the chain · replace an image · delete an image · inject an image · repoint an event at another image |
| **Replay/forgery** | duplicate `client_seq` · sequence regression · cross-session replay · invalid HMAC · malformed envelope · events after submission |
| **Judge sandbox** | fork bomb · infinite loop · memory bomb · network egress · filesystem traversal · write outside `/tmp` · stdout flood · `/proc` abuse · mounted-path access · escape attempts |
| **Evidence authz** | access before window close · cross-tenant access · another candidate's evidence · expired URL · reused single-use URL · anonymous bucket access |
| **Auth** | brute force · refresh reuse · token replay after logout · CSRF · privilege escalation via role claim |
| **Structural** | no code path from `risk_assessment` to any score field · no endpoint mutates `audit_event` · no secret in the OpenAPI schema or log formatter |

The chain tamper suite is the one that matters most commercially. **The verifier must
detect all nine attacks and report the exact failing index and reason.** If it misses one,
Sentinel has no differentiator — only a marketing claim.

### Browser (Playwright)

Fullscreen enter/exit detection · visibility change · window blur · forced reload
mid-exam preserves answers · offline for 60 s then reconnect flushes the queue ·
autosave under rapid input · large paste triggers an event · evidence captured on a
qualifying event · timer stays synced across a reload · candidate disclosure screen blocks
until acknowledged.

### Evaluation (Phase 10)

Four studies, in `tests/evaluation/`. Reproducible from scripts. **Results are never
fabricated, and unfavourable results are published as-is.**

| Study | Measures |
|---|---|
| 1 — Red team | Detection rate, false-negative rate, detection latency, evidence availability, per scripted attack |
| 2 — False positives | FPR overall and **by condition** (lighting, eyewear, head covering, camera quality, normal movement, network instability) |
| 3 — Judge load | Submissions/sec, p50/p95/p99 latency, CPU, memory, queue delay, worker utilization under increasing concurrency |
| 4 — Reviewer efficiency | Time to adjudicate, evidence views per session, inter-reviewer agreement, completion rate |

Study 2 uses consented session metadata only. **Sensitive characteristics are never
inferred from appearance.** Conditions are self-reported by consenting participants or
controlled by the experimenter — inferring "wears glasses" or demographic categories from
webcam frames to compute per-group FPR would reproduce exactly the harm the study exists
to detect.

---

## Coverage targets

| Area | Target | Rationale |
|---|---|---|
| Audit chain, canonical JSON, signing | **100% branch** | The differentiator |
| Judge sandbox | 100% of the attack matrix | Security boundary |
| Authz and tenancy | 100% of the (role × endpoint) matrix | Breach surface |
| Grading | 100% branch | Correctness of results |
| Timer, recovery, autosave | 90% | Candidate harm if wrong |
| Everything else | 80% | Reasonable |

Coverage is a floor, not a goal. 100% coverage of the chain code means every branch ran; it
does not mean the design is sound. That is what the tamper suite and an external
cryptographic review are for.

---

## CI gates

A pull request cannot merge if any of these fail:

1. Lint and type check (`ruff`, `mypy --strict` on `audit/`, `tsc --noEmit`)
2. Unit and integration suites
3. **RLS invariant**: zero tables with `org_id` lacking forced RLS
4. **Authz matrix**: zero endpoints missing from the policy matrix
5. **Structural assertions**: no risk→score path, no `audit_event` mutation endpoint
6. Security suite (chain tamper, replay, evidence authz)
7. Judge sandbox suite (Phase 3+)
8. Dependency and container scans
9. Secret scanning
10. Documentation check: any new detector has a `LIMITATIONS.md` entry

Gate 10 is deliberate. Making the limitations file a build dependency is the only reliable
way to keep it honest as the product grows — documentation that is optional decays, and a
decayed `LIMITATIONS.md` turns an honest product into a misleading one without anyone
deciding to lie.

---

## Test data

- No real candidate data in any non-production environment, ever.
- Webcam test fixtures are synthetic or from consenting contributors with recorded consent.
- Seeded credentials come from environment variables, never from the repository.
- Chain test vectors (envelope → expected canonical bytes → expected digest) are committed
  and shared between the API and the standalone verifier, so the two independent
  implementations are pinned to the same spec without sharing code (ADR-0013).
