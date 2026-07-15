import type { TaskStatus } from "../types";

const STATUS_LABEL_MAP: Record<string, string> = {
  configured: "已配置",
  running: "运行中",
  paused: "已暂停",
  stopped: "已停止",
  error: "异常",
  completed: "已完成",
};

export function statusColor(status: string): string {
  if (status === "running") return "green";
  if (status === "paused") return "gold";
  if (status === "error") return "red";
  if (status === "stopped") return "default";
  if (status === "completed") return "blue";
  return "blue";
}

export function formatStatus(status: string): string {
  return STATUS_LABEL_MAP[status] ?? status;
}

export function resolveBacktestDisplayStatus(taskStatus: string, runStatus: string | null | undefined): string {
  if (runStatus === "completed") return "completed";
  if (runStatus === "failed") return "error";
  return taskStatus;
}

export function resolveTaskDisplayStatus(
  task: Pick<TaskStatus, "task_id" | "mode" | "status">,
  latestRunStatusByTaskId?: Record<string, string | undefined>,
): string {
  if (task.mode !== "backtest") {
    return task.status;
  }
  const runStatus = latestRunStatusByTaskId?.[task.task_id];
  return resolveBacktestDisplayStatus(task.status, runStatus);
}
