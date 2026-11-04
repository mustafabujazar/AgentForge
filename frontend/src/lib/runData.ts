import type { Plan, PlanTask, RunEvent, RunRead, TaskRead } from '../api/types';
import { evaluationScore, titleCase } from './format';

export interface TaskView {
  id: string;
  title: string;
  description: string;
  agent: string;
  status: string;
  attempts: number;
  sensitive: boolean;
  dependencies: string[];
  tools: string[];
  successCriteria: string[];
  evaluation: unknown;
  result: unknown;
  record: TaskRead | null;
  spec: PlanTask | null;
}

const AGENT_PALETTE = ['#5b8cff', '#3ee6c4', '#a78bfa', '#f5b544', '#ff8fab', '#6ee7b7', '#f97316'];

export function agentColor(name: string | null | undefined): string {
  if (!name) return AGENT_PALETTE[0];
  let hash = 0;
  for (let index = 0; index < name.length; index += 1) {
    hash = (hash * 31 + name.charCodeAt(index)) % 997;
  }
  return AGENT_PALETTE[hash % AGENT_PALETTE.length];
}

export function agentLabel(name: string | null | undefined): string {
  if (!name) return 'Unassigned';
  return titleCase(name);
}

export function planTasksOf(plan: Plan | null | undefined): PlanTask[] {
  if (!plan || !Array.isArray(plan.tasks)) return [];
  return plan.tasks.filter((task): task is PlanTask => Boolean(task && typeof task === 'object'));
}

export function taskKey(spec: PlanTask, index: number): string {
  const id = spec.task_id ?? spec.id ?? spec.name;
  return typeof id === 'string' && id.trim() ? id : `task-${index}`;
}

function stringsOf(value: unknown): string[] {
  if (Array.isArray(value)) return value.filter((item): item is string => typeof item === 'string');
  if (typeof value === 'string' && value.trim()) return [value];
  return [];
}

function inferredStatus(type: string): string | null {
  switch (type) {
    case 'task.started':
    case 'tool.called':
    case 'approval.resolved':
      return 'running';
    case 'agent.completed':
    case 'evaluation.completed':
      return 'completed';
    case 'task.retry':
      return 'retrying';
    case 'approval.required':
    case 'run.awaiting_approval':
      return 'awaiting_approval';
    case 'run.failed':
      return 'failed';
    case 'run.cancelled':
      return 'cancelled';
    default:
      return null;
  }
}

/**
 * Merges plan task specs with their persisted TaskRead rows (and, before rows
 * exist, with the event stream) into a single view model for the task board and
 * workflow graph.
 */
export function buildTaskViews(
  plan: Plan | null | undefined,
  records: TaskRead[],
  events: RunEvent[] = [],
): TaskView[] {
  const byId = new Map<string, TaskView>();
  const specIds = new Map<string, string>();

  planTasksOf(plan).forEach((spec, index) => {
    const id = taskKey(spec, index);
    specIds.set(id, id);
    if (spec.id) specIds.set(String(spec.id), id);
    if (spec.task_id) specIds.set(String(spec.task_id), id);
    byId.set(id, {
      id,
      title:
        (typeof spec.title === 'string' && spec.title) ||
        (typeof spec.name === 'string' && spec.name) ||
        `Task ${index + 1}`,
      description: typeof spec.description === 'string' ? spec.description : '',
      agent: (typeof spec.agent === 'string' && spec.agent) ||
        (typeof spec.assigned_agent === 'string' && spec.assigned_agent) ||
        '',
      status: 'pending',
      attempts: 0,
      sensitive: spec.sensitive === true,
      dependencies: stringsOf(spec.dependencies ?? spec.depends_on).map(
        (dependency) => specIds.get(dependency) ?? dependency,
      ),
      tools: stringsOf(spec.tools ?? spec.tool_hints),
      successCriteria: stringsOf(spec.success_criteria),
      evaluation: null,
      result: null,
      record: null,
      spec,
    });
  });

  for (const record of records) {
    if (!record) continue;
    const rawId = record.task_id ?? (record.id !== undefined ? String(record.id) : '');
    const id = specIds.get(rawId) ?? rawId;
    if (!id) continue;
    const existing = byId.get(id);
    if (existing) {
      existing.record = record;
      existing.status = record.status ?? existing.status;
      existing.attempts = Math.max(existing.attempts, record.attempts ?? 0);
      existing.agent = record.agent ?? existing.agent;
      if (record.title) existing.title = record.title;
      if (record.evaluation !== undefined && record.evaluation !== null) {
        existing.evaluation = record.evaluation;
      }
      if (record.sensitive === true) existing.sensitive = true;
      if (record.result !== undefined && record.result !== null) existing.result = record.result;
    } else {
      byId.set(id, {
        id,
        title: record.title ?? id,
        description: '',
        agent: record.agent ?? '',
        status: record.status ?? 'pending',
        attempts: record.attempts ?? 0,
        sensitive: record.sensitive === true,
        dependencies: [],
        tools: [],
        successCriteria: [],
        evaluation: record.evaluation ?? null,
        result: record.result ?? null,
        record,
        spec: null,
      });
    }
  }

  // Event-derived enrichment (status before task rows exist, agent, retries, scores).
  for (const event of events) {
    if (!event.task_id) continue;
    const id = specIds.get(event.task_id) ?? event.task_id;
    const view = byId.get(id);
    if (!view) continue;
    if (!view.agent && event.agent) view.agent = event.agent;
    if (event.type === 'task.retry') {
      view.attempts = Math.max(view.attempts + 1, (view.record?.attempts ?? 0) + 1);
    }
    if (!view.record?.status) {
      const status = inferredStatus(event.type);
      if (status) view.status = status;
    }
    if (!view.evaluation && event.type === 'evaluation.completed' && event.data) {
      const score = evaluationScore(event.data);
      if (score !== null) view.evaluation = event.data;
    }
  }

  return Array.from(byId.values());
}

export function successCriteriaOf(plan: Plan | null | undefined): string[] {
  if (!plan) return [];
  return stringsOf(plan.success_criteria);
}

export function totalTokensOf(run: RunRead | null | undefined): number {
  return run?.token_usage?.total_tokens ?? 0;
}
