import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties } from 'react';
import { api, errorMessage } from '../api/client';
import type { MetricsResponse, RunRead } from '../api/types';
import { useAsync, useNow, usePolledAsync } from '../hooks/useApi';
import { ROUTES, navigate, routeHref } from '../lib/router';
import { agentColor, agentLabel } from '../lib/runData';
import {
  cx,
  formatCompact,
  formatDateTime,
  formatDuration,
  formatDurationSeconds,
  formatNumber,
  formatPercent,
  formatTokens,
  initials,
  relativeTime,
  runElapsedMs,
  shortId,
  statusMeta,
  truncate,
  type Tone,
} from '../lib/format';
import { Icon, type IconName } from '../components/Icon';
import {
  EmptyState,
  ErrorBanner,
  ErrorState,
  Kbd,
  Skeleton,
  SkeletonCard,
  SkeletonLines,
  Spinner,
  StatusBadge,
  Toggle,
} from '../components/ui';

interface ScenarioPreset {
  id: string;
  icon: IconName;
  title: string;
  description: string;
  objective: string;
}

const PRESETS: ScenarioPreset[] = [
  {
    id: 'sales',
    icon: 'gauge',
    title: 'Sales diagnostic',
    description: 'Find the three worst products and why',
    objective:
      'Analyze our sales performance, identify the three worst-performing products, investigate why they’re underperforming, and generate a report with recommendations.',
  },
  {
    id: 'sensitive',
    icon: 'lock',
    title: 'Sensitive execution',
    description: 'Pauses for human approval',
    objective:
      'Investigate the Q3 revenue variance between the sales database and the CRM, determine the affected enterprise accounts, and issue a corrective invoice adjustment for the top 10 accounts.',
  },
  {
    id: 'research',
    icon: 'search',
    title: 'Market research',
    description: 'Competitive pricing intel',
    objective:
      'Research the latest pricing moves of our three biggest competitors using internal knowledge and recent market notes, then recommend whether we should adjust our pricing strategy next quarter.',
  },
];

type RunFilter = 'all' | 'active' | 'completed' | 'failed';

const RUN_FILTERS: Array<{ id: RunFilter; label: string }> = [
  { id: 'all', label: 'All' },
  { id: 'active', label: 'Active' },
  { id: 'completed', label: 'Completed' },
  { id: 'failed', label: 'Failed' },
];

function matchesFilter(run: RunRead, filter: RunFilter): boolean {
  if (filter === 'all') return true;
  if (filter === 'active') {
    return run.status === 'queued' || run.status === 'running' || run.status === 'awaiting_approval';
  }
  return run.status === filter;
}

interface Kpi {
  label: string;
  value: string;
  hint: string;
  icon: IconName;
  tone: Tone;
}

function deriveKpis(metrics: MetricsResponse | null, runs: RunRead[]): Kpi[] {
  if (metrics) {
    return [
      {
        label: 'Success rate',
        value: metrics.success_rate !== null && metrics.success_rate !== undefined
          ? formatPercent(metrics.success_rate, 0)
          : '—',
        hint: `${formatNumber(metrics.completed ?? 0)} completed · ${formatNumber(metrics.failed ?? 0)} failed`,
        icon: 'gauge',
        tone: 'success',
      },
      {
        label: 'Avg duration',
        value: formatDurationSeconds(metrics.avg_duration_seconds ?? null),
        hint: 'per run, end-to-end',
        icon: 'clock',
        tone: 'info',
      },
      {
        label: 'Tokens used',
        value: formatTokens(metrics.total_tokens ?? 0),
        hint: `across ${formatNumber(metrics.total_runs ?? 0)} runs`,
        icon: 'bolt',
        tone: 'accent',
      },
      {
        label: 'Retries',
        value: formatNumber(metrics.total_retries ?? 0),
        hint:
          metrics.avg_eval_score !== null && metrics.avg_eval_score !== undefined
            ? `avg eval ${formatPercent(metrics.avg_eval_score, 0)}`
            : 'evaluator feedback loops',
        icon: 'retry',
        tone: 'warn',
      },
    ];
  }

  const completed = runs.filter((run) => run.status === 'completed').length;
  const failed = runs.filter((run) => run.status === 'failed').length;
  const finished = completed + failed;
  const tokens = runs.reduce((sum, run) => sum + (run.token_usage?.total_tokens ?? 0), 0);
  const attempts = runs.reduce(
    (sum, run) => sum + Math.max(0, ((run as { retries?: number }).retries ?? 0)),
    0,
  );

  return [
    {
      label: 'Success rate',
      value: finished > 0 ? formatPercent(completed / finished, 0) : '—',
      hint: `${completed} completed · ${failed} failed (recent)`,
      icon: 'gauge',
      tone: 'success',
    },
    {
      label: 'Avg duration',
      value: (() => {
        const finishedRuns = runs.filter(
          (run) => run.started_at && run.completed_at,
        );
        if (finishedRuns.length === 0) return '—';
        const total = finishedRuns.reduce((sum, run) => sum + (runElapsedMs(run) ?? 0), 0);
        return formatDuration(total / finishedRuns.length);
      })(),
      hint: 'derived from loaded runs',
      icon: 'clock',
      tone: 'info',
    },
    {
      label: 'Tokens used',
      value: formatTokens(tokens),
      hint: `across ${runs.length} loaded runs`,
      icon: 'bolt',
      tone: 'accent',
    },
    {
      label: 'Retries',
      value: formatNumber(attempts),
      hint: 'reported per run',
      icon: 'retry',
      tone: 'warn',
    },
  ];
}

export function DashboardPage() {
  const [objective, setObjective] = useState('');
  const [autoApprove, setAutoApprove] = useState(false);
  const [dataset, setDataset] = useState('');
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [activePreset, setActivePreset] = useState<string | null>(null);
  const [runs, setRuns] = useState<RunRead[]>([]);
  const [total, setTotal] = useState(0);
  const [runsLoading, setRunsLoading] = useState(true);
  const [runsError, setRunsError] = useState<string | null>(null);
  const [filter, setFilter] = useState<RunFilter>('all');
  const [loadingMore, setLoadingMore] = useState(false);
  const composerRef = useRef<HTMLTextAreaElement | null>(null);
  const runsRef = useRef<RunRead[]>([]);
  const now = useNow(5000);

  const metrics = usePolledAsync(() => api.getMetrics(), [], { intervalMs: 25000 });
  const agents = useAsync(() => api.listAgents(), []);

  useEffect(() => {
    runsRef.current = runs;
  }, [runs]);

  const refreshRuns = useCallback(async (silent: boolean) => {
    if (!silent) setRunsLoading(true);
    try {
      const limit = Math.max(20, runsRef.current.length);
      const response = await api.listRuns({ limit, offset: 0 });
      const items = Array.isArray(response?.items) ? response.items : [];
      setRuns(items);
      setTotal(typeof response?.total === 'number' ? response.total : items.length);
      setRunsError(null);
    } catch (error) {
      if (!silent) setRunsError(errorMessage(error));
    } finally {
      if (!silent) setRunsLoading(false);
    }
  }, []);

  useEffect(() => {
    void refreshRuns(false);
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void refreshRuns(true);
    }, 6000);
    return () => window.clearInterval(timer);
  }, [refreshRuns]);

  const loadMore = async (): Promise<void> => {
    if (loadingMore || runs.length >= total) return;
    setLoadingMore(true);
    try {
      const response = await api.listRuns({ limit: 20, offset: runs.length });
      const items = Array.isArray(response?.items) ? response.items : [];
      setRuns((previous) => {
        const seen = new Set(previous.map((run) => run.id));
        return [...previous, ...items.filter((item) => !seen.has(item.id))];
      });
      if (typeof response?.total === 'number') setTotal(response.total);
    } catch (error) {
      setRunsError(errorMessage(error));
    } finally {
      setLoadingMore(false);
    }
  };

  const createRun = async (): Promise<void> => {
    const trimmed = objective.trim();
    if (!trimmed || submitting) return;
    setSubmitting(true);
    setCreateError(null);
    try {
      const run = await api.createRun({
        objective: trimmed,
        auto_approve: autoApprove || undefined,
        dataset: dataset.trim() || undefined,
      });
      navigate(ROUTES.run(run.id));
    } catch (error) {
      setCreateError(errorMessage(error));
    } finally {
      setSubmitting(false);
    }
  };

  const filteredRuns = useMemo(
    () => runs.filter((run) => matchesFilter(run, filter)),
    [runs, filter],
  );
  const kpis = useMemo(() => deriveKpis(metrics.data, runs), [metrics.data, runs]);
  const metricsLoading = metrics.loading && !metrics.data;
  const agentList = agents.data?.agents ?? [];

  const applyPreset = (preset: ScenarioPreset): void => {
    setObjective(preset.objective);
    setActivePreset(preset.id);
    setCreateError(null);
    composerRef.current?.focus();
  };

  return (
    <div className="page page--dashboard">
      {/* ---------------------------------------------------------- */}
      {/* Hero composer                                              */}
      {/* ---------------------------------------------------------- */}
      <section className="hero card" aria-labelledby="hero-title">
        <div className="hero-grid" aria-hidden="true" />
        <div className="hero-glow" aria-hidden="true" />
        <div className="hero-inner">
          <div className="hero-kicker">
            <span className="live-dot" aria-hidden="true" />
            New mission
          </div>
          <h1 id="hero-title" className="hero-title">
            What should the agents accomplish?
          </h1>
          <p className="hero-lede">
            Describe a business objective. AgentForge will plan the work, route it to specialist
            agents, execute tools, evaluate the output and synthesize a deliverable — with an audit
            trail.
          </p>

          <form
            className="composer"
            onSubmit={(event) => {
              event.preventDefault();
              void createRun();
            }}
          >
            <div className="composer-box">
              <label className="sr-only" htmlFor="objective">
                Mission objective
              </label>
              <textarea
                id="objective"
                ref={composerRef}
                className="composer-input"
                rows={4}
                maxLength={2000}
                value={objective}
                onChange={(event) => {
                  setObjective(event.target.value);
                  setActivePreset(null);
                }}
                onKeyDown={(event) => {
                  if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
                    event.preventDefault();
                    void createRun();
                  }
                }}
                placeholder="e.g. Analyze our sales performance, identify the three worst-performing products, investigate why they’re underperforming, and generate a report with recommendations."
                spellCheck={false}
              />
              <div className="composer-foot">
                <span className="composer-hint">
                  <Kbd>Ctrl</Kbd> <span className="hint-plus">+</span> <Kbd>Enter</Kbd> to launch
                </span>
                <span className="char-count">{objective.length}/2000</span>
                <button
                  type="submit"
                  className="btn btn-primary btn-lg"
                  disabled={submitting || !objective.trim()}
                >
                  {submitting ? (
                    <>
                      <Spinner size={15} />
                      Deploying agents…
                    </>
                  ) : (
                    <>
                      Launch run
                      <Icon name="send" size={16} />
                    </>
                  )}
                </button>
              </div>
            </div>

            {createError ? (
              <ErrorBanner message={createError} onDismiss={() => setCreateError(null)} />
            ) : null}

            <div className="presets" role="group" aria-label="Scenario presets">
              <span className="presets-label">
                <Icon name="sparkles" size={13} />
                Scenarios
              </span>
              {PRESETS.map((preset) => (
                <button
                  key={preset.id}
                  type="button"
                  className={cx('preset-chip', activePreset === preset.id && 'is-active')}
                  onClick={() => applyPreset(preset)}
                  aria-pressed={activePreset === preset.id}
                >
                  <span className={cx('preset-icon', preset.id === 'sensitive' && 'preset-icon--warn')}>
                    <Icon name={preset.icon} size={15} />
                  </span>
                  <span className="preset-text">
                    <strong>{preset.title}</strong>
                    <small>{preset.description}</small>
                  </span>
                </button>
              ))}
            </div>

            <div className="composer-options">
              <Toggle
                checked={autoApprove}
                onChange={setAutoApprove}
                label="Auto-approve sensitive actions"
                description="Bypass the human approval gate for this run"
              />
              <div className="composer-advanced">
                <button
                  type="button"
                  className="link-btn"
                  onClick={() => setAdvancedOpen((value) => !value)}
                  aria-expanded={advancedOpen}
                >
                  <Icon name={advancedOpen ? 'chevronDown' : 'chevronRight'} size={13} />
                  Advanced
                </button>
                {advancedOpen ? (
                  <div className="field field--compact">
                    <label className="field-label" htmlFor="dataset">
                      Dataset override
                    </label>
                    <input
                      id="dataset"
                      className="input input--mono"
                      value={dataset}
                      onChange={(event) => setDataset(event.target.value)}
                      placeholder="default (demo sales database)"
                      autoComplete="off"
                    />
                  </div>
                ) : null}
              </div>
            </div>
          </form>
        </div>
      </section>

      {/* ---------------------------------------------------------- */}
      {/* KPI strip                                                  */}
      {/* ---------------------------------------------------------- */}
      <section className="kpi-grid" aria-label="Aggregate run metrics">
        {metricsLoading
          ? Array.from({ length: 4 }).map((_, index) => (
              <SkeletonCard key={index} height={104} />
            ))
          : kpis.map((kpi) => (
              <article className={cx('kpi-card card', `tone-${kpi.tone}`)} key={kpi.label}>
                <div className="kpi-head">
                  <span className="kpi-label">{kpi.label}</span>
                  <span className="kpi-icon">
                    <Icon name={kpi.icon} size={15} />
                  </span>
                </div>
                <p className="kpi-value">{kpi.value}</p>
                <p className="kpi-hint">{kpi.hint}</p>
                <span className="kpi-spark" aria-hidden="true" />
              </article>
            ))}
      </section>
      <p className="metrics-note">
        {metrics.error
          ? 'Aggregate metrics unavailable — KPI figures are derived from the loaded runs.'
          : 'Aggregated live from /api/metrics/runs.'}
      </p>

      {/* ---------------------------------------------------------- */}
      {/* Recent runs + aside                                        */}
      {/* ---------------------------------------------------------- */}
      <div className="dashboard-grid">
        <section className="card panel runs-panel" aria-labelledby="runs-title">
          <header className="panel-head">
            <div className="panel-heading">
              <span className="panel-icon">
                <Icon name="runs" size={16} />
              </span>
              <div>
                <h2 className="panel-title" id="runs-title">
                  Recent runs
                </h2>
                <p className="panel-subtitle">
                  {runsLoading && runs.length === 0
                    ? 'Loading missions…'
                    : `${filteredRuns.length} shown${total > runs.length ? ` · ${total} total` : ''}`}
                </p>
              </div>
            </div>
            <div className="panel-actions">
              <button
                type="button"
                className="icon-btn"
                onClick={() => void refreshRuns(false)}
                aria-label="Refresh runs"
                title="Refresh runs"
                disabled={runsLoading}
              >
                {runsLoading ? <Spinner size={14} /> : <Icon name="refresh" size={15} />}
              </button>
            </div>
          </header>

          <div className="run-filters" role="group" aria-label="Filter runs">
            {RUN_FILTERS.map((option) => (
              <button
                key={option.id}
                type="button"
                className={cx('filter-chip', filter === option.id && 'is-active')}
                onClick={() => setFilter(option.id)}
                aria-pressed={filter === option.id}
              >
                {option.label}
                <span className="filter-count">
                  {runs.filter((run) => matchesFilter(run, option.id)).length}
                </span>
              </button>
            ))}
          </div>

          <div className="panel-body runs-body">
            {runsError ? (
              <ErrorState
                message={runsError}
                onRetry={() => void refreshRuns(false)}
                title="Could not load runs"
                compact
              />
            ) : runsLoading && runs.length === 0 ? (
              <div className="runs-skeleton" aria-label="Loading runs">
                {Array.from({ length: 4 }).map((_, index) => (
                  <div className="run-row-skeleton" key={index}>
                    <Skeleton width={8} height={8} />
                    <div className="run-row-skeleton-body">
                      <Skeleton width="68%" height={13} />
                      <Skeleton width="38%" height={10} />
                    </div>
                    <Skeleton width={76} height={22} />
                  </div>
                ))}
              </div>
            ) : filteredRuns.length === 0 ? (
              <EmptyState
                icon="runs"
                title={runs.length === 0 ? 'No missions yet' : 'No runs match this filter'}
                description={
                  runs.length === 0
                    ? 'Launch your first mission above — the full orchestration trace will appear here.'
                    : 'Switch filters or launch a new run to populate this view.'
                }
                action={
                  runs.length === 0 ? (
                    <button
                      type="button"
                      className="btn btn-primary btn-sm"
                      onClick={() => composerRef.current?.focus()}
                    >
                      <Icon name="bolt" size={14} />
                      Compose a mission
                    </button>
                  ) : undefined
                }
              />
            ) : (
              <>
                <ul className="run-list">
                  {filteredRuns.map((run) => {
                    const meta = statusMeta(run.status);
                    const elapsed = runElapsedMs(run, now);
                    const taskCount = run.plan?.tasks?.length ?? 0;
                    const tokens = run.token_usage?.total_tokens ?? 0;
                    return (
                      <li key={run.id}>
                        <a
                          className="run-row"
                          href={routeHref(ROUTES.run(run.id))}
                          aria-label={`Open run: ${truncate(run.objective, 80)}`}
                        >
                          <span
                            className={cx('run-status-line', `tone-${meta.tone}`)}
                            aria-hidden="true"
                          />
                          <div className="run-row-main">
                            <div className="run-row-top">
                              <span className="run-objective">
                                {truncate(run.objective, 140)}
                              </span>
                              <StatusBadge status={run.status} size="sm" />
                            </div>
                            <div className="run-row-meta">
                              <span className="mono run-id">{shortId(run.id)}</span>
                              <span>{relativeTime(run.created_at, now)}</span>
                              {taskCount > 0 ? <span>{taskCount} tasks</span> : null}
                              {elapsed !== null ? (
                                <span className="run-duration">
                                  <Icon name="clock" size={11} />
                                  {formatDuration(elapsed)}
                                </span>
                              ) : null}
                              {tokens > 0 ? (
                                <span className="run-tokens">
                                  <Icon name="bolt" size={11} />
                                  {formatCompact(tokens)}
                                </span>
                              ) : null}
                              {run.pending_approval ? (
                                <span className="run-approval-flag">
                                  <Icon name="lock" size={11} />
                                  needs approval
                                </span>
                              ) : null}
                            </div>
                          </div>
                          <Icon name="chevronRight" size={16} className="run-chevron" />
                        </a>
                      </li>
                    );
                  })}
                </ul>
                {runs.length < total ? (
                  <button
                    type="button"
                    className="btn btn-ghost btn-block load-more"
                    onClick={() => void loadMore()}
                    disabled={loadingMore}
                  >
                    {loadingMore ? <Spinner size={14} /> : <Icon name="chevronDown" size={14} />}
                    Load more runs
                  </button>
                ) : null}
              </>
            )}
          </div>
        </section>

        <aside className="dashboard-aside">
          <section className="card panel roster-panel" aria-labelledby="roster-title">
            <header className="panel-head">
              <div className="panel-heading">
                <span className="panel-icon">
                  <Icon name="agents" size={16} />
                </span>
                <div>
                  <h2 className="panel-title" id="roster-title">
                    Agent roster
                  </h2>
                  <p className="panel-subtitle">
                    {agents.loading && !agents.data
                      ? 'Loading capabilities…'
                      : `${agentList.length} specialist agents`}
                  </p>
                </div>
              </div>
            </header>
            <div className="panel-body roster-body">
              {agents.error ? (
                <ErrorState
                  message={agents.error}
                  onRetry={agents.reload}
                  title="Roster unavailable"
                  compact
                />
              ) : agents.loading && !agents.data ? (
                <div className="roster-skeleton">
                  {Array.from({ length: 3 }).map((_, index) => (
                    <div className="roster-card" key={index}>
                      <Skeleton width="46%" height={12} />
                      <SkeletonLines lines={2} />
                    </div>
                  ))}
                </div>
              ) : agentList.length === 0 ? (
                <EmptyState
                  compact
                  icon="agents"
                  title="No agents registered"
                  description="The capability registry is empty."
                />
              ) : (
                <div className="roster">
                  {agentList.map((agent) => (
                    <article className="roster-card" key={agent.name}>
                      <div className="roster-head">
                        <span
                          className="agent-avatar"
                          style={{ '--agent-color': agentColor(agent.name) } as CSSProperties}
                        >
                          {initials(agentLabel(agent.name))}
                        </span>
                        <div className="roster-id">
                          <h3>{agentLabel(agent.name)}</h3>
                          <p>{agent.role ?? 'Specialist agent'}</p>
                        </div>
                        {Array.isArray(agent.tools) && agent.tools.length > 0 ? (
                          <span
                            className="roster-tools"
                            title={agent.tools.join(', ')}
                          >
                            <Icon name="tools" size={12} />
                            {agent.tools.length}
                          </span>
                        ) : null}
                      </div>
                      {agent.description ? (
                        <p className="roster-desc">{truncate(agent.description, 130)}</p>
                      ) : null}
                      {Array.isArray(agent.capabilities) && agent.capabilities.length > 0 ? (
                        <div className="roster-chips">
                          {agent.capabilities.slice(0, 3).map((capability) => (
                            <span className="tag tag--tiny" key={capability}>
                              {capability}
                            </span>
                          ))}
                          {agent.capabilities.length > 3 ? (
                            <span className="tag tag--tiny tag--muted">
                              +{agent.capabilities.length - 3}
                            </span>
                          ) : null}
                        </div>
                      ) : null}
                    </article>
                  ))}
                </div>
              )}
            </div>
          </section>

          <section className="card panel loop-panel" aria-labelledby="loop-title">
            <header className="panel-head">
              <div className="panel-heading">
                <span className="panel-icon">
                  <Icon name="activity" size={16} />
                </span>
                <div>
                  <h2 className="panel-title" id="loop-title">
                    Execution loop
                  </h2>
                  <p className="panel-subtitle">Governed by the orchestrator</p>
                </div>
              </div>
            </header>
            <div className="panel-body">
              <ol className="loop-steps">
                {[
                  ['layers', 'Plan', 'LLM planner decomposes the objective into 3–6 tasks.'],
                  ['agents', 'Route', 'Capability registry validates agent assignment.'],
                  ['tools', 'Execute', 'Agents call read-only tools with scoped context.'],
                  ['gauge', 'Evaluate', 'LLM judge scores 0–1; low scores retry with feedback.'],
                  ['lock', 'Approve', 'Sensitive actions pause for a human decision.'],
                  ['file', 'Synthesize', 'Final Markdown report + artifacts are persisted.'],
                ].map(([icon, label, copy], index) => (
                  <li className="loop-step" key={label}>
                    <span className="loop-index">{String(index + 1).padStart(2, '0')}</span>
                    <span className="loop-icon">
                      <Icon name={icon as IconName} size={13} />
                    </span>
                    <div>
                      <strong>{label}</strong>
                      <p>{copy}</p>
                    </div>
                  </li>
                ))}
              </ol>
            </div>
          </section>
        </aside>
      </div>

      <footer className="dashboard-footer">
        <span>AgentForge v1.0</span>
        <span className="dot-sep">·</span>
        <span>governed multi-agent execution</span>
        <span className="dot-sep">·</span>
        <span className="mono">{formatDateTime(new Date().toISOString())}</span>
      </footer>
    </div>
  );
}
