import type { ReactNode } from 'react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Icon } from './Icon';
import { cx, statusMeta, type Tone } from '../lib/format';

/* ------------------------------------------------------------------ */
/* Buttons / copy                                                      */
/* ------------------------------------------------------------------ */

interface CopyButtonProps {
  value: string;
  label?: string;
  compact?: boolean;
  className?: string;
}

export function CopyButton({ value, label = 'Copy', compact = false, className }: CopyButtonProps) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    };
  }, []);

  const copy = useCallback(async () => {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(value);
      } else {
        const area = document.createElement('textarea');
        area.value = value;
        area.style.position = 'fixed';
        area.style.opacity = '0';
        document.body.appendChild(area);
        area.select();
        document.execCommand('copy');
        document.body.removeChild(area);
      }
      setCopied(true);
      if (timer.current !== null) window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setCopied(false), 1600);
    } catch {
      setCopied(false);
    }
  }, [value]);

  return (
    <button
      type="button"
      className={cx('icon-btn', compact && 'icon-btn--compact', copied && 'is-success', className)}
      onClick={() => void copy()}
      aria-label={copied ? 'Copied to clipboard' : label}
      title={copied ? 'Copied' : label}
    >
      <Icon name={copied ? 'check' : 'copy'} size={compact ? 13 : 15} />
      {!compact && <span>{copied ? 'Copied' : label}</span>}
    </button>
  );
}

/* ------------------------------------------------------------------ */
/* Loading                                                             */
/* ------------------------------------------------------------------ */

export function Spinner({ size = 16, className }: { size?: number; className?: string }) {
  return (
    <span
      className={cx('spinner', className)}
      style={{ width: size, height: size }}
      role="status"
      aria-label="Loading"
    />
  );
}

interface SkeletonProps {
  width?: string | number;
  height?: number;
  className?: string;
  rounded?: boolean;
}

export function Skeleton({ width = '100%', height = 14, className, rounded = true }: SkeletonProps) {
  return (
    <span
      className={cx('skeleton', rounded && 'skeleton--rounded', className)}
      style={{ width, height }}
      aria-hidden="true"
    />
  );
}

export function SkeletonLines({ lines = 3, className }: { lines?: number; className?: string }) {
  return (
    <div className={cx('skeleton-lines', className)} aria-hidden="true">
      {Array.from({ length: lines }).map((_, index) => (
        <Skeleton key={index} width={index === lines - 1 ? '62%' : '100%'} height={11} />
      ))}
    </div>
  );
}

export function SkeletonCard({ height = 120 }: { height?: number }) {
  return (
    <div className="card skeleton-card" style={{ minHeight: height }} aria-hidden="true">
      <Skeleton width="34%" height={10} />
      <Skeleton width="58%" height={20} />
      <SkeletonLines lines={2} />
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Badges                                                              */
/* ------------------------------------------------------------------ */

export function StatusBadge({
  status,
  size = 'md',
  showDot = true,
}: {
  status: string | null | undefined;
  size?: 'sm' | 'md';
  showDot?: boolean;
}) {
  const meta = statusMeta(status);
  return (
    <span
      className={cx('status-badge', `tone-${meta.tone}`, `status-badge--${size}`)}
      data-tone={meta.tone}
    >
      {showDot ? (
        <span className={cx('status-dot', meta.pulse && 'status-dot--pulse')} aria-hidden="true" />
      ) : null}
      {meta.label}
    </span>
  );
}

export type ConnectionState = 'connecting' | 'live' | 'polling' | 'offline' | 'closed';

const CONNECTION_META: Record<ConnectionState, { label: string; tone: Tone }> = {
  connecting: { label: 'Connecting', tone: 'neutral' },
  live: { label: 'Live', tone: 'success' },
  polling: { label: 'Polling 1.5s', tone: 'warn' },
  offline: { label: 'Offline', tone: 'danger' },
  closed: { label: 'Final', tone: 'info' },
};

export function ConnectionBadge({ state }: { state: ConnectionState }) {
  const meta = CONNECTION_META[state];
  return (
    <span className={cx('status-badge', 'connection-badge', `tone-${meta.tone}`)} aria-live="polite">
      <span
        className={cx(
          'status-dot',
          (state === 'live' || state === 'connecting') && 'status-dot--pulse',
        )}
        aria-hidden="true"
      />
      {meta.label}
    </span>
  );
}

export function TonePill({
  tone = 'neutral',
  children,
  icon,
}: {
  tone?: Tone;
  children: ReactNode;
  icon?: ReactNode;
}) {
  return (
    <span className={cx('tone-pill', `tone-${tone}`)}>
      {icon}
      {children}
    </span>
  );
}

/* ------------------------------------------------------------------ */
/* Panels & states                                                     */
/* ------------------------------------------------------------------ */

interface PanelProps {
  title?: ReactNode;
  subtitle?: ReactNode;
  icon?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
  id?: string;
}

export function Panel({
  title,
  subtitle,
  icon,
  actions,
  children,
  className,
  bodyClassName,
  id,
}: PanelProps) {
  return (
    <section className={cx('card', 'panel', className)} id={id}>
      {title || actions ? (
        <header className="panel-head">
          <div className="panel-heading">
            {icon ? <span className="panel-icon">{icon}</span> : null}
            <div>
              <h2 className="panel-title">{title}</h2>
              {subtitle ? <p className="panel-subtitle">{subtitle}</p> : null}
            </div>
          </div>
          {actions ? <div className="panel-actions">{actions}</div> : null}
        </header>
      ) : null}
      <div className={cx('panel-body', bodyClassName)}>{children}</div>
    </section>
  );
}

export function EmptyState({
  icon = 'sparkles',
  title,
  description,
  action,
  compact = false,
}: {
  icon?: Parameters<typeof Icon>[0]['name'];
  title: string;
  description?: ReactNode;
  action?: ReactNode;
  compact?: boolean;
}) {
  return (
    <div className={cx('empty-state', compact && 'empty-state--compact')}>
      <span className="empty-icon">
        <Icon name={icon} size={22} />
      </span>
      <p className="empty-title">{title}</p>
      {description ? <p className="empty-desc">{description}</p> : null}
      {action ? <div className="empty-action">{action}</div> : null}
    </div>
  );
}

export function ErrorState({
  message,
  onRetry,
  title = 'Something went wrong',
  compact = false,
}: {
  message: string;
  onRetry?: () => void;
  title?: string;
  compact?: boolean;
}) {
  return (
    <div className={cx('error-state', compact && 'error-state--compact')} role="alert">
      <span className="error-icon">
        <Icon name="alert" size={20} />
      </span>
      <div className="error-body">
        <p className="error-title">{title}</p>
        <p className="error-message">{message}</p>
      </div>
      {onRetry ? (
        <button type="button" className="btn btn-ghost btn-sm" onClick={onRetry}>
          <Icon name="refresh" size={14} />
          Retry
        </button>
      ) : null}
    </div>
  );
}

export function ErrorBanner({ message, onDismiss }: { message: string; onDismiss?: () => void }) {
  return (
    <div className="error-banner" role="alert">
      <Icon name="alert" size={16} />
      <span>{message}</span>
      {onDismiss ? (
        <button type="button" className="icon-btn icon-btn--compact" onClick={onDismiss} aria-label="Dismiss error">
          <Icon name="x" size={14} />
        </button>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Data viz bits                                                       */
/* ------------------------------------------------------------------ */

export function ScoreRing({
  score,
  size = 46,
  label,
  muted = false,
}: {
  score: number | null;
  size?: number;
  label?: string;
  muted?: boolean;
}) {
  const radius = (size - 6) / 2;
  const circumference = 2 * Math.PI * radius;
  const clamped = score === null ? 0 : Math.max(0, Math.min(100, score));
  const offset = circumference - (clamped / 100) * circumference;
  const tone = score === null ? 'muted' : clamped >= 80 ? 'success' : clamped >= 60 ? 'warn' : 'danger';

  return (
    <span
      className={cx('score-ring', `score-ring--${tone}`, muted && 'score-ring--muted')}
      style={{ width: size, height: size }}
      title={label ?? (score === null ? 'No evaluation yet' : `Evaluation score ${Math.round(score)}%`)}
      aria-label={label ?? (score === null ? 'No evaluation score' : `Evaluation score ${Math.round(score)} percent`)}
    >
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden="true">
        <circle
          className="score-ring-track"
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          strokeWidth="3"
        />
        {score !== null ? (
          <circle
            className="score-ring-value"
            cx={size / 2}
            cy={size / 2}
            r={radius}
            fill="none"
            strokeWidth="3"
            strokeLinecap="round"
            strokeDasharray={circumference}
            strokeDashoffset={offset}
            transform={`rotate(-90 ${size / 2} ${size / 2})`}
          />
        ) : null}
      </svg>
      <span className="score-ring-label">
        {score === null ? '—' : Math.round(score)}
        {score !== null ? <small>%</small> : null}
      </span>
    </span>
  );
}

export function AttemptDots({ attempts, max = 3 }: { attempts: number; max?: number }) {
  return (
    <span className="attempt-dots" title={`${attempts} attempt${attempts === 1 ? '' : 's'}`}>
      {Array.from({ length: Math.max(max, attempts) }).map((_, index) => (
        <span
          key={index}
          className={cx('attempt-dot', index < attempts && 'attempt-dot--filled')}
          aria-hidden="true"
        />
      ))}
      <span className="attempt-label">
        {attempts} attempt{attempts === 1 ? '' : 's'}
      </span>
    </span>
  );
}

/* ------------------------------------------------------------------ */
/* Forms                                                               */
/* ------------------------------------------------------------------ */

export function Toggle({
  checked,
  onChange,
  label,
  description,
  id,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: ReactNode;
  description?: ReactNode;
  id?: string;
}) {
  return (
    <button
      id={id}
      type="button"
      role="switch"
      aria-checked={checked}
      className="toggle-row"
      onClick={() => onChange(!checked)}
    >
      <span className={cx('toggle', checked && 'toggle--on')} aria-hidden="true">
        <span className="toggle-knob" />
      </span>
      <span className="toggle-text">
        <span className="toggle-label">{label}</span>
        {description ? <span className="toggle-desc">{description}</span> : null}
      </span>
    </button>
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="kbd">{children}</kbd>;
}
