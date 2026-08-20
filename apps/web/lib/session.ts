/**
 * Session store.
 *
 * Deliberately holds no credentials — only who the user is and which
 * organization they are acting in. The access and refresh tokens live in
 * httpOnly cookies the browser manages; nothing here can read them, which is
 * the point.
 *
 * NOTE: no `persist` middleware. Artifacts and this app must not write auth
 * state to localStorage, and rehydrating a stale identity across tabs is a
 * source of confusing bugs during a timed exam.
 */

'use client';

import { create } from 'zustand';
import { api, type Me, type OrgSummary } from './api';

interface SessionState {
  user: Me | null;
  status: 'unknown' | 'loading' | 'authenticated' | 'anonymous';
  error: string | null;

  load: () => Promise<void>;
  setUser: (user: Me | null) => void;
  switchOrg: (orgId: string) => Promise<void>;
  logout: () => Promise<void>;
}

export const useSession = create<SessionState>((set, get) => ({
  user: null,
  status: 'unknown',
  error: null,

  setUser: (user) =>
    set({ user, status: user ? 'authenticated' : 'anonymous', error: null }),

  load: async () => {
    set({ status: 'loading', error: null });
    try {
      const user = await api.me();
      set({ user, status: 'authenticated' });
    } catch {
      // A failed /me is the normal state for a signed-out visitor, not an
      // error worth showing them.
      set({ user: null, status: 'anonymous' });
    }
  },

  switchOrg: async (orgId: string) => {
    await api.switchOrg(orgId);
    await get().load();
  },

  logout: async () => {
    try {
      await api.logout();
    } finally {
      set({ user: null, status: 'anonymous' });
    }
  },
}));

export function currentOrg(user: Me | null): OrgSummary | null {
  if (!user?.org_id) return null;
  return user.organizations.find((o) => o.org_id === user.org_id) ?? null;
}

export function primaryRole(roles: string[]): string {
  for (const role of ['org_admin', 'instructor', 'reviewer', 'candidate']) {
    if (roles.includes(role)) return role;
  }
  return 'candidate';
}

export const ROLE_LABELS: Record<string, string> = {
  org_admin: 'Administrator',
  instructor: 'Instructor',
  reviewer: 'Reviewer',
  candidate: 'Candidate',
};
