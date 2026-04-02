export type InstanceStatus = {
  instance_id: string;
  name: string;
  mode: "paper" | "live" | "backtest" | string;
  status: "configured" | "running" | "paused" | "stopped" | "error" | string;
  cycles: number | null;
  last_error: string;
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
};
