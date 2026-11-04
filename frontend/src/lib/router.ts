import { useMemo, useSyncExternalStore } from 'react';

/**
 * Minimal hash router. Hash routing keeps deep links (e.g. #/runs/<id>) working
 * when FastAPI serves the static bundle without an SPA catch-all route.
 */

export type Route =
  | { name: 'dashboard' }
  | { name: 'run'; id: string }
  | { name: 'agents' }
  | { name: 'tools' }
  | { name: 'knowledge' };

export const ROUTES = {
  dashboard: '/',
  agents: '/agents',
  tools: '/tools',
  knowledge: '/knowledge',
  run: (id: string) => `/runs/${encodeURIComponent(id)}`,
} as const;

export function parseRoute(hash: string): Route {
  const cleaned = hash.replace(/^#/, '').replace(/^\/+/, '').replace(/\/+$/, '');
  if (!cleaned) return { name: 'dashboard' };

  const [head, ...rest] = cleaned.split('/');
  switch (head) {
    case 'runs':
      if (rest.length > 0 && rest[0]) return { name: 'run', id: decodeURIComponent(rest[0]) };
      return { name: 'dashboard' };
    case 'agents':
      return { name: 'agents' };
    case 'tools':
      return { name: 'tools' };
    case 'knowledge':
      return { name: 'knowledge' };
    case 'login':
      return { name: 'dashboard' };
    default:
      return { name: 'dashboard' };
  }
}

export function routeHref(path: string): string {
  return `#${path}`;
}

export function navigate(path: string, options: { replace?: boolean } = {}): void {
  const target = `#${path}`;
  if (options.replace) {
    window.history.replaceState(null, '', target);
    window.dispatchEvent(new HashChangeEvent('hashchange'));
  } else if (window.location.hash !== target) {
    window.location.hash = target;
  }
}

const listeners = new Set<() => void>();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  const onHashChange = () => listener();
  window.addEventListener('hashchange', onHashChange);
  window.addEventListener('popstate', onHashChange);
  return () => {
    listeners.delete(listener);
    window.removeEventListener('hashchange', onHashChange);
    window.removeEventListener('popstate', onHashChange);
  };
}

function getHash(): string {
  return window.location.hash || '#/';
}

export function useHash(): string {
  return useSyncExternalStore(subscribe, getHash, () => '#/');
}

export function useRoute(): Route {
  const hash = useHash();
  return useMemo(() => parseRoute(hash), [hash]);
}

export function activeHref(path: string): string {
  return routeHref(path);
}
