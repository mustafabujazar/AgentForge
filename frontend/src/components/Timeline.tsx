import { useEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties } from 'react';
import type { RunEvent } from '../api/types';
import {
  agentColor,
  agentLabel,
} from '../lib/runData';
import { cx, formatClock, formatDuration, titleCase, truncate, type Tone } from '../lib/format';
import { Icon, type IconName } from './Icon';
import { ConnectionBadge, CopyButton, EmptyState, type ConnectionState } from './ui';

interface EventMeta {
  icon: IconName;
  tone: Tone;
  label: string;
}

const EVENT_META: Record<string, EventMeta> = {
  'run.created': { icon: 'bolt', tone: 'accent', label: 'Run created' },
  'run.started': { icon: 'play', tone: 'info', label: 'Run started' },
  'plan.created': { icon: 'layers', tone: 'accent', label: 'Plan created' },
  'task.started': { icon: 'play', tone: 'info', label: 'Task started' },
  'tool.called': { icon: 'tools', tone: 'accent', label: 'Tool called' },
  'agent.completed': { icon: 'check', tone: 'success', label: 'Agent completed' },
  'evaluation.completed': { icon: 'gauge', tone: 'success', label: 'Evaluation' },
  'task.retry': { icon: 'retry', tone: 'warn', label: 'Retry scheduled' },
  'approval.required': { icon: 'lock', tone: 'warn', label: 'Approval required' },
  'approval.resolved': { icon: 'unlock', tone: 'success', label: 'Approval resolved' },
  'report.created': { icon: 'file', tone: 'accent', label: 'Report created' },
  'run.completed': { icon: 'check', tone: 'success', label: 'Run completed' },
  'run.failed': { icon: 'x', tone: 'danger', label: 'Run failed' },
  'run.cancelled': { icon: 'ban', tone: 'neutral', label: 'Run cancelled' },
  'run.awaiting_approval': { icon: 'pause', tone: 'warn', label: 'Awaiting approval' },
};

type EventFilter = 'all' | 'agents' | 'tools' | 'approvals' | 'system';

const FILTERS: Array<{ id: EventFilter; label: string }> = [
  { id: 'all', label: 'All' },
  { id: 'agents', label: 'Agents' },
  { id: 'tools', label: 'Tools' },
  { id: 'approvals', label: 'Approvals' },
  { id: 'system', label: 'System' },
];

function categoryOf(type: string): Exclude<EventFilter, 'all'> {
  if (type.startsWith('approval.')) return 'approvals';
  if (type === 'tool.called') return 'tools';
  if (type.startsWith('task.') || type.startsWith('agent.') || type === 'evaluation.completed') {
    return 'agents';
  }
  return 'system';
}

function eventMeta(type: string): EventMeta {
  return (
    EVENT_META[type] ?? {
      icon: 'info',
      tone: 'neutral',
      label: titleCase(type || 'event'),
    }
  );
}

function payloadText(data: unknown): string | null {
  if (data === null || data === undefined) return null;
  if (typeof data === 'string') return data.trim() ? data : null;
  if (typeof data !== 'object') return String(data);
  const entries = Object.entries(data as Record<string, unknown>);
  if (entries.length === 0) return null;
  try {
    const text = JSON.stringify(data, null, 2);
    return text.length > 4000 ? `${text.slice(0, 4000)}\n…` : text;
  } catch {
    return null;
  }
}

export function Timeline({
  events,
  connection,
  terminal,
  loading,
}: {
  events: RunEvent[];
  connection: ConnectionState;
  terminal: boolean;
  loading: boolean;
}) {
  const [filter, setFilter] = useState<EventFilter>('all');
  const [follow, setFollow] = useState(true);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  const counts = useMemo(() => {
    const initial: Record<EventFilter, number> = {
      all: events.length,
      agents: 0,
      tools: 0,
      approvals: 0,
      system: 0,
    };
    for (const event of events) initial[categoryOf(event.type)] += 1;
    return initial;
  }, [events]);

  const visible = useMemo(
    () =>
      filter === 'all'
        ? events
        : events.filter((event) => categoryOf(event.type) === filter),
    [events, filter],
  );

  useEffect(() => {
    const node = scrollRef.current;
    if (!node || !follow) return;
    node.scrollTop = node.scrollHeight;
  }, [visible.length, follow]);

  const onScroll = (): void => {
    const node = scrollRef.current;
    if (!node) return;
    const distanceFromBottom = node.scrollHeight - node.scrollTop - node.clientHeight;
    setFollow(distanceFromBottom < 48);
  };

  return (
    <section className="card panel timeline-panel">
      <header className="panel-head timeline-head">
        <div className="panel-heading">
          <span className="panel-icon">
            <span className={cx('live-pulse', connection === 'live' && 'live-pulse--active')} />
          </span>
          <div>
            <h2 className="panel-title">Live activity</h2>
            <p className="panel-subtitle">
              {events.length} event{events.length === 1 ? '' : 's'}
              {events.length > 0 ? ` · seq ${events[events.length - 1].seq}` : ''}
            </p>
          </div>
        </div>
        <div className="panel-actions timeline-actions">
          <ConnectionBadge state={connection} />
          <button
            type="button"
            className={cx('btn btn-ghost btn-xs', follow && 'is-active')}
            onClick={() => setFollow((value) => !value)}
            aria-pressed={follow}
            title="Auto-scroll to newest events"
          >
            {follow ? 'Following' : 'Follow'}
          </button>
        </div>
      </header>

      <div className="timeline-filters" role="group" aria-label="Filter activity events">
        {FILTERS.map((option) => (
          <button
            key={option.id}
            type="button"
            className={cx('filter-chip', filter === option.id && 'is-active')}
            onClick={() => setFilter(option.id)}
            aria-pressed={filter === option.id}
          >
            {option.label}
            <span className="filter-count">{counts[option.id]}</span>
          </button>
        ))}
      </div>

      <div className="timeline-scroll" ref={scrollRef} onScroll={onScroll}>
        {loading && events.length === 0 ? (
          <div className="timeline-loading" aria-hidden="true">
            {Array.from({ length: 5 }).map((_, index) => (
              <div className="event event--skeleton" key={index}>
                <span className="event-dot" />
                <div className="event-body">
                  <span className="skeleton" style={{ width: '38%', height: 10 }} />
                  <span className="skeleton" style={{ width: '74%', height: 12 }} />
                </div>
              </div>
            ))}
          </div>
        ) : visible.length === 0 ? (
          <EmptyState
            compact
            icon={events.length === 0 ? 'activity' : 'list'}
            title={events.length === 0 ? 'Waiting for the first event' : 'No events in this filter'}
            description={
              events.length === 0
                ? terminal
                  ? 'This run finished without emitting any activity.'
                  : 'The orchestrator will stream activity here the moment it starts.'
                : 'Switch filters to see the rest of the run.'
            }
          />
        ) : (
          <ol className="timeline" aria-live="polite" aria-relevant="additions">
            {visible.map((event) => {
              const meta = eventMeta(event.type);
              const payload = payloadText(event.data);
              return (
                <li className={cx('event', `event--${meta.tone}`)} key={event.seq}>
                  <span className="event-dot" aria-hidden="true">
                    <span className="event-icon">
                      <Icon name={meta.icon} size={12} />
                    </span>
                  </span>
                  <div className="event-body">
                    <div className="event-top">
                      <span className="event-label">{meta.label}</span>
                      {event.agent ? (
                        <span
                          className="agent-chip"
                          style={{ '--agent-color': agentColor(event.agent) } as CSSProperties}
                        >
                          {agentLabel(event.agent)}
                        </span>
                      ) : null}
                      {event.task_id ? (
                        <span className="event-task" title={event.task_id}>
                          {truncate(event.task_id, 18)}
                        </span>
                      ) : null}
                      <span className="event-meta">
                        {event.duration_ms !== null && event.duration_ms !== undefined ? (
                          <span className="event-duration">{formatDuration(event.duration_ms)}</span>
                        ) : null}
                        <time dateTime={event.ts ?? undefined}>{formatClock(event.ts)}</time>
                      </span>
                    </div>
                    {event.message ? <p className="event-message">{event.message}</p> : null}
                    {payload ? (
                      <details className="event-data">
                        <summary>
                          Payload
                          <CopyButton value={payload} compact className="event-copy" />
                        </summary>
                        <pre>{payload}</pre>
                      </details>
                    ) : null}
                  </div>
                </li>
              );
            })}
          </ol>
        )}
      </div>
    </section>
  );
}
