# Sentinel — Security

**Status: PHASE 0 design. Nothing below is implemented or verified.**

Companion to [`THREAT_MODEL.md`](THREAT_MODEL.md): that document lists what we defend
against, this one lists the controls.

---

## 1. Authentication and session security

| Control | Setting |
|---|---|
| Password hashing | Argon2id, m=64 MiB, t=3, p=4, 16-byte salt |
| Password policy | ≥12 chars, checked against a breached-password list, no composition rules |
| Account lockout | Exponential backoff after 5 failures; `locked_until` |
| Access token | JWT `EdDSA`, 15 min, no permissions embedded |
| Refresh token | Opaque 256-bit, 14 days, rotating, family revocation on reuse |
| Cookies | `httpOnly; Secure; SameSite=Lax; Path=/api` |
| MFA | TOTP; **mandatory for `org_admin` and `reviewer`** |
| Password reset | Single-use token, 30 min, invalidates all sessions |

No composition rules ("must contain a symbol") is deliberate: they measurably reduce
entropy by pushing users toward `Password1!`. Length plus a breach check is the current
NIST/NCSC position.

MFA is mandatory precisely for the roles that can view webcam images of people in their
homes. Those accounts are the highest-value target in the system.

---

## 2. Authorization

- Deny by default. An endpoint absent from the policy table is unreachable.
- Three layers: endpoint policy → repository scoping → PostgreSQL RLS.
- The app database role is non-owner and **`NOBYPASSRLS`**.
- Cross-tenant access returns 404, never 403.
- A (role × endpoint) matrix test covers every endpoint; a new endpoint missing from the
  matrix fails CI.

---

## 3. Key management

The single most important control in the product. If this is wrong, the tamper-evidence
claim is marketing.

| Property | Requirement |
|---|---|
| Algorithm | Ed25519 |
| Private key location | KMS/HSM in production; file-mounted secret in local dev only |
| Access | The API service account signs; **no human role can export the key** |
| Database storage | Never. `signing_key.private_key_ref` holds a KMS reference, not material |
| Rotation | Every 90 days; `not_before`/`not_after`; old public keys retained forever |
| Key identification | Every event carries `key_fingerprint`; bundles ship all relevant public keys |
| Compromise response | Revoke, rotate, mark the affected window `UNVERIFIABLE`, notify affected orgs |
| Separation of duty | The DBA must not be able to sign; the signer must not be able to rewrite rows |

That last row is the whole design. A DBA can disable a trigger and rewrite an
`audit_event` — but cannot produce a valid signature. A signer can produce signatures —
but the candidate already holds a copy of the original chain. Neither role alone can
rewrite history undetectably, and the split is the reason the audit trail means anything
to someone who does not trust the operator.

**Local development uses a file-mounted key and is explicitly NOT production-safe.**
`DEPLOYMENT.md` states this again; it is the most likely thing to be copied into
production by accident.

---

## 4. Data protection

| Data | In transit | At rest | Retention |
|---|---|---|---|
| Credentials | TLS 1.2+ | Argon2id hash | Until changed |
| Answers | TLS | DB encryption at rest | Per org policy |
| Webcam/screen evidence | TLS | SSE-KMS, private bucket | 30 days default, 90 under appeal |
| Face embeddings (P2) | TLS | Encrypted, separate key | Session duration + short grace |
| Audit events | TLS | DB encryption at rest | 10 years default |
| IP addresses | TLS | **Hashed, never stored raw** | With parent record |
| Audio | Not stored by default | — | — |

Notes on deliberate choices:

- **Audit events outlive evidence.** The chain is small and is the record that matters for
  an appeal years later; the images are large and privacy-sensitive. Purging images while
  keeping a signed record of what was captured and when it was deleted is the right
  trade-off.
- **IPs are hashed** because they are personal data with limited forensic value here; the
  session identity is already known.
- **Audio is not retained.** VAD produces an event; the waveform is discarded in the
  browser. Storing continuous audio of someone's home to detect a possible second voice is
  disproportionate to the benefit.
- **Face embeddings are biometric data** under several regimes and get their own key,
  their own retention, and their own privacy review before P2 implementation.

### Object storage

- Buckets private; anonymous access denied at the bucket policy, asserted by a test.
- Access only via short-lived presigned URLs (5 min, single-use, user-bound).
- Server-side encryption with customer-managed keys where the provider supports it.
- Versioning + object lock in production so a compromised credential cannot silently
  overwrite evidence.
- Lifecycle rules aligned to retention policy, with purge events written to the chain.

---

## 5. Judge sandbox

```
docker run
  --network=none
  --read-only
  --memory=256m --memory-swap=256m
  --cpus=1.0
  --pids-limit=64
  --cap-drop=ALL
  --security-opt=no-new-privileges
  --security-opt=seccomp=infrastructure/security/judge-seccomp.json
  --tmpfs /tmp:rw,noexec,nosuid,size=64m
  --user 65534:65534
  --ulimit nofile=64:64 --ulimit fsize=8388608
  --rm
  <image@sha256:pinned>
```

- One ephemeral container per submission. No reuse, ever.
- Compile and execute have **separate** limits; compilers legitimately need more memory
  and time than the program they produce, and merging the budgets means either a generous
  execute limit or spurious compile failures.
- Worker connects to a **rootless** Docker daemon; the socket is never exposed to a
  sandbox container.
- Judge workers run on a dedicated host pool, separate from the API.
- Base images pinned by digest, rebuilt weekly, scanned in CI.
- An orphan reaper kills any judge container exceeding its wall-clock budget, because
  defence in depth means assuming the primary kill path failed.

---

## 6. Application security

| Control | Implementation |
|---|---|
| Input validation | Pydantic v2 at every boundary; JSON Schema for event payloads and question imports |
| Output encoding | React escaping; instructor rich text sanitized with an **allowlist** |
| SQL injection | SQLAlchemy parameterization; raw SQL requires review and a test |
| CSP | `default-src 'self'`, nonce-based scripts, `object-src 'none'`, `frame-ancestors 'none'` |
| Other headers | HSTS (preload), `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Permissions-Policy` limiting camera/mic/display-capture to self |
| CORS | Explicit origin allowlist; no wildcard with credentials |
| File uploads | Evidence only; content-type and magic-byte validated; size capped; never executed or rendered as HTML |
| Dependencies | Lockfiles, Dependabot, `pip-audit` + `npm audit` in CI, SBOM per release |
| Secrets | Environment or secret manager only; `.env.example` committed, `.env` never; secret scanning in CI |
| Logging | Structured JSON; answers, evidence bytes, tokens and keys are never logged |

The instructor rich-text sanitizer is the most likely XSS vector in the product: it is the
one place where one user's content renders in another user's browser. Allowlist, not
blocklist — blocklists lose.

---

## 7. Observability for security

Alert on: repeated auth failures per account or IP, refresh-token reuse (a stolen token),
authz denials clustered on one user, evidence access outside review hours, chain
verification failures, judge container kills and timeouts, unusual export volume, RLS
errors (which indicate a code path that lost tenant context).

Structured logs carry `request_id`, `org_id`, `user_id`, `session_id`.

---

## 8. Verification plan

Nothing above may be described as working until the corresponding test passes.

| Control | Test | Status |
|---|---|---|
| Tenant isolation | Cross-org access attempts on every endpoint | NOT IMPLEMENTED |
| RLS coverage | `information_schema` sweep for `org_id` tables lacking forced RLS | NOT IMPLEMENTED (query drafted and run manually — see `docs/DATA_MODEL.md` §9) |
| Chain immutability | UPDATE/DELETE attempts on `audit_event` | **Verified at the schema level** (`docs/DATA_MODEL.md` §9, T3/T4) |
| Chain tamper detection | Modify, delete, reorder, re-sign, swap evidence — verifier must catch each | NOT IMPLEMENTED |
| Judge sandbox | Fork bomb, memory bomb, network, FS traversal, stdout flood, `/proc` | NOT IMPLEMENTED |
| Evidence authorization | Access before window close, cross-tenant, candidate-of-another-session | NOT IMPLEMENTED |
| Replay protection | Duplicate `client_seq`, sequence regression, cross-session replay | **Partially verified at the schema level** (T5) |
| No public evidence URLs | Anonymous GET against the bucket | NOT IMPLEMENTED |
| Risk cannot fail a candidate | Static check: no code path from `risk_assessment` to any score field | NOT IMPLEMENTED |

---

## 9. Vulnerability disclosure

Once deployed: a published security contact, a stated response window, safe-harbour for
good-faith research, and coordinated disclosure. Research accounts on a dedicated
environment — never against real candidate data.

---

## 10. Known security debt (Phase 0)

1. No control here has been tested. All of it is design.
2. Container isolation is not a hard security boundary; gVisor/Firecracker is an open
   decision.
3. Local development uses a file-mounted signing key that must never reach production.
4. Evidence temporal gating is enforced in application code only.
5. Insider evidence access is detective, not preventive.
6. No formal cryptographic review of the canonical-JSON and chain construction has been
   done. Before any claim of tamper evidence is made to a paying customer, this design
   should get an external review — the failure mode of a homegrown signing scheme is that
   it looks fine until someone competent looks at it.
