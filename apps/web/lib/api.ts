/**
 * API client.
 *
 * Two things worth knowing about this file:
 *
 * 1. Tokens are never stored in JavaScript. Authentication rides on httpOnly
 *    cookies, so `credentials: 'include'` is on every request and there is no
 *    `localStorage.setItem('token', ...)` anywhere in this codebase. That is
 *    what makes an XSS bug not immediately an account-takeover bug.
 *
 * 2. Errors are RFC 9457 problem documents. The client surfaces `detail`
 *    verbatim, because the API is written to say something a human can act on.
 */

const BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? 'http://localhost:8000';

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly title: string,
    readonly detail: string,
    readonly type?: string,
    readonly requestId?: string,
  ) {
    super(detail || title);
    this.name = 'ApiError';
  }

  /** True when the caller needs to enrol or present a second factor. */
  get isMfa(): boolean {
    return this.type?.endsWith('/mfa-required') ?? false;
  }
}

function readCookie(name: string): string | null {
  if (typeof document === 'undefined') return null;
  const match = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`));
  return match ? decodeURIComponent(match[1]) : null;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body) headers.set('content-type', 'application/json');

  // Double-submit CSRF: echo the non-httpOnly cookie in a header. The cookie
  // alone carries no authority; it only proves the request came from a page
  // that could read our own cookie jar.
  const csrf = readCookie('sentinel_csrf');
  if (csrf && init.method && init.method !== 'GET') {
    headers.set('x-sentinel-csrf', csrf);
  }

  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers,
    credentials: 'include',
  });

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  const body = text ? JSON.parse(text) : null;

  if (!response.ok) {
    throw new ApiError(
      response.status,
      body?.title ?? 'Request failed',
      body?.detail ?? 'The server did not explain what went wrong.',
      body?.type,
      body?.request_id,
    );
  }

  return body as T;
}

// ---------------------------------------------------------------- types

export interface OrgSummary {
  org_id: string;
  slug: string;
  name: string;
  roles: string[];
}

export interface TokenResponse {
  access_token: string;
  expires_in: number;
  org_id: string | null;
  roles: string[];
  organizations: OrgSummary[];
}

export interface MfaChallenge {
  mfa_required: true;
  challenge_token: string;
  expires_in: number;
}

export interface Me {
  user_id: string;
  email: string;
  display_name: string;
  org_id: string | null;
  roles: string[];
  mfa_enabled: boolean;
  organizations: OrgSummary[];
}

export type WindowState = 'not_yet_open' | 'open' | 'closed';

export interface AssignedExam {
  assignment_id: string;
  exam_id: string;
  exam_version_id: string;
  title: string;
  description: string | null;
  duration_seconds: number;
  opens_at: string | null;
  closes_at: string | null;
  integrity_enabled: boolean;
  judge_enabled: boolean;
  attempts_allowed: number;
  window_state: WindowState;
}

export type QuestionKind =
  | 'single_choice'
  | 'multiple_choice'
  | 'short_answer'
  | 'numeric'
  | 'coding';

/**
 * A question as served to a candidate.
 *
 * `body` here is the *candidate-safe* body: the server strips `correct`,
 * `accepted` and `tolerance` where the row is read, and applies this
 * candidate's option permutation. There is nothing in this type to hide,
 * which is the point.
 */
export interface PaperItem {
  paper_item_id: string;
  item_ordinal: number;
  section_id: string;
  section_title: string;
  section_ordinal: number;
  kind: QuestionKind;
  prompt: string;
  prompt_format: string;
  marks: number;
  body: {
    options?: { id: string; text: string }[];
    multiple?: boolean;
    unit?: string | null;
    languages?: string[];
    starter?: Record<string, string>;
  };
  answer: AnswerValue | null;
  revision: number;
  flagged: boolean;
  saved_at: string | null;
}

export interface AnswerValue {
  selected?: string[];
  text?: string;
  value?: number | string | null;
  source?: string;
  language?: string;
}

export interface SessionState {
  session_id: string;
  exam_version_id: string;
  attempt_no: number;
  status: string;
  started_at: string | null;
  deadline_at: string | null;
  /** Authoritative. The countdown is rendered from this, never from Date.now(). */
  remaining_seconds: number;
  server_time: string;
  grace_seconds: number;
  integrity_enabled: boolean;
  question_count: number;
  items: PaperItem[];
}

export interface SaveResult {
  paper_item_id: string;
  revision: number;
  saved_at: string;
  server_time: string;
  remaining_seconds: number;
}

export type RunMode = 'sample' | 'final';

export interface JudgeTestResult {
  ordinal: number;
  passed: boolean;
  status: string;
  duration_ms: number | null;
  /** Sample tests only. `null` for hidden tests, always — that is the answer key. */
  stdout: string | null;
  stderr: string | null;
}

export interface JudgeRunView {
  run_id: string;
  status: string;
  mode: RunMode;
  language: string;
  queued_at: string;
  finished_at: string | null;
  tests_total: number;
  tests_passed: number;
  /** Fraction of test weight passed, 0..1 — not marks. */
  score: number | null;
  compile_output: string | null;
  error_detail: string | null;
  results: JudgeTestResult[];
}

export interface RunAccepted {
  run_id: string;
  submission_id: string;
  status: string;
  mode: RunMode;
  queue_depth: number;
}

export interface SubmitResult {
  session_id: string;
  status: string;
  submitted_at: string;
  answered: number;
  question_count: number;
  score_released: boolean;
  total_marks: number | null;
  max_marks: number | null;
}

// ---------------------------------------------------------------- calls

export const api = {
  login: (email: string, password: string) =>
    request<TokenResponse | MfaChallenge>('/api/v1/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    }),

  verifyMfa: (challenge_token: string, code: string) =>
    request<TokenResponse>('/api/v1/auth/mfa/verify', {
      method: 'POST',
      body: JSON.stringify({ challenge_token, code }),
    }),

  refresh: () => request<TokenResponse>('/api/v1/auth/refresh', { method: 'POST' }),

  logout: () => request<void>('/api/v1/auth/logout', { method: 'POST' }),

  switchOrg: (org_id: string) =>
    request<TokenResponse>('/api/v1/auth/switch-org', {
      method: 'POST',
      body: JSON.stringify({ org_id }),
    }),

  me: () => request<Me>('/api/v1/auth/me'),

  myExams: () => request<AssignedExam[]>('/api/v1/me/exams'),

  /**
   * Start *or resume*. Idempotent by design — a client that crashed does not
   * have to know which of the two it is doing, so it cannot get it wrong.
   */
  startSession: (assignment_id: string) =>
    request<SessionState>('/api/v1/sessions', {
      method: 'POST',
      body: JSON.stringify({ assignment_id }),
    }),

  sessionState: (session_id: string) =>
    request<SessionState>(`/api/v1/sessions/${session_id}`),

  /**
   * The candidate's most recent attempt at an assignment. Used when
   * `startSession` refuses because the attempts are used up: after a reload the
   * runner knows only the assignment, and the right thing to show someone who
   * has already submitted is their submitted paper.
   */
  latestSession: (assignment_id: string) =>
    request<SessionState>(`/api/v1/assignments/${assignment_id}/session`),

  saveAnswer: (
    session_id: string,
    paper_item_id: string,
    value: AnswerValue,
    client_revision: number | null,
    time_spent_ms = 0,
    flagged = false,
  ) =>
    request<SaveResult>(`/api/v1/sessions/${session_id}/answers/${paper_item_id}`, {
      method: 'PUT',
      body: JSON.stringify({
        value,
        client_revision,
        time_spent_ms,
        flagged,
        client_saved_at: new Date().toISOString(),
      }),
    }),

  runCode: (
    session_id: string,
    paper_item_id: string,
    language: string,
    source: string,
    mode: RunMode,
  ) =>
    request<RunAccepted>(
      `/api/v1/sessions/${session_id}/items/${paper_item_id}/run`,
      { method: 'POST', body: JSON.stringify({ language, source, mode }) },
    ),

  getRun: (session_id: string, run_id: string) =>
    request<JudgeRunView>(`/api/v1/sessions/${session_id}/runs/${run_id}`),

  listRuns: (session_id: string, paper_item_id: string) =>
    request<{ runs: Record<string, unknown>[] }>(
      `/api/v1/sessions/${session_id}/items/${paper_item_id}/runs`,
    ),

  submitSession: (session_id: string, reason = 'candidate') =>
    request<SubmitResult>(`/api/v1/sessions/${session_id}/submit`, {
      method: 'POST',
      body: JSON.stringify({ reason }),
    }),
};

export function isMfaChallenge(r: TokenResponse | MfaChallenge): r is MfaChallenge {
  return 'mfa_required' in r;
}
