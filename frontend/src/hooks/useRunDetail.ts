import { useCallback, useEffect, useRef, useState } from 'react';
import { api, errorMessage, isApiError } from '../api/client';
import { isTerminalStatus } from '../lib/format';
import type { Artifact, ReportResponse, RunRead, TaskRead } from '../api/types';

export interface RunDetailResult {
  run: RunRead | null;
  tasks: TaskRead[];
  report: ReportResponse | null;
  artifacts: Artifact[];
  loading: boolean;
  error: string | null;
  lastUpdated: number | null;
  /** Immediately adopt a RunRead returned by cancel/approve endpoints. */
  applyRun: (run: RunRead) => void;
  reload: () => void;
}

/**
 * Loads run + tasks + report + artifacts and keeps them fresh while the run is
 * active (2.5s poll, silent). The SSE timeline is handled separately so the two
 * streams don't interfere.
 */
export function useRunDetail(runId: string | null): RunDetailResult {
  const [run, setRun] = useState<RunRead | null>(null);
  const [tasks, setTasks] = useState<TaskRead[]>([]);
  const [report, setReport] = useState<ReportResponse | null>(null);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [loading, setLoading] = useState(Boolean(runId));
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const [nonce, setNonce] = useState(0);

  const runIdRef = useRef<string | null>(runId);
  const terminalRef = useRef(false);
  const mountedRef = useRef(true);
  const inFlightRef = useRef(false);

  const refresh = useCallback(async (initial: boolean) => {
    const id = runIdRef.current;
    if (!id) return;
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    if (initial) setLoading(true);

    try {
      const nextRun = await api.getRun(id);
      if (!mountedRef.current) return;

      terminalRef.current = isTerminalStatus(nextRun.status);
      setRun(nextRun);
      setError(null);

      const [tasksResult, reportResult, artifactsResult] = await Promise.allSettled([
        api.getTasks(id),
        terminalRef.current || nextRun.final_report
          ? api.getReport(id)
          : Promise.resolve(null),
        api.getArtifacts(id),
      ]);
      if (!mountedRef.current) return;

      if (tasksResult.status === 'fulfilled' && Array.isArray(tasksResult.value)) {
        setTasks(tasksResult.value);
      }

      const reportData = reportResult.status === 'fulfilled' ? reportResult.value : null;
      if (reportData) setReport(reportData);

      let nextArtifacts: Artifact[] | null = null;
      if (
        artifactsResult.status === 'fulfilled' &&
        artifactsResult.value &&
        Array.isArray(artifactsResult.value.items)
      ) {
        nextArtifacts = artifactsResult.value.items;
      } else if (reportData?.artifacts) {
        nextArtifacts = reportData.artifacts;
      } else if (Array.isArray(nextRun.artifacts)) {
        nextArtifacts = nextRun.artifacts;
      }
      if (nextArtifacts) setArtifacts(nextArtifacts);

      setLastUpdated(Date.now());
    } catch (caught) {
      if (!mountedRef.current) return;
      if (isApiError(caught) && caught.status === 401) return;
      if (isApiError(caught) && caught.status === 404) {
        setError('Run not found — it may have been removed or the link is stale.');
      } else {
        setError(errorMessage(caught));
      }
    } finally {
      inFlightRef.current = false;
      if (mountedRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    runIdRef.current = runId;
    terminalRef.current = false;
    setRun(null);
    setTasks([]);
    setReport(null);
    setArtifacts([]);
    setError(null);
    setLastUpdated(null);
    setLoading(Boolean(runId));

    if (!runId) return;

    void refresh(true);
    const timer = window.setInterval(() => {
      if (!terminalRef.current) void refresh(false);
    }, 2500);

    return () => {
      mountedRef.current = false;
      window.clearInterval(timer);
    };
  }, [runId, refresh, nonce]);

  const reload = useCallback(() => setNonce((value) => value + 1), []);

  const applyRun = useCallback((next: RunRead) => {
    terminalRef.current = isTerminalStatus(next.status);
    setRun(next);
    setError(null);
  }, []);

  return {
    run,
    tasks,
    report,
    artifacts,
    loading,
    error,
    lastUpdated,
    applyRun,
    reload,
  };
}
