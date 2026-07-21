import { Button, Descriptions, Modal, Space, Typography, message } from "antd";

import { ApiError, findRecentApiError, isAuthRedirectInFlight } from "../api";
import { formatDateTimeUtc8 } from "./datetime";

type ShowErrorDialogOptions = {
  title?: string;
  fallbackMessage?: string;
};

type ErrorDialogState = {
  title: string;
  message: string;
  detailText: string;
  traceId: string | null;
  timestamp: string;
  status: number | null;
  errorCode: string | null;
  errorType: string | null;
  hint: string | null;
};

function stringifyDetail(detail: unknown): string {
  if (detail == null) return "";
  if (typeof detail === "string") return detail;
  try {
    return JSON.stringify(detail, null, 2);
  } catch {
    return String(detail);
  }
}

function buildErrorState(error: unknown, options?: ShowErrorDialogOptions): ErrorDialogState {
  const fallbackMessage = options?.fallbackMessage?.trim() || "操作失败";
  const timestamp = new Date().toISOString();
  if (error instanceof ApiError) {
    const detailText = stringifyDetail(error.detail) || error.message || fallbackMessage;
    return {
      title: options?.title?.trim() || "请求失败",
      message: error.message || fallbackMessage,
      detailText,
      traceId: error.traceId,
      timestamp: error.timestamp || timestamp,
      status: error.status,
      errorCode: error.errorCode,
      errorType: error.errorType,
      hint: error.hint,
    };
  }
  if (error instanceof Error) {
    return {
      title: options?.title?.trim() || "操作失败",
      message: error.message || fallbackMessage,
      detailText: error.stack || error.message || fallbackMessage,
      traceId: null,
      timestamp,
      status: null,
      errorCode: null,
      errorType: error.name || "Error",
      hint: null,
    };
  }
  const text = typeof error === "string" && error.trim() ? error : fallbackMessage;
  const recentApiError = typeof text === "string" ? findRecentApiError(text) : null;
  if (recentApiError != null) {
    const detailText = stringifyDetail(recentApiError.detail) || recentApiError.message || text;
    return {
      title: options?.title?.trim() || "请求失败",
      message: text,
      detailText,
      traceId: recentApiError.traceId,
      timestamp: recentApiError.timestamp || timestamp,
      status: recentApiError.status,
      errorCode: recentApiError.errorCode,
      errorType: recentApiError.errorType,
      hint: recentApiError.hint,
    };
  }
  return {
    title: options?.title?.trim() || "操作失败",
    message: text,
    detailText: text,
    traceId: null,
    timestamp,
    status: null,
    errorCode: null,
    errorType: null,
    hint: null,
  };
}

function buildCopyText(state: ErrorDialogState): string {
  const lines = [
    `title: ${state.title}`,
    `message: ${state.message}`,
    `time: ${state.timestamp}`,
    `trace_id: ${state.traceId ?? "-"}`,
    `status_code: ${state.status ?? "-"}`,
    `error_code: ${state.errorCode ?? "-"}`,
    `error_type: ${state.errorType ?? "-"}`,
    `hint: ${state.hint ?? "-"}`,
    "",
    "detail:",
    state.detailText,
  ];
  return lines.join("\n");
}

async function copyErrorState(state: ErrorDialogState): Promise<void> {
  try {
    await navigator.clipboard.writeText(buildCopyText(state));
    message.success("已复制完整错误信息");
  } catch {
    message.warning("复制失败，请手动选择弹窗中的错误信息。");
  }
}

export function showErrorDialog(error: unknown, options?: ShowErrorDialogOptions): void {
  // 会话失效正在跳回登录入口时,后台在途请求会连环抛 401;此刻弹「请求失败」只会
  // 在导航离开前一闪吓到用户,直接抑制。
  if (isAuthRedirectInFlight()) return;
  const state = buildErrorState(error, options);
  const displayTime = formatDateTimeUtc8(state.timestamp, state.timestamp);
  Modal.error({
    title: state.title,
    width: 760,
    okText: "关闭",
    content: (
      <Space direction="vertical" size={12} className="w-full">
        <Typography.Text type="danger">{state.message}</Typography.Text>
        <Descriptions bordered size="small" column={1}>
          <Descriptions.Item label="trace_id">
            <Typography.Text className="font-mono text-xs">{state.traceId ?? "—"}</Typography.Text>
          </Descriptions.Item>
          <Descriptions.Item label="时间">
            <Typography.Text className="font-mono text-xs">{displayTime}</Typography.Text>
          </Descriptions.Item>
          <Descriptions.Item label="状态码">
            <Typography.Text className="font-mono text-xs">{state.status ?? "—"}</Typography.Text>
          </Descriptions.Item>
          <Descriptions.Item label="error_code">
            <Typography.Text className="font-mono text-xs">{state.errorCode ?? "—"}</Typography.Text>
          </Descriptions.Item>
          <Descriptions.Item label="error_type">
            <Typography.Text className="font-mono text-xs">{state.errorType ?? "—"}</Typography.Text>
          </Descriptions.Item>
          <Descriptions.Item label="hint">
            <Typography.Text>{state.hint ?? "—"}</Typography.Text>
          </Descriptions.Item>
        </Descriptions>
        <div className="flex items-center justify-between gap-3">
          <Typography.Text strong>错误详细信息</Typography.Text>
          <Button size="small" onClick={() => void copyErrorState(state)}>
            复制全部错误信息
          </Button>
        </div>
        <pre className="max-h-[360px] overflow-auto rounded-md border border-slate-200 bg-slate-950/95 p-3 text-xs leading-6 text-slate-100">
          {state.detailText}
        </pre>
      </Space>
    ),
  });
}

let errorMessageBridgeInstalled = false;

export function installGlobalErrorDialogBridge(): void {
  if (errorMessageBridgeInstalled) return;
  errorMessageBridgeInstalled = true;
  const originalError = message.error.bind(message);
  message.error = ((content: Parameters<typeof message.error>[0], duration?: number, onClose?: VoidFunction) => {
    if (typeof content === "string" || content instanceof Error) {
      showErrorDialog(content);
      onClose?.();
      return Promise.resolve() as ReturnType<typeof message.error>;
    }
    if (
      content != null &&
      typeof content === "object" &&
      "content" in content &&
      typeof (content as { content?: unknown }).content === "string"
    ) {
      showErrorDialog((content as { content: string }).content, {
        title:
          "key" in content && typeof (content as { key?: unknown }).key === "string"
            ? String((content as { key: string }).key)
            : undefined,
      });
      onClose?.();
      return Promise.resolve() as ReturnType<typeof message.error>;
    }
    return originalError(content as Parameters<typeof message.error>[0], duration, onClose);
  }) as typeof message.error;
}
