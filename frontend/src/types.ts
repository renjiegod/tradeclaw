export type ConsolePageKey =
  | "dashboard"
  | "instances"
  | "create-agent"
  | "approvals"
  | "backtests"
  | "system";

export type SystemState = {
  kill_switch_enabled: boolean;
  instance_count: number;
  running_count: number;
};

export type InstanceStatus = {
  instance_id: string;
  name: string;
  template_id: string;
  mode: "paper" | "live" | "backtest" | string;
  orchestrator_mode: "single-agent" | "multi-role" | string;
  description: string;
  status: "configured" | "running" | "paused" | "stopped" | "error" | string;
  cycles: number | null;
  last_error: string;
  data_provider: string | null;
  data_provider_effective: string;
  watch_symbols: string[];
  execution_strategy: string;
  account_id: string;
  model_id: string;
  settings: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
};

export type PendingApproval = {
  approval_id: string;
  intent_id: string;
  created_at: string;
  expires_at: string;
};

export type AgentTemplate = {
  template_id: string;
  name: string;
  default_mode: string;
  default_orchestrator_mode: string;
};

export type CreateInstancePayload = {
  name: string;
  template_id: string;
  mode?: string;
  orchestrator_mode?: string;
  description?: string;
  data_provider?: string;
  watch_symbols?: string[];
  execution_strategy?: string;
  account_id?: string;
  model_id?: string;
  settings?: Record<string, unknown> | null;
};
