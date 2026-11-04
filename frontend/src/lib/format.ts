/** Small formatting + normalization helpers shared across the dashboard. */

export function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ');
}

export function shortId(id: string | null | undefined, length = 8): string {
  if (!id) return '—';
  return id.length <= length ? id : id.slice(0, length);
}

export function titleCase(value: string): string {
  return value
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/\b[a-z]/g, (letter) => letter.toUpperCase());
}

export function humanizeKey(key: string): string {
  return titleCase(key.replace(/([a-z0-9])([A-Z])/g, '$1_$2'));
}

export function truncate(value: string, max: number): string {
  if (value.length <= max) return value;
  return `${value.slice(0, Math.max(0, max - 1)).trimEnd()}…`;
}

function parseDate(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatDate(iso: string | null | undefined): string {
  const date = parseDate(iso);
  if (!date) return '—';
  const sameYear = date.getFullYear() === new Date().getFullYear();
  return new Intl.DateTimeFormat('en-US', {
    month: 'short',
    day: 'numeric',
    ...(sameYear ? {} : { year: 'numeric' }),
  }).format(date);
}

export function formatDateTime(iso: string | null | undefined): string {
  const date = parseDate(iso);
  if (!date) return '—';
  const base = new Intl.DateTimeFormat('en-US', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date);
  return base;
}

export function formatClock(iso: string | null | undefined): string {
  const date = parseDate(iso);
  if (!date) return '--:--:--';
  return new Intl.DateTimeFormat('en-GB', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(date);
}

export function relativeTime(iso: string | null | undefined, now = Date.now()): string {
  const date = parseDate(iso);
  if (!date) return '—';
  const seconds = Math.round((now - date.getTime()) / 1000);
  if (seconds < 10) return 'just now';
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;
  return formatDate(iso);
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms) || ms < 0) return '—';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`;
  const wholeMinutes = Math.floor(seconds / 60);
  const restSeconds = Math.round(seconds % 60);
  if (wholeMinutes < 60) return `${wholeMinutes}m ${String(restSeconds).padStart(2, '0')}s`;
  const hours = Math.floor(wholeMinutes / 60);
  return `${hours}h ${String(wholeMinutes % 60).padStart(2, '0')}m`;
}

export function formatDurationSeconds(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return '—';
  return formatDuration(seconds * 1000);
}

/** Elapsed time for a run: started → completed (or now while it's still active). */
export function runElapsedMs(
  run: {
    started_at?: string | null;
    completed_at?: string | null;
    created_at?: string | null;
    status?: string | null;
  },
  now = Date.now(),
): number | null {
  const start = parseDate(run.started_at) ?? parseDate(run.created_at);
  if (!start) return null;
  const end = parseDate(run.completed_at);
  const terminal = run.status === 'completed' || run.status === 'failed' || run.status === 'cancelled';
  const endMs = end ? end.getTime() : terminal ? start.getTime() : now;
  return Math.max(0, endMs - start.getTime());
}

export function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return new Intl.NumberFormat('en-US').format(value);
}

export function formatCompact(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  const abs = Math.abs(value);
  if (abs < 1000) return String(Math.round(value));
  if (abs < 1_000_000) return `${(value / 1000).toFixed(abs < 10_000 ? 1 : 0)}K`;
  return `${(value / 1_000_000).toFixed(1)}M`;
}

export function formatTokens(value: number | null | undefined): string {
  return formatCompact(value);
}

export function formatPercent(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  const normalized = value > 1 ? value : value * 100;
  return `${normalized.toFixed(digits)}%`;
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return '—';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function initials(name: string | null | undefined): string {
  if (!name) return '··';
  const parts = name.trim().split(/\s+/).slice(0, 2);
  const letters = parts.map((part) => part.charAt(0).toUpperCase()).join('');
  return letters || name.slice(0, 2).toUpperCase();
}

export type Tone = 'success' | 'info' | 'warn' | 'danger' | 'neutral' | 'accent';

export interface StatusMeta {
  label: string;
  tone: Tone;
  pulse: boolean;
}

const STATUS_META: Record<string, StatusMeta> = {
  queued: { label: 'Queued', tone: 'neutral', pulse: false },
  pending: { label: 'Pending', tone: 'neutral', pulse: false },
  running: { label: 'Running', tone: 'info', pulse: true },
  executing: { label: 'Executing', tone: 'info', pulse: true },
  evaluating: { label: 'Evaluating', tone: 'info', pulse: true },
  planning: { label: 'Planning', tone: 'info', pulse: true },
  awaiting_approval: { label: 'Awaiting approval', tone: 'warn', pulse: true },
  retrying: { label: 'Retrying', tone: 'warn', pulse: true },
  completed: { label: 'Completed', tone: 'success', pulse: false },
  failed: { label: 'Failed', tone: 'danger', pulse: false },
  cancelled: { label: 'Cancelled', tone: 'neutral', pulse: false },
  skipped: { label: 'Skipped', tone: 'neutral', pulse: false },
  rejected: { label: 'Rejected', tone: 'danger', pulse: false },
  approved: { label: 'Approved', tone: 'success', pulse: false },
};

export function statusMeta(status: string | null | undefined): StatusMeta {
  if (!status) return { label: 'Unknown', tone: 'neutral', pulse: false };
  const known = STATUS_META[status.toLowerCase()];
  if (known) return known;
  const tone: Tone = /fail|error|reject/.test(status)
    ? 'danger'
    : /await|retry|warn/.test(status)
      ? 'warn'
      : /complete|success|done/.test(status)
        ? 'success'
        : 'neutral';
  return { label: titleCase(status), tone, pulse: false };
}

export function isTerminalStatus(status: string | null | undefined): boolean {
  return status === 'completed' || status === 'failed' || status === 'cancelled';
}

export function attemptsOf(value: { attempts?: number | null }): number {
  const attempts = typeof value.attempts === 'number' ? value.attempts : 0;
  return Math.max(0, attempts);
}

/** Evaluation scores are contractually 0–1; tolerate 0–100 payloads too. */
export function evaluationScore(evaluation: unknown): number | null {
  if (!evaluation || typeof evaluation !== 'object') return null;
  const record = evaluation as Record<string, unknown>;
  const raw =
    typeof record.score === 'number'
      ? record.score
      : typeof record.overall_score === 'number'
        ? record.overall_score
        : typeof record.value === 'number'
          ? record.value
          : null;
  if (raw === null || !Number.isFinite(raw)) {
    if (record.passed === true) return 100;
    if (record.passed === false) return 0;
    return null;
  }
  const percent = raw <= 1 ? raw * 100 : raw;
  return Math.max(0, Math.min(100, percent));
}

export function evaluationIssues(evaluation: unknown): string[] {
  if (!evaluation || typeof evaluation !== 'object') return [];
  const record = evaluation as Record<string, unknown>;
  const raw = record.issues;
  if (Array.isArray(raw)) {
    return raw
      .map((item) => (typeof item === 'string' ? item : JSON.stringify(item)))
      .filter((item) => item.trim().length > 0);
  }
  if (typeof raw === 'string' && raw.trim()) return [raw.trim()];
  return [];
}

export function evaluationFeedback(evaluation: unknown): string | null {
  if (!evaluation || typeof evaluation !== 'object') return null;
  const feedback = (evaluation as Record<string, unknown>).feedback;
  return typeof feedback === 'string' && feedback.trim() ? feedback.trim() : null;
}

/** Human-readable task result for compact display. */
export function resultText(result: unknown, max = 480): string {
  if (result === null || result === undefined) return '';
  if (typeof result === 'string') return truncate(result.trim(), max);
  if (typeof result === 'number' || typeof result === 'boolean') return String(result);
  if (typeof result === 'object') {
    const record = result as Record<string, unknown>;
    for (const key of ['summary', 'output', 'content', 'result', 'text', 'answer']) {
      const value = record[key];
      if (typeof value === 'string' && value.trim()) return truncate(value.trim(), max);
    }
    try {
      return truncate(JSON.stringify(result, null, 2), max);
    } catch {
      return '—';
    }
  }
  return String(result);
}

export function safeHref(href: string): string | null {
  const trimmed = href.trim();
  if (/^(https?:|mailto:)/i.test(trimmed)) return trimmed;
  return null;
}

export function artifactsOf(artifact: {
  name?: string | null;
  filename?: string | null;
  title?: string | null;
  path?: string | null;
  kind?: string | null;
}): { name: string; kind: string } {
  const fromPath = artifact.path ? basename(artifact.path) : '';
  const name = artifact.name ?? artifact.filename ?? artifact.title ?? (fromPath || 'artifact');
  const kind = (artifact.kind ?? 'file').toLowerCase();
  return { name, kind };
}

export function basename(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : path;
}

export function isDownloadableArtifact(artifact: {
  id?: string | number;
  kind?: string | null;
  type?: string | null;
}): boolean {
  if (artifact.id === undefined || artifact.id === null) return false;
  const kind = (artifact.kind ?? artifact.type ?? '').toLowerCase();
  return kind === 'markdown' || kind === 'report' || kind === 'md';
}

export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

export function toggleInList<T>(list: T[], item: T): T[] {
  return list.includes(item) ? list.filter((entry) => entry !== item) : [...list, item];
}

export function parseNumber(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim() !== '') {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}
