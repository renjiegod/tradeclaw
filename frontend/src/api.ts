import type {
  AgentTemplate,
  CreateInstancePayload,
  InstanceStatus,
  PendingApproval,
  SystemState,
} from "./types";

const API_BASE = (import.meta.env.VITE_API_BASE as string | undefined)?.replace(/\/$/, "") ?? "http://localhost:8000";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
    ...init,
  });

  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `HTTP ${response.status}`);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}

export async function getHealth(): Promise<{ status: string }> {
  return request("/health");
}

export async function listInstances(): Promise<InstanceStatus[]> {
  return request("/instances");
}

export async function createInstance(payload: CreateInstancePayload): Promise<InstanceStatus> {
  return request("/instances", { method: "POST", body: JSON.stringify(payload) });
}

export async function startInstance(instanceId: string): Promise<InstanceStatus> {
  return request(`/instances/${instanceId}/start`, { method: "POST" });
}

export async function pauseInstance(instanceId: string): Promise<InstanceStatus> {
  return request(`/instances/${instanceId}/pause`, { method: "POST" });
}

export async function stopInstance(instanceId: string): Promise<InstanceStatus> {
  return request(`/instances/${instanceId}/stop`, { method: "POST" });
}

export async function listPendingApprovals(): Promise<PendingApproval[]> {
  return request("/approvals/pending");
}

export async function approve(approvalId: string): Promise<{ status: string }> {
  return request(`/approvals/${approvalId}/approve`, { method: "POST" });
}

export async function reject(approvalId: string): Promise<{ status: string }> {
  return request(`/approvals/${approvalId}/reject`, { method: "POST" });
}

export async function listTemplates(): Promise<AgentTemplate[]> {
  return request("/templates");
}

export async function getSystemState(): Promise<SystemState> {
  return request("/system/state");
}

export async function setKillSwitch(enabled: boolean): Promise<SystemState> {
  return request("/system/kill-switch", {
    method: "POST",
    body: JSON.stringify({ enabled }),
  });
}

export async function tickOnce(): Promise<{ executed: number; expired_count: number }> {
  return request("/system/tick", { method: "POST" });
}
