/**
 * SSR smoke test: renders the real components with ReactDOMServer to catch
 * runtime/render errors (bad props, invalid hooks, missing exports) without a
 * browser. Compiled with tsc to CommonJS and executed with the React preload.
 */
import { renderToString } from 'react-dom/server';
import { version as reactVersion } from 'react';
import { LoginPage } from '../src/pages/LoginPage';
import { DashboardPage } from '../src/pages/DashboardPage';
import { RunPage } from '../src/pages/RunPage';
import { AgentsPage } from '../src/pages/AgentsPage';
import { ToolsPage } from '../src/pages/ToolsPage';
import { KnowledgePage } from '../src/pages/KnowledgePage';
import { Shell } from '../src/components/Shell';
import { TaskBoard } from '../src/components/TaskBoard';
import { Timeline } from '../src/components/Timeline';
import { WorkflowGraph } from '../src/components/WorkflowGraph';
import { TokenUsage } from '../src/components/TokenUsage';
import { Artifacts } from '../src/components/Artifacts';
import { ApprovalBanner } from '../src/components/ApprovalBanner';
import { ReportPanel } from '../src/components/ReportPanel';
import { Markdown } from '../src/components/Markdown';
import { buildTaskViews } from '../src/lib/runData';
import type { Artifact, PendingApproval, Plan, RunEvent, TaskRead } from '../src/api/types';

const plan: Plan = {
  objective: 'Analyze sales performance',
  summary: 'Diagnose the three worst-performing products.',
  success_criteria: ['Evidence is cited', 'Numbers reconcile'],
  tasks: [
    {
      task_id: 't1',
      title: 'Pull Q3 revenue',
      description: 'Query the sales database.',
      agent: 'analyst',
      dependencies: [],
      tools: ['sales_sql'],
      sensitive: false,
      success_criteria: ['Revenue reconciles'],
    },
    {
      task_id: 't2',
      title: 'Diagnose root causes',
      agent: 'research',
      dependencies: ['t1'],
      tools: ['knowledge_search', 'web_search'],
      sensitive: true,
      success_criteria: ['Each claim has a source'],
    },
  ],
};

const records: TaskRead[] = [
  {
    id: 'r1',
    task_id: 't1',
    title: 'Pull Q3 revenue',
    agent: 'analyst',
    status: 'completed',
    attempts: 2,
    result: 'Revenue reconciled to $4.18M.',
    evaluation: { score: 0.92, passed: true, issues: [], feedback: 'Meets criteria.' },
  },
  {
    id: 'r2',
    task_id: 't2',
    title: 'Diagnose root causes',
    agent: 'research',
    status: 'running',
    attempts: 1,
    result: null,
    evaluation: null,
  },
];

const events: RunEvent[] = [
  { seq: 1, run_id: 'demo', type: 'run.created', ts: new Date().toISOString(), message: 'Run created', data: { objective_chars: 40 } },
  { seq: 2, run_id: 'demo', type: 'plan.created', ts: new Date().toISOString(), message: 'Plan created', agent: null, task_id: null, data: { tasks: 2 } },
  { seq: 3, run_id: 'demo', type: 'tool.called', ts: new Date().toISOString(), agent: 'analyst', task_id: 't1', message: 'Called sales_sql', duration_ms: 84, data: null },
  { seq: 4, run_id: 'demo', type: 'evaluation.completed', ts: new Date().toISOString(), agent: 'analyst', task_id: 't1', message: 'Score 0.92', data: { score: 0.92 } },
  { seq: 5, run_id: 'demo', type: 'task.retry', ts: new Date().toISOString(), agent: 'analyst', task_id: 't1', message: 'Retrying with feedback', data: { attempt: 2 } },
  { seq: 6, run_id: 'demo', type: 'approval.required', ts: new Date().toISOString(), agent: null, task_id: 't2', message: 'Approval required', data: { tool: 'invoice_adjustment' } },
];

const tasks = buildTaskViews(plan, records, events);

const approval: PendingApproval = {
  id: 'apr-1',
  task_id: 't2',
  title: 'Execute invoice adjustment',
  requested_action: 'Issue a corrective invoice adjustment.',
  reason: 'Sensitive billing write requires human approval.',
};

const artifacts: Artifact[] = [
  { id: 'art-1', name: 'q3-report.md', kind: 'markdown', path: 'artifacts/q3-report.md', created_at: new Date().toISOString() },
  { id: 'art-2', name: 'evaluations.json', kind: 'json', path: 'artifacts/evaluations.json', created_at: new Date().toISOString() },
];

const report = [
  '# Q3 Sales Report',
  '',
  '**Net revenue** was $4.18M.',
  '',
  '| Product | Variance |',
  '| --- | ---: |',
  '| Nimbus | -22.4% |',
  '',
  '- Recommendation one',
  '- Recommendation two',
  '',
  '> Evidence is traceable.',
].join('\n');

const checks: Array<{ name: string; html: string; includes: string[] }> = [];

function check(name: string, html: string, includes: string[]): void {
  for (const needle of includes) {
    if (!html.includes(needle)) {
      throw new Error(`${name}: rendered HTML is missing ${JSON.stringify(needle)}`);
    }
  }
  if (html.length < 40) throw new Error(`${name}: suspiciously empty render (${html.length} chars)`);
  checks.push({ name, html, includes });
}

check('LoginPage', renderToString(<LoginPage notice="Session expired" />), [
  'Sign in to mission control',
  'demo@agentforge.io',
  'Session expired',
  'Enter mission control',
]);

check(
  'Shell+Dashboard',
  renderToString(
    <Shell route={{ name: 'dashboard' }}>
      <DashboardPage />
    </Shell>,
  ),
  ['AGENTFORGE', 'What should the agents accomplish?', 'Sales diagnostic', 'Recent runs', 'Agent roster', 'Execution loop'],
);

check('RunPage (loading skeleton)', renderToString(<RunPage runId="demo" />), ['All runs']);

check('AgentsPage', renderToString(<AgentsPage />), ['Specialist agents', 'Capability registry']);
check('ToolsPage', renderToString(<ToolsPage />), ['Tool catalog', 'Execution surface', 'Search tools']);
check('KnowledgePage', renderToString(<KnowledgePage />), ['Knowledge base', 'Search the retrieval corpus']);

check('TaskBoard', renderToString(<TaskBoard tasks={tasks} highlightedId="t2" />), [
  'Pull Q3 revenue',
  'Diagnose root causes',
  'Sensitive',
  'attempts',
  'Success criteria',
]);

check(
  'Timeline',
  renderToString(<Timeline events={events} connection="live" terminal={false} loading={false} />),
  ['Live activity', 'Live', 'Run created', 'Called sales_sql', 'Payload', 'Approval required'],
);

check(
  'WorkflowGraph',
  renderToString(<WorkflowGraph tasks={tasks} selectedId="t1" onSelect={() => undefined} />),
  ['Workflow task graph', 'Pull Q3 revenue', 'Diagnose root causes'],
);

check(
  'TokenUsage',
  renderToString(<TokenUsage usage={{ prompt_tokens: 12400, completion_tokens: 6020, total_tokens: 18420, calls: 26 }} />),
  ['18,420', 'Prompt', 'Completion', 'LLM calls'],
);

check(
  'Artifacts',
  renderToString(<Artifacts artifacts={artifacts} downloadingId={null} error={null} onDownload={() => undefined} />),
  ['q3-report.md', 'evaluations.json', 'markdown'],
);

check(
  'ApprovalBanner',
  renderToString(<ApprovalBanner approval={approval} deciding={null} error={null} onDecision={() => undefined} />),
  ['Human approval required', 'Execute invoice adjustment', 'Approve &amp; resume', 'Reject'],
);

check(
  'ReportPanel',
  renderToString(<ReportPanel report={report} loading={false} terminal failed={false} />),
  ['Final report', 'Q3 Sales Report', 'Net revenue', 'Nimbus'],
);

check('Markdown', renderToString(<Markdown source={report} />), ['md-h1', 'md-table-wrap', '<strong>', '<blockquote>']);

check(
  'ReportPanel empty',
  renderToString(<ReportPanel report={null} loading={false} terminal failed />),
  ['Report unavailable'],
);

console.log(`[ssr] ${checks.length} component trees rendered with React ${reactVersion}`);
for (const entry of checks) {
  console.log(`[ssr]   ✓ ${entry.name} (${entry.html.length} chars)`);
}
console.log('[ssr] OK');
