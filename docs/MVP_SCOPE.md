# Sentinel — MVP Scope and Phase Plan

**Status: PHASE 0. Every item below is NOT IMPLEMENTED.**

---

## 1. The MVP bar

The MVP is not a feature list. It is one flow working end-to-end for a real organization:

```
Create organization → Create exam → Import questions → Assign candidates
  → Candidate takes exam → Answers autosaved → Code executed securely
  → Integrity events recorded → Relevant evidence captured → Audit chain signed
  → Exam submitted → Results calculated → Suspicious sessions enter review queue
  → Reviewer examines evidence → Reviewer decides
  → Candidate inspects their permitted evidence → Organization exports results
```

Until that runs unbroken, **no P2 work starts.** A sophisticated gaze detector attached to
a product that loses a candidate's answers on reload is worth nothing.

---

## 2. P0 — must work

| # | Feature | Why it is P0 |
|---|---|---|
| P0-01 | Auth: login, refresh, logout, reset, TOTP for staff | Nothing else is reachable |
| P0-02 | Organizations, memberships, four roles | Tenancy must exist before data does |
| P0-03 | RLS on every tenant table + CI invariant test | Retrofitting isolation is not realistic |
| P0-04 | Question bank CRUD + versioning | Content is the product's raw material |
| P0-05 | Versioned JSON import/export with per-field validation errors | Nobody types 500 questions into a form |
| P0-06 | Exam authoring: sections, `k of n` pools, settings | Core authoring |
| P0-07 | Candidate assignment, bulk + CSV | Core delivery |
| P0-08 | Deterministic per-candidate paper generation | Required for reproducible recovery |
| P0-09 | Server-authoritative timer + clock sync | The browser cannot own the clock |
| P0-10 | Autosave, reload/crash/reconnect recovery | Losing exam progress is unforgivable |
| P0-11 | Submission with server-side deadline enforcement | Correctness of the assessment itself |
| P0-12 | Auto-grading: choice, multi-choice, short answer, numeric | Results must exist |
| P0-13 | Coding questions with sample + hidden tests | Differentiator for technical assessment |
| P0-14 | **Secure judge with security tests passing first** | Hard gate — see Phase 3 |
| P0-15 | Browser integrity signals with hysteresis | Baseline integrity layer |
| P0-16 | Event ingest: server seq/ts, replay protection, late marking | The chain's foundation |
| P0-17 | Screen/webcam evidence: rolling buffer, caps, cooldown, dedupe | Evidence without runaway cost |
| P0-18 | Canonical JSON + SHA-256 chain + Ed25519 signing | **The core differentiator** |
| P0-19 | Standalone `verify_chain.py` | The differentiator is worthless unverified |
| P0-20 | Tamper test suite (modify/delete/reorder/re-sign/swap evidence) | The claim requires proof |
| P0-21 | Reviewer roster + candidate detail + timeline + evidence grid | The commercial surface |
| P0-22 | Review decisions with mandatory justification, audited | Humans decide, on the record |
| P0-23 | Evidence access logging | Insider threat is a real threat |
| P0-24 | Candidate transparency: disclosure screen, evidence mirror, signed export | Product identity, not a nicety |
| P0-25 | Candidate statements on flagged events | Due process |
| P0-26 | CSV/XLSX results export | Every buyer asks in week one |
| P0-27 | `docker compose up` works from a clean clone | Nothing ships if it cannot run |
| P0-28 | Health/ready endpoints, structured logging | Operability |
| P0-29 | The ten documentation files, maintained | Documentation *is* the product here |

### What is deliberately NOT in P0

Webcam CV detectors. Risk scoring. Retention automation. Accommodations. Analytics.
Billing. SSO. Evidence clips. Identity continuity.

P0 ships with **browser-signal integrity only** and an honest statement of what that
covers. An organization can run real assessments on P0 with real tamper-evident records.
That is a sellable product; a half-built CV pipeline is not.

---

## 3. P1 — important

| # | Feature | Depends on |
|---|---|---|
| P1-01 | Face presence + face count detectors | P0-15..18 |
| P1-02 | Head pose detector | P1-01 |
| P1-03 | Calibration flow before the clock starts | P1-01 |
| P1-04 | Accommodation profiles | P1-01 |
| P1-05 | Baseline interpretable risk engine | P0-16 |
| P1-06 | Review queue driven by risk | P1-05 |
| P1-07 | Retention automation with purge events in the chain | P0-18 |
| P1-08 | Evaluation harness: red team + false-positive studies | P1-01, P1-05 |
| P1-09 | Reviewer efficiency analytics | P0-22 |
| P1-10 | Business metrics collection | P0-28 |
| P1-11 | Manual grading workflow for short answers | P0-12 |
| P1-12 | Playwright browser test suite | P0-10, P0-15 |
| P1-13 | Per-condition false-positive reporting | P1-08 |

**P1-08 gates P1-01.** A detector without a measured false-positive rate is not a feature;
it is a liability that generates review workload and candidate harm at an unknown rate.

---

## 4. P2 — advanced

| # | Feature | Note |
|---|---|---|
| P2-01 | Gaze proxy | Ship only with published accuracy limits |
| P2-02 | Object detection | Only the classes the model actually detects |
| P2-03 | Identity continuity via face embeddings | Requires a privacy review first |
| P2-04 | Optional voice-activity detection | No audio retention by default |
| P2-05 | Rolling video evidence clips | Cost and privacy review required |
| P2-06 | HMM or learned risk model | **Only if it beats the baseline on measured FPR** |
| P2-07 | Advanced org administration | |
| P2-08 | Billing and subscriptions | Metering facts already recorded in P1-10 |
| P2-09 | OIDC/SAML SSO | Seams already in place per ADR-0002 |
| P2-10 | Enterprise controls: IP allowlists, retention locks, audit export | |
| P2-11 | Stronger judge isolation (gVisor/Firecracker) | Open decision |
| P2-12 | Native lockdown client | Open decision; a different product |

---

## 5. Phase plan and exit criteria

Each phase ends with the report in §6. **A phase is not complete because code exists.**

| Phase | Scope | Exit criteria |
|---|---|---|
| **0** | Architecture, schema, threat model, scope | **This deliverable.** Approved by product owner. |
| **1** | Docker Compose, web, API, Postgres, Redis, MinIO, migrations, health, auth, orgs, roles, seed | `docker compose up` from clean clone; seed creates 1 org, 1 instructor, 1 reviewer, 5 candidates, 1 exam; RLS invariant test passes; auth integration tests pass |
| **2** | Question bank, exam authoring, pools, assignment, timer, autosave, recovery, submission, grading, import/export | Candidate completes an exam end-to-end; forced reload mid-exam loses nothing; server rejects a late submit; deterministic paper reproduces on recovery |
| **3** | **Security tests first**, then sandbox, queue, compile, execute, limits, hidden tests, partial scoring | **All sandbox security tests pass before the runner is called complete.** Fork bomb, memory bomb, network egress, FS traversal, `/proc`, stdout flood all contained |
| **4** | Browser signals, WebSocket, sequencing, validation, replay protection, late handling, hysteresis | Replay rejected; forged sequence rejected; hysteresis suppresses single-frame noise; offline queue survives a 60s outage |
| **5** | Canonical JSON, chain, signing, export, standalone verifier, corruption tests | Verifier detects modify / delete / reorder / re-sign / evidence-swap / evidence-delete / evidence-inject / cross-reference, each at the exact index |
| **6** | Evidence: capture, buffer, hashing, dedupe, upload, retention, authz, candidate visibility | Cap enforced with a chain event; dedupe measured; pre-exam-close access denied by API test |
| **7** | CV detectors, incrementally, each with tests + thresholds + limitations + metrics | Each detector has a measured FPR before the next one starts |
| **8** | Review system: roster, filters, detail, timeline, evidence grid, verification badge, statements, decisions | Reviewer completes an adjudication end-to-end; every evidence view logged |
| **9** | Baseline risk, then evaluate whether a learned model helps | Calibration curve produced; baseline FPR measured; learned model ships only on measured improvement |
| **10** | Four evaluation studies, figures, reproducible scripts | Reproducible from scripts; **no fabricated results** |
| **11** | Production hardening | Full checklist in `DEPLOYMENT.md` |

---

## 6. Required end-of-phase report

Every phase ends with exactly this, including the last question:

```
IMPLEMENTED          what actually works, with test evidence
TESTS                what was run, results, coverage of the new code
SECURITY             what security properties were verified, and how
LIMITATIONS          what remains imperfect
FILES CHANGED        important files
DATABASE CHANGES     migrations and schema changes
API CHANGES          endpoints added or modified
NEXT PHASE           what should happen next

WHAT DID I IMPLEMENT THAT DOES NOT ACTUALLY WORK AS CLAIMED?
```

Status vocabulary, used strictly:

```
IMPLEMENTED           works, tested, documented, handles failure cases
PARTIALLY IMPLEMENTED works for the main path; gaps named explicitly
STUB                  interface exists, behaviour does not
NOT IMPLEMENTED       absent
KNOWN LIMITATION      works as designed; the design has a documented limit
```

---

## 7. Commercial prioritization filter

For any proposed feature, in order:

1. Does it make Sentinel more useful to a real organization?
2. Does it improve reliability, security, usability, assessment quality, reviewer
   efficiency, candidate trust, or deployment simplicity?
3. Is it technically impressive but commercially inert?

If the answer to 3 is yes and to 1 and 2 is no, it does not get built. The strongest
example: a more accurate gaze detector improves nothing commercially if reviewers already
dismiss 90% of gaze flags. **Reviewer efficiency and false-positive rate are the metrics
that decide whether the integrity layer is worth its price**, and they are the ones the
evaluation harness exists to measure.
