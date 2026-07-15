import { Alert, Button, Space, Spin, Typography } from "antd";
import { useCallback, useEffect, useState } from "react";

import { getDebugSession, getTaskRun } from "../api";
import { JsonCodeBlock } from "./JsonCodeBlock";

type BacktestRunConfigPanelProps = {
  taskId: string;
  selectedRunId: string | null;
};

type LoadState = "idle" | "loading" | "success" | "error";

export function BacktestRunConfigPanel({ taskId, selectedRunId }: BacktestRunConfigPanelProps) {
  const [loadState, setLoadState] = useState<LoadState>("idle");
  const [errorMessage, setErrorMessage] = useState<string>("");
  const [effectiveConfig, setEffectiveConfig] = useState<Record<string, unknown> | null>(null);
  const [debugDisabled, setDebugDisabled] = useState(false);
  const [retryKey, setRetryKey] = useState(0);

  const triggerRetry = useCallback(() => {
    setRetryKey((value) => value + 1);
  }, []);

  useEffect(() => {
    let cancelled = false;

    if (!selectedRunId) {
      setLoadState("idle");
      setErrorMessage("");
      setEffectiveConfig(null);
      return () => {
        cancelled = true;
      };
    }

    const load = async () => {
      setLoadState("loading");
      setErrorMessage("");
      setEffectiveConfig(null);
      setDebugDisabled(false);
      try {
        const run = await getTaskRun(taskId, selectedRunId);
        if (!run.session_id) {
          if (!cancelled) {
            // No debug session: either debug was turned off for this run (fast
            // mode) or the run predates session capture. Flag fast mode so the
            // empty trace reads as intentional rather than a fault.
            setDebugDisabled(run.debug_enabled === false);
            setLoadState("idle");
          }
          return;
        }
        const session = await getDebugSession(taskId, run.session_id);
        if (!cancelled) {
          const config = session.effective_config ?? null;
          setEffectiveConfig(config);
          setLoadState(config ? "success" : "idle");
        }
      } catch (error: unknown) {
        if (!cancelled) {
          const detail = error instanceof Error ? error.message : String(error);
          setErrorMessage(detail);
          setLoadState("error");
        }
      }
    };

    void load();
    return () => {
      cancelled = true;
    };
  }, [taskId, selectedRunId, retryKey]);

  if (!selectedRunId) {
    return <Typography.Text type="secondary">暂无运行配置，先发起一次回测</Typography.Text>;
  }

  if (loadState === "loading") {
    return (
      <div className="flex min-h-[160px] items-center justify-center">
        <Spin />
      </div>
    );
  }

  if (loadState === "error") {
    return (
      <Alert
        type="error"
        showIcon
        message="加载运行配置失败"
        description={
          <Space direction="vertical" size={8}>
            <Typography.Text type="secondary">{errorMessage || "请稍后重试"}</Typography.Text>
            <Button onClick={triggerRetry}>重试</Button>
          </Space>
        }
      />
    );
  }

  if (loadState === "idle") {
    if (debugDisabled) {
      return (
        <Alert
          type="info"
          showIcon
          message="本次回测以非调试（快速）模式运行"
          description="未记录调试会话 / span / cycle / 模型调用等 trace 明细，这是预期行为而非故障。运行状态、回测报告与成交仍可正常查看；如需完整 trace，请在创建任务时开启「调试模式」重跑。"
        />
      );
    }
    return <Typography.Text type="secondary">该运行未记录有效配置快照</Typography.Text>;
  }

  return (
    <Space direction="vertical" size={12} className="w-full">
      <Typography.Text strong>本次生效配置</Typography.Text>
      <JsonCodeBlock value={effectiveConfig} maxHeight={360} />
    </Space>
  );
}
