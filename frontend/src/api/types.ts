/**
 * Types mirroring docs/API_CONTRACT.md (v1).
 *
 * Optional/unknown fields are tolerated on purpose: the SPA is expected to keep
 * rendering when the backend grows extra fields (the contract is authoritative,
 * but decorative extras should never break mission control).
 */

export type RunStatus =
  | 'queued'
  | 'running'
  | 'awaiting_approval'
  | 'completed'
  | 'failed'
  | 'cancelled';

export interface UserRead {
  id: string;
  email: string;
  full_name: string;
  role: string;
  created_at?: string | null;
}

export interface LoginResponse {
  access_token: string;
  token_type?: string;
  user: UserRead;
}

export interface TokenUsage {
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
  calls?: number;
  [key: string]: unknown;
}

export interface PlanTask {
  id?: string;
  task_id?: string;
  title?: string;
  name?: string;
  description?: string;
  agent?: string;
  assigned_agent?: string;
  dependencies?: string[];
  depends_on?: string[];
  tools?: string[];
  tool_hints?: string[];
  sensitive?: boolean;
  success_criteria?: string[] | string;
  [key: string]: unknown;
}

export interface Plan {
  objective?: string;
  summary?: string;
  tasks?: PlanTask[];
  success_criteria?: string[];
  [key: string]: unknown;
}

export interface PendingApproval {
  id: string;
  task_id?: string | null;
  title?: string | null;
  requested_action?: string | null;
  reason?: string | null;
  [key: string]: unknown;
}

export interface Evaluation {
  score?: number | null;
  passed?: boolean | null;
  issues?: string[] | string | null;
  feedback?: string | null;
  [key: string]: unknown;
}

export interface TaskRead {
  id: string | number;
  task_id?: string | null;
  title?: string | null;
  agent?: string | null;
  status?: string | null;
  attempts?: number | null;
  result?: unknown;
  evaluation?: Evaluation | null;
  sensitive?: boolean;
  [key: string]: unknown;
}

export interface RunEvent {
  seq: number;
  run_id?: string;
  type: string;
  ts?: string | null;
  agent?: string | null;
  task_id?: string | null;
  message?: string | null;
  data?: unknown;
  duration_ms?: number | null;
  [key: string]: unknown;
}

export interface Artifact {
  id?: string | number;
  run_id?: string;
  name?: string | null;
  kind?: string | null;
  description?: string | null;
  filename?: string | null;
  title?: string | null;
  path?: string | null;
  url?: string | null;
  type?: string | null;
  mime_type?: string | null;
  size?: number | null;
  size_bytes?: number | null;
  created_at?: string | null;
  [key: string]: unknown;
}

export interface ArtifactsResponse {
  items: Artifact[];
}

export interface RunRead {
  id: string;
  objective: string;
  status: RunStatus | string;
  plan?: Plan | null;
  final_report?: string | null;
  error?: string | null;
  created_at?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  token_usage?: TokenUsage | null;
  pending_approval?: PendingApproval | null;
  artifacts?: Artifact[] | null;
  dataset?: string | null;
  [key: string]: unknown;
}

export interface RunsResponse {
  items: RunRead[];
  total: number;
}

export interface ReportResponse {
  run_id: string;
  report: string | null;
  artifacts?: Artifact[];
}

export interface AgentInfo {
  name: string;
  role?: string | null;
  description?: string | null;
  capabilities?: string[];
  tools?: string[];
  [key: string]: unknown;
}

export interface AgentsResponse {
  agents: AgentInfo[];
}

export interface ToolInfo {
  name: string;
  description?: string | null;
  category?: string | null;
  sensitive?: boolean;
  scopes?: string[];
  input_schema?: Record<string, unknown> | null;
  [key: string]: unknown;
}

export interface ToolsResponse {
  tools: ToolInfo[];
}

export interface KnowledgeResult {
  title?: string | null;
  source?: string | null;
  text?: string | null;
  score?: number | null;
  chunk_id?: string | null;
  doc_id?: string | null;
  metadata?: Record<string, unknown> | null;
  [key: string]: unknown;
}

export interface KnowledgeSearchResponse {
  results: KnowledgeResult[];
}

export interface HealthInfo {
  status?: string;
  version?: string;
  llm_provider?: string;
  llm_model?: string;
  database?: string;
  [key: string]: unknown;
}

export interface MetricsResponse {
  total_runs?: number;
  completed?: number;
  failed?: number;
  success_rate?: number | null;
  avg_duration_seconds?: number | null;
  total_tokens?: number;
  total_retries?: number;
  avg_eval_score?: number | null;
  by_status?: Record<string, number>;
  [key: string]: unknown;
}

export type ApprovalDecision = 'approved' | 'rejected';

export interface CreateRunPayload {
  objective: string;
  dataset?: string;
  auto_approve?: boolean;
}
