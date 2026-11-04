import type { CSSProperties } from 'react';
import { api } from '../api/client';
import { useAsync } from '../hooks/useApi';
import { agentColor, agentLabel } from '../lib/runData';
import { initials } from '../lib/format';
import { Icon } from '../components/Icon';
import { EmptyState, ErrorState, SkeletonLines } from '../components/ui';

export function AgentsPage() {
  const agents = useAsync(() => api.listAgents(), []);
  const list = agents.data?.agents ?? [];

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <p className="kicker">Capability registry</p>
          <h1 className="page-title">Specialist agents</h1>
          <p className="page-lede">
            The router validates every plan task against these agent capabilities before dispatch.
            Each agent exposes the tools it is allowed to call.
          </p>
        </div>
        <span className="count-pill">
          <Icon name="agents" size={13} />
          {agents.loading && !agents.data ? 'Loading…' : `${list.length} registered`}
        </span>
      </header>

      {agents.error ? (
        <ErrorState message={agents.error} onRetry={agents.reload} title="Could not load agents" />
      ) : agents.loading && !agents.data ? (
        <div className="agent-grid" aria-label="Loading agents">
          {Array.from({ length: 4 }).map((_, index) => (
            <div className="card agent-card" key={index}>
              <SkeletonLines lines={2} />
              <div style={{ height: 14 }} />
              <SkeletonLines lines={3} />
            </div>
          ))}
        </div>
      ) : list.length === 0 ? (
        <EmptyState
          icon="agents"
          title="No agents registered"
          description="The capability registry is empty — the orchestrator will be unable to route planned tasks."
          action={
            <button type="button" className="btn btn-ghost btn-sm" onClick={agents.reload}>
              <Icon name="refresh" size={14} /> Retry
            </button>
          }
        />
      ) : (
        <div className="agent-grid">
          {list.map((agent) => {
            const capabilities = Array.isArray(agent.capabilities) ? agent.capabilities : [];
            const tools = Array.isArray(agent.tools) ? agent.tools : [];
            const style = {
              '--agent-color': agentColor(agent.name),
            } as CSSProperties;
            return (
              <article className="card agent-card" key={agent.name}>
                <header className="agent-card-head">
                  <span className="agent-avatar agent-avatar--lg" style={style} aria-hidden="true">
                    {initials(agentLabel(agent.name))}
                  </span>
                  <div className="agent-card-id">
                    <h2 className="agent-name">{agentLabel(agent.name)}</h2>
                    <p className="agent-role">{agent.role ?? 'Specialist agent'}</p>
                  </div>
                  <span className="tag tag--mono" title="Tool count">
                    <Icon name="tools" size={11} />
                    {tools.length}
                  </span>
                </header>

                <p className="agent-desc">
                  {agent.description ?? 'No description provided by the capability registry.'}
                </p>

                <div className="agent-section">
                  <span className="stat-label">Capabilities</span>
                  {capabilities.length > 0 ? (
                    <div className="tag-wrap">
                      {capabilities.map((capability) => (
                        <span className="tag" key={capability}>
                          {capability}
                        </span>
                      ))}
                    </div>
                  ) : (
                    <span className="muted-text">None declared</span>
                  )}
                </div>

                <div className="agent-section">
                  <span className="stat-label">Tools</span>
                  {tools.length > 0 ? (
                    <div className="tag-wrap">
                      {tools.map((tool) => (
                        <span className="tag tag--mono tag--tiny" key={tool}>
                          {tool}
                        </span>
                      ))}
                    </div>
                  ) : (
                    <span className="muted-text">No tools bound</span>
                  )}
                </div>
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}
