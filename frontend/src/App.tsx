import { useEffect, useState, useSyncExternalStore, type ReactElement } from 'react';
import { Shell } from './components/Shell';
import { LoginPage } from './pages/LoginPage';
import { DashboardPage } from './pages/DashboardPage';
import { RunPage } from './pages/RunPage';
import { AgentsPage } from './pages/AgentsPage';
import { ToolsPage } from './pages/ToolsPage';
import { KnowledgePage } from './pages/KnowledgePage';
import { useRoute } from './lib/router';
import {
  UNAUTHORIZED_EVENT,
  getSessionSnapshot,
  subscribeSession,
  type SessionSnapshot,
} from './lib/session';

const EMPTY_SESSION: SessionSnapshot = { token: null, user: null };

const TITLES: Record<string, string> = {
  dashboard: 'Dashboard',
  run: 'Run detail',
  agents: 'Agents',
  tools: 'Tool catalog',
  knowledge: 'Knowledge base',
};

export default function App() {
  const session = useSyncExternalStore(
    subscribeSession,
    getSessionSnapshot,
    () => EMPTY_SESSION,
  );
  const route = useRoute();
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    const onUnauthorized = (): void => {
      setNotice('Your session expired. Sign in again to continue.');
    };
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, []);

  useEffect(() => {
    if (session.token) setNotice(null);
  }, [session.token]);

  useEffect(() => {
    window.scrollTo(0, 0);
  }, [route]);

  useEffect(() => {
    const label = TITLES[route.name] ?? 'Mission control';
    document.title = `${label} · AgentForge`;
  }, [route]);

  if (!session.token) {
    return <LoginPage notice={notice} />;
  }

  let page: ReactElement;
  switch (route.name) {
    case 'run':
      page = <RunPage key={route.id} runId={route.id} />;
      break;
    case 'agents':
      page = <AgentsPage />;
      break;
    case 'tools':
      page = <ToolsPage />;
      break;
    case 'knowledge':
      page = <KnowledgePage />;
      break;
    default:
      page = <DashboardPage />;
  }

  return <Shell route={route}>{page}</Shell>;
}
