'use client';

/**
 * The exam runner.
 *
 * Design notes that are not obvious from the markup:
 *
 * - The countdown turns amber at five minutes and red at one, and the page
 *   never auto-submits on the *client's* count reaching zero. It asks the
 *   server to submit, and the server decides. A client-side auto-submit driven
 *   by a clock the client owns is how a candidate loses ten minutes to a bad
 *   NTP sync.
 * - Save state is always visible. "Saving…", "Saved", or an explicit failure
 *   with what to do about it. A silent autosave is indistinguishable from a
 *   broken one, and the difference matters enormously to the person in the room.
 * - No answer key is ever in the DOM, because the API never sends one.
 */

import { useParams, useRouter } from 'next/navigation';
import { useEffect, useMemo, useState } from 'react';
import type { AnswerValue, PaperItem } from '@/lib/api';
import { formatRemaining, useExamSession } from '@/lib/exam/useExamSession';
import { CodeQuestion } from './CodeQuestion';
import { useSession } from '@/lib/session';

export default function TakeExamPage() {
  const params = useParams<{ assignmentId: string }>();
  const router = useRouter();
  const { status, load } = useSession();
  const assignmentId = params?.assignmentId ?? null;

  useEffect(() => {
    if (status === 'unknown') void load();
  }, [status, load]);
  useEffect(() => {
    if (status === 'anonymous') router.replace('/login');
  }, [status, router]);

  const exam = useExamSession(status === 'authenticated' ? assignmentId : null);
  const [confirming, setConfirming] = useState(false);

  const answered = useMemo(
    () => (exam.state ? exam.state.items.filter((i) => hasAnswer(exam.draft[i.paper_item_id])).length : 0),
    [exam.state, exam.draft],
  );

  if (exam.error && !exam.state) {
    return (
      <Shell>
        <p role="alert" className="rounded-md bg-danger-soft px-3 py-2 text-sm text-danger">
          {exam.error}
        </p>
        <button
          onClick={() => router.push('/exams')}
          className="mt-4 rounded-md border border-surface-border px-3 py-1.5 text-sm"
        >
          Back to your assessments
        </button>
      </Shell>
    );
  }

  if (!exam.state) {
    return (
      <Shell>
        <p className="text-sm text-ink-muted">Preparing your paper…</p>
      </Shell>
    );
  }

  if (exam.submitted) {
    return (
      <Shell>
        <div className="rounded-lg border border-surface-border bg-surface p-6">
          <h1 className="text-lg font-semibold">
            {exam.state.status === 'expired' ? 'Time expired' : 'Submitted'}
          </h1>
          <p className="mt-2 text-sm text-ink-muted">
            {exam.state.status === 'expired'
              ? 'The deadline passed. Every answer you saved before it has been kept and graded.'
              : 'Your answers are recorded. You can review what you submitted below.'}
          </p>
          {exam.error && (
            <p role="alert" className="mt-3 rounded-md bg-danger-soft px-3 py-2 text-sm text-danger">
              {exam.error}
            </p>
          )}
          {exam.result?.released && exam.result.max !== null && (
            <p className="mt-4 text-2xl font-semibold tabular-nums">
              {exam.result.total} <span className="text-base text-ink-muted">/ {exam.result.max}</span>
            </p>
          )}
          {exam.result && !exam.result.released && (
            <p className="mt-4 text-sm text-ink-muted">
              Results are released by your institution. You will be able to see them here.
            </p>
          )}
          <button
            onClick={() => router.push('/exams')}
            className="mt-6 rounded-md border border-surface-border px-3 py-1.5 text-sm transition hover:bg-surface-sunken"
          >
            Back to your assessments
          </button>
        </div>

        <ol className="mt-6 space-y-4">
          {exam.state.items.map((item) => (
            <li key={item.paper_item_id} className="rounded-lg border border-surface-border bg-surface p-5">
              <QuestionHeader item={item} />
              <p className="mt-3 whitespace-pre-wrap text-sm">{item.prompt}</p>
              <p className="mt-3 text-sm text-ink-muted">
                Your answer: <span className="text-ink">{describe(item, exam.draft[item.paper_item_id])}</span>
              </p>
            </li>
          ))}
        </ol>
      </Shell>
    );
  }

  // Narrowed once, here: `exam.state` is non-null past the guard above, but
  // TypeScript cannot see that through the closures below.
  const state = exam.state;
  const urgency =
    exam.remaining <= 60 ? 'text-danger' : exam.remaining <= 300 ? 'text-warn' : 'text-ink';

  return (
    <Shell>
      <div className="sticky top-0 z-10 -mx-4 mb-6 border-b border-surface-border bg-surface/95 px-4 py-3 backdrop-blur">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="text-xs uppercase tracking-wide text-ink-faint">Time remaining</p>
            <p className={`text-2xl font-semibold tabular-nums ${urgency}`} aria-live="off">
              {formatRemaining(exam.remaining)}
            </p>
          </div>
          <div className="text-right">
            <p className="text-sm text-ink-muted">
              {answered} of {exam.state.question_count} answered
            </p>
            <SaveIndicator state={exam.saveState} message={exam.saveMessage} />
          </div>
          <button
            onClick={() => setConfirming(true)}
            className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-white transition hover:opacity-90"
          >
            Submit exam
          </button>
        </div>
        {exam.remaining === 0 && (
          <p role="alert" className="mt-2 text-sm text-danger">
            Your time is up. Submit now — the server decides whether it is in time.
          </p>
        )}
      </div>

      {exam.state.integrity_enabled && (
        <p className="mb-6 rounded-md bg-accent-soft px-3 py-2 text-xs leading-relaxed text-accent">
          Integrity monitoring is recording browser activity to a signed log. No automated signal
          decides your result.
        </p>
      )}

      <ol className="space-y-5">
        {exam.state.items.map((item) => (
          <li
            key={item.paper_item_id}
            className="rounded-lg border border-surface-border bg-surface p-5"
          >
            <QuestionHeader item={item} />
            <p className="mt-3 whitespace-pre-wrap text-sm">{item.prompt}</p>
            <div className="mt-4">
              <AnswerInput
                item={item}
                value={exam.draft[item.paper_item_id]}
                onChange={(v) => exam.setAnswer(item.paper_item_id, v)}
                sessionId={state.session_id}
              />
            </div>
          </li>
        ))}
      </ol>

      {confirming && (
        <ConfirmSubmit
          answered={answered}
          total={exam.state.question_count}
          onCancel={() => setConfirming(false)}
          onConfirm={() => {
            setConfirming(false);
            void exam.submit();
          }}
        />
      )}
    </Shell>
  );
}

// ------------------------------------------------------------------ pieces

function Shell({ children }: { children: React.ReactNode }) {
  return <main className="mx-auto max-w-3xl px-4 py-8">{children}</main>;
}

function QuestionHeader({ item }: { item: PaperItem }) {
  return (
    <div className="flex items-baseline justify-between gap-4">
      <h2 className="text-sm font-medium">
        <span className="text-ink-faint">Q{item.item_ordinal}</span>{' '}
        <span className="text-ink-muted">· {item.section_title}</span>
      </h2>
      <span className="shrink-0 text-xs text-ink-muted">
        {item.marks} {item.marks === 1 ? 'mark' : 'marks'}
      </span>
    </div>
  );
}

function SaveIndicator({ state, message }: { state: string; message: string | null }) {
  if (state === 'saving') return <p className="text-xs text-ink-muted">Saving…</p>;
  if (state === 'saved') return <p className="text-xs text-ok">All answers saved</p>;
  if (state === 'conflict')
    return (
      <p role="alert" className="text-xs text-warn">
        {message ?? 'Reloaded from the server.'}
      </p>
    );
  if (state === 'error')
    return (
      <p role="alert" className="text-xs text-danger">
        {message ?? 'Save failed — retrying.'}
      </p>
    );
  return <p className="text-xs text-ink-faint">&nbsp;</p>;
}

function AnswerInput({
  item,
  value,
  onChange,
  sessionId,
  readOnly,
}: {
  item: PaperItem;
  value: AnswerValue | undefined;
  onChange: (v: AnswerValue) => void;
  sessionId: string;
  readOnly?: boolean;
}) {
  if (item.kind === 'single_choice' || item.kind === 'multiple_choice') {
    const selected = value?.selected ?? [];
    const multiple = item.kind === 'multiple_choice';
    return (
      <fieldset className="space-y-2">
        <legend className="sr-only">Options</legend>
        {(item.body.options ?? []).map((option) => (
          <label
            key={option.id}
            className="flex cursor-pointer items-start gap-3 rounded-md border border-surface-border px-3 py-2 text-sm transition hover:bg-surface-sunken"
          >
            <input
              type={multiple ? 'checkbox' : 'radio'}
              name={item.paper_item_id}
              checked={selected.includes(option.id)}
              onChange={(e) => {
                const next = multiple
                  ? e.target.checked
                    ? [...selected, option.id]
                    : selected.filter((s) => s !== option.id)
                  : [option.id];
                onChange({ selected: next });
              }}
              className="mt-0.5"
            />
            <span>{option.text}</span>
          </label>
        ))}
      </fieldset>
    );
  }

  if (item.kind === 'short_answer') {
    return (
      <input
        type="text"
        aria-label={`Answer to question ${item.item_ordinal}`}
        value={value?.text ?? ''}
        onChange={(e) => onChange({ text: e.target.value })}
        className="w-full rounded-md border border-surface-border bg-surface-sunken px-3 py-2 text-sm outline-none focus:border-accent"
      />
    );
  }

  if (item.kind === 'numeric') {
    return (
      <div className="flex items-center gap-2">
        <input
          type="text"
          inputMode="decimal"
          aria-label={`Answer to question ${item.item_ordinal}`}
          value={value?.value === null || value?.value === undefined ? '' : String(value.value)}
          onChange={(e) => onChange({ value: e.target.value })}
          className="w-40 rounded-md border border-surface-border bg-surface-sunken px-3 py-2 text-sm outline-none focus:border-accent"
        />
        {item.body.unit && <span className="text-sm text-ink-muted">{item.body.unit}</span>}
      </div>
    );
  }

  // Coding. The editor autosaves through the same path as every other answer,
  // so a judge outage never costs a candidate their work.
  const fallbackLanguage = item.body.languages?.[0] ?? '';
  return (
    <CodeQuestion
      item={item}
      sessionId={sessionId}
      source={value?.source ?? item.body.starter?.[fallbackLanguage] ?? ''}
      language={value?.language ?? fallbackLanguage}
      onChange={(source, language) => onChange({ source, language })}
      disabled={readOnly}
    />
  );
}

function ConfirmSubmit({
  answered,
  total,
  onCancel,
  onConfirm,
}: {
  answered: number;
  total: number;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const blank = total - answered;
  return (
    <div className="fixed inset-0 z-20 flex items-center justify-center bg-black/40 px-4">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="confirm-title"
        className="w-full max-w-md rounded-lg border border-surface-border bg-surface p-6"
      >
        <h2 id="confirm-title" className="text-lg font-semibold">
          Submit your exam?
        </h2>
        <p className="mt-2 text-sm text-ink-muted">
          {blank > 0
            ? `${blank} of ${total} question${blank === 1 ? '' : 's'} ${
                blank === 1 ? 'is' : 'are'
              } unanswered. Unanswered questions score zero — they are not penalised.`
            : `All ${total} questions are answered.`}{' '}
          You cannot change your answers after submitting.
        </p>
        <div className="mt-6 flex justify-end gap-3">
          <button
            onClick={onCancel}
            className="rounded-md border border-surface-border px-3 py-1.5 text-sm transition hover:bg-surface-sunken"
          >
            Keep working
          </button>
          <button
            onClick={onConfirm}
            className="rounded-md bg-accent px-4 py-1.5 text-sm font-medium text-white transition hover:opacity-90"
          >
            Submit
          </button>
        </div>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ helpers

function hasAnswer(value: AnswerValue | undefined): boolean {
  if (!value) return false;
  if (value.selected) return value.selected.length > 0;
  if (typeof value.text === 'string') return value.text.trim() !== '';
  if (value.value !== undefined && value.value !== null) return String(value.value).trim() !== '';
  if (typeof value.source === 'string') return value.source.trim() !== '';
  return false;
}

function describe(item: PaperItem, value: AnswerValue | undefined): string {
  if (!hasAnswer(value)) return 'not answered';
  if (value?.selected) {
    const byId = new Map((item.body.options ?? []).map((o) => [o.id, o.text]));
    return value.selected.map((id) => byId.get(id) ?? id).join(', ');
  }
  if (value?.text !== undefined) return value.text;
  if (value?.value !== undefined && value.value !== null) return String(value.value);
  return 'answered';
}
