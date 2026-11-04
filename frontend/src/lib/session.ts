import type { UserRead } from '../api/types';

/**
 * Session store for the JWT + cached user profile. Backed by localStorage and
 * consumed through useSyncExternalStore from React.
 */

const TOKEN_KEY = 'agentforge.jwt';
const USER_KEY = 'agentforge.user';

export interface SessionSnapshot {
  token: string | null;
  user: UserRead | null;
}

function safeGet(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function safeSet(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* Private-mode / storage-disabled: session is memory-only for this tab. */
  }
}

function safeRemove(key: string): void {
  try {
    window.localStorage.removeItem(key);
  } catch {
    /* ignore */
  }
}

function readUser(): UserRead | null {
  const raw = safeGet(USER_KEY);
  if (!raw) return null;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (parsed && typeof parsed === 'object') return parsed as UserRead;
    return null;
  } catch {
    return null;
  }
}

function read(): SessionSnapshot {
  const token = safeGet(TOKEN_KEY);
  return { token: token && token.length > 0 ? token : null, user: token ? readUser() : null };
}

let cached: SessionSnapshot = read();
const listeners = new Set<() => void>();

function notify(): void {
  cached = read();
  for (const listener of listeners) listener();
}

export function subscribeSession(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Stable reference between notifications — required by useSyncExternalStore. */
export function getSessionSnapshot(): SessionSnapshot {
  return cached;
}

export function getToken(): string | null {
  return cached.token;
}

export function getSessionUser(): UserRead | null {
  return cached.user;
}

export function setSession(token: string, user: UserRead): void {
  safeSet(TOKEN_KEY, token);
  safeSet(USER_KEY, JSON.stringify(user));
  notify();
}

export function clearSession(): void {
  safeRemove(TOKEN_KEY);
  safeRemove(USER_KEY);
  notify();
}

/**
 * Called by the API client on a 401: drops the session and lets the app shell
 * bounce back to the login screen with an explanatory notice.
 */
export function notifyUnauthorized(): void {
  const hadSession = cached.token !== null;
  clearSession();
  if (hadSession && typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent('agentforge:unauthorized'));
  }
}

export const UNAUTHORIZED_EVENT = 'agentforge:unauthorized';

// Keep multiple tabs in sync.
if (typeof window !== 'undefined') {
  window.addEventListener('storage', (event: StorageEvent) => {
    if (event.key === null || event.key === TOKEN_KEY || event.key === USER_KEY) notify();
  });
}
