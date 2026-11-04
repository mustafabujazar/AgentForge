import { useEffect, useState, useSyncExternalStore, type ReactNode } from 'react';
import { api } from '../api/client';
import { useNow, usePolledAsync } from '../hooks/useApi';
import { cx, formatClock, initials } from '../lib/format';
import {
  clearSession,
  getSessionSnapshot,
  subscribeSession,
  type SessionSnapshot,
} from '../lib/session';
import { ROUTES, navigate, routeHref, type Route } from '../lib/router';
import { Icon, LogoMark, type IconName } from './Icon';
import { Spinner } from './ui';

const EMPTY_SESSION: SessionSnapshot = { token: null, user: null };

const NAV: Array<{ path: string; label: string; icon: IconName; route: Route['name'] }> = [
  { path: ROUTES.dashboard, label: 'Dashboard', icon: 'dashboard', route: 'dashboard' },
  { path: ROUTES.agents, label: 'Agents', icon: 'agents', route: 'agents' },
  { path: ROUTES.tools, label: 'Tools', icon: 'tools', route: 'tools' },
  { path: ROUTES.knowledge, label: 'Knowledge', icon: 'knowledge', route: 'knowledge' },
];

const PAGE_META: Record<Route['name'], { title: string; subtitle: string }> = {
  dashboard: { title: 'Dashboard', subtitle: 'Mission overview' },
  run: { title: 'Run detail', subtitle: 'Live orchestration trace' },
  agents: { title: 'Agents', subtitle: 'Capability registry' },
  tools: { title: 'Tool catalog', subtitle: 'Execution surface' },
  knowledge: { title: 'Knowledge base', subtitle: 'Retrieval corpus' },
};

export function Shell({ route, children }: { route: Route; children: ReactNode }) {
  const session = useSyncExternalStore(
    subscribeSession,
    getSessionSnapshot,
    () => EMPTY_SESSION,
  );
  const [menuOpen, setMenuOpen] = useState(false);
  const now = useNow(1000);
  const health = usePolledAsync(() => api.health(), [], { intervalMs: 20000 });

  const online = health.data !== null && !health.error;
  const checking = health.loading && health.data === null;
  const provider =
    typeof health.data?.llm_provider === 'string' ? health.data.llm_provider : null;
  const model = typeof health.data?.llm_model === 'string' ? health.data.llm_model : null;
  const database = typeof health.data?.database === 'string' ? health.data.database : null;
  const version = typeof health.data?.version === 'string' ? health.data.version : null;
  const page = PAGE_META[route.name];
  const user = session.user;

  useEffect(() => {
    setMenuOpen(false);
  }, [route]);

  useEffect(() => {
    if (!menuOpen) return;
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') setMenuOpen(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [menuOpen]);

  const logout = (): void => {
    clearSession();
    navigate(ROUTES.dashboard, { replace: true });
  };

  return (
    <div className={cx('app-shell', menuOpen && 'nav-open')}>
      <a className="skip-link" href="#main">
        Skip to content
      </a>

      <aside className={cx('sidebar', menuOpen && 'sidebar--open')} id="sidebar">
        <div className="brand">
          <LogoMark size={32} />
          <div className="brand-text">
            <span className="brand-name">AGENTFORGE</span>
            <span className="brand-sub">mission control</span>
          </div>
        </div>

        <nav className="nav" aria-label="Primary">
          <p className="nav-label">Workspace</p>
          {NAV.map((item) => {
            const active = route.name === item.route;
            return (
              <a
                key={item.path}
                href={routeHref(item.path)}
                className={cx('nav-item', active && 'is-active')}
                aria-current={active ? 'page' : undefined}
              >
                <Icon name={item.icon} size={17} className="nav-icon" />
                <span>{item.label}</span>
                {active ? <span className="nav-active-mark" aria-hidden="true" /> : null}
              </a>
            );
          })}
        </nav>

        <div className="sidebar-footer">
          <div className={cx('api-card', online ? 'is-online' : health.error ? 'is-offline' : '')}>
            <div className="api-card-head">
              <span
                className={cx(
                  'status-dot',
                  online && 'status-dot--pulse',
                  health.error && 'status-dot--offline',
                )}
                aria-hidden="true"
              />
              <span className="api-card-status" aria-live="polite">
                {online ? 'API online' : checking ? 'Connecting…' : 'API offline'}
              </span>
              {checking ? <Spinner size={11} /> : null}
            </div>
            <dl className="api-card-rows">
              <div>
                <dt>LLM</dt>
                <dd>{provider ? `${provider}${model ? ` · ${model}` : ''}` : '—'}</dd>
              </div>
              <div>
                <dt>Database</dt>
                <dd>{database ?? '—'}</dd>
              </div>
              <div>
                <dt>Version</dt>
                <dd>{version ?? (online ? '—' : 'unreachable')}</dd>
              </div>
            </dl>
          </div>

          <a
            className="nav-item nav-item--subtle"
            href="/docs"
            target="_blank"
            rel="noopener noreferrer"
          >
            <Icon name="terminal" size={16} className="nav-icon" />
            <span>API reference</span>
            <Icon name="external" size={13} className="nav-external" />
          </a>

          <div className="user-card">
            <span className="avatar" aria-hidden="true">
              {initials(user?.full_name ?? user?.email ?? 'Agent Operator')}
            </span>
            <div className="user-meta">
              <span className="user-name">{user?.full_name ?? 'Demo operator'}</span>
              <span className="user-email">{user?.email ?? 'offline session'}</span>
            </div>
            <button
              type="button"
              className="icon-btn"
              onClick={logout}
              aria-label="Sign out"
              title="Sign out"
            >
              <Icon name="logout" size={16} />
            </button>
          </div>
        </div>
      </aside>

      <button
        type="button"
        className={cx('nav-backdrop', menuOpen && 'is-visible')}
        onClick={() => setMenuOpen(false)}
        aria-label="Close navigation"
        tabIndex={menuOpen ? 0 : -1}
      />

      <div className="app-body">
        <header className="topbar">
          <button
            type="button"
            className="icon-btn topbar-menu"
            onClick={() => setMenuOpen(true)}
            aria-label="Open navigation"
            aria-expanded={menuOpen}
            aria-controls="sidebar"
          >
            <Icon name="menu" size={19} />
          </button>

          <div className="topbar-title">
            <span className="crumb">Mission control</span>
            <span className="crumb-sep" aria-hidden="true">
              /
            </span>
            <span className="topbar-page">{page.title}</span>
            <span className="topbar-subtitle">{page.subtitle}</span>
          </div>

          <div className="topbar-actions">
            <span className="topbar-clock" title="Local time">
              <Icon name="clock" size={13} />
              <span className="mono">{formatClock(new Date(now).toISOString())}</span>
            </span>
            <span
              className={cx(
                'api-pill',
                online ? 'api-pill--online' : health.error ? 'api-pill--offline' : 'api-pill--waiting',
              )}
              title={online ? `${provider ?? 'LLM'} · ${database ?? 'database'}` : 'Backend health'}
            >
              <span className="status-dot" aria-hidden="true" />
              {online ? 'API online' : checking ? 'Connecting' : 'API offline'}
            </span>
          </div>
        </header>

        <main className="main" id="main">
          {children}
        </main>
      </div>
    </div>
  );
}
