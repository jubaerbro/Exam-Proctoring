# Decisions requiring your approval

**Phase 1 does not start until these are settled.** Three are already resolved; twelve are
open. Each open item states my recommendation and what changes if you choose otherwise.

Items marked **BLOCKING** change the schema or the security architecture and are
expensive to reverse later. Items marked **SOFT** can be deferred a phase or two without
rework.

---

## Already approved

| # | Decision | Choice | ADR |
|---|---|---|---|
| A1 | Tenant isolation | Shared schema + `org_id` + forced RLS | ADR-0001 |
| A2 | Authentication | Local auth now, OIDC-ready seams | ADR-0002 |
| A3 | Deployment target | Cloud-agnostic containers | ADR-0003 |
| A4 | D7 — live candidate feedback | **No live feedback.** Disclosure before, evidence mirror after | ADR-0016 |
| A5 | D2 — identity across orgs | Global `app_user` + tenant-scoped `membership` (default applied in Phase 1) | — |
| A6 | D12 — Phase 1 build order | Vertical slice (default applied) | — |
| A7 | D13 — repository | `git init` local, no remote (default applied) | — |

---

## D1 — Is the first customer academic or professional? **BLOCKING**

The single most consequential product question, and it is not a technical one.

|  | Academic (universities) | Professional (hiring, certification) |
|---|---|---|
| Sales cycle | 6–18 months, committee-driven | Weeks, single buyer |
| Integrity expectations | High scrutiny, appeals process, legal exposure | Lower scrutiny, cost-sensitive |
| Must-have features | SSO/LMS integration, accessibility, retention control, appeals | ATS integration, candidate experience, speed |
| Volume | Bursty (exam weeks), high peak | Steady, low peak |
| What kills the deal | Accessibility failure, a false accusation | Candidate drop-off, slow judge |
| Data residency | Frequently mandatory | Rarely raised |

**My recommendation: professional/technical hiring assessment first.**

Reasoning: the sales cycle is short enough to get real usage data within the project
timeline; the code judge is a genuine differentiator there and a nice-to-have in academia;
and paradoxically, Sentinel's evidence-first, no-automatic-accusation stance is *easier* to
sell to a hiring team (who mostly want a fair, defensible process) than to a university
procurement committee (who often arrive expecting the "AI catches cheaters" pitch and need
to be talked out of it).

Academic remains the larger long-term market and nothing in the architecture forecloses
it. But building for both simultaneously means building the union of two feature sets and
shipping neither well.

**If you pick academic instead:** SSO moves from P2 to P1, accessibility audit moves into
P0, LMS integration (LTI 1.3) enters the roadmap, and the code judge drops in priority.

---

## D2 — Does a candidate account persist across organizations? **DEFAULT APPLIED**

> Phase 1 shipped the recommended design: a global `app_user` with tenant-scoped
> `membership`. Still reversible, but the cost rises once real accounts exist.



The schema currently says yes: `app_user` is global, `membership` is tenant-scoped, so one
person with one login can be a candidate at two universities.

**Recommendation: keep it.** It is the right SaaS model and it makes the candidate
transparency features coherent — a candidate has one place to see their own records.

**The cost, stated plainly:** a global unique email means organization A can learn that an
email address is already registered, by attempting an invitation. That is a small
cross-tenant information leak, inherent to shared identity. The alternative
(per-org identities, duplicate accounts) removes the leak but makes a candidate manage one
login per institution and breaks the unified transparency view.

**Decide:** accept the leak, or scope identity per-organization?

---

## D3 — Is a native lockdown client on the roadmap? **BLOCKING for positioning**

Browser-based monitoring has a hard ceiling
([`THREAT_MODEL.md §T2`](../THREAT_MODEL.md#t2--compromised-browser)): a candidate who
controls their browser can feed Sentinel whatever they like, and the chain will faithfully
record the fabrication.

A native client with OS-level integrity checks raises that ceiling substantially. It is
also a fundamentally different product: installation friction, elevated privileges on a
candidate's personal machine, per-platform builds, a much larger attack surface on the
candidate's side, and a meaningful ethical shift in how invasive Sentinel is.

**Recommendation: no native client. Ever, as a positioning choice — not merely "not yet".**

Reasoning: it is more honest and more defensible to say "we are browser-based, here is
exactly what that can and cannot do, and here is why we never auto-fail anyone" than to
join an arms race Sentinel cannot win against a determined attacker with root on their own
machine. Committing to browser-only also makes `LIMITATIONS.md` a permanent asset rather
than a temporary embarrassment.

**If you disagree**, decide now — it changes how the integrity layer is marketed from
Phase 1, and half-committing (browser now, native "later") produces the worst outcome:
overclaiming today against a capability that never ships.

---

## D4 — Judge isolation: hardened Docker, or gVisor/Firecracker? **BLOCKING for Phase 3**

Container isolation shares a kernel and is not a hard security boundary. A kernel
vulnerability defeats it ([`THREAT_MODEL.md §T5.9`](../THREAT_MODEL.md)).

| Option | Isolation | Cost |
|---|---|---|
| Hardened Docker | Namespaces + cgroups + seccomp | Baseline; works everywhere including self-hosted |
| gVisor | User-space kernel intercepts syscalls | ~10–30% slowdown; some syscall incompatibility |
| Firecracker | True microVM | Strongest; highest operational complexity |

**Recommendation: hardened Docker for Phase 3, with the runner behind an interface so
gVisor can be swapped in without touching the grading logic.** Revisit before the first
paying customer runs untrusted code at volume.

**Decide:** accept the residual risk for now, or pay the complexity cost up front?

---

## D5 — Question types beyond the five specified? **SOFT**

Currently: single-choice, multiple-choice, short answer, numeric, coding.

Commonly requested additions: file upload (essays, diagrams), matching/ordering, fill-in-
the-blank with multiple gaps, and essay with rubric-based manual grading.

**Recommendation: ship the five, add manual-graded essay in P1.** Essay is the most-asked-
for and the least well served by auto-grading, and adding it forces the manual grading
workflow to exist — which short-answer grading needs anyway.

**Note:** file upload introduces malware scanning, storage cost, and a new attack surface.
Do not add it casually.

---

## D6 — Which browsers are officially supported? **SOFT, but affects Phase 1 test setup**

`screen.isExtended` is Chromium-only. `requestVideoFrameCallback` support varies. Safari's
screen-capture behaviour differs.

**Recommendation: Chrome/Edge fully supported; Firefox and Safari supported for assessment
with reduced integrity signals, disclosed to the candidate at calibration.**

**Alternative:** require Chromium for integrity-enabled exams. Stronger signals, but
excludes candidates and raises an accessibility and equity question — mandating a specific
browser is a real barrier for some people.

---

## D7 — Should the candidate see evidence during the exam? **RESOLVED**

> **Decided: no live monitoring feedback.** Recorded as ADR-0016 and implemented in the
> Phase 1 candidate UI, with a browser test asserting that no flag count or risk score
> appears in candidate-facing copy. The discussion below is kept for the record.



Currently designed as: transparency *after* the exam, disclosure *before*.

**Recommendation: no live evidence view, but a live, calm status indicator** ("monitoring
active", "camera OK"). Showing a candidate their flags in real time turns a timed exam
into an anxiety loop, and it tells a would-be cheater exactly which behaviours evade
detection.

**Alternative:** show live warnings ("please stay in frame"). This is arguably *fairer* —
it lets an honest candidate fix a genuine problem, like having drifted out of frame,
rather than being flagged for something they would have corrected instantly. There is a
real argument here and I do not think the recommendation is obviously right. A middle
option: gentle corrective nudges for recoverable technical conditions (face not visible,
poor lighting) without ever showing flag counts or severity.

**Decide:** silent, warning, or nudge-only?

---

## D8 — What is the default risk threshold for entering the review queue? **SOFT**

Too low: reviewers drown, and review quality collapses. Too high: the integrity layer does
nothing.

**Recommendation: do not set a default until Study 2 measures the false-positive rate.**
Until then, queue on **any severity-3 event or chain verification failure** — a rule that
is explainable to a customer without a calibrated model behind it.

Shipping a number like "0.7" before measurement would be inventing a threshold and
presenting it as a finding.

---

## D9 — Retention defaults: is 30/90 days right for your first market? **SOFT**

Currently 30 days, extended to 90 under appeal.

Universities often have appeal windows of a full semester or longer; certification bodies
sometimes require multi-year retention for accreditation. Both are configurable, but the
*default* signals the product's privacy posture, and defaults are what most customers
actually run.

**Recommendation: keep 30/90.** Minimal by default is the right stance for a product whose
pitch is privacy-conscious integrity, and organizations that need longer must consciously
choose it.

---

## D10 — Should chain heads be publicly anchored? **SOFT, deferrable to P2**

A stronger transparency claim: publish a daily Merkle root of all chain heads (to a public
log, a transparency log, or a blockchain) so Sentinel cannot retroactively re-sign an
entire session even with the private key.

**Recommendation: not now, but design the export bundle so it can be added.** It closes the
T6.4 key-compromise gap and is genuinely differentiating — but it invites "blockchain
proctoring" positioning, which would cheapen a serious product. If added later, it should
be presented as a transparency log, not as a blockchain feature.

---

## D11 — Timeline and team? **SOFT, but shapes everything**

Eleven phases is a lot. The realistic minimum for P0 alone, done to the definition of done
in [`TESTING.md`](TESTING.md), is substantial — Phases 3 and 5 in particular are slow
because their security tests are the deliverable, not an afterthought.

**Please tell me:** solo or team, and the deadline. If this is also an academic project
with a submission date, that changes what "done" should mean — I would rather scope
honestly to Phases 0–5 done properly than have Phases 0–11 half-built, and
[`DECISIONS.md`](../DECISIONS.md) is already structured to support a project report.

---

## D12 — What should I build first in Phase 1? **RESOLVED — vertical slice**

Two reasonable orders:

**(a) Infrastructure-first** — Docker Compose, migrations, health checks, auth, orgs,
roles, seed. Nothing visible for a while, then everything works.

**(b) Vertical-slice-first** — one thin path end-to-end (login → see one exam → answer one
question → submit → see a score), then broaden.

**Recommendation: (b), a vertical slice.** It surfaces integration problems immediately —
the tenancy context plumbing, the `SET LOCAL` behaviour under pooling, the frontend/API
contract — while they are cheap to fix. Infrastructure-first tends to produce a beautifully
configured stack that turns out to have the wrong seams.

---

## D13 — Repository and version control **PARTIALLY RESOLVED**

> `git init` done locally with a `.gitignore` covering `.env`, keys, build output and
> data directories. **No remote configured** — say the word if you want it pushed.



The project folder is currently empty. Before Phase 1 I would:

- `git init`, with a `.gitignore` covering `.env`, keys, `node_modules`, `__pycache__`,
  build output, and MinIO data
- commit these Phase 0 documents as the first commit
- commit in small logical units per the brief's §52

**Confirm:** should I initialize the repository, and do you want it pushed to a remote
(GitHub) or kept local?

---

## D14 — Is there an existing exam system to integrate with? **SOFT**

If Sentinel must coexist with Moodle, Canvas, an ATS, or an internal system, LTI 1.3 or a
webhook/API integration moves up the roadmap significantly, and the assignment model may
need an external-identity mapping beyond `membership.external_ref`.

---

## D15 — Anything in the brief I should push back on further? **OPEN**

I have already pushed back in three places, and you should know where:

1. **`verify_chain.py` at the repo root** → moved to `tools/verifier/` so its independence
   from the application is structural rather than conventional (ADR-0013).
2. **Sample-run rate limiting** → the brief mentions it; I made it a hard per-session limit
   because candidate-triggered compute is the cheapest thing in the product to abuse.
3. **"AI proctoring" framing** → I have gone further than the brief asked and made it
   *structurally impossible* to auto-fail a candidate: there is no `cheating` field in the
   schema, no code path from risk to score, and a test that asserts both (ADR-0015).
   If you ever want a "flagged" boolean for reporting convenience, please raise it as a
   decision rather than a ticket — that field is exactly how evidence quietly becomes
   verdict.

If any of that is wrong for your goals, now is the cheapest time to say so.

---

## What I need from you to start Phase 1

**Still open after Phase 1: D1, D3, D4, D5, D6, D8, D9, D10, D11, D14.**

Of these, only **D4** (judge isolation) blocks a phase — Phase 3. **D1** and **D3** shape
positioning and should be settled before there is marketing material to be wrong.
Phase 2 needs none of them.

*Superseded guidance below (kept for the record):* Minimum: **D1, D2, D3, D4, D12, D13.**

Everything else can be decided as the relevant phase approaches. If you would rather not
decide D1 and D3 yet, I can start Phase 1 on the shared foundation — auth, orgs, roles,
migrations, seed — since none of it changes based on those answers. But D2 changes the
schema and D4 changes Phase 3's structure, so those two are worth settling now.
