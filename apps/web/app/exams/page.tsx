'use client';

import { useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';
import { api, type AssignedExam, type WindowState } from '@/lib/api';
import { ROLE_LABELS, currentOrg, primaryRole, useSession } from '@/lib/session';

const WINDOW_LABEL: Record<WindowState, { text: string; className: string }> = {
  open: { text: 'Open', className: 'bg-ok-soft text-ok' },
  not_yet_open: { text: 'Not yet open', className: 'bg-surface-sunken text-ink-muted' },
  closed: { text: 'Closed', className: 'bg-surface-sunken text-ink-faint' },
};

export default function ExamsPage() {
  const router = useRouter();
  const { user, status, load, logout } = useSession();
  const [exams, setExams] = useState<AssignedExam[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (status === 'unknown') void load();
  }, [status, load]);

  useEffect(() => {
    if (status === 'anonymous') router.replace('/login');
  }, [status, router]);

  useEffect(() => {
    if (status !== 'authenticated') return;
    api
      .myExams()
      .then(setExams)
      .catch(() => setError('Could not load your assessments.'));
  }, [status]);

  if (status !== 'authenticated' || !user) {
    return (
      <main className="flex min-h-screen items-center justify-center">
        <p className="text-sm text-ink-muted">Loading…</p>
      </main>
    );
  }

  const org = currentOrg(user);
  const role = primaryRole(user.roles);

  return (
    <main className="mx-auto max-w-3xl px-4 py-10">
      <header className="flex items-start justify-between gap-4 border-b border-surface-border pb-6">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Your assessments</h1>
          <p className="mt-1 text-sm text-ink-muted">
            {user.display_name}
            {org ? ` · ${org.name}` : ''} · {ROLE_LABELS[role] ?? role}
          </p>
        </div>
        <button
          onClick={() => void logout().then(() => router.push('/login'))}
          className="rounded-md border border-surface-border px-3 py-1.5 text-sm transition hover:bg-surface-sunken"
        >
          Sign out
        </button>
      </header>

      {error && (
        <p role="alert" className="mt-6 rounded-md bg-danger-soft px-3 py-2 text-sm text-danger">
          {error}
        </p>
      )}

      {exams === null && !error && (
        <p className="mt-6 text-sm text-ink-muted">Loading assessments…</p>
      )}

      {exams?.length === 0 && (
        <p className="mt-6 text-sm text-ink-muted">You have no assessments assigned.</p>
      )}

      <ul className="mt-6 space-y-3">
        {exams?.map((exam) => {
          const badge = WINDOW_LABEL[exam.window_state];
          return (
            <li
              key={exam.assignment_id}
              className="rounded-lg border border-surface-border bg-surface p-5"
            >
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <h2 className="truncate font-medium">{exam.title}</h2>
                  {exam.description && (
                    <p className="mt-1 text-sm text-ink-muted">{exam.description}</p>
                  )}
                </div>
                <span
                  className={`shrink-0 rounded-full px-2.5 py-1 text-xs font-medium ${badge.className}`}
                >
                  {badge.text}
                </span>
              </div>

              <dl className="mt-4 flex flex-wrap gap-x-6 gap-y-2 text-sm text-ink-muted">
                <div className="flex gap-1.5">
                  <dt>Duration</dt>
                  <dd className="font-medium text-ink">
                    {Math.round(exam.duration_seconds / 60)} min
                  </dd>
                </div>
                <div className="flex gap-1.5">
                  <dt>Closes</dt>
                  <dd className="font-medium text-ink">
                    {exam.closes_at ? new Date(exam.closes_at).toLocaleString() : '—'}
                  </dd>
                </div>
                <div className="flex gap-1.5">
                  <dt>Attempts</dt>
                  <dd className="font-medium text-ink">{exam.attempts_allowed}</dd>
                </div>
              </dl>

              {exam.integrity_enabled && (
                /*
                 * Disclosure before the exam, not a live feed during it (D7).
                 * Showing a candidate their flags in real time turns a timed
                 * exam into an anxiety loop and tells anyone inclined to cheat
                 * exactly which behaviours evade detection.
                 */
                <p className="mt-4 rounded-md bg-accent-soft px-3 py-2 text-xs leading-relaxed text-accent">
                  <strong className="font-semibold">Integrity monitoring is enabled.</strong>{' '}
                  Browser activity (fullscreen, tab and window focus, large pastes) is recorded
                  to a signed log. You will see exactly what is monitored, what is stored, and
                  for how long before the exam begins — and you can download and verify your own
                  record afterwards. No automated signal decides your result.
                </p>
              )}

              <div className="mt-4 flex items-center gap-3">
                <button
                  disabled={exam.window_state !== 'open'}
                  onClick={() => router.push(`/exams/${exam.assignment_id}/take`)}
                  className="rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white transition hover:opacity-90 disabled:opacity-40"
                >
                  {/*
                   * "Start or continue": the endpoint is idempotent, and the
                   * candidate should not have to work out which they are doing
                   * after their browser crashed.
                   */}
                  Start or continue
                </button>
                {exam.window_state !== 'open' && (
                  <span className="text-xs text-ink-faint">
                    {exam.window_state === 'not_yet_open'
                      ? 'This exam has not opened yet.'
                      : 'This exam has closed.'}
                  </span>
                )}
              </div>
            </li>
          );
        })}
      </ul>
    </main>
  );
}
