# Sentinel — Data Model

**Status: PHASE 0 design. No Alembic migration exists yet.**

**The DDL below has been executed against a live PostgreSQL 16.13 instance and applies
cleanly** — 40 tables, 57 CHECK constraints, 103 foreign keys, 113 indexes, 6 triggers,
zero errors. Nine behavioural assertions were then run against the live schema; all nine
behaved as designed. See [§9 Verification](#9-verification-what-was-actually-executed).

This verifies that the *schema* is valid and its constraints bite. It verifies nothing
about the application, which does not exist.

Target: PostgreSQL 16.

---

## 1. Multi-tenancy model

**Decision (approved): shared schema + `org_id` + PostgreSQL Row-Level Security.**
See [`../DECISIONS.md`](../DECISIONS.md) ADR-0001.

Three layers of defence, in order of how much I trust them:

| Layer | Mechanism | Trust |
|---|---|---|
| 1. Application authz | Explicit policy check per endpoint | Lowest — one forgotten check |
| 2. Repository scoping | Every query filtered by org context | Medium — one raw SQL bypasses it |
| 3. **Database RLS** | Postgres refuses rows outside the tenant | Highest — cannot be forgotten |

The application connects as a **non-superuser, non-`BYPASSRLS`** role. This is essential:
RLS is silently ignored for table owners and superusers. `sentinel_app` owns nothing;
`sentinel_migrate` owns the schema and is used only by Alembic.

Every request opens a transaction and sets the tenant context:

```sql
SET LOCAL sentinel.org_id = '<uuid>';
SET LOCAL sentinel.user_id = '<uuid>';
SET LOCAL sentinel.role = 'instructor';
```

`SET LOCAL` is transaction-scoped, so a pooled connection cannot leak tenant context into
the next request. This is a correctness requirement under PgBouncer transaction pooling,
not a nicety.

### The invariant test

A test enumerates `information_schema.tables`, and for every table carrying an `org_id`
column asserts `relrowsecurity = true` **and** `relforcerowsecurity = true` **and** at
least one policy exists. A new table without RLS fails CI. This converts "remember to add
RLS" from discipline into a build error.

### Cross-tenant objects

Only three things are global: `users` (an identity may belong to several organizations),
`signing_keys` (server-owned, never tenant-readable), and reference/lookup data. Nothing
tenant-owned is global.

---

## 2. Entity relationships

```
                          ┌──────────────┐
                          │ organization │
                          └──────┬───────┘
                                 │ 1:N (everything below is org-scoped)
   ┌──────────────┬──────────────┼───────────────┬──────────────────┐
   │              │              │               │                  │
┌──▼─────────┐ ┌──▼──────────┐ ┌─▼───────────┐ ┌─▼──────────────┐ ┌─▼──────────────┐
│ membership │ │question_bank│ │    exam     │ │ accommodation_ │ │ retention_     │
│  (user ↔   │ └──┬──────────┘ └─┬───────────┘ │ profile        │ │ policy         │
│   org+role)│    │              │             └────────────────┘ └────────────────┘
└──┬─────────┘    │ 1:N          │ 1:N
   │              │              │
   │        ┌─────▼────────┐ ┌───▼──────────┐
   │        │   question   │ │ exam_version │──1:N──┐
   │        └─────┬────────┘ └───┬──────────┘       │
   │              │ 1:N          │ 1:N        ┌─────▼──────────┐
   │        ┌─────▼────────────┐ │            │  exam_section  │
   │        │ question_version │ │            └─────┬──────────┘
   │        └─────┬────────────┘ │                  │ 1:N
   │              │ 1:N          │            ┌─────▼──────────┐
   │        ┌─────▼────────┐     │            │  section_pool  │
   │        │  test_case   │     │            │ (k of n from   │
   │        │ (coding q's) │     │            │  a question set)│
   │        └──────────────┘     │            └────────────────┘
   │                             │
   │  ┌──────────────────────────┘
   │  │ 1:N
┌──▼──▼─────────────┐
│ exam_assignment   │  candidate ↔ exam_version, with window + accommodation
└──┬────────────────┘
   │ 1:N
┌──▼────────────────────────────────────────────────────────────────────────┐
│                            exam_session                                   │
│  the central object: one candidate's one attempt                          │
└─┬────┬────┬────┬────┬────┬─────┬──────┬──────┬───────┬──────┬─────────────┘
  │    │    │    │    │    │     │      │      │       │      │
  │    │    │    │    │    │     │      │      │       │      └─► session_result
  │    │    │    │    │    │     │      │      │       │            └─► question_score
  │    │    │    │    │    │     │      │      │       └─► candidate_statement
  │    │    │    │    │    │     │      │      └─► review  ──► review_decision
  │    │    │    │    │    │     │      └─► risk_assessment ──► risk_contribution
  │    │    │    │    │    │     └─► evidence_object ──► evidence_access_log
  │    │    │    │    │    └─► audit_event  (THE HASH CHAIN, append-only)
  │    │    │    │    └─► session_integrity_config  (frozen snapshot of thresholds)
  │    │    │    └─► code_submission ──► judge_run ──► judge_test_result
  │    │    └─► answer  (+ answer_revision history)
  │    └─► session_paper_item  (materialized, deterministic question order)
  └─► calibration_record
```

### Why `exam_session` is the hub

Everything integrity-related hangs off the session, never off the candidate. That
containment is what makes retention purging tractable (delete a session's evidence
without touching a person's record), what makes export bundles self-contained, and what
keeps the audit chain per-session rather than global — a global chain would serialize all
writes across all tenants, which is an availability disaster and a cross-tenant
information leak.

### Why exams and questions are versioned

An in-flight session must be pinned to the exact paper it started. Editing a live exam
must not mutate an ongoing attempt, and a result released last term must remain
explainable. `exam_version` and `question_version` are immutable once published;
`exam_session` references a version, never a mutable parent.

---

## 3. Complete schema (DDL)

### 3.0 Extensions, roles, helpers

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid, digest
CREATE EXTENSION IF NOT EXISTS citext;     -- case-insensitive email

-- Roles. sentinel_app MUST NOT own tables and MUST NOT have BYPASSRLS.
-- CREATE ROLE sentinel_migrate LOGIN PASSWORD :'migrate_pw';
-- CREATE ROLE sentinel_app     LOGIN PASSWORD :'app_pw' NOBYPASSRLS;

CREATE OR REPLACE FUNCTION current_org_id() RETURNS uuid
LANGUAGE sql STABLE AS $$
  SELECT NULLIF(current_setting('sentinel.org_id', true), '')::uuid
$$;

CREATE OR REPLACE FUNCTION current_user_id() RETURNS uuid
LANGUAGE sql STABLE AS $$
  SELECT NULLIF(current_setting('sentinel.user_id', true), '')::uuid
$$;

CREATE OR REPLACE FUNCTION current_role_name() RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT NULLIF(current_setting('sentinel.role', true), '')
$$;

-- Guard used by triggers on append-only tables.
CREATE OR REPLACE FUNCTION deny_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'Table % is append-only; % is not permitted',
    TG_TABLE_NAME, TG_OP USING ERRCODE = 'restrict_violation';
END $$;
```

### 3.1 Enumerated types

```sql
CREATE TYPE org_status        AS ENUM ('active','suspended','closed');
CREATE TYPE member_role       AS ENUM ('org_admin','instructor','reviewer','candidate');
CREATE TYPE member_status     AS ENUM ('invited','active','disabled');

CREATE TYPE question_kind     AS ENUM ('single_choice','multiple_choice','short_answer',
                                       'numeric','coding');
CREATE TYPE publish_status    AS ENUM ('draft','published','archived');

CREATE TYPE exam_delivery     AS ENUM ('scheduled','window','on_demand');
CREATE TYPE session_status    AS ENUM ('created','calibrating','in_progress','paused',
                                       'submitted','expired','abandoned','voided');
CREATE TYPE submit_reason     AS ENUM ('candidate','timer','proctor','system');

CREATE TYPE judge_status      AS ENUM ('queued','running','completed','compile_error',
                                       'runtime_error','timeout','memory_exceeded',
                                       'output_exceeded','internal_error','cancelled');
CREATE TYPE judge_language    AS ENUM ('python311','cpp20','java17');
CREATE TYPE run_mode          AS ENUM ('sample','final');

CREATE TYPE evidence_kind     AS ENUM ('webcam_frame','screen_frame','webcam_clip',
                                       'enrollment_frame');
CREATE TYPE evidence_state    AS ENUM ('pending','stored','verified','failed','purged');

CREATE TYPE detector_id       AS ENUM ('browser','face_presence','face_count','head_pose',
                                       'gaze_proxy','object_detect','identity','audio_vad',
                                       'clipboard','network','server');

CREATE TYPE review_state      AS ENUM ('not_required','queued','in_review','decided');
CREATE TYPE review_outcome    AS ENUM ('dismiss','note_concern','escalate');
CREATE TYPE chain_state       AS ENUM ('unverified','verified','failed');
```

**Note on event types:** deliberately *not* an enum. Detector taxonomies change often, and
`ALTER TYPE ... ADD VALUE` cannot run inside a transaction with other DDL, which makes
migrations awkward. Event types live in a reference table (`event_type`) with FK
enforcement — same integrity, better evolution.

### 3.2 Organizations and identity

```sql
CREATE TABLE organization (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug           citext NOT NULL UNIQUE,
  name           text   NOT NULL,
  status         org_status NOT NULL DEFAULT 'active',
  -- billing/plan hooks: architecture-ready, NOT implemented in P0/P1
  plan_code      text,
  settings       jsonb  NOT NULL DEFAULT '{}'::jsonb,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT organization_slug_len CHECK (length(slug) BETWEEN 2 AND 64)
);

-- Global identity. A person may belong to several organizations with one login.
CREATE TABLE app_user (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email              citext NOT NULL UNIQUE,
  email_verified_at  timestamptz,
  display_name       text NOT NULL,
  password_hash      text,               -- NULL when the user is OIDC-only
  password_changed_at timestamptz,
  mfa_secret_enc     bytea,              -- envelope-encrypted TOTP secret
  mfa_enabled_at     timestamptz,
  failed_logins      int NOT NULL DEFAULT 0,
  locked_until       timestamptz,
  last_login_at      timestamptz,
  external_subject   text,               -- OIDC 'sub' seam, unused in P0
  external_issuer    text,
  status             member_status NOT NULL DEFAULT 'active',
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT app_user_auth_present CHECK (
    password_hash IS NOT NULL OR external_subject IS NOT NULL
  ),
  CONSTRAINT app_user_external_pair CHECK (
    (external_subject IS NULL) = (external_issuer IS NULL)
  )
);
CREATE UNIQUE INDEX app_user_external_uq
  ON app_user (external_issuer, external_subject)
  WHERE external_subject IS NOT NULL;

-- Tenant-scoped role grant. One user may hold several roles in one org.
CREATE TABLE membership (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id       uuid NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  user_id      uuid NOT NULL REFERENCES app_user(id)     ON DELETE CASCADE,
  role         member_role NOT NULL,
  status       member_status NOT NULL DEFAULT 'active',
  -- org-local identifier: student number, employee id, candidate ref
  external_ref text,
  invited_by   uuid REFERENCES app_user(id),
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, user_id, role)
);
CREATE UNIQUE INDEX membership_external_ref_uq
  ON membership (org_id, external_ref) WHERE external_ref IS NOT NULL;
CREATE INDEX membership_user_idx ON membership (user_id);
CREATE INDEX membership_org_role_idx ON membership (org_id, role) WHERE status = 'active';

-- Refresh tokens: stored hashed, rotating, with reuse detection.
CREATE TABLE refresh_token (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id        uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  family_id      uuid NOT NULL,           -- rotation family; reuse revokes the family
  token_hash     bytea NOT NULL UNIQUE,   -- SHA-256 of the opaque token
  issued_at      timestamptz NOT NULL DEFAULT now(),
  expires_at     timestamptz NOT NULL,
  revoked_at     timestamptz,
  replaced_by    uuid REFERENCES refresh_token(id),
  user_agent     text,
  ip_hash        bytea,                   -- hashed, never raw IP at rest
  CONSTRAINT refresh_token_window CHECK (expires_at > issued_at)
);
CREATE INDEX refresh_token_family_idx ON refresh_token (family_id);
CREATE INDEX refresh_token_expiry_idx ON refresh_token (expires_at)
  WHERE revoked_at IS NULL;
```

### 3.3 Question bank

```sql
CREATE TABLE question_bank (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id      uuid NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  name        text NOT NULL,
  description text,
  created_by  uuid NOT NULL REFERENCES app_user(id),
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, name)
);

CREATE TABLE question (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id         uuid NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  bank_id        uuid NOT NULL REFERENCES question_bank(id) ON DELETE CASCADE,
  kind           question_kind NOT NULL,
  external_key   text,                     -- stable key for re-import/update
  current_version_id uuid,                 -- FK added after question_version exists
  created_by     uuid NOT NULL REFERENCES app_user(id),
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX question_external_key_uq
  ON question (org_id, bank_id, external_key) WHERE external_key IS NOT NULL;
CREATE INDEX question_bank_kind_idx ON question (bank_id, kind);

-- Immutable once published. Editing creates a new version.
CREATE TABLE question_version (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id          uuid NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  question_id     uuid NOT NULL REFERENCES question(id) ON DELETE CASCADE,
  version         int  NOT NULL,
  status          publish_status NOT NULL DEFAULT 'draft',
  schema_version  text NOT NULL DEFAULT 'question.v1',
  prompt          text NOT NULL,
  prompt_format   text NOT NULL DEFAULT 'markdown',
  -- Type-specific body, validated against packages/schemas/versions/v1/question.json
  --  single/multiple_choice: {options:[{id,text}], correct:[id], shuffle_options:bool}
  --  short_answer:           {accepted:[...], match:'exact'|'ci'|'regex', trim:bool}
  --  numeric:                {value:number, tolerance:number, tolerance_kind:'abs'|'rel',
  --                           unit:string|null}
  --  coding:                 {languages:[...], starter:{lang:code}, time_limit_ms,
  --                           memory_limit_mb, stdout_limit_kb}
  body            jsonb NOT NULL,
  explanation     text,
  marks           numeric(8,3) NOT NULL,
  negative_marks  numeric(8,3) NOT NULL DEFAULT 0,
  partial_credit  boolean NOT NULL DEFAULT false,
  difficulty      smallint,
  tags            text[] NOT NULL DEFAULT '{}',
  metadata        jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_by      uuid NOT NULL REFERENCES app_user(id),
  created_at      timestamptz NOT NULL DEFAULT now(),
  published_at    timestamptz,
  UNIQUE (question_id, version),
  CONSTRAINT qv_marks_positive     CHECK (marks > 0),
  CONSTRAINT qv_negative_sane      CHECK (negative_marks >= 0 AND negative_marks <= marks),
  CONSTRAINT qv_difficulty_range   CHECK (difficulty IS NULL OR difficulty BETWEEN 1 AND 5),
  CONSTRAINT qv_published_has_time CHECK (status <> 'published' OR published_at IS NOT NULL)
);
CREATE INDEX question_version_q_idx ON question_version (question_id, version DESC);
CREATE INDEX question_version_tags_idx ON question_version USING gin (tags);

ALTER TABLE question
  ADD CONSTRAINT question_current_version_fk
  FOREIGN KEY (current_version_id) REFERENCES question_version(id)
  DEFERRABLE INITIALLY DEFERRED;

-- Coding questions only.
CREATE TABLE test_case (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id            uuid NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  question_version_id uuid NOT NULL REFERENCES question_version(id) ON DELETE CASCADE,
  ordinal           int  NOT NULL,
  is_sample         boolean NOT NULL DEFAULT false,   -- sample = visible to candidate
  weight            numeric(8,3) NOT NULL DEFAULT 1,
  stdin_ref         text,          -- object key; large inputs are not stored inline
  stdin_inline      text,
  expected_ref      text,
  expected_inline   text,
  comparator        text NOT NULL DEFAULT 'trim_exact',  -- trim_exact|token|float_eps|custom
  comparator_config jsonb NOT NULL DEFAULT '{}'::jsonb,
  time_limit_ms     int,           -- NULL inherits the question default
  memory_limit_mb   int,
  UNIQUE (question_version_id, ordinal),
  CONSTRAINT tc_weight_positive CHECK (weight > 0),
  CONSTRAINT tc_stdin_one_of    CHECK (num_nonnulls(stdin_ref, stdin_inline) <= 1),
  CONSTRAINT tc_expected_one_of CHECK (num_nonnulls(expected_ref, expected_inline) = 1)
);
CREATE INDEX test_case_qv_idx ON test_case (question_version_id, ordinal);
```

### 3.4 Exams, sections, pools

```sql
CREATE TABLE exam (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id       uuid NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  title        text NOT NULL,
  description  text,
  owner_id     uuid NOT NULL REFERENCES app_user(id),
  current_version_id uuid,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX exam_org_idx ON exam (org_id, created_at DESC);

CREATE TABLE exam_version (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id             uuid NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  exam_id            uuid NOT NULL REFERENCES exam(id) ON DELETE CASCADE,
  version            int  NOT NULL,
  status             publish_status NOT NULL DEFAULT 'draft',
  delivery           exam_delivery NOT NULL DEFAULT 'window',
  duration_seconds   int  NOT NULL,
  grace_seconds      int  NOT NULL DEFAULT 60,
  opens_at           timestamptz,
  closes_at          timestamptz,
  -- Exam window state gates examiner access to evidence (enforced in the API,
  -- not only the UI).
  results_release_at timestamptz,
  max_attempts       int NOT NULL DEFAULT 1,
  navigation         text NOT NULL DEFAULT 'free',   -- free | sequential | one_way
  show_score_on_submit boolean NOT NULL DEFAULT false,
  integrity_enabled  boolean NOT NULL DEFAULT false,
  integrity_config   jsonb NOT NULL DEFAULT '{}'::jsonb,  -- detector thresholds
  judge_enabled      boolean NOT NULL DEFAULT false,
  published_at       timestamptz,
  published_by       uuid REFERENCES app_user(id),
  created_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (exam_id, version),
  CONSTRAINT ev_duration_positive CHECK (duration_seconds BETWEEN 60 AND 86400),
  CONSTRAINT ev_grace_range       CHECK (grace_seconds BETWEEN 0 AND 1800),
  CONSTRAINT ev_window_order      CHECK (opens_at IS NULL OR closes_at IS NULL
                                         OR closes_at > opens_at),
  CONSTRAINT ev_attempts_positive CHECK (max_attempts >= 1),
  CONSTRAINT ev_navigation_valid  CHECK (navigation IN ('free','sequential','one_way')),
  CONSTRAINT ev_published_frozen  CHECK (status <> 'published' OR published_at IS NOT NULL)
);
CREATE INDEX exam_version_exam_idx ON exam_version (exam_id, version DESC);

ALTER TABLE exam
  ADD CONSTRAINT exam_current_version_fk
  FOREIGN KEY (current_version_id) REFERENCES exam_version(id)
  DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE exam_section (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id          uuid NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  exam_version_id uuid NOT NULL REFERENCES exam_version(id) ON DELETE CASCADE,
  ordinal         int  NOT NULL,
  title           text NOT NULL,
  instructions    text,
  shuffle_questions boolean NOT NULL DEFAULT true,
  time_limit_seconds int,          -- optional per-section limit
  UNIQUE (exam_version_id, ordinal),
  CONSTRAINT section_time_positive CHECK (time_limit_seconds IS NULL
                                          OR time_limit_seconds > 0)
);

-- "Select k questions from this pool of n."
CREATE TABLE section_pool (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id       uuid NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  section_id   uuid NOT NULL REFERENCES exam_section(id) ON DELETE CASCADE,
  ordinal      int  NOT NULL,
  label        text,
  select_count int  NOT NULL,          -- k
  -- Selection source: an explicit list, or a tag/difficulty filter over a bank
  source_kind  text NOT NULL DEFAULT 'explicit',   -- explicit | filter
  filter       jsonb NOT NULL DEFAULT '{}'::jsonb, -- {bank_id, tags[], difficulty, kind}
  UNIQUE (section_id, ordinal),
  CONSTRAINT pool_k_positive CHECK (select_count >= 1),
  CONSTRAINT pool_source_valid CHECK (source_kind IN ('explicit','filter'))
);

CREATE TABLE section_pool_item (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id              uuid NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  pool_id             uuid NOT NULL REFERENCES section_pool(id) ON DELETE CASCADE,
  question_version_id uuid NOT NULL REFERENCES question_version(id) ON DELETE RESTRICT,
  ordinal             int  NOT NULL,
  UNIQUE (pool_id, question_version_id),
  UNIQUE (pool_id, ordinal)
);
CREATE INDEX section_pool_item_pool_idx ON section_pool_item (pool_id, ordinal);
```

### 3.5 Assignment, sessions, papers, answers

```sql
CREATE TABLE accommodation_profile (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id      uuid NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  name        text NOT NULL,
  description text,
  -- multipliers/overrides applied to integrity_config, plus extra_time_ratio
  overrides   jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, name)
);

CREATE TABLE exam_assignment (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id           uuid NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  exam_version_id  uuid NOT NULL REFERENCES exam_version(id) ON DELETE RESTRICT,
  candidate_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
  accommodation_id uuid REFERENCES accommodation_profile(id),
  opens_at         timestamptz,      -- per-candidate override of the exam window
  closes_at        timestamptz,
  extra_time_seconds int NOT NULL DEFAULT 0,
  attempts_allowed int NOT NULL DEFAULT 1,
  assigned_by      uuid NOT NULL REFERENCES app_user(id),
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (exam_version_id, candidate_user_id),
  CONSTRAINT assignment_extra_time_sane CHECK (extra_time_seconds BETWEEN 0 AND 86400)
);
CREATE INDEX assignment_candidate_idx ON exam_assignment (candidate_user_id);

CREATE TABLE exam_session (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id            uuid NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  assignment_id     uuid NOT NULL REFERENCES exam_assignment(id) ON DELETE RESTRICT,
  exam_version_id   uuid NOT NULL REFERENCES exam_version(id) ON DELETE RESTRICT,
  candidate_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
  attempt_no        int  NOT NULL DEFAULT 1,
  status            session_status NOT NULL DEFAULT 'created',

  -- The server clock is the only clock that matters.
  paper_seed        bytea NOT NULL,              -- SHA-256(exam_id||candidate_id||version)
  started_at        timestamptz,
  deadline_at       timestamptz,                 -- computed server-side at start
  submitted_at      timestamptz,
  submit_reason     submit_reason,
  last_seen_at      timestamptz,

  -- Audit chain head, maintained transactionally with audit_event inserts.
  chain_head_seq    bigint NOT NULL DEFAULT 0,
  chain_head_hash   bytea,
  signing_key_id    uuid,                        -- FK added below
  chain_status      chain_state NOT NULL DEFAULT 'unverified',
  chain_verified_at timestamptz,

  evidence_count    int  NOT NULL DEFAULT 0,
  evidence_cap      int  NOT NULL DEFAULT 40,
  evidence_capped_at timestamptz,

  review_status     review_state NOT NULL DEFAULT 'not_required',
  user_agent        text,
  ip_hash           bytea,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),

  UNIQUE (assignment_id, attempt_no),
  CONSTRAINT session_started_has_deadline CHECK (
    status IN ('created','calibrating') OR (started_at IS NOT NULL AND deadline_at IS NOT NULL)
  ),
  CONSTRAINT session_deadline_after_start CHECK (
    deadline_at IS NULL OR started_at IS NULL OR deadline_at > started_at
  ),
  CONSTRAINT session_submitted_has_reason CHECK (
    (submitted_at IS NULL) = (submit_reason IS NULL)
  ),
  CONSTRAINT session_evidence_cap_sane CHECK (evidence_count >= 0
                                              AND evidence_cap BETWEEN 0 AND 500)
);
CREATE INDEX session_org_status_idx  ON exam_session (org_id, status);
CREATE INDEX session_exam_idx        ON exam_session (exam_version_id, status);
CREATE INDEX session_candidate_idx   ON exam_session (candidate_user_id, created_at DESC);
CREATE INDEX session_review_idx      ON exam_session (org_id, review_status)
  WHERE review_status IN ('queued','in_review');
CREATE INDEX session_active_idx      ON exam_session (deadline_at)
  WHERE status = 'in_progress';

-- Frozen copy of the thresholds actually in force, after accommodation overrides.
-- Without this, a later config edit makes historical flags unexplainable.
CREATE TABLE session_integrity_config (
  session_id     uuid PRIMARY KEY REFERENCES exam_session(id) ON DELETE CASCADE,
  org_id         uuid NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  config         jsonb NOT NULL,
  accommodation_id uuid REFERENCES accommodation_profile(id),
  config_hash    bytea NOT NULL,          -- SHA-256 of canonical JSON, sealed in the chain
  created_at     timestamptz NOT NULL DEFAULT now()
);

-- The materialized paper: what this candidate actually got, in order.
CREATE TABLE session_paper_item (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id              uuid NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  session_id          uuid NOT NULL REFERENCES exam_session(id) ON DELETE CASCADE,
  section_id          uuid NOT NULL REFERENCES exam_section(id) ON DELETE RESTRICT,
  pool_id             uuid REFERENCES section_pool(id) ON DELETE RESTRICT,
  question_version_id uuid NOT NULL REFERENCES question_version(id) ON DELETE RESTRICT,
  section_ordinal     int NOT NULL,
  item_ordinal        int NOT NULL,       -- position within the whole paper
  option_order        int[],              -- shuffled option indices, if applicable
  marks               numeric(8,3) NOT NULL,
  UNIQUE (session_id, item_ordinal),
  UNIQUE (session_id, question_version_id)
);
CREATE INDEX paper_item_session_idx ON session_paper_item (session_id, item_ordinal);

CREATE TABLE answer (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id           uuid NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  session_id       uuid NOT NULL REFERENCES exam_session(id) ON DELETE CASCADE,
  paper_item_id    uuid NOT NULL REFERENCES session_paper_item(id) ON DELETE CASCADE,
  revision         int  NOT NULL DEFAULT 1,
  value            jsonb NOT NULL,      -- shape depends on question kind
  is_flagged_by_candidate boolean NOT NULL DEFAULT false,
  time_spent_ms    bigint NOT NULL DEFAULT 0,
  client_saved_at  timestamptz,
  saved_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (session_id, paper_item_id),
  CONSTRAINT answer_revision_positive CHECK (revision >= 1),
  CONSTRAINT answer_time_sane CHECK (time_spent_ms >= 0)
);
CREATE INDEX answer_session_idx ON answer (session_id);

-- Append-only history. Recovering a session and proving what was saved when both
-- depend on this existing.
CREATE TABLE answer_revision (
  id            bigserial PRIMARY KEY,
  org_id        uuid NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  session_id    uuid NOT NULL REFERENCES exam_session(id) ON DELETE CASCADE,
  paper_item_id uuid NOT NULL REFERENCES session_paper_item(id) ON DELETE CASCADE,
  revision      int  NOT NULL,
  value         jsonb NOT NULL,
  saved_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (session_id, paper_item_id, revision)
);
CREATE INDEX answer_revision_session_idx ON answer_revision (session_id, saved_at);
CREATE TRIGGER answer_revision_no_update BEFORE UPDATE OR DELETE ON answer_revision
  FOR EACH ROW EXECUTE FUNCTION deny_mutation();

CREATE TABLE calibration_record (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id          uuid NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  session_id      uuid NOT NULL REFERENCES exam_session(id) ON DELETE CASCADE,
  camera_ok       boolean NOT NULL,
  lighting_score  numeric(4,3),
  face_detected   boolean NOT NULL,
  warnings        text[] NOT NULL DEFAULT '{}',
  raw             jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT calibration_lighting_range CHECK (
    lighting_score IS NULL OR lighting_score BETWEEN 0 AND 1)
);
```

### 3.6 Code judge

```sql
CREATE TABLE code_submission (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id            uuid NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  session_id        uuid NOT NULL REFERENCES exam_session(id) ON DELETE CASCADE,
  paper_item_id     uuid NOT NULL REFERENCES session_paper_item(id) ON DELETE CASCADE,
  language          judge_language NOT NULL,
  source            text NOT NULL,
  source_sha256     bytea NOT NULL,
  source_bytes      int  NOT NULL,
  mode              run_mode NOT NULL DEFAULT 'sample',
  created_at        timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT submission_size_cap CHECK (source_bytes > 0 AND source_bytes <= 262144)
);
CREATE INDEX submission_session_idx ON code_submission (session_id, created_at DESC);
CREATE INDEX submission_item_idx    ON code_submission (paper_item_id, created_at DESC);

CREATE TABLE judge_run (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id            uuid NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  submission_id     uuid NOT NULL REFERENCES code_submission(id) ON DELETE CASCADE,
  status            judge_status NOT NULL DEFAULT 'queued',
  worker_id         text,
  image_digest      text,                -- pinned sandbox image actually used
  queued_at         timestamptz NOT NULL DEFAULT now(),
  started_at        timestamptz,
  finished_at       timestamptz,
  queue_delay_ms    int,
  compile_ms        int,
  execute_ms        int,
  peak_memory_kb    int,
  compile_output    text,                -- truncated server-side
  error_detail      text,
  tests_total       int NOT NULL DEFAULT 0,
  tests_passed      int NOT NULL DEFAULT 0,
  score             numeric(8,3),
  CONSTRAINT judge_run_counts CHECK (tests_passed >= 0 AND tests_passed <= tests_total),
  CONSTRAINT judge_run_times  CHECK (finished_at IS NULL OR started_at IS NULL
                                     OR finished_at >= started_at)
);
CREATE INDEX judge_run_status_idx ON judge_run (status, queued_at)
  WHERE status IN ('queued','running');
CREATE INDEX judge_run_submission_idx ON judge_run (submission_id);

CREATE TABLE judge_test_result (
  id            bigserial PRIMARY KEY,
  org_id        uuid NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  run_id        uuid NOT NULL REFERENCES judge_run(id) ON DELETE CASCADE,
  test_case_id  uuid NOT NULL REFERENCES test_case(id) ON DELETE RESTRICT,
  ordinal       int  NOT NULL,
  passed        boolean NOT NULL,
  status        judge_status NOT NULL,
  duration_ms   int,
  memory_kb     int,
  -- Output is stored only for sample tests. Hidden-test output would leak the answers.
  stdout_excerpt text,
  stderr_excerpt text,
  UNIQUE (run_id, test_case_id)
);
CREATE INDEX judge_test_result_run_idx ON judge_test_result (run_id, ordinal);
```

### 3.7 The audit chain

This is the heart of the product. `audit_event` is **append-only, enforced by trigger**,
not by convention.

```sql
-- Reference table rather than an enum: taxonomies evolve, enums migrate badly.
CREATE TABLE event_type (
  code            text PRIMARY KEY,
  category        text NOT NULL,      -- session | browser | vision | evidence |
                                      -- judge | review | admin | system
  default_severity smallint NOT NULL DEFAULT 1,
  warrants_evidence boolean NOT NULL DEFAULT false,
  evidence_kind   evidence_kind,
  description     text NOT NULL,
  CONSTRAINT event_type_severity_range CHECK (default_severity BETWEEN 0 AND 4)
);

INSERT INTO event_type (code, category, default_severity, warrants_evidence, evidence_kind, description) VALUES
 ('SESSION_STARTED',        'session',  0, false, NULL,           'Session start sealed with paper seed and config hash'),
 ('SESSION_RESUMED',        'session',  0, false, NULL,           'Session resumed after reload or reconnect'),
 ('SESSION_SUBMITTED',      'session',  0, false, NULL,           'Submission accepted by the server'),
 ('CALIBRATION_COMPLETED',  'session',  0, false, NULL,           'Pre-exam environment check result'),
 ('FULLSCREEN_EXIT',        'browser',  2, true,  'screen_frame', 'Document left fullscreen'),
 ('FULLSCREEN_ENTER',       'browser',  0, false, NULL,           'Document entered fullscreen'),
 ('TAB_HIDDEN',             'browser',  2, true,  'screen_frame', 'visibilitychange to hidden'),
 ('TAB_VISIBLE',            'browser',  0, false, NULL,           'visibilitychange to visible'),
 ('WINDOW_BLUR',            'browser',  2, true,  'screen_frame', 'Window lost focus'),
 ('WINDOW_FOCUS',           'browser',  0, false, NULL,           'Window regained focus'),
 ('MULTI_MONITOR_DETECTED', 'browser',  2, true,  'screen_frame', 'screen.isExtended reported an extended desktop'),
 ('LARGE_PASTE',            'browser',  3, true,  'screen_frame', 'Paste exceeding the configured character threshold'),
 ('COPY_DETECTED',          'browser',  1, false, NULL,           'Copy event on exam content'),
 ('NETWORK_DISCONNECT',     'browser',  1, false, NULL,           'Client lost connectivity'),
 ('NETWORK_RECONNECT',      'browser',  0, false, NULL,           'Client regained connectivity'),
 ('FACE_ABSENT',            'vision',   2, true,  'webcam_frame', 'No face detected beyond the dwell threshold'),
 ('FACE_RETURNED',          'vision',   0, false, NULL,           'Face detection recovered'),
 ('MULTIPLE_FACES',         'vision',   3, true,  'webcam_frame', 'More than one face beyond the dwell threshold'),
 ('OFFSCREEN_GAZE',         'vision',   2, true,  'webcam_frame', 'Sustained gaze proxy away from the screen'),
 ('OBJECT_DETECTED',        'vision',   3, true,  'webcam_frame', 'Configured object class detected (phone, paper, earbuds)'),
 ('IDENTITY_MISMATCH',      'vision',   3, true,  'webcam_frame', 'Face embedding distance from enrollment exceeded threshold'),
 ('AUDIO_VOICE_DETECTED',   'vision',   1, false, NULL,           'Voice activity detected (no audio stored by default)'),
 ('EVIDENCE_BOUND',         'evidence', 0, false, NULL,           'Evidence object digest bound to a prior event'),
 ('EVIDENCE_UPLOAD_FAILED', 'evidence', 1, false, NULL,           'Evidence upload did not complete'),
 ('CAPTURE_LIMIT_REACHED',  'evidence', 1, false, NULL,           'Session evidence cap reached; uploads stopped, events continue'),
 ('EVIDENCE_PURGED',        'evidence', 0, false, NULL,           'Evidence deleted under the retention policy'),
 ('CODE_SUBMITTED',         'judge',    0, false, NULL,           'Code submission accepted'),
 ('REVIEW_DECISION',        'review',   0, false, NULL,           'Reviewer decision with justification'),
 ('EVIDENCE_ACCESSED',      'review',   0, false, NULL,           'Staff member viewed evidence'),
 ('DETECTOR_ERROR',         'system',   1, false, NULL,           'A detector failed; absence of events is not evidence of absence'),
 ('CLOCK_ANOMALY',          'system',   1, false, NULL,           'Client-reported time diverged from the server beyond tolerance');

CREATE TABLE signing_key (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  algorithm     text NOT NULL DEFAULT 'ed25519',
  public_key    bytea NOT NULL,          -- raw 32-byte Ed25519 public key
  key_fingerprint text NOT NULL UNIQUE,  -- base64url(SHA-256(public_key))
  -- The private key is NEVER stored here. It lives in a KMS/HSM or a mounted
  -- secret readable only by the API service account. See SECURITY.md.
  private_key_ref text NOT NULL,
  not_before    timestamptz NOT NULL DEFAULT now(),
  not_after     timestamptz,
  revoked_at    timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT signing_key_alg CHECK (algorithm = 'ed25519')
);

ALTER TABLE exam_session
  ADD CONSTRAINT session_signing_key_fk
  FOREIGN KEY (signing_key_id) REFERENCES signing_key(id);

CREATE TABLE audit_event (
  id             bigserial PRIMARY KEY,
  org_id         uuid   NOT NULL REFERENCES organization(id) ON DELETE RESTRICT,
  session_id     uuid   NOT NULL REFERENCES exam_session(id) ON DELETE RESTRICT,

  server_seq     bigint NOT NULL,        -- authoritative; the chain's spine
  server_ts      timestamptz NOT NULL DEFAULT clock_timestamp(),

  -- Untrusted client claims, retained for gap and latency analysis only.
  client_seq     bigint,
  client_ts      timestamptz,
  is_late        boolean NOT NULL DEFAULT false,
  arrival_delay_ms int,

  event_type     text NOT NULL REFERENCES event_type(code),
  detector       detector_id NOT NULL DEFAULT 'browser',
  severity       smallint NOT NULL DEFAULT 1,
  confidence     numeric(4,3),
  payload        jsonb NOT NULL DEFAULT '{}'::jsonb,

  -- Exam position at the time of the event. Deliberately coarse: no answer content.
  section_ordinal int,
  item_ordinal    int,
  elapsed_ms      bigint,

  -- Chain
  canonical_sha256 bytea NOT NULL,       -- SHA-256 over RFC 8785 canonical JSON
  prev_hash        bytea,                -- NULL only for server_seq = 1
  signature        bytea NOT NULL,       -- Ed25519 over canonical_sha256
  signing_key_id   uuid NOT NULL REFERENCES signing_key(id),

  UNIQUE (session_id, server_seq),
  CONSTRAINT audit_seq_positive     CHECK (server_seq >= 1),
  CONSTRAINT audit_genesis_prev     CHECK ((server_seq = 1) = (prev_hash IS NULL)),
  CONSTRAINT audit_hash_len         CHECK (octet_length(canonical_sha256) = 32),
  CONSTRAINT audit_prev_len         CHECK (prev_hash IS NULL OR octet_length(prev_hash) = 32),
  CONSTRAINT audit_sig_len          CHECK (octet_length(signature) = 64),
  CONSTRAINT audit_confidence_range CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
  CONSTRAINT audit_severity_range   CHECK (severity BETWEEN 0 AND 4),
  CONSTRAINT audit_delay_sane       CHECK (arrival_delay_ms IS NULL OR arrival_delay_ms >= 0)
);
-- Dedupe / replay rejection on the client's claimed sequence.
CREATE UNIQUE INDEX audit_client_seq_uq ON audit_event (session_id, client_seq)
  WHERE client_seq IS NOT NULL;
CREATE INDEX audit_session_seq_idx  ON audit_event (session_id, server_seq);
CREATE INDEX audit_type_idx         ON audit_event (org_id, event_type, server_ts DESC);
CREATE INDEX audit_severity_idx     ON audit_event (session_id, severity DESC)
  WHERE severity >= 2;
CREATE INDEX audit_payload_gin      ON audit_event USING gin (payload jsonb_path_ops);

-- Append-only, enforced. Even a bug cannot silently rewrite history.
CREATE TRIGGER audit_event_immutable BEFORE UPDATE OR DELETE ON audit_event
  FOR EACH ROW EXECUTE FUNCTION deny_mutation();
```

**Concurrency note.** `server_seq` must be gapless and race-free per session. It is
allocated inside the ingest transaction via
`SELECT chain_head_seq FROM exam_session WHERE id = :sid FOR UPDATE`, which serializes
appends per session while leaving different sessions fully parallel. A global sequence
would be a platform-wide bottleneck; `bigserial` alone would leave gaps on rollback and
break the chain.

### 3.8 Evidence

```sql
CREATE TABLE evidence_object (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id          uuid NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  session_id      uuid NOT NULL REFERENCES exam_session(id) ON DELETE CASCADE,
  -- The event that caused capture. Set once, never repointed.
  audit_event_seq bigint,
  kind            evidence_kind NOT NULL,
  state           evidence_state NOT NULL DEFAULT 'pending',

  storage_bucket  text NOT NULL,
  storage_key     text NOT NULL,
  content_type    text NOT NULL DEFAULT 'image/jpeg',
  byte_size       int,
  width           int,
  height          int,

  sha256          bytea,          -- computed server-side on confirm, not trusted from client
  phash           bit(64),        -- perceptual hash for near-duplicate suppression
  duplicate_of    uuid REFERENCES evidence_object(id),

  -- 'primary' = frame at the event; 'context' = the ~1.5s-prior frame
  role            text NOT NULL DEFAULT 'primary',
  captured_at     timestamptz NOT NULL,
  uploaded_at     timestamptz,
  purge_after     timestamptz,
  purged_at       timestamptz,

  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (storage_bucket, storage_key),
  CONSTRAINT evidence_role_valid CHECK (role IN ('primary','context','enrollment')),
  CONSTRAINT evidence_sha_len CHECK (sha256 IS NULL OR octet_length(sha256) = 32),
  CONSTRAINT evidence_stored_has_hash CHECK (
    state NOT IN ('stored','verified') OR (sha256 IS NOT NULL AND byte_size IS NOT NULL)
  ),
  CONSTRAINT evidence_purged_consistent CHECK (
    (state = 'purged') = (purged_at IS NOT NULL)
  ),
  CONSTRAINT evidence_not_self_duplicate CHECK (duplicate_of IS NULL OR duplicate_of <> id)
);
ALTER TABLE evidence_object
  ADD CONSTRAINT evidence_event_fk
  FOREIGN KEY (session_id, audit_event_seq)
  REFERENCES audit_event (session_id, server_seq) ON DELETE RESTRICT;

CREATE INDEX evidence_session_idx ON evidence_object (session_id, captured_at);
CREATE INDEX evidence_purge_idx   ON evidence_object (purge_after)
  WHERE purged_at IS NULL;
CREATE INDEX evidence_phash_idx   ON evidence_object (session_id, kind, phash);

-- Every read of evidence is itself auditable. Insider access is a real threat.
CREATE TABLE evidence_access_log (
  id             bigserial PRIMARY KEY,
  org_id         uuid NOT NULL REFERENCES organization(id) ON DELETE RESTRICT,
  evidence_id    uuid NOT NULL REFERENCES evidence_object(id) ON DELETE RESTRICT,
  session_id     uuid NOT NULL REFERENCES exam_session(id) ON DELETE RESTRICT,
  actor_user_id  uuid NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
  actor_role     member_role NOT NULL,
  action         text NOT NULL,        -- url_issued | downloaded | exported
  purpose        text,                 -- review_id or export job reference
  ip_hash        bytea,
  accessed_at    timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT evidence_access_action CHECK (action IN ('url_issued','downloaded','exported'))
);
CREATE INDEX evidence_access_evidence_idx ON evidence_access_log (evidence_id, accessed_at DESC);
CREATE INDEX evidence_access_actor_idx    ON evidence_access_log (actor_user_id, accessed_at DESC);
CREATE TRIGGER evidence_access_immutable BEFORE UPDATE OR DELETE ON evidence_access_log
  FOR EACH ROW EXECUTE FUNCTION deny_mutation();

CREATE TABLE retention_policy (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id                uuid NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  name                  text NOT NULL,
  evidence_days         int NOT NULL DEFAULT 30,
  appeal_extension_days int NOT NULL DEFAULT 60,   -- 30 + 60 = 90 under appeal
  audit_event_days      int NOT NULL DEFAULT 3650, -- the chain outlives the images
  is_default            boolean NOT NULL DEFAULT false,
  created_at            timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, name),
  CONSTRAINT retention_days_sane CHECK (evidence_days BETWEEN 1 AND 3650)
);
CREATE UNIQUE INDEX retention_default_uq ON retention_policy (org_id)
  WHERE is_default;

CREATE TABLE purge_record (
  id             bigserial PRIMARY KEY,
  org_id         uuid NOT NULL REFERENCES organization(id) ON DELETE RESTRICT,
  session_id     uuid NOT NULL REFERENCES exam_session(id) ON DELETE RESTRICT,
  evidence_id    uuid NOT NULL,          -- intentionally not FK: row may be gone
  storage_key    text NOT NULL,
  sha256         bytea NOT NULL,         -- proves what was deleted
  policy_id      uuid REFERENCES retention_policy(id),
  reason         text NOT NULL,          -- retention | org_request | subject_request
  purged_at      timestamptz NOT NULL DEFAULT now(),
  audit_event_seq bigint NOT NULL        -- the EVIDENCE_PURGED event in the chain
);
CREATE TRIGGER purge_record_immutable BEFORE UPDATE OR DELETE ON purge_record
  FOR EACH ROW EXECUTE FUNCTION deny_mutation();
```

**Why purge writes to the chain.** Deleting evidence changes what a verifier can check.
If deletion were silent, a later verification failure would be indistinguishable from
tampering. `EVIDENCE_PURGED` plus `purge_record.sha256` lets the verifier report
"this object was lawfully deleted on date X" rather than "hash mismatch".

### 3.9 Results, risk, review

```sql
CREATE TABLE session_result (
  session_id      uuid PRIMARY KEY REFERENCES exam_session(id) ON DELETE CASCADE,
  org_id          uuid NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  total_marks     numeric(10,3) NOT NULL,
  max_marks       numeric(10,3) NOT NULL,
  percentage      numeric(6,3) GENERATED ALWAYS AS
                    (CASE WHEN max_marks > 0 THEN 100 * total_marks / max_marks END) STORED,
  auto_graded_at  timestamptz NOT NULL DEFAULT now(),
  released_at     timestamptz,
  released_by     uuid REFERENCES app_user(id),
  CONSTRAINT result_marks_sane CHECK (max_marks >= 0 AND total_marks >= 0)
);

CREATE TABLE question_score (
  id             bigserial PRIMARY KEY,
  org_id         uuid NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  session_id     uuid NOT NULL REFERENCES exam_session(id) ON DELETE CASCADE,
  paper_item_id  uuid NOT NULL REFERENCES session_paper_item(id) ON DELETE CASCADE,
  awarded        numeric(8,3) NOT NULL,
  max_marks      numeric(8,3) NOT NULL,
  grader         text NOT NULL DEFAULT 'auto',   -- auto | judge | manual
  detail         jsonb NOT NULL DEFAULT '{}'::jsonb,
  graded_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (session_id, paper_item_id),
  CONSTRAINT score_range CHECK (awarded >= -max_marks AND awarded <= max_marks),
  CONSTRAINT score_grader CHECK (grader IN ('auto','judge','manual'))
);

CREATE TABLE risk_assessment (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id         uuid NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  session_id     uuid NOT NULL REFERENCES exam_session(id) ON DELETE CASCADE,
  model_name     text NOT NULL,           -- 'baseline_weighted_v1'
  model_version  text NOT NULL,
  -- "Estimated session anomaly probability". NOT a probability of cheating.
  anomaly_probability numeric(5,4) NOT NULL,
  ci_low         numeric(5,4) NOT NULL,
  ci_high        numeric(5,4) NOT NULL,
  chain_status   chain_state NOT NULL DEFAULT 'unverified',
  computed_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (session_id, model_name, model_version),
  CONSTRAINT risk_prob_range CHECK (anomaly_probability BETWEEN 0 AND 1),
  CONSTRAINT risk_ci_order   CHECK (ci_low <= anomaly_probability AND anomaly_probability <= ci_high)
);
-- NOTE: there is deliberately NO 'cheating' boolean and no pass/fail column here,
-- and no foreign key from this table into session_result. Enforced by test.

CREATE TABLE risk_contribution (
  id            bigserial PRIMARY KEY,
  org_id        uuid NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  assessment_id uuid NOT NULL REFERENCES risk_assessment(id) ON DELETE CASCADE,
  event_type    text NOT NULL REFERENCES event_type(code),
  event_count   int NOT NULL,
  weight        numeric(8,4) NOT NULL,
  reliability   numeric(5,4) NOT NULL,   -- measured, from the evaluation harness
  contribution  numeric(8,4) NOT NULL,
  explanation   text NOT NULL,
  UNIQUE (assessment_id, event_type)
);

CREATE TABLE candidate_statement (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id        uuid NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  session_id    uuid NOT NULL REFERENCES exam_session(id) ON DELETE CASCADE,
  audit_event_seq bigint,                 -- optional: statement about one event
  body          text NOT NULL,
  submitted_at  timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT statement_len CHECK (length(body) BETWEEN 1 AND 8000)
);
CREATE INDEX statement_session_idx ON candidate_statement (session_id, submitted_at);

CREATE TABLE review (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id         uuid NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  session_id     uuid NOT NULL REFERENCES exam_session(id) ON DELETE CASCADE,
  state          review_state NOT NULL DEFAULT 'queued',
  queued_reason  text NOT NULL,          -- 'risk_threshold' | 'manual' | 'chain_failed'
  assigned_to    uuid REFERENCES app_user(id),
  queued_at      timestamptz NOT NULL DEFAULT now(),
  opened_at      timestamptz,
  decided_at     timestamptz,
  UNIQUE (session_id)
);
CREATE INDEX review_queue_idx ON review (org_id, state, queued_at);

CREATE TABLE review_decision (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id         uuid NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  review_id      uuid NOT NULL REFERENCES review(id) ON DELETE CASCADE,
  session_id     uuid NOT NULL REFERENCES exam_session(id) ON DELETE CASCADE,
  reviewer_id    uuid NOT NULL REFERENCES app_user(id),
  outcome        review_outcome NOT NULL,
  justification  text NOT NULL,          -- mandatory, enforced here and in the API
  evidence_viewed int NOT NULL DEFAULT 0,
  time_spent_ms  bigint,
  audit_event_seq bigint NOT NULL,       -- the REVIEW_DECISION event in the chain
  decided_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT decision_justification_len CHECK (length(btrim(justification)) >= 20)
);
CREATE TRIGGER review_decision_immutable BEFORE UPDATE OR DELETE ON review_decision
  FOR EACH ROW EXECUTE FUNCTION deny_mutation();
CREATE INDEX review_decision_session_idx ON review_decision (session_id, decided_at);
```

`review_decision` is append-only: a changed mind creates a second decision row. The
history of who thought what, when, is not erasable. A 20-character minimum on
justification is a crude but effective guard against `"ok"`.

### 3.10 Org-level activity log (not chained)

```sql
CREATE TABLE activity_log (
  id           bigserial PRIMARY KEY,
  org_id       uuid REFERENCES organization(id) ON DELETE RESTRICT,
  actor_user_id uuid REFERENCES app_user(id),
  actor_role   member_role,
  action       text NOT NULL,        -- 'exam.publish', 'user.login', 'assignment.create'
  object_type  text,
  object_id    uuid,
  detail       jsonb NOT NULL DEFAULT '{}'::jsonb,
  ip_hash      bytea,
  occurred_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX activity_org_time_idx ON activity_log (org_id, occurred_at DESC);
CREATE INDEX activity_action_idx   ON activity_log (org_id, action, occurred_at DESC);
CREATE TRIGGER activity_log_immutable BEFORE UPDATE OR DELETE ON activity_log
  FOR EACH ROW EXECUTE FUNCTION deny_mutation();
```

Two logs, deliberately: `audit_event` is per-session, signed, and hash-chained because it
must survive adversarial scrutiny. `activity_log` is administrative and high-volume;
chaining it would add cost without adding a defensible claim.

### 3.11 Usage metering (architecture-ready, NOT implemented)

```sql
CREATE TABLE usage_meter (
  id           bigserial PRIMARY KEY,
  org_id       uuid NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  period_start date NOT NULL,
  metric       text NOT NULL,   -- sessions_started, evidence_bytes, judge_cpu_ms, seats
  value        bigint NOT NULL DEFAULT 0,
  updated_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, period_start, metric)
);
```

No price, plan, or currency is modelled in the core schema. Pricing dimensions change;
hard-coding them into the assessment engine would be a mistake. `usage_meter` records
*facts*; a future billing service interprets them.

---

## 4. Row-Level Security

Pattern applied to **every** table carrying `org_id`:

```sql
ALTER TABLE exam_session ENABLE ROW LEVEL SECURITY;
ALTER TABLE exam_session FORCE ROW LEVEL SECURITY;

CREATE POLICY exam_session_tenant ON exam_session
  USING      (org_id = current_org_id())
  WITH CHECK (org_id = current_org_id());
```

`FORCE` matters: without it the policy is skipped for the table owner, and a
misconfigured deployment where the app connects as the owner silently loses all
isolation.

### Role-differentiated policies where the tenant boundary is not enough

A candidate must not read another candidate's session *within the same org*:

```sql
CREATE POLICY exam_session_candidate_self ON exam_session
  FOR SELECT
  USING (
    org_id = current_org_id()
    AND (
      current_role_name() IN ('org_admin','instructor','reviewer')
      OR candidate_user_id = current_user_id()
    )
  );

-- Candidates may never write to the chain or to evidence.
CREATE POLICY audit_event_no_candidate_write ON audit_event
  FOR INSERT
  WITH CHECK (org_id = current_org_id()
              AND current_role_name() <> 'candidate');
```

Candidate-facing event ingest therefore goes through a dedicated service context, not
the candidate's own database role — the browser proposes an event; the server decides
whether and how it is written.

### Evidence visibility gating

Examiner access to evidence is gated on the exam window state **in the API**, and the
API is the only path to a signed URL. RLS cannot express "after the exam closes" cleanly
across the join, so this is a documented case where layer 1 is the enforcement point —
and it is covered by an explicit security test rather than trusted silently.

---

## 5. Canonical JSON contract

The hash chain is meaningless unless two independent implementations serialize a payload
identically. The contract is **RFC 8785 (JSON Canonicalization Scheme)**, with these
explicit commitments:

| Aspect | Rule |
|---|---|
| Key ordering | Lexicographic by UTF-16 code unit (RFC 8785 §3.2.3) |
| Encoding | UTF-8, no BOM |
| Whitespace | None between tokens |
| Strings | RFC 8785 escaping; no `\uXXXX` for characters representable directly |
| Numbers | ECMAScript `Number::toString`; integers only in the signed envelope |
| Floats | **Forbidden** in the signed envelope — `confidence` is serialized as an integer in thousandths (`0.873` → `873`) |
| Null | Present as `null`; absent keys are omitted, and omission ≠ null |
| Timestamps | RFC 3339 UTC, exactly `YYYY-MM-DDTHH:MM:SS.sssZ` (millisecond precision, always 3 digits, always `Z`) |
| Binary | base64url, unpadded |

Banning floats from the signed envelope is a deliberate constraint. IEEE-754 shortest
round-trip formatting differs subtly between Python and JavaScript in enough edge cases
that a verifier written in the other language would eventually produce spurious failures —
and a spurious verification failure is worse than no verifier at all.

### The signed envelope

```json
{
  "v": 1,
  "session_id": "0f3c...",
  "server_seq": 42,
  "server_ts": "2026-08-18T09:14:22.317Z",
  "event_type": "MULTIPLE_FACES",
  "detector": "face_count",
  "severity": 3,
  "confidence_milli": 873,
  "is_late": false,
  "payload": { "face_count": 2, "dwell_ms": 4200 },
  "position": { "section": 1, "item": 7, "elapsed_ms": 812340 },
  "prev_hash": "s0Zq...",
  "key_fingerprint": "Jx9k..."
}
```

`canonical_sha256 = SHA-256(JCS(envelope))`; `signature = Ed25519(sk, canonical_sha256)`.

Note that `prev_hash` is **inside** the signed envelope. Signing only the current event
would leave the links unauthenticated and let an attacker who could write to the database
re-thread the chain.

---

## 6. Export bundle (what the verifier consumes)

```
session-<id>-export.zip
├── manifest.json          bundle schema version, session metadata, counts
├── events.jsonl           one canonical envelope per line, ascending server_seq
├── signatures.jsonl       {server_seq, signature, canonical_sha256}
├── evidence/
│   ├── index.json         [{evidence_id, event_seq, sha256, role, kind, bytes, purged}]
│   └── <evidence_id>.jpg  present unless purged
├── public_keys.json       [{key_fingerprint, algorithm, public_key_b64, validity}]
└── README.txt             how to verify without trusting Sentinel
```

The candidate bundle is the same structure with staff-only fields removed (reviewer
identity, other candidates' data, internal notes) and only that candidate's evidence
included. Removed fields are *omitted*, and the manifest states which field groups were
omitted — so a candidate verifying their bundle sees a complete chain over the fields
they received, plus an honest statement of what was withheld.

---

## 7. Seed data (Phase 1)

One organization (`demo-university`), one instructor, one reviewer, five candidates, one
published exam containing: 3 single-choice, 2 multiple-choice, 1 short answer, 1 numeric,
1 coding question with 2 sample and 4 hidden test cases; two sections, one using a
`k of n` pool. Passwords come from `.env`, never from the repository.

---

## 8. Known gaps in this schema

Honest list of what is *not* yet resolved:

1. **`activity_log` and `audit_event` will grow without bound.** Partitioning by month is
   the obvious answer but is not designed yet; it interacts with RLS and with the
   `(session_id, server_seq)` foreign key from `evidence_object`.
2. **Manual grading of short answers** has a `grader = 'manual'` value but no workflow
   tables (rubrics, marker assignment, moderation). Deferred to P1/P2.
3. **Question bank import conflict resolution** (same `external_key`, changed content) is
   not modelled beyond versioning; the merge policy is an open decision.
4. **Multi-region / data residency** is not modelled. A university requiring EU-only
   storage would need a per-org storage endpoint, which is a schema change.
5. **Face embeddings** are referenced conceptually (identity continuity) but have no
   table. They are biometric data with distinct legal handling and deserve their own
   design pass, not a column bolted onto `evidence_object`. Deferred to P2 with an
   explicit privacy review.
6. **`session_paper_item.option_order`** assumes option shuffling is expressible as an
   index permutation. This holds for the current question kinds but would need revisiting
   for matrix or drag-and-drop types.
7. **RLS policies are shown as a pattern, not written out per table.** See §9 — this is a
   real gap that the Phase 1 migration must close for all 35 tenant-owned tables.

---

## 9. Verification — what was actually executed

Run on PostgreSQL 16.13 (Ubuntu 24.04) in a throwaway cluster on 2026-08-18. Method: the
`sql` blocks in this document were extracted in order and applied with
`psql -v ON_ERROR_STOP=1`.

### Structural result

| Metric | Count |
|---|---|
| Tables created | 40 |
| CHECK constraints | 57 |
| Foreign keys | 103 |
| Indexes | 113 |
| Non-internal triggers | 6 |
| Errors | 0 |

The only diagnostics emitted were three expected `SET LOCAL can only be used in
transaction blocks` warnings from the illustrative snippet in §1.

### Behavioural assertions

| # | Assertion | Expected | Result |
|---|---|---|---|
| T1 | Genesis event (`server_seq=1`, `prev_hash IS NULL`) | accepted | **PASS** |
| T2 | `server_seq=2` with `prev_hash IS NULL` | rejected by `audit_genesis_prev` | **PASS** |
| T3 | `UPDATE` on `audit_event` | rejected by append-only trigger | **PASS** (`restrict_violation`) |
| T4 | `DELETE` on `audit_event` | rejected by append-only trigger | **PASS** (`restrict_violation`) |
| T5 | Second event reusing the same `client_seq` | rejected by `audit_client_seq_uq` | **PASS** |
| T6 | Signature not 64 bytes | rejected by `audit_sig_len` | **PASS** |
| T7 | Review decision with a 5-character justification | rejected by `decision_justification_len` | **PASS** |
| T8 | Risk assessment with `ci_low > probability` | rejected by `risk_ci_order` | **PASS** |
| T9 | Evidence pointing at a non-existent `(session, seq)` | rejected by composite FK | **PASS** |

T3–T5 are the ones that matter commercially: they are the database-level reason a
Sentinel audit trail cannot be quietly rewritten by application code, by a support script,
or by a careless migration.

### The gap this exercise found

A query for tables that carry `org_id` but lack `relrowsecurity AND relforcerowsecurity`
returned **34 tables** — everything except `exam_session`, which is the only table this
document spells the policy out for.

That is expected for a design document, but it is exactly the failure mode described in
§1, and it demonstrates why the invariant test is not optional:

```sql
SELECT c.relname
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r'
  AND EXISTS (SELECT 1 FROM information_schema.columns col
              WHERE col.table_name = c.relname AND col.column_name = 'org_id')
  AND (c.relrowsecurity = false OR c.relforcerowsecurity = false);
-- Phase 1 requirement: this query must return zero rows, asserted in CI.
```

**Phase 1 must generate `ENABLE`/`FORCE ROW LEVEL SECURITY` plus a tenant policy for all
35 tenant-owned tables**, and wire the query above into the test suite as a hard failure.
Until that lands, tenant isolation in this design rests on application code alone —
which is the posture explicitly rejected in §1.

### What this verification does *not* establish

- No Alembic migration exists; the DDL was applied as one flat script.
- No RLS enforcement was tested, because the policies are not written yet.
- No performance or index-effectiveness testing was done. Index choices are reasoned,
  not measured.
- Partitioning of `audit_event` / `activity_log` was not attempted (see gap 1).
- The canonical-JSON contract in §5 is unimplemented and therefore untested. Its
  cross-language determinism is currently a claim, not a result.
