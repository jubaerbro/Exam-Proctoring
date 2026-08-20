'use client';

import { useRouter } from 'next/navigation';
import { useState } from 'react';
import { ApiError, api, isMfaChallenge } from '@/lib/api';
import { useSession } from '@/lib/session';

export default function LoginPage() {
  const router = useRouter();
  const load = useSession((s) => s.load);

  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [challenge, setChallenge] = useState<string | null>(null);
  const [code, setCode] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (challenge) {
        await api.verifyMfa(challenge, code);
      } else {
        const result = await api.login(email, password);
        if (isMfaChallenge(result)) {
          setChallenge(result.challenge_token);
          setBusy(false);
          return;
        }
      }
      await load();
      router.push('/exams');
    } catch (err) {
      // Show the server's wording rather than inventing our own. The API is
      // deliberately vague about which half of the credentials was wrong, and
      // paraphrasing it here would risk leaking the distinction back.
      setError(err instanceof ApiError ? err.detail : 'Something went wrong. Try again.');
      setBusy(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center px-4 py-12">
      <div className="w-full max-w-sm">
        <div className="mb-8">
          <h1 className="text-2xl font-semibold tracking-tight">Sentinel</h1>
          <p className="mt-1 text-sm text-ink-muted">
            {challenge ? 'Enter your authenticator code.' : 'Sign in to your assessments.'}
          </p>
        </div>

        <form
          onSubmit={submit}
          // `method="post"` matters even though `submit` calls preventDefault.
          // If the page has not hydrated — a slow network, a JS error, a stale
          // chunk — a native submit still happens, and the default method is
          // GET. That puts the password in the URL, the browser history and
          // every access log in between. POST-ing to a route that does not
          // accept it fails visibly instead, which is the better failure.
          method="post"
          className="rounded-lg border border-surface-border bg-surface p-6 shadow-sm"
        >
          {challenge ? (
            <div>
              <label htmlFor="code" className="block text-sm font-medium">
                Verification code
              </label>
              <input
                id="code"
                name="code"
                inputMode="numeric"
                autoComplete="one-time-code"
                autoFocus
                required
                value={code}
                onChange={(e) => setCode(e.target.value)}
                className="mt-1.5 w-full rounded-md border border-surface-border px-3 py-2 text-sm tracking-[0.3em] focus:border-accent focus:outline-none focus:ring-1 focus:ring-accent"
                placeholder="000000"
              />
            </div>
          ) : (
            <>
              <div>
                <label htmlFor="email" className="block text-sm font-medium">
                  Email
                </label>
                <input
                  id="email"
                  name="email"
                  type="email"
                  autoComplete="username"
                  autoFocus
                  required
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  className="mt-1.5 w-full rounded-md border border-surface-border px-3 py-2 text-sm focus:border-accent focus:outline-none focus:ring-1 focus:ring-accent"
                />
              </div>
              <div className="mt-4">
                <label htmlFor="password" className="block text-sm font-medium">
                  Password
                </label>
                <input
                  id="password"
                  name="password"
                  type="password"
                  autoComplete="current-password"
                  required
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className="mt-1.5 w-full rounded-md border border-surface-border px-3 py-2 text-sm focus:border-accent focus:outline-none focus:ring-1 focus:ring-accent"
                />
              </div>
            </>
          )}

          {error && (
            <p
              role="alert"
              className="mt-4 rounded-md bg-danger-soft px-3 py-2 text-sm text-danger"
            >
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={busy}
            className="mt-6 w-full rounded-md bg-accent px-3 py-2 text-sm font-medium text-white transition hover:bg-accent-hover disabled:opacity-50"
          >
            {busy ? 'Signing in…' : challenge ? 'Verify' : 'Sign in'}
          </button>
        </form>

        <p className="mt-6 text-xs leading-relaxed text-ink-faint">
          Sentinel records a signed, tamper-evident log of monitored assessment sessions.
          You can download and independently verify your own record after an exam.
        </p>
      </div>
    </main>
  );
}
