import { useEffect, useMemo, useState } from 'react';
import type { ApprovalDecision, Artifact } from '../api/types';
import { api, errorMessage } from '../api/client';
import { useRunDetail } from '../hooks/useRunDetail';
import { useLiveEvents } from '../hooks/useLiveEvents';
import { useNow } from '../hooks/useApi';
import { planTasksOf, successCriteriaOf, buildTaskViews } from '../lib/runData';
import {
  artifactsOf,
  cx,
  formatCompact,
  formatDateTime,
  formatDuration,
  formatNumber,
  isTerminalStatus,
  relativeTime,
  shortId,
} from '../lib/format';
import { saveBlob } from '../lib/download';
import { ROUTES, routeHref } from '../lib/router';
import { Icon } from '../components/Icon';
import { ApprovalBanner } from '../components/ApprovalBanner';
import { Artifacts } from '../components/Artifacts';
import { ReportPanel } from '../components/ReportPanel';
import { TaskBoard } from '../components/TaskBoard';
import { Timeline } from '../components/Timeline';
import { TokenUsage } from '../components/TokenUsage';
import { WorkflowGraph } from '../components/WorkflowGraph';
import {
  CopyButton,
  EmptyState,
  ErrorBanner,
  ErrorState,
  Panel,
  SkeletonCard,
  SkeletonLines,
  Spinner,
  StatusBadge,
} from '../components/ui';

function emptyHint(status: string | null | undefined): string {
  switch (status) {
    case 'queued':
      return 'The run is queued. The planner will publish the task graph shortly.';
    case 'running':
      return 'The planner is decomposing the objective — tasks will appear as they are dispatched.';
    case 'failed':
      return 'The run failed before a plan was published. Inspect the activity log for the failure point.';
    case 'cancelled':
      return 'The run was cancelled before the plan was published.';
    case 'completed':
      return 'This run completed without a recorded plan.';
    default:
      return 'The planner has not published its task graph for this run.';
  }
}

export function RunPage({ runId }: { runId: string }) {
  const {
    run,
    tasks,
    report,
    artifacts,
    loading,
    error,
    lastUpdated,
    applyRun,
    reload,
  } = useRunDetail(runId);
  const terminal = isTerminalStatus(run?.status);
  const { events, connection } = useLiveEvents(runId, terminal);
  const now = useNow(1000);

  const [highlightedTask, setHighlightedTask] = useState<string | null>(null);
  const [deciding, setDeciding] = useState<ApprovalDecision | null>(null);
  const [approvalError, setApprovalError] = useState<string | null>(null);
  const [cancelBusy, setCancelBusy] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [downloadingId, setDownloadingId] = useState<string | number | null>(null);
  const [downloadError, setDownloadError] = useState<string | null>(null);

  const taskViews = useMemo(
    () => buildTaskViews(run?.plan, tasks, events),
    [run?.plan, tasks, events],
  );
  const planSummary = typeof run?.plan?.summary === 'string' ? run.plan.summary : null;
  const criteria = successCriteriaOf(run?.plan);
  const reportText = report?.report ?? run?.final_report ?? null;
  const completedTasks = taskViews.filter((task) => task.status === 'completed').length;
  const retryEvents = events.filter((event) => event.type === 'task.retry').length;
  const activeStatus = Boolean(run) && !terminal;
  const elapsed = run
    ? (() => {
        const start = run.started_at ?? run.created_at;
        if (!start) return null;
        const startMs = new Date(start).getTime();
        if (Number.isNaN(startMs)) return null;
        const endMs = run.completed_at ? new Date(run.completed_at).getTime() : now;
        return Math.max(0, endMs - startMs);
      })()
    : null;

  useEffect(() => {
    if (!activeStatus) setConfirmCancel(false);
  }, [activeStatus]);

  useEffect(() => {
    if (!confirmCancel) return;
    const timer = window.setTimeout(() => setConfirmCancel(false), 5000);
    return () => window.clearTimeout(timer);
  }, [confirmCancel]);

  const decideApproval = async (decision: ApprovalDecision, note: string): Promise<void> => {
    if (!run?.pending_approval) return;
    setDeciding(decision);
    setApprovalError(null);
    try {
      const next = await api.submitApproval(run.id, run.pending_approval.id, decision, note);
      applyRun(next);
    } catch (caught) {
      setApprovalError(errorMessage(caught));
    } finally {
      setDeciding(null);
    }
  };

  const cancelRun = async (): Promise<void> => {
    if (!run || cancelBusy) return;
    setCancelBusy(true);
    setCancelError(null);
    try {
      const next = await api.cancelRun(run.id);
      applyRun(next);
      setConfirmCancel(false);
    } catch (caught) {
      setCancelError(errorMessage(caught));
    } finally {
      setCancelBusy(false);
    }
  };

  const downloadArtifact = async (artifact: Artifact): Promise<void> => {
    if (artifact.id === undefined || artifact.id === null) return;
    setDownloadingId(artifact.id);
    setDownloadError(null);
    try {
      const blob = await api.downloadArtifact(artifact.id);
      saveBlob(blob, artifactsOf(artifact).name);
    } catch (caught) {
      setDownloadError(errorMessage(caught));
    } finally {
      setDownloadingId(null);
    }
  };

  /* ---------------------------------------------------------------- */
  /* Loading / not-found states                                       */
  /* ---------------------------------------------------------------- */
  if (loading && !run) {
    return (
      <div className="page page--run">
        <div className="run-topline">
          <a className="back-link" href={routeHref(ROUTES.dashboard)}>
            <Icon name="arrowLeft" size={14} /> All runs
          </a>
        </div>
        <div className="card run-header run-header--skeleton">
          <SkeletonLines lines={1} />
          <div style={{ height: 14 }} />
          <SkeletonLines lines={2} />
          <div style={{ height: 20 }} />
          <SkeletonLines lines={1} />
        </div>
        <div className="run-grid">
          <div className="run-main-col">
            <SkeletonCard height={320} />
            <SkeletonCard height={260} />
          </div>
          <aside className="run-side-col">
            <SkeletonCard height={220} />
            <SkeletonCard height={180} />
          </aside>
        </div>
      </div>
    );
  }

  if (!run) {
    return (
      <div className="page page--run">
        <div className="run-topline">
          <a className="back-link" href={routeHref(ROUTES.dashboard)}>
            <Icon name="arrowLeft" size={14} /> All runs
          </a>
        </div>
        <ErrorState
          title="Run unavailable"
          message={error ?? 'This run could not be loaded.'}
          onRetry={reload}
        />
      </div>
    );
  }

  const syncedLabel =
    lastUpdated === null ? 'syncing…' : `synced ${Math.max(0, Math.round((now - lastUpdated) / 1000))}s ago`;

  return (
    <div className="page page--run">
      <div className="run-topline">
        <a className="back-link" href={routeHref(ROUTES.dashboard)}>
          <Icon name="arrowLeft" size={14} /> All runs
        </a>
        <span className="run-synced" aria-live="polite">
          <span className={cx('sync-dot', activeStatus && 'sync-dot--active')} aria-hidden="true" />
          {syncedLabel}
        </span>
      </div>

      {error ? <ErrorBanner message={error} onDismiss={() => reload()} /> : null}

      {/* ---------------------------------------------------------- */}
      {/* Run header                                                 */}
      {/* ---------------------------------------------------------- */}
      <header className="card run-header">
        <div className="run-header-main">
          <div className="run-head-row">
            <StatusBadge status={run.status} />
            <span className="run-id-chip mono" title={run.id}>
              run_{shortId(run.id, 10)}
            </span>
            <CopyButton value={run.id} compact label="Copy run id" />
            {run.dataset ? <span className="tag tag--mono">{run.dataset}</span> : null}
            {run.pending_approval ? (
              <span className="tag tag--warn">
                <Icon name="lock" size={11} />
                approval pending
              </span>
            ) : null}
          </div>
          <h1 className="run-objective">{run.objective}</h1>

          <dl className="run-meta-grid">
            <div className="run-meta-item">
              <dt>Created</dt>
              <dd>{formatDateTime(run.created_at)}</dd>
            </div>
            <div className="run-meta-item">
              <dt>Started</dt>
              <dd>{run.started_at ? relativeTime(run.started_at, now) : '—'}</dd>
            </div>
            <div className="run-meta-item">
              <dt>Elapsed</dt>
              <dd className="mono">{elapsed !== null ? formatDuration(elapsed) : '—'}</dd>
            </div>
            <div className="run-meta-item">
              <dt>Tasks</dt>
              <dd className="mono">
                {completedTasks}/{taskViews.length}
              </dd>
            </div>
            <div className="run-meta-item">
              <dt>Tokens</dt>
              <dd className="mono">{formatCompact(run.token_usage?.total_tokens ?? 0)}</dd>
            </div>
            <div className="run-meta-item">
              <dt>Retries</dt>
              <dd className="mono">{formatNumber(retryEvents)}</dd>
            </div>
          </dl>
        </div>

        <div className="run-header-actions">
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={reload}
            disabled={loading}
          >
            {loading ? <Spinner size={14} /> : <Icon name="refresh" size={14} />}
            Refresh
          </button>
          {activeStatus ? (
            <button
              type="button"
              className={cx('btn', 'btn-sm', confirmCancel ? 'btn-danger' : 'btn-ghost-danger')}
              onClick={() => {
                if (!confirmCancel) {
                  setConfirmCancel(true);
                  return;
                }
                void cancelRun();
              }}
              disabled={cancelBusy}
            >
              {cancelBusy ? <Spinner size={14} /> : <Icon name="ban" size={14} />}
              {confirmCancel ? 'Confirm cancel' : 'Cancel run'}
            </button>
          ) : null}
        </div>
      </header>

      {cancelError ? (
        <ErrorBanner message={cancelError} onDismiss={() => setCancelError(null)} />
      ) : null}

      {run.pending_approval ? (
        <ApprovalBanner
          approval={run.pending_approval}
          deciding={deciding}
          error={approvalError}
          onDecision={(decision, note) => void decideApproval(decision, note)}
        />
      ) : null}

      {run.error ? (
        <section className="card run-error" role="alert">
          <span className="run-error-icon">
            <Icon name="alert" size={18} />
          </span>
          <div>
            <h2 className="run-error-title">Run failed</h2>
            <pre className="run-error-text">{run.error}</pre>
          </div>
        </section>
      ) : null}

      {/* ---------------------------------------------------------- */}
      {/* Main grid                                                  */}
      {/* ---------------------------------------------------------- */}
      <div className="run-grid">
        <div className="run-main-col">
          <Timeline
            events={events}
            connection={connection}
            terminal={terminal}
            loading={loading && events.length === 0}
          />
          <ReportPanel
            report={reportText}
            loading={loading && !terminal}
            terminal={terminal}
            failed={run.status === 'failed'}
          />
        </div>

        <aside className="run-side-col">
          <Panel
            title="Workflow graph"
            subtitle={`${taskViews.length} planned task${taskViews.length === 1 ? '' : 's'}`}
            icon={<Icon name="layers" size={16} />}
            bodyClassName="panel-body--graph"
          >
            <WorkflowGraph
              tasks={taskViews}
              selectedId={highlightedTask}
              onSelect={(id) => setHighlightedTask((current) => (current === id ? null : id))}
            />
          </Panel>

          <Panel
            title="Token usage"
            subtitle="LLM accounting"
            icon={<Icon name="bolt" size={16} />}
          >
            <TokenUsage usage={run.token_usage} />
          </Panel>

          <Panel
            title="Artifacts"
            subtitle={`${artifacts.length} file${artifacts.length === 1 ? '' : 's'}`}
            icon={<Icon name="file" size={16} />}
          >
            <Artifacts
              artifacts={artifacts}
              downloadingId={downloadingId}
              error={downloadError}
              onDownload={(artifact) => void downloadArtifact(artifact)}
            />
          </Panel>

          {planSummary || criteria.length > 0 ? (
            <Panel
              title="Plan"
              subtitle="Planner output"
              icon={<Icon name="list" size={16} />}
            >
              <div className="plan-panel">
                {planSummary ? <p className="plan-summary">{planSummary}</p> : null}
                {criteria.length > 0 ? (
                  <>
                    <p className="plan-criteria-label">Success criteria</p>
                    <ul className="criteria-list">
                      {criteria.map((criterion, index) => (
                        <li key={index}>{criterion}</li>
                      ))}
                    </ul>
                  </>
                ) : null}
              </div>
            </Panel>
          ) : null}
        </aside>
      </div>

      {/* ---------------------------------------------------------- */}
      {/* Task board                                                 */}
      {/* ---------------------------------------------------------- */}
      <section className="card panel task-panel" aria-labelledby="tasks-title">
        <header className="panel-head">
          <div className="panel-heading">
            <span className="panel-icon">
              <Icon name="layers" size={16} />
            </span>
            <div>
              <h2 className="panel-title" id="tasks-title">
                Plan &amp; task board
              </h2>
              <p className="panel-subtitle">
                {taskViews.length > 0
                  ? `${completedTasks}/${taskViews.length} completed · ${retryEvents} retr${retryEvents === 1 ? 'y' : 'ies'} · per-task evaluation`
                  : 'Awaiting planner output'}
              </p>
            </div>
          </div>
          <div className="panel-actions">
            {planTasksOf(run.plan).length > 0 ? (
              <span className="tag tag--mono">plan v1</span>
            ) : (
              <span className="tag tag--muted">
                <Icon name="clock" size={11} /> pending
              </span>
            )}
          </div>
        </header>
        <div className="panel-body">
          {taskViews.length > 0 ? (
            <TaskBoard tasks={taskViews} highlightedId={highlightedTask} />
          ) : (
            <EmptyState
              icon="layers"
              title="No tasks recorded"
              description={emptyHint(run.status)}
            />
          )}
        </div>
      </section>
    </div>
  );
}
