import { getToken, notifyUnauthorized } from '../lib/session';
import type {
  AgentsResponse,
  ApprovalDecision,
  ArtifactsResponse,
  CreateRunPayload,
  HealthInfo,
  KnowledgeSearchResponse,
  LoginResponse,
  MetricsResponse,
  ReportResponse,
  RunEvent,
  RunRead,
  RunsResponse,
  TaskRead,
  ToolsResponse,
  UserRead,
} from './types';

const API_ROOT = '/api';

export interface ApiError extends Error {
  readonly status: number;
  readonly detail: string;
  readonly payload: unknown;
  readonly network: boolean;
}

export class HttpError extends Error implements ApiError {
  readonly status: number;
  readonly detail: string;
  readonly payload: unknown;
  readonly network: boolean;

  constructor(status: number, detail: string, payload?: unknown, network = false) {
    super(detail);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
    this.payload = payload;
    this.network = network;
  }
}

export function isApiError(error: unknown): error is ApiError {
  return error instanceof HttpError;
}

export function errorMessage(error: unknown): string {
  if (isApiError(error)) return error.detail;
  if (error instanceof Error) return error.message;
  return 'Something went wrong.';
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  body?: unknown;
  signal?: AbortSignal;
  /** Set false for unauthenticated calls (login). */
  auth?: boolean;
  headers?: Record<string, string>;
  cache?: RequestCache;
}

function authHeaders(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function toHttpError(response: Response): Promise<HttpError> {
  let detail = `${response.status} ${response.statusText}`.trim();
  let payload: unknown = null;
  try {
    const text = await response.text();
    if (text) {
      try {
        payload = JSON.parse(text) as unknown;
      } catch {
        payload = text;
      }
      if (payload && typeof payload === 'object' && 'detail' in payload) {
        const raw = (payload as { detail?: unknown }).detail;
        if (typeof raw === 'string' && raw.length > 0) detail = raw;
        else if (Array.isArray(raw) && raw.length > 0) {
          // FastAPI validation errors: [{loc, msg, type}]
          detail = raw
            .map((item) =>
              item && typeof item === 'object' && 'msg' in item
                ? String((item as { msg?: unknown }).msg)
                : String(item),
            )
            .join(' · ');
        }
      } else if (typeof payload === 'string' && payload.length < 300) {
        detail = payload;
      }
    }
  } catch {
    /* body already consumed / empty */
  }
  return new HttpError(response.status, detail, payload);
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, signal, auth = true, headers = {}, cache = 'no-store' } = options;
  const init: RequestInit = {
    method,
    signal,
    cache,
    headers: {
      Accept: 'application/json',
      ...(auth ? authHeaders() : {}),
      ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      ...headers,
    },
  };
  if (body !== undefined) init.body = JSON.stringify(body);

  let response: Response;
  try {
    response = await fetch(`${API_ROOT}${path}`, init);
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new HttpError(
      0,
      'Cannot reach the AgentForge API. Check that the backend is running.',
      null,
      true,
    );
  }

  if (response.status === 401 && auth) {
    notifyUnauthorized();
    throw new HttpError(401, 'Your session expired. Please sign in again.', null);
  }

  if (!response.ok) throw await toHttpError(response);

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  if (!text) return undefined as T;
  const contentType = response.headers.get('content-type') ?? '';
  if (contentType.includes('application/json') || /^\s*[[{]/.test(text)) {
    try {
      return JSON.parse(text) as T;
    } catch {
      return text as unknown as T;
    }
  }
  return text as unknown as T;
}

function query(params: Record<string, string | number | boolean | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') search.set(key, String(value));
  }
  const encoded = search.toString();
  return encoded ? `?${encoded}` : '';
}

/**
 * Opens the SSE endpoint with a fetch stream so the bearer token can be sent
 * (EventSource cannot set Authorization headers). Caller parses the body.
 */
export async function openEventStream(
  runId: string,
  afterSeq: number,
  signal: AbortSignal,
): Promise<Response> {
  let response: Response;
  try {
    response = await fetch(
      `${API_ROOT}/runs/${encodeURIComponent(runId)}/events/stream${query({ after_seq: afterSeq })}`,
      {
        method: 'GET',
        signal,
        cache: 'no-store',
        headers: {
          Accept: 'text/event-stream',
          ...authHeaders(),
        },
      },
    );
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new HttpError(0, 'Live stream unreachable.', null, true);
  }
  if (response.status === 401) {
    notifyUnauthorized();
    throw new HttpError(401, 'Your session expired. Please sign in again.', null);
  }
  if (!response.ok) throw await toHttpError(response);
  return response;
}

export const api = {
  login(email: string, password: string): Promise<LoginResponse> {
    return request<LoginResponse>('/auth/login', {
      method: 'POST',
      body: { email, password },
      auth: false,
    });
  },

  me(): Promise<UserRead> {
    return request<UserRead>('/auth/me');
  },

  listRuns(params: { limit?: number; offset?: number; status?: string } = {}): Promise<RunsResponse> {
    return request<RunsResponse>(`/runs${query(params)}`);
  },

  createRun(payload: CreateRunPayload): Promise<RunRead> {
    return request<RunRead>('/runs', { method: 'POST', body: payload });
  },

  getRun(runId: string): Promise<RunRead> {
    return request<RunRead>(`/runs/${encodeURIComponent(runId)}`);
  },

  getTasks(runId: string): Promise<TaskRead[]> {
    return request<TaskRead[]>(`/runs/${encodeURIComponent(runId)}/tasks`);
  },

  getEvents(runId: string, afterSeq = 0): Promise<RunEvent[]> {
    return request<RunEvent[]>(
      `/runs/${encodeURIComponent(runId)}/events${query({ after_seq: afterSeq })}`,
    );
  },

  getReport(runId: string): Promise<ReportResponse> {
    return request<ReportResponse>(`/runs/${encodeURIComponent(runId)}/report`);
  },

  getArtifacts(runId: string): Promise<ArtifactsResponse> {
    return request<ArtifactsResponse>(`/runs/${encodeURIComponent(runId)}/artifacts`);
  },

  /** Authenticated binary download — must go through fetch so the JWT is sent. */
  async downloadArtifact(artifactId: string | number): Promise<Blob> {
    let response: Response;
    try {
      response = await fetch(
        `${API_ROOT}/artifacts/${encodeURIComponent(String(artifactId))}/download`,
        {
          method: 'GET',
          cache: 'no-store',
          headers: { ...authHeaders() },
        },
      );
    } catch {
      throw new HttpError(0, 'Download failed — the API is unreachable.', null, true);
    }
    if (response.status === 401) {
      notifyUnauthorized();
      throw new HttpError(401, 'Your session expired. Please sign in again.', null);
    }
    if (!response.ok) throw await toHttpError(response);
    return response.blob();
  },

  cancelRun(runId: string): Promise<RunRead> {
    return request<RunRead>(`/runs/${encodeURIComponent(runId)}/cancel`, { method: 'POST' });
  },

  submitApproval(
    runId: string,
    approvalId: string,
    decision: ApprovalDecision,
    note?: string,
  ): Promise<RunRead> {
    return request<RunRead>(
      `/runs/${encodeURIComponent(runId)}/approvals/${encodeURIComponent(approvalId)}`,
      {
        method: 'POST',
        body: note && note.trim() ? { decision, note: note.trim() } : { decision },
      },
    );
  },

  getMetrics(): Promise<MetricsResponse> {
    return request<MetricsResponse>('/metrics/runs');
  },

  listAgents(): Promise<AgentsResponse> {
    return request<AgentsResponse>('/agents');
  },

  listTools(): Promise<ToolsResponse> {
    return request<ToolsResponse>('/tools');
  },

  searchKnowledge(q: string, k = 5): Promise<KnowledgeSearchResponse> {
    return request<KnowledgeSearchResponse>(`/knowledge/search${query({ q, k })}`);
  },

  health(): Promise<HealthInfo> {
    return request<HealthInfo>('/health');
  },
};
