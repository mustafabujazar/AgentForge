import { useId, useMemo } from 'react';
import type { TaskView } from '../lib/runData';
import { agentColor, agentLabel } from '../lib/runData';
import { statusMeta, truncate } from '../lib/format';
import { EmptyState, StatusBadge } from './ui';

const NODE_W = 136;
const NODE_H = 48;
const GAP_X = 14;
const ROW_GAP = 34;
const PAD = 16;

interface Point {
  x: number;
  y: number;
}

interface Edge {
  id: string;
  from: Point;
  to: Point;
}

interface Layout {
  width: number;
  height: number;
  positions: Map<string, Point>;
  edges: Edge[];
  depths: number[];
}

export function WorkflowGraph({
  tasks,
  selectedId,
  onSelect,
}: {
  tasks: TaskView[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const markerId = `wf-arrow-${useId().replace(/[^A-Za-z0-9]/g, '')}`;

  const layout = useMemo<Layout | null>(() => {
    if (tasks.length === 0) return null;
    const byId = new Map(tasks.map((task) => [task.id, task]));
    const depthCache = new Map<string, number>();

    const depthOf = (task: TaskView, stack: Set<string>): number => {
      const cached = depthCache.get(task.id);
      if (cached !== undefined) return cached;
      if (stack.has(task.id)) return 0;
      stack.add(task.id);
      let depth = 0;
      for (const dependency of task.dependencies) {
        const parent = byId.get(dependency);
        if (parent) depth = Math.max(depth, depthOf(parent, stack) + 1);
      }
      stack.delete(task.id);
      depthCache.set(task.id, depth);
      return depth;
    };

    for (const task of tasks) depthOf(task, new Set());

    const rows = new Map<number, TaskView[]>();
    for (const task of tasks) {
      const depth = depthCache.get(task.id) ?? 0;
      const row = rows.get(depth);
      if (row) row.push(task);
      else rows.set(depth, [task]);
    }

    const depths = Array.from(rows.keys()).sort((a, b) => a - b);
    const widestRow = Math.max(...depths.map((depth) => rows.get(depth)?.length ?? 0));
    const width = Math.max(300, widestRow * NODE_W + (widestRow - 1) * GAP_X + PAD * 2);
    const height = depths.length * NODE_H + (depths.length - 1) * ROW_GAP + PAD * 2;

    const positions = new Map<string, Point>();
    depths.forEach((depth, rowIndex) => {
      const row = rows.get(depth) ?? [];
      const rowWidth = row.length * NODE_W + (row.length - 1) * GAP_X;
      const startX = (width - rowWidth) / 2;
      row.forEach((task, index) => {
        positions.set(task.id, {
          x: startX + index * (NODE_W + GAP_X),
          y: PAD + rowIndex * (NODE_H + ROW_GAP),
        });
      });
    });

    const edges: Edge[] = [];
    for (const task of tasks) {
      const target = positions.get(task.id);
      if (!target) continue;
      for (const dependency of task.dependencies) {
        const parent = positions.get(dependency);
        if (!parent) continue;
        edges.push({
          id: `${dependency}->${task.id}`,
          from: { x: parent.x + NODE_W / 2, y: parent.y + NODE_H },
          to: { x: target.x + NODE_W / 2, y: target.y },
        });
      }
    }

    return { width, height, positions, edges, depths };
  }, [tasks]);

  if (!layout) {
    return (
      <EmptyState
        compact
        icon="layers"
        title="No workflow yet"
        description="The plan appears here once the planner has decomposed the objective."
      />
    );
  }

  const presentTones = Array.from(new Set(tasks.map((task) => statusMeta(task.status).tone)));

  return (
    <div className="workflow-graph">
      <div className="workflow-svg-wrap">
        <svg
          viewBox={`0 0 ${layout.width} ${layout.height}`}
          width="100%"
          role="group"
          aria-label="Workflow task graph"
        >
          <defs>
            <marker
              id={markerId}
              viewBox="0 0 10 10"
              refX="8"
              refY="5"
              markerWidth="5"
              markerHeight="5"
              orient="auto-start-reverse"
            >
              <path d="M 0 0 L 10 5 L 0 10 z" className="wf-arrow-head" />
            </marker>
          </defs>

          {layout.edges.map((edge) => {
            const midY = (edge.from.y + edge.to.y) / 2;
            return (
              <path
                key={edge.id}
                d={`M ${edge.from.x} ${edge.from.y} C ${edge.from.x} ${midY}, ${edge.to.x} ${midY}, ${edge.to.x} ${edge.to.y}`}
                className="wf-edge"
                markerEnd={`url(#${markerId})`}
              />
            );
          })}

          {tasks.map((task) => {
            const position = layout.positions.get(task.id);
            if (!position) return null;
            const meta = statusMeta(task.status);
            const selected = selectedId === task.id;
            const title = truncate(task.title, 19);
            return (
              <g
                key={task.id}
                transform={`translate(${position.x} ${position.y})`}
                className={`wf-node wf-node--${meta.tone}${selected ? ' is-selected' : ''}`}
                role="button"
                tabIndex={0}
                aria-label={`Task ${task.title}, ${meta.label}, agent ${agentLabel(task.agent)}`}
                aria-pressed={selected}
                onClick={() => onSelect(task.id)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    onSelect(task.id);
                  }
                }}
              >
                <title>{`${task.title} · ${meta.label}${task.agent ? ` · ${agentLabel(task.agent)}` : ''}`}</title>
                <rect
                  x="0"
                  y="0"
                  width={NODE_W}
                  height={NODE_H}
                  rx="9"
                  className="wf-node-bg"
                />
                <rect x="0" y="0" width="3" height={NODE_H} rx="1.5" className="wf-node-accent" />
                <circle cx="13" cy="15" r="3" className="wf-node-dot" />
                <text x="22" y="18.5" className="wf-node-title">
                  {title}
                </text>
                <text
                  x="13"
                  y="35"
                  className="wf-node-agent"
                  style={{ fill: agentColor(task.agent) }}
                >
                  {truncate(agentLabel(task.agent), 18)}
                </text>
                {task.sensitive ? (
                  <g className="wf-node-lock" transform={`translate(${NODE_W - 22} 11)`}>
                    <rect x="0" y="3" width="9" height="7" rx="1.5" />
                    <path d="M2 3V1.8a2.5 2.5 0 0 1 5 0V3" fill="none" />
                  </g>
                ) : null}
              </g>
            );
          })}
        </svg>
      </div>

      <div className="workflow-legend" aria-hidden="true">
        {presentTones.map((tone) => (
          <span key={tone} className={`legend-item tone-${tone}`}>
            <StatusBadge status={toneToStatus(tone)} size="sm" />
          </span>
        ))}
      </div>
    </div>
  );
}

function toneToStatus(tone: string): string {
  switch (tone) {
    case 'success':
      return 'completed';
    case 'info':
      return 'running';
    case 'warn':
      return 'retrying';
    case 'danger':
      return 'failed';
    default:
      return 'pending';
  }
}
