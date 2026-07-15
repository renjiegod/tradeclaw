import type {
  Account,
  AccountListResponse,
  Agent,
  AgentListResponse,
  AgentTemplate,
  ApprovalListResponse,
  ApprovalQuery,
  AgentPromptTemplate,
  AssistantEvent,
  AssistantMessage,
  AssistantPendingApproval,
  AssistantSendMessageResponse,
  AssistantSession,
  AssistantSessionListResponse,
  AssistantTool,
  AssistantChannel,
  AssistantChannelListResponse,
  AssistantChannelSecretCopyResponse,
  BacktestChartSnapshot,
  LocalMarketBarsSnapshot,
  LocalMarketOverlaySnapshot,
  LocalMarketSyncJob,
  LocalMarketSyncResponse,
  RunRow,
  CreateTaskPayload,
  CycleRunDebugView,
  CycleRunRow,
  DebugSessionDetail,
  DebugSessionSummary,
  InstrumentCatalogDeleteResponse,
  InstrumentCatalogListResponse,
  InstrumentCatalogRow,
  InstrumentCatalogSyncResponse,
  InstrumentUniverseSearchResponse,
  KnowledgeJournal,
  KnowledgeJournalList,
  KnowledgeFile,
  KnowledgeIndex,
  Playbook,
  SentimentTimeline,
  SymbolRoles,
  TradeAttribution,
  WatchlistEntry,
  WatchlistListResponse,
  WatchlistTagsResponse,
  QuotesResponse,
  CreateWatchlistPayload,
  UpdateWatchlistPayload,
  MonitorRule,
  MonitorListResponse,
  MonitorAlertListResponse,
  MonitorRunOnceResponse,
  CreateMonitorPayload,
  UpdateMonitorPayload,
  TaskDuplicatePreset,
  TaskListResponse,
  TaskStatus,
  TaskTrigger,
  TaskTriggerListResponse,
  FeishuChatOption,
  FeishuChatListResponse,
  MarketBreadthData,
  LhbData,
  FundFlowData,
  FundFlowScope,
  SectorHeatData,
  SectorHeatType,
  ModelInvocationRow,
  ModelRouteRow,
  PendingApproval,
  PushedMessage,
  AssistantSessionSummary,
  PushDetail,
  Skill,
  SkillDetail,
  SkillFile,
  SkillFrontmatter,
  StrategyDefinitionRow,
  StrategyDefinitionDetail,
  StrategyDefinitionFile,
  StrategyDefinitionCompileResult,
  SystemState,
  DataProvidersResponse,
  BacktestChartTrade,
  CreateAgentPayload,
  RuntimeCapabilitiesResponse,
  RuntimeStatus,
  SwarmPresetSummary,
  SwarmRun,
} from "./types";

export type {
  CronJob,
  CronJobListResponse,
  CronJobFormValues,
  CronJobState,
  CronJobRun,
  CronJobRunListResponse,
  CronPreAction,
} from "./types";

// Same-origin by default in a production build (the bundle is served by the
// doyoutrade server itself, so an empty base yields relative ``/assistant/...``
// requests to the serving origin). Dev falls back to the standalone Vite proxy
// target. ``VITE_API_BASE`` overrides both (e.g. pointing a dev UI at a remote
// server).
const API_BASE =
  (import.meta.env.VITE_API_BASE as string | undefined)?.replace(/\/$/, "") ??
  (import.meta.env.PROD ? "" : "http://localhost:8000");

/**
 * WebSocket origin. When {@link API_BASE} is absolute it is reused (``http`` →
 * ``ws`` / ``https`` → ``wss``). When it is empty (same-origin production
 * build) the WS origin is derived from the current page origin so consumers get
 * an absolute ``ws(s)://host`` URL. Consumers append the WS path, e.g.
 * ``${WS_BASE}/ws/market/quotes``.
 */
export const WS_BASE = API_BASE
  ? API_BASE.replace(/^http/, "ws")
  : typeof window !== "undefined"
    ? window.location.origin.replace(/^http/, "ws")
    : "";

/**
 * Error thrown by {@link request} for any non-2xx response. Carries the HTTP
 * status so callers can branch on it (e.g. a 409 "agent in use" can offer a
 * cascade/force retry instead of dead-ending in a generic alert).
 */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;
  readonly traceId: string | null;
  readonly timestamp: string | null;
  readonly errorCode: string | null;
  readonly errorType: string | null;
  readonly hint: string | null;

  constructor(
    message: string,
    status: number,
    options?: {
      detail?: unknown;
      traceId?: string | null;
      timestamp?: string | null;
      errorCode?: string | null;
      errorType?: string | null;
      hint?: string | null;
    },
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = options?.detail;
    this.traceId = options?.traceId ?? null;
    this.timestamp = options?.timestamp ?? null;
    this.errorCode = options?.errorCode ?? null;
    this.errorType = options?.errorType ?? null;
    this.hint = options?.hint ?? null;
  }
}

const RECENT_API_ERRORS_MAX = 8;
const RECENT_API_ERROR_TTL_MS = 15_000;
const recentApiErrors: Array<{ error: ApiError; recordedAt: number }> = [];

function rememberApiError(error: ApiError): void {
  recentApiErrors.unshift({ error, recordedAt: Date.now() });
  if (recentApiErrors.length > RECENT_API_ERRORS_MAX) {
    recentApiErrors.length = RECENT_API_ERRORS_MAX;
  }
}

export function findRecentApiError(messageText: string | null | undefined): ApiError | null {
  const normalized = messageText?.trim();
  const now = Date.now();
  for (let i = recentApiErrors.length - 1; i >= 0; i -= 1) {
    if (now - recentApiErrors[i].recordedAt > RECENT_API_ERROR_TTL_MS) {
      recentApiErrors.splice(i, 1);
    }
  }
  if (!normalized) return null;
  const matched = recentApiErrors.find(({ error }) => {
    const apiMessage = error.message?.trim();
    return !!apiMessage && (normalized === apiMessage || normalized.includes(apiMessage));
  });
  return matched?.error ?? null;
}

type ParsedErrorResponse = {
  message: string;
  detail?: unknown;
  traceId: string | null;
  timestamp: string | null;
  errorCode: string | null;
  errorType: string | null;
  hint: string | null;
};

function parseErrorResponse(rawText: string, status: number): ParsedErrorResponse {
  const fallback = rawText || `HTTP ${status}`;
  if (!rawText) {
    return {
      message: fallback,
      detail: undefined,
      traceId: null,
      timestamp: null,
      errorCode: null,
      errorType: null,
      hint: null,
    };
  }
  try {
    const parsed = JSON.parse(rawText) as {
      detail?: unknown;
      message?: unknown;
      error_message?: unknown;
      trace_id?: unknown;
      timestamp?: unknown;
      error_code?: unknown;
      error_type?: unknown;
      hint?: unknown;
    };
    let detailMessage = fallback;
    if (typeof parsed.detail === "string" && parsed.detail.trim()) {
      detailMessage = parsed.detail;
    } else if (parsed.detail && typeof parsed.detail === "object") {
      const detailObj = parsed.detail as Record<string, unknown>;
      const messageFromDetail = detailObj.message ?? detailObj.error_message ?? detailObj.detail;
      if (typeof messageFromDetail === "string" && messageFromDetail.trim()) {
        detailMessage = messageFromDetail;
      }
    } else if (typeof parsed.error_message === "string" && parsed.error_message.trim()) {
      detailMessage = parsed.error_message;
    } else if (typeof parsed.message === "string" && parsed.message.trim()) {
      detailMessage = parsed.message;
    }
    return {
      message: detailMessage || fallback,
      detail: parsed.detail,
      traceId: typeof parsed.trace_id === "string" && parsed.trace_id.trim() ? parsed.trace_id : null,
      timestamp: typeof parsed.timestamp === "string" && parsed.timestamp.trim() ? parsed.timestamp : null,
      errorCode: typeof parsed.error_code === "string" && parsed.error_code.trim() ? parsed.error_code : null,
      errorType: typeof parsed.error_type === "string" && parsed.error_type.trim() ? parsed.error_type : null,
      hint: typeof parsed.hint === "string" && parsed.hint.trim() ? parsed.hint : null,
    };
  } catch {
    return {
      message: fallback,
      detail: rawText,
      traceId: null,
      timestamp: null,
      errorCode: null,
      errorType: null,
      hint: null,
    };
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
    ...init,
  });

  if (!response.ok) {
    const rawText = await response.text();
    const parsed = parseErrorResponse(rawText, response.status);
    const error = new ApiError(parsed.message, response.status, parsed);
    rememberApiError(error);
    throw error;
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}

type QueryValue = string | number | boolean | null | undefined;

/** Build a `?a=1&b=2` suffix (or "" when empty) from a params object.
 *
 * Collapses the `new URLSearchParams()` + per-field `if (x != null) set(...)`
 * boilerplate that was repeated across ~14 list endpoints. Rules, chosen to
 * match the prior hand-written behaviour exactly:
 *  - `null` / `undefined` / `""` are dropped (so callers can pass optional
 *    fields straight through; trim beforehand to drop whitespace-only values);
 *  - booleans serialise only when `true` (`?flag=true`), matching the backend's
 *    presence-as-flag reading;
 *  - numbers serialise verbatim, so `0` is preserved (not treated as empty).
 *
 * Endpoints that must send a possibly-empty value unconditionally (e.g. the
 * universe search `q`) keep building params by hand on purpose. */
function buildQueryString(params: Record<string, QueryValue>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value == null || value === "") continue;
    if (typeof value === "boolean") {
      if (value) search.set(key, "true");
      continue;
    }
    search.set(key, String(value));
  }
  const qs = search.toString();
  return qs ? `?${qs}` : "";
}

function normalizeTaskStatus(raw: unknown): TaskStatus {
  const r = raw as Record<string, unknown>;
  const id = String(r.task_id ?? "");
  return { ...(raw as TaskStatus), task_id: id };
}

function normalizeTaskList(raw: unknown): TaskStatus[] {
  if (!Array.isArray(raw)) {
    return [];
  }
  return raw.map((row) => normalizeTaskStatus(row));
}

function normalizeRunRow(raw: unknown): RunRow {
  const r = raw as Record<string, unknown>;
  return {
    ...(raw as RunRow),
    run_id: String(r.run_id ?? ""),
    task_id: String(r.task_id ?? ""),
  };
}

function normalizeCycleRunRow(raw: unknown): CycleRunRow {
  const r = raw as Record<string, unknown>;
  return { ...(raw as CycleRunRow), task_id: String(r.task_id ?? "") };
}

function normalizeDebugSessionSummary(raw: unknown): DebugSessionSummary {
  const r = raw as Record<string, unknown>;
  return {
    ...(raw as DebugSessionSummary),
    task_id: String(r.task_id ?? ""),
    error_type: (r.error_type as string | null | undefined) ?? null,
    traceback_tail: (r.traceback_tail as string | null | undefined) ?? null,
  };
}

function normalizeModelInvocationRow(raw: unknown): ModelInvocationRow {
  const r = raw as Record<string, unknown>;
  const taskId = r.task_id as string | null | undefined;
  return { ...(raw as ModelInvocationRow), task_id: taskId ?? null };
}

function normalizeSystemState(raw: unknown): SystemState {
  const r = raw as Record<string, unknown>;
  return {
    kill_switch_enabled: Boolean(r.kill_switch_enabled),
    task_count: Number(r.task_count ?? r.instance_count ?? 0),
    running_count: Number(r.running_count ?? 0),
  };
}

function normalizeAgent(raw: unknown): Agent {
  const row = raw as Record<string, unknown>;
  const templateId = row.system_prompt_template_id;
  const resolved = row.resolved_system_prompt;
  return {
    ...(raw as Agent),
    system_prompt: String(row.system_prompt ?? ""),
    system_prompt_template_id:
      typeof templateId === "string" && templateId.trim() ? templateId : null,
    resolved_system_prompt:
      typeof resolved === "string" ? resolved : undefined,
    is_builtin: !!row.is_builtin,
    editable_fields: Array.isArray(row.editable_fields)
      ? (row.editable_fields as string[])
      : undefined,
  };
}

function asString(value: unknown): string {
  return typeof value === "string" ? value : value == null ? "" : String(value);
}

function asNullableString(value: unknown): string | null {
  return typeof value === "string" ? value : value == null ? null : String(value);
}

function normalizePushedMessage(raw: unknown): PushedMessage {
  const r = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  return {
    message_id: asString(r.message_id),
    session_id: asNullableString(r.session_id),
    role: asString(r.role),
    content: asString(r.content),
    created_at: asNullableString(r.created_at),
    source: asNullableString(r.source),
    channel_target: asNullableString(r.channel_target),
    delivery_status: asNullableString(r.delivery_status),
    run_id: asNullableString(r.run_id),
    cron_job_run_id: asNullableString(r.cron_job_run_id),
    reconstructed: r.reconstructed === true,
    note: asNullableString(r.note),
  };
}

function normalizeAssistantSessionSummary(raw: unknown): AssistantSessionSummary {
  const r = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  return {
    session_id: asString(r.session_id),
    title: asNullableString(r.title),
    status: asString(r.status),
    agent_id: asNullableString(r.agent_id),
  };
}

function normalizePushDetail(raw: unknown): PushDetail {
  const r = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  const strategy = (r.strategy && typeof r.strategy === "object" ? r.strategy : {}) as Record<string, unknown>;
  const composer = (r.composer_agent && typeof r.composer_agent === "object" ? r.composer_agent : {}) as Record<string, unknown>;
  const session = (r.assistant_session && typeof r.assistant_session === "object" ? r.assistant_session : {}) as Record<string, unknown>;
  const pushed = (r.pushed_messages && typeof r.pushed_messages === "object" ? r.pushed_messages : {}) as Record<string, unknown>;
  const approvals = (r.approvals && typeof r.approvals === "object" ? r.approvals : {}) as Record<string, unknown>;
  const pushedItems = Array.isArray(pushed.items) ? pushed.items : [];
  const approvalItems = Array.isArray(approvals.items) ? (approvals.items as PendingApproval[]) : [];
  return {
    resolved_from_kind: asString(r.resolved_from_kind),
    strategy: {
      name: asNullableString(strategy.name),
      task_id: asNullableString(strategy.task_id),
      reason: asNullableString(strategy.reason),
    },
    composer_agent: {
      agent: composer.agent != null ? normalizeAgent(composer.agent) : null,
      agent_id: asNullableString(composer.agent_id),
      compose_mode: asNullableString(composer.compose_mode),
      reason: asNullableString(composer.reason),
    },
    assistant_session: {
      session: session.session != null ? normalizeAssistantSessionSummary(session.session) : null,
      reason: asNullableString(session.reason),
    },
    pushed_messages: {
      items: pushedItems.map((item) => normalizePushedMessage(item)),
      reason: asNullableString(pushed.reason),
    },
    approvals: {
      items: approvalItems.map((item) => {
        const a = (item && typeof item === "object" ? item : {}) as Record<string, unknown>;
        return { ...(item as PendingApproval), approval_id: asString(a.approval_id) };
      }),
      total: Number(approvals.total ?? approvalItems.length ?? 0),
      reason: asNullableString(approvals.reason),
    },
  };
}

function normalizeAssistantSession(raw: unknown): AssistantSession {
  const row = raw as Record<string, unknown>;
  const config = (row.config && typeof row.config === "object" ? row.config : {}) as Record<string, unknown>;
  const sourceChannelRaw =
    // Backend serializes this field as `channel_source` (see
    // doyoutrade/assistant/repository.py::_derive_channel_source); the older
    // `source_channel` aliases are kept as fallbacks for forward/backward compat.
    (row.channel_source && typeof row.channel_source === "object" ? row.channel_source : null) ??
    (row.source_channel && typeof row.source_channel === "object" ? row.source_channel : null) ??
    (config.source_channel && typeof config.source_channel === "object" ? config.source_channel : null) ??
    null;

  let source_channel: AssistantSession["source_channel"] = null;
  if (sourceChannelRaw) {
    const sourceRow = sourceChannelRaw as Record<string, unknown>;
    const id = sourceRow.id ?? sourceRow.channel_id;
    const name = sourceRow.name ?? sourceRow.channel_name;
    const type = sourceRow.type ?? sourceRow.channel_type;
    if (typeof id === "string" && id.trim()) {
      source_channel = {
        id,
        name: typeof name === "string" && name.trim() ? name : null,
        type: typeof type === "string" && type.trim() ? type : null,
      };
    }
  }

  return {
    ...(raw as AssistantSession),
    config,
    source_channel,
  };
}

export async function getHealth(): Promise<{ status: string }> {
  return request("/health");
}

export async function getRuntimeStatus(): Promise<RuntimeStatus> {
  return request("/runtime/status");
}

export async function getRuntimeCapabilities(): Promise<RuntimeCapabilitiesResponse> {
  return request("/runtime/capabilities");
}

export async function createAssistantSession(payload: {
  title?: string;
  agent_id: string;
}): Promise<AssistantSession> {
  const result = await request<AssistantSession>("/assistant/sessions", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  return normalizeAssistantSession(result);
}

export async function listAssistantAgents(params?: {
  include_inactive?: boolean;
}): Promise<AgentListResponse> {
  const suffix = buildQueryString({ include_inactive: params?.include_inactive });
  const result = await request<AgentListResponse>(`/assistant/agents${suffix}`);
  return {
    ...result,
    items: Array.isArray(result.items) ? result.items.map((item) => normalizeAgent(item)) : [],
  };
}

export async function createAssistantAgent(
  payload: CreateAgentPayload,
): Promise<Agent> {
  const result = await request<Agent>("/assistant/agents", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  return normalizeAgent(result);
}

export async function getAssistantAgent(agentId: string): Promise<Agent> {
  const result = await request<Agent>(`/assistant/agents/${encodeURIComponent(agentId)}`);
  return normalizeAgent(result);
}

export async function updateAssistantAgent(
  agentId: string,
  payload: Partial<CreateAgentPayload>,
): Promise<Agent> {
  const result = await request<Agent>(`/assistant/agents/${encodeURIComponent(agentId)}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
  return normalizeAgent(result);
}

export async function deleteAssistantAgent(
  agentId: string,
  opts?: { force?: boolean },
): Promise<void> {
  const query = opts?.force ? "?force=true" : "";
  return request(`/assistant/agents/${encodeURIComponent(agentId)}${query}`, {
    method: "DELETE",
  });
}

export async function cloneAssistantAgent(
  agentId: string,
  newName: string,
): Promise<Agent> {
  const result = await request<Agent>(`/assistant/agents/${encodeURIComponent(agentId)}/clone`, {
    method: "POST",
    body: JSON.stringify({ name: newName }),
  });
  return normalizeAgent(result);
}

export async function listCronJobs(agentId: string): Promise<import("./types").CronJobListResponse> {
  return request(`/assistant/agents/${encodeURIComponent(agentId)}/cron/jobs`);
}

export async function createCronJob(
  agentId: string,
  payload: import("./types").CronJobFormValues,
): Promise<import("./types").CronJob> {
  return request(`/assistant/agents/${encodeURIComponent(agentId)}/cron/jobs`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function getCronJob(agentId: string, jobId: string): Promise<import("./types").CronJob> {
  return request(`/assistant/agents/${encodeURIComponent(agentId)}/cron/jobs/${encodeURIComponent(jobId)}`);
}

export async function updateCronJob(
  agentId: string,
  jobId: string,
  payload: Partial<import("./types").CronJobFormValues>,
): Promise<import("./types").CronJob> {
  return request(`/assistant/agents/${encodeURIComponent(agentId)}/cron/jobs/${encodeURIComponent(jobId)}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export async function deleteCronJob(agentId: string, jobId: string): Promise<void> {
  return request(`/assistant/agents/${encodeURIComponent(agentId)}/cron/jobs/${encodeURIComponent(jobId)}`, {
    method: "DELETE",
  });
}

export async function pauseCronJob(agentId: string, jobId: string): Promise<import("./types").CronJob> {
  return request(`/assistant/agents/${encodeURIComponent(agentId)}/cron/jobs/${encodeURIComponent(jobId)}/pause`, {
    method: "POST",
  });
}

export async function resumeCronJob(agentId: string, jobId: string): Promise<import("./types").CronJob> {
  return request(`/assistant/agents/${encodeURIComponent(agentId)}/cron/jobs/${encodeURIComponent(jobId)}/resume`, {
    method: "POST",
  });
}

export async function triggerCronJob(
  agentId: string,
  jobId: string,
): Promise<{ cron_job_run_id: string }> {
  return request(`/assistant/agents/${encodeURIComponent(agentId)}/cron/jobs/${encodeURIComponent(jobId)}/run`, {
    method: "POST",
  });
}

export async function listCronJobRuns(
  jobId: string,
  limit = 20,
): Promise<{ items: import("./types").CronJobRun[] }> {
  const suffix = buildQueryString({ limit });
  return request(`/assistant/cron-jobs/${encodeURIComponent(jobId)}/runs${suffix}`);
}

export async function getCronJobRun(runId: string): Promise<import("./types").CronJobRun> {
  return request(`/assistant/cron-job-runs/${encodeURIComponent(runId)}`);
}

export async function getCronJobRunTrace(
  runId: string,
): Promise<import("./types").CronJobRunTrace> {
  return request(`/assistant/cron-job-runs/${encodeURIComponent(runId)}/trace`);
}

export async function listAssistantAgentTools(): Promise<{ tools: AssistantTool[] }> {
  return request("/assistant/agents/tools");
}

export async function listAssistantAgentSkills(): Promise<{
  items: Array<{ name: string; description: string; category: string }>;
}> {
  return request("/assistant/agents/skills");
}

function normalizeAssistantAgentPromptTemplate(raw: unknown): AgentPromptTemplate {
  const row = raw as Record<string, unknown>;
  return {
    template_id: String(row.template_id ?? row.id ?? ""),
    name: String(row.name ?? row.title ?? row.template_name ?? ""),
    system_prompt: String(row.system_prompt ?? row.prompt ?? row.content ?? ""),
    description: String(row.description ?? row.summary ?? ""),
  };
}

export async function listAssistantAgentPromptTemplates(): Promise<{
  items: AgentPromptTemplate[];
  total: number;
}> {
  const raw = await request<unknown>("/assistant/agents/prompt-templates");
  const items = Array.isArray(raw)
    ? raw
    : Array.isArray((raw as { items?: unknown[] })?.items)
      ? (raw as { items: unknown[] }).items
      : [];
  return {
    items: items.map((item) => normalizeAssistantAgentPromptTemplate(item)),
    total: items.length,
  };
}

export async function listAssistantChannels(): Promise<AssistantChannelListResponse> {
  return request("/assistant/channels");
}

export async function createAssistantChannel(
  payload: import("./types").CreateAssistantChannelPayload,
): Promise<AssistantChannel> {
  return request("/assistant/channels", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function getAssistantChannel(channelId: string): Promise<AssistantChannel> {
  return request(`/assistant/channels/${encodeURIComponent(channelId)}`);
}

export async function updateAssistantChannel(
  channelId: string,
  payload: import("./types").UpdateAssistantChannelPayload,
): Promise<AssistantChannel> {
  return request(`/assistant/channels/${encodeURIComponent(channelId)}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export async function deleteAssistantChannel(channelId: string): Promise<void> {
  return request(`/assistant/channels/${encodeURIComponent(channelId)}`, {
    method: "DELETE",
  });
}

export async function copyAssistantChannelSecret(
  channelId: string,
  secretKey: string,
): Promise<AssistantChannelSecretCopyResponse> {
  return request(
    `/assistant/channels/${encodeURIComponent(channelId)}/secrets/${encodeURIComponent(secretKey)}/copy`,
    { method: "POST" },
  );
}

export async function startAssistantChannel(channelId: string): Promise<AssistantChannel> {
  return request(`/assistant/channels/${encodeURIComponent(channelId)}/start`, { method: "POST" });
}

export async function stopAssistantChannel(channelId: string): Promise<AssistantChannel> {
  return request(`/assistant/channels/${encodeURIComponent(channelId)}/stop`, { method: "POST" });
}

export async function listAssistantSessions(params?: {
  limit?: number;
  offset?: number;
  channel_id?: string;
  source?: "web" | "channel";
}): Promise<AssistantSessionListResponse> {
  const suffix = buildQueryString({
    limit: params?.limit,
    offset: params?.offset,
    channel_id: params?.channel_id,
    source: params?.source,
  });
  const result = await request<AssistantSessionListResponse>(`/assistant/sessions${suffix}`);
  return {
    ...result,
    items: Array.isArray(result.items) ? result.items.map((item) => normalizeAssistantSession(item)) : [],
  };
}

export async function getAssistantSession(sessionId: string): Promise<AssistantSession> {
  const result = await request<AssistantSession>(`/assistant/sessions/${encodeURIComponent(sessionId)}`);
  return normalizeAssistantSession(result);
}

export async function listAssistantMessages(sessionId: string): Promise<AssistantMessage[]> {
  return request(`/assistant/sessions/${encodeURIComponent(sessionId)}/messages`);
}

export async function listAssistantEvents(
  sessionId: string,
  // `tail: true` asks the backend for the most recent `limit` events (in
  // chronological order) instead of the oldest `limit` — required whenever
  // the caller needs to know the session's *current* state (is a run still
  // in flight, what did it just do) rather than its earliest history. The
  // default (tail omitted) still returns the earliest page, unchanged, for
  // callers that genuinely want the beginning of the log.
  params?: { limit?: number; tail?: boolean },
): Promise<AssistantEvent[]> {
  const suffix = buildQueryString({ limit: params?.limit, tail: params?.tail });
  return request(`/assistant/sessions/${encodeURIComponent(sessionId)}/events${suffix}`);
}

export async function listAssistantTraces(
  sessionId: string,
  params?: { limit?: number; offset?: number },
): Promise<{ items: import("./types").TraceSummary[]; total: number }> {
  const suffix = buildQueryString({ limit: params?.limit, offset: params?.offset });
  return request(`/assistant/sessions/${encodeURIComponent(sessionId)}/traces${suffix}`);
}

export async function getAssistantTraceDetail(
  sessionId: string,
  traceId: string,
): Promise<import("./types").TraceDetail> {
  return request(`/assistant/sessions/${encodeURIComponent(sessionId)}/traces/${encodeURIComponent(traceId)}`);
}

export function assistantEventStreamUrl(sessionId: string, lastEventId?: string | null): string {
  const suffix = buildQueryString({ last_event_id: lastEventId });
  return `${API_BASE}/assistant/sessions/${encodeURIComponent(sessionId)}/events/stream${suffix}`;
}

// ---------------------------------------------------------------- Swarm

export async function listSwarmPresets(): Promise<SwarmPresetSummary[]> {
  const result = await request<{ presets: SwarmPresetSummary[] }>("/swarm/presets");
  return result.presets ?? [];
}

export async function inspectSwarmPreset(name: string): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>(`/swarm/presets/${encodeURIComponent(name)}`);
}

export async function startSwarmRun(
  presetName: string,
  userVars: Record<string, string>,
): Promise<SwarmRun> {
  return request<SwarmRun>("/swarm/runs", {
    method: "POST",
    body: JSON.stringify({ preset_name: presetName, user_vars: userVars }),
  });
}

export async function listSwarmRuns(limit = 50): Promise<SwarmRun[]> {
  const suffix = buildQueryString({ limit });
  const result = await request<{ runs: SwarmRun[] }>(`/swarm/runs${suffix}`);
  return result.runs ?? [];
}

export async function getSwarmRun(runId: string): Promise<SwarmRun> {
  return request<SwarmRun>(`/swarm/runs/${encodeURIComponent(runId)}`);
}

export async function cancelSwarmRun(runId: string): Promise<{ cancelled: boolean }> {
  return request<{ cancelled: boolean }>(`/swarm/runs/${encodeURIComponent(runId)}/cancel`, {
    method: "POST",
  });
}

export function swarmRunEventStreamUrl(runId: string, lastEventId?: string | null): string {
  const suffix = buildQueryString({ last_event_id: lastEventId });
  return `${API_BASE}/swarm/runs/${encodeURIComponent(runId)}/events/stream${suffix}`;
}

export async function sendAssistantMessage(
  sessionId: string,
  content: string,
): Promise<AssistantSendMessageResponse> {
  const result = await request<AssistantSendMessageResponse>(`/assistant/sessions/${encodeURIComponent(sessionId)}/messages`, {
    method: "POST",
    body: JSON.stringify({ content }),
  });
  return {
    ...result,
    session: normalizeAssistantSession(result.session),
  };
}

export async function stopAssistantSession(sessionId: string): Promise<AssistantStopResponse> {
  return request(`/assistant/sessions/${encodeURIComponent(sessionId)}/stop`, {
    method: "POST",
  });
}

export async function resolveAssistantApproval(
  approvalId: string,
  action: "approve_once" | "approve_always" | "reject",
): Promise<{ status: string; approval_id: string; action: string }> {
  return request(`/assistant/approvals/${encodeURIComponent(approvalId)}/resolve`, {
    method: "POST",
    body: JSON.stringify({ action }),
  });
}

export async function listPendingAssistantApprovals(
  sessionId: string,
): Promise<{ items: AssistantPendingApproval[] }> {
  return request(
    `/assistant/sessions/${encodeURIComponent(sessionId)}/approvals/pending`,
  );
}

export async function searchInstrumentUniverse(params: {
  source: string;
  q: string;
  limit?: number;
}): Promise<InstrumentUniverseSearchResponse> {
  const searchParams = new URLSearchParams();
  searchParams.set("source", params.source);
  searchParams.set("q", params.q);
  if (params.limit != null) {
    searchParams.set("limit", String(params.limit));
  }
  return request(`/instrument-universe/search?${searchParams.toString()}`);
}

export async function listInstrumentCatalog(params: {
  q?: string;
  limit?: number;
  offset?: number;
}): Promise<InstrumentCatalogListResponse> {
  const suffix = buildQueryString({
    q: params.q?.trim(),
    limit: params.limit,
    offset: params.offset,
  });
  return request(`/instruments/catalog${suffix}`);
}

export async function getInstrumentCatalogItem(symbol: string): Promise<InstrumentCatalogRow> {
  const searchParams = new URLSearchParams();
  searchParams.set("symbol", symbol);
  return request(`/instruments/catalog/item?${searchParams.toString()}`);
}

export async function syncInstrumentCatalog(payload: {
  source: "akshare" | "qmt";
  mode: "full" | "symbols";
  symbols?: string[];
}): Promise<InstrumentCatalogSyncResponse> {
  return request("/instruments/catalog/sync", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function deleteInstrumentCatalogSymbols(symbols: string[]): Promise<InstrumentCatalogDeleteResponse> {
  return request("/instruments/catalog/delete", {
    method: "POST",
    body: JSON.stringify({ symbols }),
  });
}

/** Must pass confirm exactly ``clear_all_instrument_catalog`` (server-enforced). */
export async function clearInstrumentCatalog(confirm: string): Promise<InstrumentCatalogDeleteResponse> {
  return request("/instruments/catalog/clear", {
    method: "POST",
    body: JSON.stringify({ confirm }),
  });
}

export async function listWatchlist(tag?: string): Promise<WatchlistListResponse> {
  const suffix = buildQueryString({ tag: tag?.trim() });
  return request(`/watchlist${suffix}`);
}

export async function getWatchlistEntry(entryId: string): Promise<WatchlistEntry> {
  return request(`/watchlist/${encodeURIComponent(entryId)}`);
}

export async function addWatchlistEntry(payload: CreateWatchlistPayload): Promise<WatchlistEntry> {
  return request("/watchlist", { method: "POST", body: JSON.stringify(payload) });
}

export async function updateWatchlistEntry(
  entryId: string,
  payload: UpdateWatchlistPayload,
): Promise<WatchlistEntry> {
  return request(`/watchlist/${encodeURIComponent(entryId)}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export async function deleteWatchlistEntry(entryId: string): Promise<void> {
  return request(`/watchlist/${encodeURIComponent(entryId)}`, { method: "DELETE" });
}

export async function listWatchlistTags(): Promise<WatchlistTagsResponse> {
  return request("/watchlist/tags");
}

// ---------------------------------------------------------------------------
// 股票智能盯盘 (stock intelligent monitoring) — /monitors
// ---------------------------------------------------------------------------

export async function listMonitors(): Promise<MonitorListResponse> {
  return request("/monitors");
}

export async function getMonitor(id: string): Promise<MonitorRule> {
  return request(`/monitors/${encodeURIComponent(id)}`);
}

export async function createMonitor(payload: CreateMonitorPayload): Promise<MonitorRule> {
  return request("/monitors", { method: "POST", body: JSON.stringify(payload) });
}

export async function updateMonitor(
  id: string,
  payload: UpdateMonitorPayload,
): Promise<MonitorRule> {
  return request(`/monitors/${encodeURIComponent(id)}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export async function deleteMonitor(id: string): Promise<{ status: string }> {
  return request(`/monitors/${encodeURIComponent(id)}`, { method: "DELETE" });
}

/** Enable a monitor (PUT ``{enabled:true,status:"active"}``). */
export async function enableMonitor(id: string): Promise<MonitorRule> {
  return updateMonitor(id, { enabled: true, status: "active" });
}

/** Disable a monitor (PUT ``{enabled:false,status:"paused"}``). */
export async function disableMonitor(id: string): Promise<MonitorRule> {
  return updateMonitor(id, { enabled: false, status: "paused" });
}

export async function listMonitorAlerts(
  id: string,
  params?: { symbol?: string; limit?: number },
): Promise<MonitorAlertListResponse> {
  const suffix = buildQueryString({ symbol: params?.symbol?.trim(), limit: params?.limit });
  return request(`/monitors/${encodeURIComponent(id)}/alerts${suffix}`);
}

export async function runMonitorOnce(id: string): Promise<MonitorRunOnceResponse> {
  return request(`/monitors/${encodeURIComponent(id)}/run-once`, { method: "POST" });
}

/**
 * One-shot snapshot of quotes for the given symbols (``GET /market/quotes``).
 * ``symbol`` is sent as a repeated query param so the backend can read a list.
 * Used for the first paint and the detail modal; the live column is driven by
 * the WebSocket stream (see {@link WS_BASE} / ``useMarketQuoteStream``).
 */
export async function getQuotesOnce(symbols: string[]): Promise<QuotesResponse> {
  const q = new URLSearchParams();
  for (const symbol of symbols) {
    const trimmed = symbol.trim();
    if (trimmed) {
      q.append("symbol", trimmed);
    }
  }
  const suffix = q.size ? `?${q.toString()}` : "";
  return request(`/market/quotes${suffix}`);
}

export async function listTasks(): Promise<TaskStatus[]> {
  const rows = await request<unknown>("/tasks");
  return normalizeTaskList(rows);
}

export async function listTasksPage(params: {
  q?: string;
  status?: string;
  mode?: string;
  modes?: string[];
  definition_id?: string;
  limit?: number;
  offset?: number;
}): Promise<TaskListResponse> {
  const modesClean = (params.modes ?? []).map((m) => m.trim()).filter((m) => m !== "");
  const suffix = buildQueryString({
    q: params.q?.trim(),
    status: params.status,
    mode: params.mode,
    modes: modesClean.join(","),
    definition_id: params.definition_id,
    limit: params.limit,
    offset: params.offset,
  });
  const body = await request<{ items: unknown[]; total: number; limit: number; offset: number }>(`/tasks/page${suffix}`);
  return {
    items: body.items.map((row) => normalizeTaskStatus(row)),
    total: body.total,
    limit: body.limit,
    offset: body.offset,
  };
}

export async function getTask(taskId: string): Promise<TaskStatus> {
  const row = await request<unknown>(`/tasks/${encodeURIComponent(taskId)}`);
  return normalizeTaskStatus(row);
}

export async function getTaskDuplicatePreset(taskId: string): Promise<TaskDuplicatePreset> {
  return request(`/tasks/${encodeURIComponent(taskId)}/duplicate-preset`);
}

export async function listStrategyDefinitions(): Promise<{ items: StrategyDefinitionRow[] }> {
  return request("/strategy-definitions");
}

export async function getStrategyDefinition(definitionId: string): Promise<StrategyDefinitionDetail> {
  return request(`/strategy-definitions/${encodeURIComponent(definitionId)}`);
}

export async function updateStrategyDefinition(
  definitionId: string,
  payload: { name?: string; status?: string },
): Promise<StrategyDefinitionDetail> {
  return request(`/strategy-definitions/${encodeURIComponent(definitionId)}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export async function deleteStrategyDefinition(definitionId: string): Promise<void> {
  return request(`/strategy-definitions/${encodeURIComponent(definitionId)}`, { method: "DELETE" });
}

export async function deleteStrategyDefinitions(definitionIds: string[]): Promise<void> {
  return request("/strategy-definitions", {
    method: "DELETE",
    body: JSON.stringify({ definition_ids: definitionIds }),
  });
}

export async function compileStrategyDefinition(definitionId: string): Promise<StrategyDefinitionCompileResult> {
  return request(`/strategy-definitions/${encodeURIComponent(definitionId)}/compile`, { method: "POST" });
}

export async function createTask(payload: CreateTaskPayload): Promise<TaskStatus> {
  const row = await request<unknown>("/tasks", { method: "POST", body: JSON.stringify(payload) });
  return normalizeTaskStatus(row);
}

export async function startTask(taskId: string): Promise<TaskStatus> {
  const row = await request<unknown>(`/tasks/${encodeURIComponent(taskId)}/start`, { method: "POST" });
  return normalizeTaskStatus(row);
}

export async function pauseTask(taskId: string): Promise<TaskStatus> {
  const row = await request<unknown>(`/tasks/${encodeURIComponent(taskId)}/pause`, { method: "POST" });
  return normalizeTaskStatus(row);
}

export async function stopTask(taskId: string): Promise<TaskStatus> {
  const row = await request<unknown>(`/tasks/${encodeURIComponent(taskId)}/stop`, { method: "POST" });
  return normalizeTaskStatus(row);
}

export async function deleteTask(taskId: string): Promise<void> {
  return request(`/tasks/${encodeURIComponent(taskId)}`, { method: "DELETE" });
}

export async function deleteTasks(taskIds: string[]): Promise<void> {
  return request("/tasks", { method: "DELETE", body: JSON.stringify({ task_ids: taskIds }) });
}

export type CreateAccountPayload = {
  name: string;
  mode: "live" | "mock";
  base_url?: string;
  token?: string | null;
  timeout_seconds?: number;
  qmt_account_id?: string | null;
  qmt_terminal_id?: string | null;
  session_id?: string | null;
  mock_cash?: number;
  mock_equity?: number;
  mock_positions?: import("./types").AccountMockPosition[];
  is_default?: boolean;
  enabled?: boolean;
};

export async function listAccounts(): Promise<AccountListResponse> {
  return request("/accounts");
}

export async function getAccount(accountId: string): Promise<Account> {
  return request(`/accounts/${encodeURIComponent(accountId)}`);
}

export async function createAccount(payload: CreateAccountPayload): Promise<Account> {
  return request("/accounts", { method: "POST", body: JSON.stringify(payload) });
}

export async function updateAccount(
  accountId: string,
  payload: Partial<CreateAccountPayload>,
): Promise<Account> {
  return request(`/accounts/${encodeURIComponent(accountId)}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export async function deleteAccount(accountId: string): Promise<void> {
  return request(`/accounts/${encodeURIComponent(accountId)}`, { method: "DELETE" });
}

export async function setDefaultAccount(accountId: string): Promise<Account> {
  return request(`/accounts/${encodeURIComponent(accountId)}/set-default`, {
    method: "POST",
  });
}

export async function createDebugSession(
  taskId: string,
  payload: {
    input_overrides?: Record<string, unknown> | null;
  } = {},
): Promise<DebugSessionSummary> {
  const row = await request<unknown>(`/tasks/${encodeURIComponent(taskId)}/debug-sessions`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
  return normalizeDebugSessionSummary(row);
}

export async function listDebugSessions(taskId: string): Promise<DebugSessionSummary[]> {
  const rows = await request<unknown[]>(`/tasks/${encodeURIComponent(taskId)}/debug-sessions`);
  return rows.map((r) => normalizeDebugSessionSummary(r));
}

export async function getDebugSession(taskId: string, debugSessionId: string): Promise<DebugSessionDetail> {
  const raw = await request<Record<string, unknown>>(
    `/tasks/${encodeURIComponent(taskId)}/debug-sessions/${encodeURIComponent(debugSessionId)}`,
  );
  return {
    ...normalizeDebugSessionSummary(raw),
    spans: (raw.spans as DebugSessionDetail["spans"]) ?? [],
    model_invocations: ((raw.model_invocations as ModelInvocationRow[]) ?? []).map((m) => normalizeModelInvocationRow(m)),
  };
}

export type ListCycleRunsParams = {
  limit?: number;
  offset?: number;
  /** Substring match on run_id (backend query param `q`). */
  q?: string;
  status?: string;
  run_kind?: string;
  /** Exact match on ``cycle_runs.run_mode`` (e.g. ``backtest``, ``paper``). */
  run_mode?: string;
  /** Exclude rows where ``run_kind`` equals this (e.g. ``debug`` for scheduler-only backtest). */
  exclude_run_kind?: string;
  started_after?: string;
  started_before?: string;
  /** Backtest run id; sent as ``run_id`` to filter cycles to that run's session. */
  run_id?: string;
};

export async function listCycleRuns(
  taskId: string,
  params?: ListCycleRunsParams,
): Promise<{ items: CycleRunRow[]; total: number }> {
  const suffix = buildQueryString({
    limit: params?.limit,
    offset: params?.offset,
    q: params?.q,
    status: params?.status,
    run_kind: params?.run_kind,
    run_mode: params?.run_mode,
    exclude_run_kind: params?.exclude_run_kind,
    started_after: params?.started_after,
    started_before: params?.started_before,
    run_id: params?.run_id,
  });
  const body = await request<{ items: unknown[]; total: number }>(
    `/tasks/${encodeURIComponent(taskId)}/cycle-runs${suffix}`,
  );
  return {
    items: body.items.map((row) => normalizeCycleRunRow(row)),
    total: body.total,
  };
}

export type StartTaskRunPayload = {
  range_start: string;
  range_end: string;
  market_profile?: string;
  bar_interval?: string;
  /** When set, overrides the instance default model route for this job. */
  model_route_name?: string | null;
  /** Per-job merge patch: `settings` deep-merged into instance; optional `universe`. */
  config_overrides?: {
    settings?: Record<string, unknown>;
    universe?: string[];
  };
  /**
   * Capture full debug observability for the run. Omit / true = normal (records
   * trace, cycle runs, model invocations). false = fast mode: skips that
   * persistence so the backtest runs faster, keeping status + report + fills.
   */
  debug_enabled?: boolean;
};

export async function startTaskRun(
  taskId: string,
  payload: StartTaskRunPayload,
): Promise<RunRow> {
  const row = await request<unknown>(`/tasks/${encodeURIComponent(taskId)}/runs`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
  return normalizeRunRow(row);
}

export async function listTaskRuns(
  taskId: string,
  params?: { limit?: number; offset?: number },
): Promise<{ items: RunRow[]; total: number }> {
  const suffix = buildQueryString({ limit: params?.limit, offset: params?.offset });
  const body = await request<{ items: unknown[]; total: number }>(
    `/tasks/${encodeURIComponent(taskId)}/runs${suffix}`,
  );
  return {
    items: body.items.map((row) => normalizeRunRow(row)),
    total: body.total,
  };
}
export async function getTaskRun(taskId: string, runId: string): Promise<RunRow> {
  const row = await request<unknown>(
    `/tasks/${encodeURIComponent(taskId)}/runs/${encodeURIComponent(runId)}`,
  );
  return normalizeRunRow(row);
}

function normalizeBacktestChartTrade(raw: BacktestChartTrade): BacktestChartTrade {
  const toNum = (v: unknown): number | null => {
    if (v == null) return null;
    if (typeof v === "number") return Number.isFinite(v) ? v : null;
    if (typeof v === "string" && v.trim() !== "") {
      const n = Number(v);
      return Number.isFinite(n) ? n : null;
    }
    return null;
  };
  return {
    ...raw,
    price: toNum(raw.price),
    quantity: toNum(raw.quantity),
  };
}

export async function getTaskRunChart(
  taskId: string,
  runId: string,
  params?: {
    symbol?: string;
  },
): Promise<BacktestChartSnapshot> {
  const suffix = buildQueryString({ symbol: params?.symbol });
  const raw = await request<BacktestChartSnapshot>(
    `/tasks/${encodeURIComponent(taskId)}/runs/${encodeURIComponent(runId)}/chart${suffix}`,
  );
  const trades = (raw.trades ?? []).map(normalizeBacktestChartTrade);
  return {
    ...raw,
    run: normalizeRunRow(raw.run),
    trades,
  };
}

export async function getLocalMarketBars(params: {
  symbol: string;
  interval?: string;
  start?: string;
  end?: string;
  provider?: string;
  adjust?: string;
}): Promise<LocalMarketBarsSnapshot> {
  const suffix = buildQueryString({
    symbol: params.symbol,
    interval: params.interval,
    start: params.start,
    end: params.end,
    provider: params.provider,
    adjust: params.adjust,
  });
  return request<LocalMarketBarsSnapshot>(`/market/bars${suffix}`);
}

export async function syncLocalMarketBarsRange(payload: {
  symbol: string;
  interval: string;
  start: string;
  end: string;
  provider?: string | null;
  adjust?: string | null;
  mode: "fill_gap" | "force_refresh";
}): Promise<LocalMarketSyncResponse> {
  return request("/market/bars/sync-range", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function getLocalMarketSyncJob(jobId: string): Promise<LocalMarketSyncJob> {
  return request(`/market/bars/sync-jobs/${encodeURIComponent(jobId)}`);
}

export async function getLocalMarketOverlays(params: {
  symbol: string;
  interval: string;
  start: string;
  end: string;
  overlay_kind: "backtest_trades" | "task_fills" | "signals";
  run_id?: string | null;
  task_id?: string | null;
  signal_source_id?: string | null;
}): Promise<LocalMarketOverlaySnapshot> {
  const suffix = buildQueryString({
    symbol: params.symbol,
    interval: params.interval,
    start: params.start,
    end: params.end,
    overlay_kind: params.overlay_kind,
    run_id: params.run_id,
    task_id: params.task_id,
    signal_source_id: params.signal_source_id,
  });
  return request(`/market/bars/overlays${suffix}`);
}

export async function pauseTaskRun(taskId: string, runId: string): Promise<RunRow> {
  const row = await request<unknown>(
    `/tasks/${encodeURIComponent(taskId)}/runs/${encodeURIComponent(runId)}/pause`,
    { method: "POST" },
  );
  return normalizeRunRow(row);
}

export async function resumeTaskRun(taskId: string, runId: string): Promise<RunRow> {
  const row = await request<unknown>(
    `/tasks/${encodeURIComponent(taskId)}/runs/${encodeURIComponent(runId)}/resume`,
    { method: "POST" },
  );
  return normalizeRunRow(row);
}

export async function stopTaskRun(taskId: string, runId: string): Promise<RunRow> {
  const row = await request<unknown>(
    `/tasks/${encodeURIComponent(taskId)}/runs/${encodeURIComponent(runId)}/stop`,
    { method: "POST" },
  );
  return normalizeRunRow(row);
}

export async function getCycleRun(_taskId: string, runId: string): Promise<CycleRunRow> {
  const row = await request<unknown>(
    `/cycle-runs/${encodeURIComponent(runId)}`,
  );
  return normalizeCycleRunRow(row);
}

export async function getCycleRunDebugView(_taskId: string, runId: string): Promise<CycleRunDebugView> {
  const raw = await request<Record<string, unknown>>(
    `/cycle-runs/${encodeURIComponent(runId)}/debug-view`,
  );
  return {
    cycle_run: normalizeCycleRunRow(raw.cycle_run),
    session: raw.session != null ? normalizeDebugSessionSummary(raw.session) : null,
    spans: (raw.spans as CycleRunDebugView["spans"]) ?? [],
    model_invocations: ((raw.model_invocations as ModelInvocationRow[]) ?? []).map((m) =>
      normalizeModelInvocationRow(m),
    ),
    // ``signal_timeline`` is part of the v2 debug-view payload. Older
    // backend builds omit it; fall back to ``[]`` so consumers can render
    // "no per-cycle signals available" without branching on undefined.
    signal_timeline: (raw.signal_timeline as CycleRunDebugView["signal_timeline"]) ?? [],
    // ``signal_timeline_summary`` is the v3 compact summary placed at the
    // top of the payload so it survives truncation. Fall back to a fully
    // shaped zero summary so the component can render without optional
    // chaining on every field.
    signal_timeline_summary:
      (raw.signal_timeline_summary as CycleRunDebugView["signal_timeline_summary"]) ?? {
        total_cycles: 0,
        total_signals_buy: 0,
        total_signals_sell: 0,
        total_signals_hold: 0,
        total_signals_target_exposure: 0,
        total_signals_target_quantity: 0,
        top_hold_tags: {},
        top_buy_tags: {},
        top_sell_tags: {},
        top_target_exposure_tags: {},
        top_target_quantity_tags: {},
        first_cycle_time: null,
        last_cycle_time: null,
        first_buy_cycle_time: null,
        first_sell_cycle_time: null,
        first_target_exposure_cycle_time: null,
        first_target_quantity_cycle_time: null,
        zero_trade: true,
      },
    // ``push_detail`` is the optional "what actually got pushed" payload added
    // in a later backend build. Absent on older payloads → leave undefined so
    // the component renders the legacy view; normalize defensively otherwise.
    push_detail: raw.push_detail != null ? normalizePushDetail(raw.push_detail) : undefined,
  };
}

export async function listPendingApprovals(): Promise<PendingApproval[]> {
  return request("/approvals/pending");
}

/** Full approval history with server-side filtering + pagination. Powers the
 * Approvals page's single filterable table. ``status`` is sent comma-joined
 * (the backend accepts both comma and repeated forms). */
export async function listApprovals(query: ApprovalQuery = {}): Promise<ApprovalListResponse> {
  const suffix = buildQueryString({
    status: query.status && query.status.length ? query.status.join(",") : undefined,
    symbol: query.symbol,
    task_id: query.task_id,
    account_id: query.account_id,
    decision_source: query.decision_source,
    q: query.q,
    created_after: query.created_after,
    created_before: query.created_before,
    limit: query.limit,
    offset: query.offset,
  });
  return request(`/approvals${suffix}`);
}

export async function approve(
  approvalId: string,
  resolverId?: string,
): Promise<{ status: string }> {
  const body = resolverId ? JSON.stringify({ resolver_id: resolverId }) : undefined;
  return request(`/approvals/${approvalId}/approve`, { method: "POST", body });
}

export async function reject(
  approvalId: string,
  reason?: string,
): Promise<{ status: string }> {
  const body = reason ? JSON.stringify({ reason }) : undefined;
  return request(`/approvals/${approvalId}/reject`, { method: "POST", body });
}

export async function listTemplates(): Promise<AgentTemplate[]> {
  return request("/templates");
}

export async function listDataProviders(): Promise<DataProvidersResponse> {
  return request("/data-providers");
}

// ---------------------------------------------------------------------------
// 打板复盘 / 盘面 (market review) — three whole-market data axes.
// The endpoints return the tool's ``data`` payload directly; a non-2xx (e.g.
// market_breadth_empty on a non-trading day) is thrown as an ApiError by
// ``request()`` carrying ``errorCode`` / ``status`` / ``message`` so callers
// can render a friendly empty state instead of fabricating numbers.
// ---------------------------------------------------------------------------

export async function getMarketBreadth(params?: {
  date?: string;
  data_source?: string;
}): Promise<MarketBreadthData> {
  const body: Record<string, unknown> = {};
  if (params?.date) body.date = params.date;
  if (params?.data_source) body.data_source = params.data_source;
  return request<MarketBreadthData>("/data/breadth", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function getDragonTigerBoard(params?: {
  date?: string;
  start?: string;
  end?: string;
}): Promise<LhbData> {
  const body: Record<string, unknown> = {};
  if (params?.date) body.date = params.date;
  if (params?.start) body.start = params.start;
  if (params?.end) body.end = params.end;
  return request<LhbData>("/data/lhb", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function getFundFlowRanking(params: {
  scope: FundFlowScope;
  period: string;
  sector_type?: string;
  top?: number;
}): Promise<FundFlowData> {
  const body: Record<string, unknown> = {
    scope: params.scope,
    period: params.period,
  };
  if (params.sector_type) body.sector_type = params.sector_type;
  if (params.top != null) body.top = params.top;
  return request<FundFlowData>("/data/fund-flow", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function getSectorHeat(params?: {
  sector_type?: SectorHeatType;
  top?: number;
}): Promise<SectorHeatData> {
  const body: Record<string, unknown> = {};
  if (params?.sector_type) body.sector_type = params.sector_type;
  if (params?.top != null) body.top = params.top;
  return request<SectorHeatData>("/data/sector-heat", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function listModelRoutes(): Promise<{ items: ModelRouteRow[] }> {
  return request("/model-routes");
}

export async function createModelRoute(payload: {
  route_name: string;
  provider_kind: string;
  api_key: string;
  base_url?: string | null;
  target_model?: string | null;
  settings?: Record<string, unknown> | null;
}): Promise<ModelRouteRow> {
  return request("/model-routes", { method: "POST", body: JSON.stringify(payload) });
}

export async function patchModelRoute(
  routeId: string,
  payload: Partial<{
    route_name: string;
    provider_kind: string;
    api_key: string;
    base_url: string | null;
    target_model: string | null;
    settings: Record<string, unknown> | null;
  }>,
): Promise<ModelRouteRow> {
  return request(`/model-routes/${encodeURIComponent(routeId)}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export async function deleteModelRoute(routeId: string): Promise<void> {
  return request(`/model-routes/${encodeURIComponent(routeId)}`, { method: "DELETE" });
}

export async function revealModelRouteApiKey(routeId: string): Promise<{ api_key: string }> {
  return request(`/model-routes/${encodeURIComponent(routeId)}/api-key`);
}

// ---------------------------------------------------------------------------
// 系统配置 (static / low-frequency YAML config) — contract A. The Settings page
// only ever talks to these four doyoutrade endpoints; the qmt-proxy ones are
// server-side forwards (400 ``qmt_proxy_unreachable`` / 502 ``qmt_proxy_error``
// surface as {@link ApiError} with the matching ``errorCode``).
// ---------------------------------------------------------------------------

export async function getConfig(): Promise<import("./types").DoyoutradeConfigResponse> {
  return request("/config");
}

/** PUT a deep-merge patch of only the changed fields (secret fields left as
 * ``"********"`` or omitted keep their stored value). */
export async function updateConfig(
  patch: Record<string, unknown>,
): Promise<import("./types").ConfigUpdateResponse> {
  return request("/config", { method: "PUT", body: JSON.stringify(patch) });
}

export async function getQmtProxyConfig(): Promise<import("./types").QmtProxyConfigResponse> {
  return request("/qmt-proxy/config");
}

export async function updateQmtProxyConfig(
  patch: Record<string, unknown>,
): Promise<import("./types").ConfigUpdateResponse> {
  return request("/qmt-proxy/config", { method: "PUT", body: JSON.stringify(patch) });
}

// ---------------------------------------------------------------------------
// 自动更新 (release-based self-update). ``check`` polls GitHub server-side;
// ``apply`` stages the reinstall and gracefully restarts the server — expect
// the API to go away shortly after a successful apply.
// ---------------------------------------------------------------------------

export async function getUpdateStatus(): Promise<import("./types").UpdateStatus> {
  return request("/update/status");
}

export async function checkForUpdate(): Promise<import("./types").UpdateStatus> {
  return request("/update/check", { method: "POST" });
}

export async function applyUpdate(): Promise<import("./types").UpdateStatus> {
  return request("/update/apply", { method: "POST" });
}

export async function getSystemState(): Promise<SystemState> {
  const raw = await request<unknown>("/system/state");
  return normalizeSystemState(raw);
}

export async function setKillSwitch(enabled: boolean): Promise<SystemState> {
  const raw = await request<unknown>("/system/kill-switch", {
    method: "POST",
    body: JSON.stringify({ enabled }),
  });
  return normalizeSystemState(raw);
}

export async function tickOnce(): Promise<{ executed: number; expired_count: number }> {
  return request("/system/tick", { method: "POST" });
}

export async function listSkills(): Promise<Skill[]> {
  return request("/skills");
}

export async function getSkillDetail(skillId: string): Promise<SkillDetail> {
  return request(`/skills/${encodeURIComponent(skillId)}`);
}

export async function getSkillFile(skillId: string, path: string): Promise<SkillFile> {
  const qs = new URLSearchParams({ path }).toString();
  return request(`/skills/${encodeURIComponent(skillId)}/files?${qs}`);
}

export async function putSkillFile(
  skillId: string,
  path: string,
  body: { content: string; encoding?: "utf-8" | "base64"; if_unmodified_since?: string },
): Promise<{ path: string; size: number; mtime: string }> {
  const qs = new URLSearchParams({ path }).toString();
  return request(`/skills/${encodeURIComponent(skillId)}/files?${qs}`, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function createSkillFile(
  skillId: string,
  body: { path: string; kind: "file" | "dir"; content?: string; encoding?: "utf-8" | "base64" },
): Promise<{ path: string; kind: string }> {
  return request(`/skills/${encodeURIComponent(skillId)}/files`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function renameSkillFile(
  skillId: string,
  fromPath: string,
  toPath: string,
): Promise<{ from: string; to: string }> {
  return request(`/skills/${encodeURIComponent(skillId)}/files/rename`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ from: fromPath, to: toPath }),
  });
}

export async function deleteSkillFile(skillId: string, path: string): Promise<void> {
  const qs = new URLSearchParams({ path }).toString();
  await request(`/skills/${encodeURIComponent(skillId)}/files?${qs}`, { method: "DELETE" });
}

export async function createSkill(body: {
  folder_name: string;
  name: string;
  description: string;
  license?: string;
}): Promise<{ skill_id: string }> {
  return request(`/skills`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function renameSkillFolder(
  skillId: string,
  newFolderName: string,
): Promise<{ skill_id: string }> {
  return request(`/skills/${encodeURIComponent(skillId)}/rename`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ new_folder_name: newFolderName }),
  });
}

export async function deleteSkill(skillId: string): Promise<void> {
  await request(`/skills/${encodeURIComponent(skillId)}`, { method: "DELETE" });
}

export async function updateSkillFrontmatter(
  skillId: string,
  body: { name?: string; description?: string; license?: string },
): Promise<{ frontmatter: SkillFrontmatter }> {
  return request(`/skills/${encodeURIComponent(skillId)}/frontmatter`, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function setSkillEnabled(
  skillId: string,
  enabled: boolean,
): Promise<{ name: string; enabled: boolean }> {
  return request(`/skills/${encodeURIComponent(skillId)}/${enabled ? "enable" : "disable"}`, {
    method: "POST",
  });
}

export async function listAssistantTools(): Promise<AssistantTool[]> {
  const data = await request<{ tools: AssistantTool[] }>("/assistant/tools");
  return data.tools;
}

/**
 * List the user's private 复盘 (trading review) journals
 * (``GET /knowledge/journals``). Items are returned newest-first; the response
 * is empty with ``root_exists: false`` when no journals have been recorded yet.
 */
export async function listKnowledgeJournals(): Promise<KnowledgeJournalList> {
  return request("/knowledge/journals");
}

/**
 * Fetch a single 复盘 journal's markdown body
 * (``GET /knowledge/journal?path=<journal-relative .md path>``). The backend
 * rejects traversal / non-``.md`` paths with 400 and returns 404 when missing.
 */
export async function getKnowledgeJournal(path: string): Promise<KnowledgeJournal> {
  return request(`/knowledge/journal?path=${encodeURIComponent(path)}`);
}

/**
 * Fetch the structured knowledge-base index — the navigation map for the
 * top-level Knowledge page (``GET /knowledge/index``). Fresh-generated on
 * every call; optional ``partition`` scopes to one of
 * cycles/symbols/trades/journal/backtests.
 */
export async function getKnowledgeIndex(partition?: string): Promise<KnowledgeIndex> {
  const qs = partition ? `?partition=${encodeURIComponent(partition)}` : "";
  return request(`/knowledge/index${qs}`);
}

/**
 * Read one file from any partition (``GET /knowledge/file``). Returns markdown
 * content (``kind: "markdown"``) or a parsed CSV table (``kind: "csv"``). The
 * backend sandboxes to ``<kb_root>/<partition>/`` and rejects traversal /
 * non-md-csv / oversize with 4xx.
 */
export async function getKnowledgeFile(partition: string, path: string): Promise<KnowledgeFile> {
  const qs = `?partition=${encodeURIComponent(partition)}&path=${encodeURIComponent(path)}`;
  return request(`/knowledge/file${qs}`);
}

/**
 * Fetch the per-day sentiment-cycle timeline
 * (``GET /knowledge/sentiment-timeline?months=N``, default 3). This is the
 * emotional-cycle memory the daily 复盘 accumulates into the private knowledge
 * base; items come back ordered by ``date`` ascending and empty
 * (``{ items: [] }``) when no reviews have been recorded yet.
 */
export async function getSentimentTimeline(months?: number): Promise<SentimentTimeline> {
  const qs = buildQueryString({ months });
  return request(`/knowledge/sentiment-timeline${qs}`);
}

/**
 * Fetch the per-symbol role cards (``GET /knowledge/symbol-roles``) — the role
 * tags the user keeps in the private knowledge base (e.g. 龙头 / 中军 / 补涨).
 * Items come back ordered by ``updated_at`` descending and empty
 * (``{ items: [] }``) when nothing has been tagged yet.
 */
export async function getSymbolRoles(): Promise<SymbolRoles> {
  return request("/knowledge/symbol-roles");
}

/**
 * Fetch the 打板模式库 (``GET /knowledge/playbook``) — the 战法 / 打法
 * summaries the user keeps in the private knowledge base's ``playbook``
 * partition. Items come back ordered by ``updated_at`` descending and empty
 * (``{ items: [] }``) when nothing has been recorded yet. Fetch a single
 * entry's full markdown with {@link getKnowledgeFile}(``"playbook"``, path).
 */
export async function getPlaybook(): Promise<Playbook> {
  return request("/knowledge/playbook");
}

/**
 * Fetch the 交割单归因 (trade attribution) view
 * (``GET /knowledge/trade-attribution?months=N``) — realized-PnL round-trips
 * reconstructed from the broker settlement statements the user dropped into the
 * private knowledge base's ``trades/`` partition. Money fields come back as
 * decimal strings (possibly negative). An empty account returns a zeroed
 * summary + empty arrays; files that failed to parse are surfaced honestly in
 * ``unparsed`` rather than silently dropped.
 */
export async function getTradeAttribution(months?: number): Promise<TradeAttribution> {
  const qs = buildQueryString({ months });
  return request(`/knowledge/trade-attribution${qs}`);
}

export async function updateTask(
  taskId: string,
  payload: Partial<{
    name: string;
    mode: string;
    description: string;
    data_provider: string;
    settings: Record<string, unknown> | null;
  }>,
): Promise<TaskStatus> {
  const row = await request<unknown>(`/tasks/${encodeURIComponent(taskId)}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
  return normalizeTaskStatus(row);
}


// ---- Task Triggers (child schedules under a Task) ----

export type CreateTaskTriggerPayload = {
  name: string;
  enabled?: boolean;
  schedule_kind: import("./types").TriggerScheduleKind;
  interval_seconds?: number | null;
  cron_expression?: string | null;
  timezone?: string;
  at_iso?: string | null;
  range_start?: string | null;
  range_end?: string | null;
  bar_interval?: string | null;
  trading_session?: string | null;
  delete_after_run?: boolean;
  execution_intent?: import("./types").ExecutionIntent;
  delivery_json?: import("./types").TriggerDelivery | null;
};

export type UpdateTaskTriggerPayload = Partial<CreateTaskTriggerPayload>;

/** List the Feishu groups each running bot belongs to (trigger channel picker). */
export async function listFeishuChats(): Promise<FeishuChatOption[]> {
  const resp = await request<FeishuChatListResponse>("/assistant/feishu/chats");
  return resp.items;
}

export async function listTaskTriggers(taskId: string): Promise<TaskTrigger[]> {
  const resp = await request<TaskTriggerListResponse>(
    `/tasks/${encodeURIComponent(taskId)}/triggers`,
  );
  return resp.triggers;
}

export async function getTaskTrigger(taskId: string, triggerId: string): Promise<TaskTrigger> {
  return request(
    `/tasks/${encodeURIComponent(taskId)}/triggers/${encodeURIComponent(triggerId)}`,
  );
}

export async function createTaskTrigger(
  taskId: string,
  payload: CreateTaskTriggerPayload,
): Promise<TaskTrigger> {
  return request(`/tasks/${encodeURIComponent(taskId)}/triggers`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function updateTaskTrigger(
  taskId: string,
  triggerId: string,
  payload: UpdateTaskTriggerPayload,
): Promise<TaskTrigger> {
  return request(
    `/tasks/${encodeURIComponent(taskId)}/triggers/${encodeURIComponent(triggerId)}`,
    {
      method: "PUT",
      body: JSON.stringify(payload),
    },
  );
}

export async function pauseTaskTrigger(taskId: string, triggerId: string): Promise<TaskTrigger> {
  return request(
    `/tasks/${encodeURIComponent(taskId)}/triggers/${encodeURIComponent(triggerId)}/pause`,
    { method: "POST" },
  );
}

export async function resumeTaskTrigger(taskId: string, triggerId: string): Promise<TaskTrigger> {
  return request(
    `/tasks/${encodeURIComponent(taskId)}/triggers/${encodeURIComponent(triggerId)}/resume`,
    { method: "POST" },
  );
}

export async function runTaskTrigger(
  taskId: string,
  triggerId: string,
): Promise<{ run_id: string | null }> {
  return request(
    `/tasks/${encodeURIComponent(taskId)}/triggers/${encodeURIComponent(triggerId)}/run`,
    { method: "POST" },
  );
}

export async function deleteTaskTrigger(taskId: string, triggerId: string): Promise<void> {
  return request(
    `/tasks/${encodeURIComponent(taskId)}/triggers/${encodeURIComponent(triggerId)}`,
    { method: "DELETE" },
  );
}

export async function listModelInvocations(params: {
  limit?: number;
  offset?: number;
  /** Exact match on stored trace_id */
  traceId?: string | null;
  /** Exact match on stored span_id */
  spanId?: string | null;
}): Promise<{ items: ModelInvocationRow[]; total: number }> {
  const suffix = buildQueryString({
    limit: params.limit,
    offset: params.offset,
    trace_id: params.traceId?.trim(),
    span_id: params.spanId?.trim(),
  });
  const body = await request<{ items: unknown[]; total: number }>(`/model-invocations${suffix}`);
  return {
    items: body.items.map((row) => normalizeModelInvocationRow(row)),
    total: body.total,
  };
}

export async function getModelInvocationBySpan(spanId: string): Promise<ModelInvocationRow | null> {
  const row = await request<unknown>(`/model-invocations/by-span/${encodeURIComponent(spanId)}`);
  return normalizeModelInvocationRow(row);
}

export async function uploadFile(file: File): Promise<import("./types").UploadResult> {
  const form = new FormData();
  form.append("file", file);
  const response = await fetch(`${API_BASE}/upload`, {
    method: "POST",
    body: form,
  });
  if (!response.ok) {
    const rawText = await response.text();
    const parsed = parseErrorResponse(rawText, response.status);
    const error = new ApiError(parsed.message, response.status, parsed);
    rememberApiError(error);
    throw error;
  }
  return response.json() as Promise<import("./types").UploadResult>;
}
