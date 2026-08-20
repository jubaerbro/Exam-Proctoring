'use client';

/**
 * The coding question: an editor, a Run button, and the judge's verdict.
 *
 * Two things this component is careful about.
 *
 * **"Run" and "Submit for marking" are different buttons with different
 * consequences, and the UI says so.** Run executes the sample tests and shows
 * everything; Submit executes the hidden suite and is what scores. A candidate
 * who cannot tell which one they pressed will press the wrong one.
 *
 * **The code is autosaved independently of the judge.** The answer goes through
 * the same `setAnswer` path as every other question type, so a candidate whose
 * browser dies between typing and running loses nothing — and a judge outage
 * never costs anyone their work.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, api, type JudgeRunView, type PaperItem, type RunMode } from '@/lib/api';

const POLL_MS = 700;
const POLL_TIMEOUT_MS = 90_000;

const TERMINAL = new Set([
  'completed',
  'compile_error',
  'runtime_error',
  'timeout',
  'memory_exceeded',
  'output_exceeded',
  'internal_error',
  'cancelled',
]);

const STATUS_COPY: Record<string, string> = {
  queued: 'Queued…',
  running: 'Running…',
  completed: 'Finished',
  compile_error: 'Your program did not compile',
  runtime_error: 'Your program stopped with an error',
  timeout: 'Your program ran out of time',
  memory_exceeded: 'Your program ran out of memory',
  output_exceeded: 'Your program printed too much output',
  internal_error: 'The judge failed — this is not your fault',
  cancelled: 'Cancelled',
};

export function CodeQuestion({
  item,
  sessionId,
  source,
  language,
  onChange,
  disabled,
}: {
  item: PaperItem;
  sessionId: string;
  source: string;
  language: string;
  onChange: (source: string, language: string) => void;
  disabled?: boolean;
}) {
  const [run, setRun] = useState<JudgeRunView | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const polling = useRef<ReturnType<typeof setTimeout> | null>(null);

  const languages = item.body.languages ?? [];

  useEffect(
    () => () => {
      if (polling.current) clearTimeout(polling.current);
    },
    [],
  );

  const poll = useCallback(
    (runId: string, startedAt: number) => {
      polling.current = setTimeout(async () => {
        try {
          const view = await api.getRun(sessionId, runId);
          setRun(view);
          if (TERMINAL.has(view.status)) {
            setBusy(false);
            return;
          }
          if (Date.now() - startedAt > POLL_TIMEOUT_MS) {
            // Give up *visibly*. A spinner that never resolves is the worst
            // possible state to leave someone in during a timed exam.
            setBusy(false);
            setError(
              'The judge has not answered in 90 seconds. Your code is saved — try again, ' +
                'and tell your invigilator if it keeps happening.',
            );
            return;
          }
          poll(runId, startedAt);
        } catch (e) {
          setBusy(false);
          setError(e instanceof ApiError ? e.detail : 'Could not reach the judge.');
        }
      }, POLL_MS);
    },
    [sessionId],
  );

  const start = useCallback(
    async (mode: RunMode) => {
      setBusy(true);
      setError(null);
      setRun(null);
      try {
        const accepted = await api.runCode(
          sessionId,
          item.paper_item_id,
          language,
          source,
          mode,
        );
        poll(accepted.run_id, Date.now());
      } catch (e) {
        setBusy(false);
        setError(e instanceof ApiError ? e.detail : 'Could not start the run.');
      }
    },
    [sessionId, item.paper_item_id, language, source, poll],
  );

  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-3">
        <label className="text-xs text-ink-muted">
          Language{' '}
          <select
            value={language}
            disabled={disabled || languages.length <= 1}
            onChange={(e) => onChange(source, e.target.value)}
            className="ml-1 rounded-md border border-surface-border bg-surface px-2 py-1 text-xs"
          >
            {languages.map((l) => (
              <option key={l} value={l}>
                {l}
              </option>
            ))}
          </select>
        </label>
        <span className="text-xs text-ink-faint">
          {item.marks} {item.marks === 1 ? 'mark' : 'marks'}
        </span>
      </div>

      <textarea
        aria-label={`Code for question ${item.item_ordinal}`}
        rows={14}
        spellCheck={false}
        disabled={disabled}
        value={source}
        onChange={(e) => onChange(e.target.value, language)}
        className="w-full rounded-md border border-surface-border bg-surface-sunken px-3 py-2 font-mono text-xs outline-none focus:border-accent disabled:opacity-60"
      />

      {!disabled && (
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <button
            onClick={() => void start('sample')}
            disabled={busy || !source.trim()}
            className="rounded-md border border-surface-border px-3 py-1.5 text-sm transition hover:bg-surface-sunken disabled:opacity-40"
          >
            Run sample tests
          </button>
          <button
            onClick={() => void start('final')}
            disabled={busy || !source.trim()}
            className="rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white transition hover:opacity-90 disabled:opacity-40"
          >
            Submit for marking
          </button>
          <span className="text-xs text-ink-faint">
            {/*
              Saying this plainly is the point. Without it a candidate cannot
              tell why one button is fast and honest and the other is slow and
              silent about which tests it ran.
            */}
            Running uses only the example tests. Submitting also runs hidden
            tests and sets your mark.
          </span>
        </div>
      )}

      {error && (
        <p role="alert" className="mt-3 rounded-md bg-danger-soft px-3 py-2 text-xs text-danger">
          {error}
        </p>
      )}

      {run && <RunResult run={run} busy={busy} />}
    </div>
  );
}

function RunResult({ run, busy }: { run: JudgeRunView; busy: boolean }) {
  const headline = STATUS_COPY[run.status] ?? run.status;
  const tone =
    run.status === 'internal_error'
      ? 'text-warn'
      : run.status === 'completed' && run.tests_passed === run.tests_total
        ? 'text-ok'
        : 'text-ink';

  return (
    <div className="mt-4 rounded-md border border-surface-border bg-surface-sunken p-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className={`text-sm font-medium ${tone}`} aria-live="polite">
          {busy ? STATUS_COPY[run.status] ?? 'Working…' : headline}
        </p>
        {run.tests_total > 0 && (
          <p className="text-xs text-ink-muted">
            {run.tests_passed} of {run.tests_total} tests passed
            {run.mode === 'final' && run.score !== null && (
              <> · {Math.round(run.score * 100)}% of the marks</>
            )}
          </p>
        )}
      </div>

      {run.status === 'internal_error' && (
        <p className="mt-2 text-xs text-warn">
          Nothing has been marked against you. Try again, and tell your invigilator if it
          keeps happening.
        </p>
      )}

      {run.compile_output && (
        <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap rounded bg-surface p-2 font-mono text-[11px] text-ink-muted">
          {run.compile_output}
        </pre>
      )}

      {run.results.length > 0 && (
        <ul className="mt-3 space-y-1.5">
          {run.results.map((result) => (
            <li key={result.ordinal} className="text-xs">
              <span className={result.passed ? 'text-ok' : 'text-danger'}>
                {result.passed ? '✓' : '✗'}
              </span>{' '}
              <span className="text-ink-muted">
                Test {result.ordinal}
                {result.status !== 'completed' && ` — ${STATUS_COPY[result.status] ?? result.status}`}
                {result.duration_ms !== null && ` · ${result.duration_ms} ms`}
              </span>
              {/*
                `stdout` is populated for sample tests only — the API never sends
                it for hidden ones, because a hidden test's output is the answer
                key. There is nothing to hide here on the client.
              */}
              {result.stdout && (
                <pre className="mt-1 max-h-24 overflow-auto whitespace-pre-wrap rounded bg-surface p-2 font-mono text-[11px]">
                  {result.stdout}
                </pre>
              )}
            </li>
          ))}
        </ul>
      )}

      {run.mode === 'final' && run.tests_total > run.results.length && (
        <p className="mt-2 text-xs text-ink-faint">
          Hidden tests ran too. Their inputs and expected outputs are not shown.
        </p>
      )}
    </div>
  );
}
