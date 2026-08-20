'use client';

/**
 * The exam runner's state machine.
 *
 * Three things live here, and they are here rather than in the page component
 * because each of them is a correctness property rather than a rendering
 * concern.
 *
 * **The clock is the server's.** `remaining_seconds` and `server_time` come
 * from the API; the hook records the browser's `performance.now()` at the
 * moment it received them and ticks down from that monotonic delta. It never
 * reads `Date.now()`. A candidate who moves their system clock — or whose
 * laptop is simply wrong — changes nothing, and `performance.now()` does not
 * jump when NTP corrects the wall clock mid-exam.
 *
 * **Autosave is debounced, queued, and revision-checked.** Every keystroke does
 * not hit the network; every answer does eventually reach it. A save carries
 * the revision it was edited from, so a tab that was offline cannot silently
 * overwrite newer work — the server refuses it with a 409 carrying the current
 * server state, and this hook reconciles to that rather than retrying.
 *
 * **Nothing is stored in the browser.** No localStorage, no sessionStorage. The
 * recovery story is "ask the server", which is the only version of recovery
 * that also works when the candidate switches machines.
 */

// React is supplied by the web app at runtime. Suppress this file-local
// declaration error when the standalone TypeScript checker cannot resolve the
// app's React dependency.
// @ts-ignore
import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, api, type AnswerValue, type SessionState } from '@/lib/api';

export const AUTOSAVE_DEBOUNCE_MS = 900;
/** How often to reconcile the clock with the server. */
const CLOCK_SYNC_MS = 30_000;

export type SaveState = 'idle' | 'saving' | 'saved' | 'error' | 'conflict';

export interface ExamSessionApi {
  state: SessionState | null;
  error: string | null;
  /** Seconds left, ticked locally between server syncs. */
  remaining: number;
  saveState: SaveState;
  saveMessage: string | null;
  /** paper_item_id -> the value currently on screen (may be unsaved). */
  draft: Record<string, AnswerValue>;
  unsavedCount: number;
  setAnswer: (itemId: string, value: AnswerValue) => void;
  flushNow: () => Promise<void>;
  submit: () => Promise<void>;
  submitted: boolean;
  result: { total: number | null; max: number | null; released: boolean } | null;
}

export function useExamSession(assignmentId: string | null): ExamSessionApi {
  const [state, setState] = useState<SessionState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [remaining, setRemaining] = useState(0);
  const [saveState, setSaveState] = useState<SaveState>('idle');
  const [saveMessage, setSaveMessage] = useState<string | null>(null);
  const [draft, setDraft] = useState<Record<string, AnswerValue>>({});
  const [submitted, setSubmitted] = useState(false);
  const [result, setResult] =
    useState<{ total: number | null; max: number | null; released: boolean } | null>(null);

  // Refs, not state: these are read inside timers and must not re-create them.
  const revisions = useRef<Record<string, number>>({});
  const dirty = useRef<Set<string>>(new Set());
  const draftRef = useRef<Record<string, AnswerValue>>({});
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const inFlight = useRef(false);
  const sessionId = useRef<string | null>(null);
  /** Monotonic anchor: (performance.now() at sync, seconds remaining then). */
  const clock = useRef<{ at: number; seconds: number } | null>(null);

  const adopt = useCallback((next: SessionState) => {
    setState(next);
    sessionId.current = next.session_id;
    clock.current = { at: performance.now(), seconds: next.remaining_seconds };
    setRemaining(next.remaining_seconds);

    const nextDraft: Record<string, AnswerValue> = {};
    const nextRevisions: Record<string, number> = {};
    for (const item of next.items) {
      nextRevisions[item.paper_item_id] = item.revision;
      // Do not clobber an edit the candidate has made but not yet saved.
      if (dirty.current.has(item.paper_item_id)) continue;
      if (item.answer) nextDraft[item.paper_item_id] = item.answer;
    }
    revisions.current = nextRevisions;
    setDraft((prev) => ({ ...nextDraft, ...pick(prev, [...dirty.current]) }));
    draftRef.current = { ...nextDraft, ...pick(draftRef.current, [...dirty.current]) };
    if (next.status !== 'in_progress') setSubmitted(true);
  }, []);

  // ---- start / resume ----------------------------------------------------
  useEffect(() => {
    if (!assignmentId) return;
    let cancelled = false;
    api
      .startSession(assignmentId)
      .then((s) => {
        if (!cancelled) adopt(s);
      })
      .catch(async (e: unknown) => {
        // A 409 here usually means the attempts are used up, which is not an
        // error state for the *page* — it means this candidate has already
        // sat the exam and should see what they submitted.
        if (e instanceof ApiError && e.status === 409) {
          try {
            const previous = await api.latestSession(assignmentId);
            if (!cancelled) adopt(previous);
            return;
          } catch {
            /* fall through to the error below */
          }
        }
        if (!cancelled) {
          setError(e instanceof ApiError ? e.detail : 'Could not start this exam.');
        }
      });
    return () => {
      cancelled = true;
    };
  }, [assignmentId, adopt]);

  // ---- the countdown -----------------------------------------------------
  useEffect(() => {
    if (!state || submitted) return;
    const id = setInterval(() => {
      const anchor = clock.current;
      if (!anchor) return;
      const elapsed = (performance.now() - anchor.at) / 1000;
      setRemaining(Math.max(0, Math.round(anchor.seconds - elapsed)));
    }, 250);
    return () => clearInterval(id);
  }, [state, submitted]);

  // ---- periodic reconciliation with the server clock ---------------------
  useEffect(() => {
    if (!state || submitted) return;
    const id = setInterval(() => {
      if (!sessionId.current) return;
      api
        .sessionState(sessionId.current)
        .then((s) => {
          clock.current = { at: performance.now(), seconds: s.remaining_seconds };
          setRemaining(s.remaining_seconds);
          if (s.status !== 'in_progress') {
            setSubmitted(true);
            setState(s);
          }
        })
        .catch(() => {
          /* A failed sync is not fatal: the local countdown keeps running and
             the next sync corrects it. The server still owns the deadline. */
        });
    }, CLOCK_SYNC_MS);
    return () => clearInterval(id);
  }, [state, submitted]);

  // ---- autosave ----------------------------------------------------------
  const flush = useCallback(async () => {
    if (inFlight.current || !sessionId.current) return;
    const pending = [...dirty.current];
    if (pending.length === 0) return;

    inFlight.current = true;
    setSaveState('saving');
    try {
      for (const itemId of pending) {
        const value = draftRef.current[itemId];
        if (value === undefined) {
          dirty.current.delete(itemId);
          continue;
        }
        try {
          const saved = await api.saveAnswer(
            sessionId.current,
            itemId,
            value,
            revisions.current[itemId] ?? null,
          );
          revisions.current[itemId] = saved.revision;
          dirty.current.delete(itemId);
          clock.current = { at: performance.now(), seconds: saved.remaining_seconds };
        } catch (e) {
          if (e instanceof ApiError && e.status === 409) {
            // Either the session closed, or a newer revision exists. Both are
            // resolved by adopting the server's view rather than retrying: the
            // server is the record, and a retry loop here would be a way to
            // overwrite work with stale content.
            dirty.current.delete(itemId);
            setSaveState('conflict');
            setSaveMessage(e.detail);
            if (sessionId.current) adopt(await api.sessionState(sessionId.current));
            return;
          }
          throw e;
        }
      }
      setSaveState('saved');
      setSaveMessage(null);
    } catch (e) {
      setSaveState('error');
      setSaveMessage(
        e instanceof ApiError
          ? e.detail
          : 'Your answer could not be saved. Check your connection — it will retry.',
      );
    } finally {
      inFlight.current = false;
    }
  }, [adopt]);

  const setAnswer = useCallback(
    (itemId: string, value: AnswerValue) => {
      draftRef.current = { ...draftRef.current, [itemId]: value };
      setDraft(draftRef.current);
      dirty.current.add(itemId);
      setSaveState('idle');
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => void flush(), AUTOSAVE_DEBOUNCE_MS);
    },
    [flush],
  );

  // Retry loop for a failed save. Deliberately slow and unconditional: the
  // common cause is a dropped connection, and the answer must not be lost
  // just because the candidate did not touch that question again.
  useEffect(() => {
    if (saveState !== 'error') return;
    const id = setTimeout(() => void flush(), 5000);
    return () => clearTimeout(id);
  }, [saveState, flush]);

  // Best-effort save when the tab is hidden or closed. Not relied upon —
  // the debounce is short enough that little is ever in flight — but the
  // cost of trying is nil.
  useEffect(() => {
    const handler = () => {
      if (document.visibilityState === 'hidden') void flush();
    };
    document.addEventListener('visibilitychange', handler);
    return () => document.removeEventListener('visibilitychange', handler);
  }, [flush]);

  const submit = useCallback(async () => {
    if (!sessionId.current) return;
    if (timer.current) clearTimeout(timer.current);
    await flush();
    try {
      const outcome = await api.submitSession(sessionId.current);
      setSubmitted(true);
      setResult({
        total: outcome.total_marks,
        max: outcome.max_marks,
        released: outcome.score_released,
      });
    } catch (e) {
      if (e instanceof ApiError) {
        setError(e.detail);
        setSubmitted(true);
        if (sessionId.current) {
          try {
            adopt(await api.sessionState(sessionId.current));
          } catch {
            /* nothing further to show */
          }
        }
      } else {
        setError('Submission failed. Your answers are saved; try again.');
      }
    }
  }, [flush, adopt]);

  return {
    state,
    error,
    remaining,
    saveState,
    saveMessage,
    draft,
    unsavedCount: dirty.current.size,
    setAnswer,
    flushNow: flush,
    submit,
    submitted,
    result,
  };
}

function pick<T>(source: Record<string, T>, keys: string[]): Record<string, T> {
  const out: Record<string, T> = {};
  for (const key of keys) if (key in source) out[key] = source[key];
  return out;
}

export function formatRemaining(seconds: number): string {
  const s = Math.max(0, seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (n: number) => String(n).padStart(2, '0');
  return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${pad(m)}:${pad(sec)}`;
}
