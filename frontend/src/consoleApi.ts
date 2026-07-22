import { beginAuthRedirect } from "./api";

/**
 * dytc console API 客户端（cloud 部署专用）。
 *
 * 与 `api.ts` 分离：后者的 `API_BASE` 指向 doyoutrade 后端（本地或每用户
 * copilot 进程），而这里固定打同域的 `/api/console/v1/*` —— copilot 网关将其
 * 直通到 dytc 数据网关（OAuth 会话 + keys / usage / me）。响应契约也不同：
 * console API 的错误体是 `{error_code, message}` 而非 doyoutrade 的
 * `{detail, trace_id, ...}`，故不复用 `api.ts` 的 request/错误装配，只复用
 * `beginAuthRedirect`（401 会话失效时回登录入口的统一入口，见 api.ts:206）。
 *
 * 类型从 doyoutrade-cloud/console/src/api.ts 移植（仅用户面，不含 admin），
 * 加 `Console` 前缀避免与本仓 `types.ts` / `api.ts` 中的同名类型混淆。
 */

const CONSOLE_API_BASE = "/api/console/v1";

// ---------- 类型（用户面） ----------

export interface ConsolePlan {
  plan_name: string;
  rate_per_minute: number;
  daily_requests: number;
  scopes: string[];
  max_ws_connections: number;
}

export interface ConsoleQuota {
  daily_requests: number;
  used_today: number;
  remaining_today: number;
}

export interface ConsoleMe {
  user: {
    id: string;
    github_login: string | null;
    name: string;
    avatar_url: string | null;
    is_admin: boolean;
    status: "active" | "suspended";
    plan: ConsolePlan;
  };
  quota: ConsoleQuota;
}

export interface ConsoleApiKey {
  id: string;
  key_prefix: string;
  name: string | null;
  status: "active" | "revoked";
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
}

export interface CreateConsoleKeyResult {
  key: ConsoleApiKey;
  full_key: string;
}

export interface ConsoleUsageBucket {
  requests: number;
  cache_hits: number;
  errors: number;
}

export interface ConsoleUsagePeriod extends ConsoleUsageBucket {
  by_operation: Record<string, ConsoleUsageBucket>;
}

export interface ConsoleUsage {
  date: string;
  today: ConsoleUsagePeriod;
  month: ConsoleUsagePeriod;
  quota: ConsoleQuota;
}

// ---------- 错误 ----------

export class ConsoleApiError extends Error {
  readonly status: number;
  readonly errorCode: string;

  constructor(status: number, errorCode: string, message: string) {
    super(message);
    this.name = "ConsoleApiError";
    this.status = status;
    this.errorCode = errorCode;
  }
}

// ---------- 请求封装 ----------

async function consoleRequest<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${CONSOLE_API_BASE}${path}`, {
      credentials: "include",
      ...init,
    });
  } catch {
    throw new ConsoleApiError(0, "network_error", "网络错误，请检查连接后重试");
  }

  if (res.status === 204) {
    return undefined as T;
  }

  // 非 JSON 响应（网关 502 HTML、空 body 等）归一成结构化错误，不让
  // res.json() 的 SyntaxError 裸奔到调用方。
  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }

  if (!res.ok) {
    const b = body as { error_code?: string; message?: string } | null;
    const errorCode = b?.error_code ?? "unknown_error";
    if (res.status === 401 && errorCode === "not_authenticated") {
      // 会话失效（cookie 过期 / 退出登录竞态）：回登录入口并抑制错误弹窗，
      // 与 api.ts 的 401 处理共用同一条 beginAuthRedirect 链路。
      beginAuthRedirect();
    }
    throw new ConsoleApiError(
      res.status,
      errorCode,
      b?.message ?? `请求失败（HTTP ${res.status}）`,
    );
  }

  return body as T;
}

/** 写操作固定携带 CSRF 头（dytc 网关校验 `X-Requested-With: console`）。 */
function writeHeaders(json = false): HeadersInit {
  const h: Record<string, string> = { "X-Requested-With": "console" };
  if (json) h["Content-Type"] = "application/json";
  return h;
}

// ---------- API ----------

export function fetchConsoleMe(): Promise<ConsoleMe> {
  return consoleRequest<ConsoleMe>("/me");
}

export function listConsoleKeys(): Promise<{ keys: ConsoleApiKey[] }> {
  return consoleRequest<{ keys: ConsoleApiKey[] }>("/keys");
}

export function createConsoleKey(name: string | null): Promise<CreateConsoleKeyResult> {
  return consoleRequest<CreateConsoleKeyResult>("/keys", {
    method: "POST",
    headers: writeHeaders(true),
    body: JSON.stringify({ name }),
  });
}

export function revokeConsoleKey(id: string): Promise<void> {
  return consoleRequest<void>(`/keys/${encodeURIComponent(id)}`, {
    method: "DELETE",
    headers: writeHeaders(),
  });
}

export function fetchConsoleUsage(): Promise<ConsoleUsage> {
  return consoleRequest<ConsoleUsage>("/usage");
}
