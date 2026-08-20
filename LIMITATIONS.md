# Sentinel — Limitations

**Status: PHASE 0. This document describes limits inherent to the approach. It is written
before implementation because these limits should shape what gets built, not be discovered
afterwards as excuses.**

This is a product artifact, not an appendix. It is intended to be readable by a
procurement officer, an academic-integrity committee, and a candidate — and it is intended
to be shown to them, not hidden.

**Sentinel cannot determine that a candidate cheated. No browser-based system can.**
Anything claiming otherwise is either misdescribing what it measures or lying.

---

## 1. What Sentinel actually guarantees

To make the limits meaningful, here is the positive claim, stated as narrowly as it is
true:

> Sentinel produces a cryptographically signed, hash-chained, append-only record of the
> integrity signals its client reported and the evidence its server received, verifiable
> by an independent tool without trusting Sentinel.

Everything outside that sentence is out of scope. In particular, Sentinel does **not**
guarantee that the reported signals reflect physical reality, that all misconduct was
detected, or that every flag indicates misconduct.

---

## 2. Browser platform limits

These are properties of the web platform, not of Sentinel's implementation quality. No
amount of engineering removes them.

### 2.1 Cannot prevent or detect OS-level screenshots

No web API can block or observe a screenshot taken by the operating system, a phone
camera pointed at the screen, or an external capture card. **Unmitigated.**

### 2.2 Cannot detect a second physical device

A phone on a stand outside the camera cone, a second laptop, a smart watch, or a person
reading from a printout below the desk are invisible to a browser. Object detection may
catch a phone *that enters the webcam frame*. **This is the largest gap in remote
proctoring, and Sentinel does not close it.**

### 2.3 Cannot prevent exiting fullscreen

The Fullscreen API is cooperative. `Escape` always exits, by browser design and for good
accessibility reasons. Sentinel detects and records exits; it cannot prevent them.

### 2.4 `visibilitychange` does not catch everything

A second window covering the exam tab does not necessarily fire `visibilitychange` — the
tab may still be "visible". Focus loss fires `blur`, but on a multi-monitor setup a
candidate can read another screen while the exam window retains focus and visibility.
Coverage differs across browsers and OSes.

### 2.5 Background timers are throttled

Hidden tabs have `setTimeout`/`setInterval` clamped (typically ≥1 s) and may be frozen
entirely after a few minutes. This is why evidence capture is driven by
`requestVideoFrameCallback` rather than timers — but **`requestVideoFrameCallback` is not
available in every browser**, and where it is missing the buffer degrades to a
timer-driven fallback with exactly the throttling problem it was meant to avoid. Sentinel
feature-detects and records which capture path was used, so a reviewer knows the fidelity
of what they are looking at.

### 2.6 Multi-monitor detection is Chromium-only and permissioned

`screen.isExtended` comes from the Window Management API, which is not implemented in all
browsers. Where it is absent, Sentinel cannot tell whether a second display exists.
Absence of a `MULTI_MONITOR_DETECTED` event therefore means "not detected", never "not
present".

### 2.7 Screen capture is user-controlled

`getDisplayMedia()` requires explicit user selection, and **the user chooses what to
share**. A candidate can share a single window rather than the whole screen. The
`displaySurface` value reports what they picked; it does not let Sentinel force a choice.
The picker cannot be pre-selected or bypassed. Support and behaviour vary by browser and
by OS (macOS additionally requires a system-level permission).

### 2.8 Clipboard monitoring is partial

`paste` events fire on elements Sentinel controls. Content pasted via devtools, via
autofill, via an IME, or typed from another window is not a paste event. Large-paste
detection is a signal about *one* copying route, not about copying in general.

### 2.9 `beforeunload` is unreliable

It may not fire on crash, force-quit, mobile background-kill, or OS shutdown. A missing
"session ended" signal does not imply anything.

### 2.10 Cannot reliably detect extensions, devtools, VMs, or remote-desktop software

There is no supported API for any of these. Published heuristics are fragile, trivially
defeated, and produce false positives on ordinary accessibility tools. Sentinel does not
ship detection it cannot stand behind.

### 2.11 Mobile browsers behave differently

Fullscreen, camera constraints, background behaviour, and permission models all differ on
mobile. Sentinel's integrity features target desktop browsers; mobile is best-effort and
should be disclosed as such by the organization.

---

## 3. Computer vision limits

### 3.1 Gaze is a proxy, not eye tracking

Sentinel estimates gaze direction from face landmarks and head pose using a consumer
webcam. It is **not** an eye tracker. Accuracy degrades with:

glasses (especially anti-reflective or tinted), contact lenses, low or backlit lighting,
camera below or far to the side of the screen, large or ultrawide monitors, distance from
camera, head coverings, and normal thinking-away behaviour.

A "sustained off-screen gaze" event means *the proxy indicated sustained deviation*. It
does not mean the candidate looked at unauthorized material. It is called `OFFSCREEN_GAZE`
and never anything stronger.

### 3.2 Face detection fails on legitimate people

Detection quality varies with lighting, camera quality, occlusion, head coverings
(religious dress, medical masks), facial hair, and — documented repeatedly in the
literature, including NIST's FRVT evaluations — **across skin tones and demographic
groups**. A `FACE_ABSENT` event may mean the candidate left, or may mean the system could
not see a person who was sitting there the whole time.

This is why Study 2 in the evaluation harness measures false-positive rate *by condition*,
and why those numbers are meant to be published even when they are unflattering. An
unmeasured false-positive rate is not a low one; it is an unknown one.

### 3.3 Identity continuity is weaker than it sounds

Face-embedding comparison against an enrollment capture is sensitive to lighting change,
pose, expression, and camera change, and inherits the demographic accuracy differentials
above. `IDENTITY_MISMATCH` is a prompt for human review, never an identity determination.
Sentinel does not perform identity verification against government documents and does not
claim to.

### 3.4 Object detection covers a narrow, honest set of classes

General-purpose detectors reliably recognize a limited vocabulary — a phone, a book, a
laptop. **"Earbuds" are not a class in standard pretrained detectors**, and small in-ear
devices at webcam resolution are close to undetectable without a purpose-trained model and
a dataset that does not currently exist in this project. Sentinel will ship the classes it
can actually detect and will name them explicitly rather than implying broader coverage.

### 3.5 Multiple-face detection catches only what enters frame

A helper standing behind the camera, or beside the candidate outside the field of view, is
not detected.

### 3.6 A virtual camera defeats all of it

Software that presents a recorded or synthetic video stream as a camera is
indistinguishable from a real camera at the `MediaStream` level. Every vision detector can
be fed whatever the attacker chooses. Liveness challenges raise the cost; they do not
solve it.

### 3.7 Client-side inference varies by device

Models run in the candidate's browser. A low-end device yields lower frame rates and
therefore lower temporal resolution; thermal throttling degrades it further mid-exam. Two
candidates can receive materially different monitoring fidelity for reasons entirely
outside their control. Sentinel records the achieved sample rate so a reviewer can see it.

---

## 4. Audit chain limits

### 4.1 It proves recording, not truth

The chain proves the events are what the server signed, in order, unaltered. It says
nothing about whether the client told the truth. **A compromised browser produces a
perfectly verifiable record of fabricated events.** This is the central limitation of the
approach and it is stated in the product documentation, not only here.

### 4.2 It depends entirely on key custody

If the Ed25519 private key is stolen, an attacker can forge a chain that verifies. The
tamper-evidence claim reduces to "the key was not compromised".

### 4.3 It cannot recover destroyed evidence

The verifier detects that an image was altered or is missing. It cannot restore it. A
successful destructive attack yields a provable gap, not the lost content.

### 4.4 Events that never reached the server are not in the chain

Power loss, a browser crash, or a hard network failure can lose queued events. The
heartbeat gap is visible; the content is not. Absence of events is not evidence of
absence, and the reviewer UI must present it that way.

---

## 5. Judge limits

### 5.1 Container isolation is not a hard security boundary

Namespaces and cgroups share a kernel. A kernel vulnerability can defeat them. Sentinel's
Phase 3 baseline is hardened Docker; gVisor or Firecracker would be stronger and is an
open decision. This is a real, irreducible residual risk, not a formality.

### 5.2 Timing is not perfectly reproducible

Wall-clock limits vary with host load. Sentinel uses generous multiples of reference
solution times and reports resource usage, but a borderline-efficiency solution may pass
on one run and time out on another. Time limits should be set with margin.

### 5.3 Auto-grading measures correctness against test cases

It does not measure code quality, design, maintainability, or whether the candidate
understood the problem. Partial credit reflects tests passed, nothing more.

### 5.4 Hidden test results leak information

Per-test pass/fail necessarily reveals something about the hidden tests. Submission caps
bound the leak; they do not remove it.

---

## 6. Assessment limits

- **Short-answer auto-grading is string matching**, optionally case-insensitive or regex.
  It is not semantic. Anything requiring judgement needs manual grading, which is P1/P2.
- **Numeric tolerance** handles absolute and relative tolerance and does not do unit
  conversion or symbolic equivalence.
- **Randomized selection changes difficulty between candidates.** Two candidates drawing
  from the same pool do not receive equally hard papers unless the pool is carefully
  calibrated. Sentinel provides tags and difficulty fields; it does not perform IRT-based
  equating.
- **Server-authoritative timing depends on the server being reachable.** A candidate
  offline at the deadline submits on reconnect; the server decides whether to accept it,
  and that policy decision belongs to the organization.

---

## 7. Risk estimation limits

- The output is an **estimated session anomaly probability**, not a probability of
  cheating. It is calibrated against a labelled evaluation dataset that Sentinel
  constructs; it does not generalize automatically to a new population, a new exam
  format, or a new candidate demographic.
- Weights are configured, not learned from ground truth — because ground truth for
  cheating is not obtainable at scale without exactly the accusations this product refuses
  to make.
- Correlated signals can compound: poor lighting can simultaneously raise face-absence,
  gaze, and identity signals, producing a high score from **one** underlying environmental
  cause. Co-occurrence modelling is intended to reduce this and will not eliminate it.
- **No threshold has legitimate meaning as a verdict.** There is no score above which
  misconduct is established.

---

## 8. Operational and organizational limits

- Sentinel cannot make a policy fair. It can record what happened under whatever policy
  the organization sets.
- Sentinel cannot make a reviewer competent, unbiased, or attentive. It can measure review
  time, evidence views, and inter-reviewer agreement, and surface those numbers.
- Sentinel does not confer legal compliance. Deploying organizations remain responsible
  for their lawful basis, disclosure, retention, accessibility obligations, and
  data-subject rights.
- Sentinel cannot verify that a candidate consented meaningfully rather than because
  refusing meant failing the course.

---

## 9. Accessibility and fairness

Remote proctoring risks systematically disadvantaging candidates who:

- have involuntary movement, tics, or conditions affecting gaze and posture,
- use screen readers, magnifiers, or other assistive technology,
- wear glasses, head coverings, or medical devices,
- have poor lighting, an old camera, or an unstable connection,
- share a home with other people or lack a private room,
- are neurodivergent and display atypical attention patterns.

Sentinel's responses: accommodation profiles that adjust thresholds without stigmatizing
labels; calibration before the clock starts so environment problems surface early;
false-positive measurement by condition; and — most importantly — **the rule that no
automated signal produces a sanction**.

None of this makes remote proctoring neutral. Organizations should weigh whether
proctoring is appropriate for a given assessment at all. A product that never raises that
question is not being straight with its buyers.

---

## 10. Status of this document

This document will be updated continuously and **must be updated whenever a detector is
added**. A detector shipped without a limitations entry is not complete under the
[definition of done](docs/TESTING.md#definition-of-done).

If a claim in Sentinel's marketing material contradicts this document, this document is
correct and the marketing is wrong.

---

## Phase 2 additions (2026-08-19)

Recorded here because a limitation that lives only in a phase report gets read once.

**Losing an answer is still possible, in one narrow case.** Autosave debounces for ~900 ms
and retries every five seconds while the page is open. If the network drops *and* the
candidate closes the tab before it recovers, the last edit is gone. P0-10 ("autosave,
reload/crash/reconnect recovery") is therefore only partly met: reload and crash recovery
work and are tested in a real browser; a genuine offline queue (IndexedDB, replay-safe
resend) is Phase 4.

**Coding questions look delivered and are not.** They are authored, served, and autosaved.
Nothing runs them and nothing scores them; they are recorded with `grader='judge'` and 0
awarded so that the maximum stays honest. Publishing an exam with coding questions
requires `judge_enabled`, which no honest deployment should set before Phase 3.

**`section.time_limit_seconds` is stored and ignored.** Unlike `navigation`, it is not
rejected at authoring time. An author can set a per-section limit that the delivery layer
does not enforce.

**Results release is enforced but unreachable.** `results_release_at` gates
`GET /sessions/{id}/result`, and no API endpoint sets that column, so results are readable
as soon as they exist.

**Grading is synchronous.** It runs inline on submit, in its own transaction. A large
paper makes submission slower than it should be.

**`answer_revision` cannot be deleted, even by cascade.** The append-only trigger fires on
DELETE as well as UPDATE, so a session with answers cannot be removed by deleting its
parent. Correct for an evidence-first product; it means retention purging (Phase 6) needs
an explicit, audited purge path rather than a DELETE.

**Short-answer matching does not fold typographic apostrophes.** NFKC normalisation does
not map U+2019 to U+0027, so "Dijkstra’s" typed on a phone will not match an accepted
answer of "Dijkstra's". A test records the behaviour rather than hiding it.

**Pools are explicit lists only.** `section_pool.source_kind = 'filter'` exists in the
schema and is not implemented.

**There is no staff authoring UI.** Question banks, exams, pools, publishing and
assignment are API-only. The only interface shipped is the candidate's.

---

## Phase 3 additions (2026-08-20)

**The judge sandbox shares the host kernel.** runc containers, hardened with
every flag the runtime offers, but a kernel exploit escapes them. This is D4's
accepted residual risk (ADR-0022). Production must run judge workers on an
isolated host pool; `JUDGE_RUNTIME=runsc` switches to gVisor where it is
installed.

**Java is listed and unverified.** `java17` appears in the language list, the
compose file and the UI dropdown. No Java submission has ever executed. Remove
it from a question's `languages` until it has.

**The shipped judge images have never been built.** Every sandbox security claim
was verified against images assembled from the local filesystem, because Docker
Hub was unreachable from the build environment. The controls under test are
runtime flags rather than image properties, so the evidence transfers — but
`docker compose --profile judge up` is unproven.

**The compile step writes to a host directory.** `/box` is a tmpfs, but compiled
artefacts have to outlive the compile container, so a fresh per-run host
directory is bind-mounted read-write at `/out`. It is emptied and removed around
every run and the `fsize` ulimit applies, but it is the one writable host path a
sandbox sees.

**The seccomp profile is default-allow.** See ADR-0023. It is a second layer,
not the primary control.

**`peak_memory_kb` and `compile_ms` are always NULL.** The columns exist; nothing
measures either.

**The judge rate limit is per session, not per question.** A candidate with two
coding questions shares one budget of 12 runs a minute across both.

**Stale-job recovery is untested.** `requeue_stale` is written and reachable. No
test kills a worker mid-run to prove a job comes back.

**There is no re-judge.** Fixing a broken test case and re-marking a cohort is a
database operation, not an endpoint.

**No measured judge throughput.** P0-14's evaluation study is Phase 10. There is
no figure for how many submissions a worker sustains, or what queue latency
looks like when 200 candidates press Submit at once.

