# Sentinel — Threat Model

**Status: PHASE 0 outline. No mitigation listed here has been implemented or tested.
Every "Mitigation" row is a design intention. Residual risk is stated honestly.**

Scope: the Sentinel platform as designed in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
Method: actor-based (STRIDE-informed but organized by who is attacking, because that is
how the mitigations actually differ).

---

## 0. Security objectives, ranked

When these conflict, higher wins:

1. **A candidate must not be wrongly harmed.** A false accusation is worse than a missed
   detection. This ranks first deliberately, and it is why no detector can fail anyone.
2. **The audit trail must be tamper-evident**, including against Sentinel's own operators.
3. **Tenant data must not cross tenants.**
4. **The judge must not be an execution foothold.**
5. **Candidate data must not leak**, including to insiders.
6. **Detection of misconduct.** Genuinely last. A product that ranks this first produces
   the failure modes this document exists to avoid.

---

## T1 — Malicious candidate (no browser modification)

The ordinary case: a candidate using the product as shipped, trying to gain an advantage.

| # | Threat | Mitigation | Detection | Residual risk |
|---|---|---|---|---|
| T1.1 | Submit after the deadline | Server-authoritative deadline; `submitted_at` set server-side; client clock ignored | Late attempt logged | **Low.** Grace window is a deliberate policy choice, not a hole. |
| T1.2 | Replay a previously captured event to mask a gap | `UNIQUE (session_id, client_seq)`; strictly increasing `client_seq`; per-session HMAC | `duplicate` / `seq_regression` rejections logged | **Low** for an unmodified browser. |
| T1.3 | Tamper with an answer after submission | `answer` frozen on submit; `answer_revision` append-only | Revision history | **Low** |
| T1.4 | Delete or alter their own evidence | No delete endpoint exists; RLS denies candidate writes; storage keys are not candidate-writable | Access log | **Low** |
| T1.5 | Suppress events by killing the network | Offline IndexedDB queue; `NETWORK_DISCONNECT`/`RECONNECT` events; heartbeat gaps visible on the timeline | Gap in heartbeats | **Medium.** A hard power-off loses queued events. The gap is visible, but its *content* is gone. A reviewer sees "we stopped hearing from this session", not what happened. |
| T1.6 | Probe hidden test cases via repeated submissions | Hidden outputs never returned; `code/submit` capped at 10/exam; per-test results give pass/fail only | Submission rate | **Medium.** Pass/fail per test still leaks ~1 bit per test per submission. Capping submissions bounds this rather than eliminating it. |
| T1.7 | Use a second device (phone, second laptop) | Webcam object detection *may* catch a visible phone; gaze proxy *may* catch sustained off-screen attention | Weak, probabilistic | **HIGH — largely unmitigated.** A phone below the desk, out of frame, is not detectable from a browser. This is the single largest gap in remote proctoring generally and Sentinel does not solve it. See [`LIMITATIONS.md`](LIMITATIONS.md). |
| T1.8 | OS-level screenshot of the exam | None available | None | **HIGH — unmitigated.** No web API can prevent or detect an OS screenshot. |
| T1.9 | Human helper off-camera | Optional VAD may register voice; multi-face detection catches only those who enter frame | Weak | **HIGH.** A silent helper reading over a shoulder outside the camera cone is undetectable. |
| T1.10 | Abuse `code/run` to burn compute | Rate limit 1/10s burst 3; judge pool isolated; queue depth monitored | Queue metrics | **Low** |

**Honest summary of T1:** Sentinel handles protocol-level cheating well and physical-world
cheating poorly. Anyone claiming otherwise about any browser-based product is selling
something. Sentinel's answer is not to pretend — it is to make the evidence trail
trustworthy for the cases it *does* capture and to be explicit with buyers about T1.7–T1.9.

---

## T2 — Compromised browser

The candidate controls the client entirely: devtools, extensions, a patched build, a
virtual camera.

| # | Threat | Mitigation | Detection | Residual risk |
|---|---|---|---|---|
| T2.1 | Disable detectors so no events fire | Heartbeat expected at a fixed cadence; a session with zero detector telemetry is anomalous; `DETECTOR_ERROR` when a worker dies | Silence is itself a signal | **Medium.** Detectable as *anomalous*, not as *cheating*. Routed to review, never to a verdict. |
| T2.2 | Forge benign events / suppress real ones | Per-session HMAC; server timestamps | Inconsistency between event stream and heartbeat pattern | **HIGH — fundamentally unmitigable.** The client holds the ingest secret. Any browser-resident secret is readable by whoever controls the browser. |
| T2.3 | Feed a virtual camera (loop, deepfake, static image) | Optional liveness challenge (P2); frame-entropy heuristics | Weak | **HIGH.** Detecting a good virtual camera from JavaScript is not reliably possible. |
| T2.4 | Fake client timestamps | `server_ts` is authoritative; `client_ts` retained only as an untrusted claim; `CLOCK_ANOMALY` on divergence | Divergence event | **Low** — because we never trusted it. |
| T2.5 | Replay a chain from a clean prior session | Chain is per-session; `session_id` is inside the signed envelope; server signs, not the client | Cross-session mismatch | **Low** |
| T2.6 | Modify evidence before upload | Server recomputes SHA-256; digest sealed in a server-signed event | — | **Note the precise property:** we prove *the bytes we received are the bytes we sealed*. We cannot prove the bytes reflect physical reality. |

**T2 is the honest ceiling of browser-based proctoring.** Sentinel's design response is
architectural rather than aspirational: because a compromised browser can lie
convincingly, no automated signal may produce a sanction. The chain guarantees *what was
recorded*; humans decide what it means. A product that instead auto-fails on detector
output converts T2 from a limitation into a mechanism for harming innocent people.

Mitigating T2 properly requires a native lockdown client with OS-level integrity checks —
a different product, with its own trade-offs (installation friction, elevated privileges,
platform coverage, and a much larger attack surface on the candidate's machine). Whether
to build one is [an open decision](docs/OPEN_DECISIONS.md).

---

## T3 — Network attacker

| # | Threat | Mitigation | Detection | Residual risk |
|---|---|---|---|---|
| T3.1 | Intercept traffic | TLS 1.2+ enforced, HSTS with preload, secure cookies | TLS termination logs | **Low** |
| T3.2 | Replay captured requests | Idempotency keys; `client_seq` uniqueness; short token lifetimes | Duplicate rejections | **Low** |
| T3.3 | Modify requests in flight | TLS; HMAC on events | Integrity failures | **Low** |
| T3.4 | Steal the session cookie via XSS | `httpOnly`; strict CSP with nonces, no `unsafe-inline`; React escaping; sanitized question HTML | CSP violation reports | **Medium.** Question prompts are instructor-authored rich text — a genuine XSS surface. Sanitization must be allowlist-based; a blocklist will eventually fail. |
| T3.5 | CSRF | `SameSite=Lax` + double-submit token + `Origin` validation | Rejections | **Low** |
| T3.6 | Steal a signed evidence URL from a log or referrer | URLs expire in 5 min, single-use, bound to the issuing user; `Referrer-Policy: no-referrer` | Access log | **Medium.** A URL leaked within its window is usable once by whoever holds it. |
| T3.7 | DoS the ingest endpoint | Per-session and per-IP rate limits; WS connection caps; edge rate limiting | Metrics | **Medium.** Application limits do not stop a volumetric attack; that needs edge/CDN protection, which is deployment-specific. |

---

## T4 — Malicious or negligent insider

The threat model most proctoring vendors quietly omit. Staff hold the keys to webcam
footage of students in their homes.

| # | Threat | Mitigation | Detection | Residual risk |
|---|---|---|---|---|
| T4.1 | Reviewer browses evidence with no legitimate purpose | Every issuance writes `evidence_access_log` **and** an `EVIDENCE_ACCESSED` chain event; access requires an open review; org admins can audit | Access-per-session anomaly reports | **Medium.** Detective, not preventive. A determined insider with legitimate access can still look. |
| T4.2 | Instructor accesses evidence before the exam window closes | API-level temporal gate on signed-URL issuance | Denials logged | **Medium.** Enforced in application code, not RLS — a documented single-layer control, so it gets an explicit security test. |
| T4.3 | Admin alters an audit event to fabricate or erase misconduct | Append-only triggers; no API path exists; Ed25519 signature would fail; the candidate holds an independently verifiable copy | Verifier reports the exact failing index | **Low via the product; Medium via direct DB access.** A DBA with superuser rights can `ALTER TABLE ... DISABLE TRIGGER` and rewrite rows — but cannot forge signatures without the private key. **This is why the private key must live in a KMS the DBA does not control.** |
| T4.4 | Admin deletes evidence to bury an incident | Purge writes `EVIDENCE_PURGED` to the chain with the deleted object's digest; object versioning + retention lock in production | Verifier distinguishes purged from missing | **Medium** |
| T4.5 | Reviewer decides without reading the evidence | Mandatory ≥20-char justification; `evidence_viewed` counted; per-reviewer analytics | Reviewer efficiency metrics | **Medium.** Process control, not a technical one. |
| T4.6 | Export candidate data in bulk | Export jobs are logged, rate limited, and org-scoped | Export audit | **Medium** |
| T4.7 | Reviewer bias against particular candidates | Accommodations not visually flagged; no inference of sensitive characteristics; decision + justification permanently attributed | Inter-reviewer agreement metric (Study 4) | **Medium.** Measurable, not preventable by software. |

**T4.3 deserves emphasis as a design principle:** the audit chain's value comes precisely
from being verifiable *against the operator*. If the signing key sits in the same database
the operator administers, the whole construction degrades to "trust us". Key custody is
therefore a security requirement, not an ops detail — see
[`SECURITY.md`](SECURITY.md#3-key-management).

---

## T5 — Hostile code submission (judge)

Assume every submission is written by an attacker who has read the source of the sandbox.

| # | Threat | Mitigation | Detection | Residual risk |
|---|---|---|---|---|
| T5.1 | Fork bomb | `--pids-limit=64`; cgroup pids controller | Container metrics | **Low** (must be tested) |
| T5.2 | Infinite loop | Wall-clock and CPU timeouts; unconditional kill; orphan reaper | Timeout status | **Low** |
| T5.3 | Memory bomb | `--memory=256m`, swap disabled → OOM-kill inside the container | OOM status | **Low** |
| T5.4 | Network exfiltration / callback | `--network=none` (no interface exists, not merely firewalled) | — | **Low** |
| T5.5 | Read host filesystem | `--read-only`; no bind mounts; `tmpfs /tmp` with `noexec,nosuid`; non-root UID | — | **Low** |
| T5.6 | Write outside allowed paths | Read-only root; only `/tmp` writable, size-capped | — | **Low** |
| T5.7 | Flood stdout to exhaust disk/memory | Hard output cap; stream truncated at the reader | `output_exceeded` | **Low** |
| T5.8 | `/proc`, `/sys` abuse for host info or escape | Masked paths; `--cap-drop=ALL`; `no-new-privileges`; seccomp allowlist | — | **Medium** |
| T5.9 | Container escape via a kernel bug | Seccomp, dropped caps, non-root, pinned minimal images, prompt patching; **judge workers on a dedicated host pool** | Host IDS | **Medium — irreducible.** Namespace isolation is not a hard security boundary. A kernel 0-day defeats it. |
| T5.10 | Escape via the Docker socket | Worker talks to a **rootless** daemon or a broker; the socket is never mounted into a sandbox container | — | **Medium.** The worker itself is high-value; it is isolated from the API. |
| T5.11 | Side-channel reading another submission | One ephemeral container per submission; no shared writable volumes | — | **Low–Medium.** CPU side channels between co-tenant containers are not addressed. |

**Phase 3 gate.** The security tests for T5.1–T5.8 are written and passing *before* the
normal runner is considered complete. Until they pass, the judge is `NOT IMPLEMENTED`
regardless of whether it runs correct code correctly.

**Stronger isolation** (gVisor, Firecracker microVMs, or Kata) would meaningfully reduce
T5.9 and is [an open decision](docs/OPEN_DECISIONS.md). Docker-with-hardening is the
Phase 3 baseline because it is deployable everywhere; it is not the strongest option
available and this document does not pretend it is.

---

## T6 — Infrastructure compromise

| # | Threat | Mitigation | Detection | Residual risk |
|---|---|---|---|---|
| T6.1 | Database read access | TLS in transit, encryption at rest, least-privilege roles, no plaintext secrets in rows | DB audit | **Medium.** Read access exposes answers and metadata. Evidence bytes live in object storage, not the DB. |
| T6.2 | Database write access | Ed25519 signatures fail on any modified event; the candidate's copy is independent | Verifier | **Low for the chain, High for everything else.** Grades and answers are not chained. |
| T6.3 | Object store compromise | Digests sealed in signed events; object versioning; no public access | Verifier reports mismatch | **Low for integrity, High for confidentiality.** We can prove images were swapped; we cannot un-leak them. |
| T6.4 | Signing key compromise | KMS/HSM custody, key rotation with `not_before`/`not_after`, per-key fingerprint on every event | Key-use anomalies | **HIGH impact if it occurs.** A stolen key lets an attacker forge a chain that verifies. Events remain attributable to a key, so a revoked key marks a bounded window as untrusted — but that window's chains become worthless. |
| T6.5 | Redis compromise | Network-isolated, authenticated, no evidence or answers stored; sessions revocable | — | **Medium** |
| T6.6 | Supply-chain (dependency or base image) | Pinned versions with lockfiles, pinned image digests, SBOM, automated CVE scanning in CI | Scanner alerts | **Medium.** A compromised upstream at build time is not solved by scanning. |
| T6.7 | CI/CD compromise | Protected branches, required review, scoped deploy credentials, signed images | Deploy audit | **Medium** |

**T6.4 is the load-bearing assumption of the entire product.** Everything Sentinel claims
about tamper evidence reduces to "the private key was not stolen". This is stated in the
open rather than buried, and it drives the requirement that the key live in a KMS with
audited access and short-lived rotation.

---

## T7 — Availability

A proctoring platform failing mid-exam is a serious harm to candidates, even though it is
not a confidentiality breach.

| # | Threat | Mitigation | Residual risk |
|---|---|---|---|
| T7.1 | API outage during a live exam | Stateless API, multiple replicas, client-side offline queue, server-side recovery | **Medium.** Answers survive; the exam experience degrades. |
| T7.2 | Judge queue saturation | Isolated worker pool, autoscaling, queue-depth alerts, exam continues if the judge lags | **Medium** |
| T7.3 | Object store unavailable | Evidence upload retried from IndexedDB; failures recorded as `EVIDENCE_UPLOAD_FAILED` | **Low.** Never blocks the exam. |
| T7.4 | Database failover | Managed Postgres with replicas; short statement timeouts | **Medium** |
| T7.5 | Candidate's own connection fails | Offline queue, resumable session, server-side state, technical-issue flagging for reviewers | **Medium.** A candidate must never be penalized for an outage; reviewers see technical issues as a separate category from suspicion. |

---

## 8. Assumptions

Stated so they can be challenged:

1. TLS is correctly terminated and certificates are valid.
2. The Ed25519 private key is not accessible to application operators.
3. PostgreSQL RLS behaves as documented and the app role lacks `BYPASSRLS`.
4. Container runtime and kernel are patched.
5. Deploying organizations configure their own lawful basis and disclosure.
6. Reviewers are trained and act in good faith. Software cannot supply either.
7. Candidates are informed before monitoring begins. Sentinel enforces the disclosure
   screen; it cannot enforce that the organization's policy is fair.

---

## 9. What this threat model does not yet cover

- Formal data-flow diagrams per trust boundary (Phase 1).
- Per-jurisdiction legal analysis (GDPR Art. 9 biometric handling, FERPA, state
  biometric statutes). Deliberately out of scope for an engineering document; it needs
  counsel, and Sentinel will not claim compliance it has not obtained.
- Accessibility as a fairness threat: detectors trained predominantly on some populations
  may produce uneven false-positive rates across skin tone, eyewear, head coverings, and
  disability-related movement. Study 2 in the evaluation harness is designed to *measure*
  this. Measuring it is a commitment to publish it, including if the numbers are
  unflattering — an unmeasured false-positive rate is not a good one, it is an unknown one.
- Denial-of-service against a single candidate (targeted disruption of one exam).
- Physical security of deployment infrastructure.
