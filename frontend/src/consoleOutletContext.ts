import { useOutletContext } from "react-router-dom";
import type { Dispatch, SetStateAction } from "react";

import type { TaskStatus, PendingApproval, RuntimeStatus, SystemState } from "./types";

export type ConsoleOutletContext = {
  approvals: PendingApproval[];
  instances: TaskStatus[];
  health: string;
  runtimeStatus: RuntimeStatus | null;
  systemState: SystemState;
  loading: boolean;
  dataRefreshFailed: boolean;
  deploymentMode: string | null;
  refresh: (options?: { silent?: boolean }) => Promise<void>;
  setSystemState: Dispatch<SetStateAction<SystemState>>;
};

export function useConsoleOutlet(): ConsoleOutletContext {
  return useOutletContext<ConsoleOutletContext>();
}
