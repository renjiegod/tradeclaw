import { CloudDownloadOutlined, SyncOutlined } from "@ant-design/icons";
import { Alert, Button, Modal, Space, Tag, Typography, message } from "antd";
import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, applyUpdate, checkForUpdate, getUpdateStatus } from "../api";
import type { UpdateStatus } from "../types";

// ---------------------------------------------------------------------------
// 自动更新（release-based self-update）前端流程。
//
// - <UpdateBanner /> 挂在全局布局：后台开关开启时服务端会周期性检查 GitHub
//   Release，这里轮询 /update/status，一旦 update_available 就横幅提示；
//   「立即更新」由用户显式点击才进入更新流程（安装 + 自动重启）。
//   源码 checkout（install_kind=source）不展示「立即更新」，改为提示 git pull。
// - <UpdateSection /> 挂在设置页「自动更新」卡片：显示当前版本 / 最新版本 /
//   检查时间 / 错误，并提供「检查更新」「立即更新」按钮。
// ---------------------------------------------------------------------------

const BANNER_POLL_MS = 30 * 60_000;
const DISMISS_KEY = "update_banner_dismissed_tag";

/** 安全获取更新状态：更新服务未接线（503）或网络失败时返回 null，不打扰用户。 */
async function fetchStatusQuietly(): Promise<UpdateStatus | null> {
  try {
    return await getUpdateStatus();
  } catch {
    return null;
  }
}

function applyErrorText(e: unknown): string {
  if (e instanceof ApiError) {
    const detail = e.detail as { hint?: string | null } | null;
    return detail?.hint ? `${e.message}（${detail.hint}）` : e.message;
  }
  return e instanceof Error ? e.message : String(e);
}

/**
 * 用户确认后执行更新：POST /update/apply → 服务器优雅重启并重装新版本。
 * 之后轮询 /update/status 等服务器带着新版本回来，成功后整页刷新。
 */
export async function runUpdateFlow(status: UpdateStatus): Promise<void> {
  const target = status.latest;
  if (!target) return;
  const confirmed = await new Promise<boolean>((resolve) => {
    Modal.confirm({
      title: `更新到 ${target.tag}？`,
      content:
        `当前版本 v${status.current_version}。确认后服务器会安装 ${target.tag} ` +
        "并自动重启，期间页面会短暂不可用（通常 1-2 分钟）。",
      okText: "开始更新",
      cancelText: "取消",
      onOk: () => resolve(true),
      onCancel: () => resolve(false),
    });
  });
  if (!confirmed) return;

  const versionBefore = status.current_version;
  try {
    await applyUpdate();
  } catch (e) {
    message.error(`更新启动失败：${applyErrorText(e)}`);
    return;
  }

  const hide = message.loading("正在安装新版本并重启服务器…", 0);
  const deadline = Date.now() + 10 * 60_000;
  let wentDown = false;
  try {
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 3000));
      const st = await fetchStatusQuietly();
      if (st === null) {
        wentDown = true;
        continue;
      }
      // 回来了：要么版本号已变，要么先掉线后恢复且不再处于 restarting。
      if (st.current_version !== versionBefore || (wentDown && st.state !== "restarting")) {
        hide();
        message.success(`更新完成，当前版本 v${st.current_version}，页面即将刷新`);
        await new Promise((r) => setTimeout(r, 1200));
        window.location.reload();
        return;
      }
    }
    hide();
    message.warning("更新耗时较长，请稍后手动刷新页面确认版本。");
  } catch {
    hide();
    message.warning("更新过程中失去连接，请稍后手动刷新页面确认版本。");
  }
}

/** 全局「有新版本」提示横幅（挂在 App 布局里，所有页面可见）。 */
export function UpdateBanner() {
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [dismissedTag, setDismissedTag] = useState<string | null>(() =>
    localStorage.getItem(DISMISS_KEY),
  );
  const [applying, setApplying] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      const st = await fetchStatusQuietly();
      if (!cancelled && st) setStatus(st);
    };
    void poll();
    const timer = setInterval(() => void poll(), BANNER_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  if (!status?.update_available || !status.latest) return null;
  if (dismissedTag === status.latest.tag) return null;

  return (
    <Alert
      className="mb-4 rounded-2xl border border-shell-line"
      type="info"
      showIcon
      icon={<CloudDownloadOutlined />}
      closable
      onClose={() => {
        localStorage.setItem(DISMISS_KEY, status.latest!.tag);
        setDismissedTag(status.latest!.tag);
      }}
      message={
        <span>
          发现新版本 <Tag color="blue" className="font-mono">{status.latest.tag}</Tag>
          （当前 v{status.current_version}）
        </span>
      }
      description={
        <Space size={8} wrap>
          {status.latest.html_url ? (
            <Typography.Link href={status.latest.html_url} target="_blank">
              查看发布说明
            </Typography.Link>
          ) : null}
          {status.install_kind === "source" ? (
            <Typography.Text type="secondary" className="text-xs">
              源码运行，请 git pull 后重启
            </Typography.Text>
          ) : (
            <Button
              size="small"
              type="primary"
              loading={applying}
              onClick={() => {
                setApplying(true);
                void runUpdateFlow(status).finally(() => setApplying(false));
              }}
            >
              立即更新
            </Button>
          )}
        </Space>
      }
    />
  );
}

/** 设置页「自动更新」卡片里的版本状态 + 操作区。 */
export function UpdateSection() {
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [checking, setChecking] = useState(false);
  const [applying, setApplying] = useState(false);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const load = useCallback(async () => {
    const st = await fetchStatusQuietly();
    if (!mounted.current) return;
    setStatus(st);
    setUnavailable(st === null);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const onCheck = async () => {
    setChecking(true);
    try {
      const st = await checkForUpdate();
      if (!mounted.current) return;
      setStatus(st);
      if (st.last_error) {
        message.error(`检查失败（${st.last_error.error_code}）：${st.last_error.message}`);
      } else if (st.update_available && st.latest) {
        message.info(`发现新版本 ${st.latest.tag}`);
      } else {
        message.success("已是最新版本");
      }
    } catch (e) {
      message.error(`检查更新失败：${applyErrorText(e)}`);
    } finally {
      if (mounted.current) setChecking(false);
    }
  };

  if (unavailable) {
    return (
      <Typography.Text type="secondary" className="text-xs">
        更新服务在当前部署中不可用。
      </Typography.Text>
    );
  }
  if (!status) return null;

  return (
    <div className="flex flex-col gap-2" data-testid="update-section">
      <div className="text-sm">
        当前版本：<Typography.Text code>v{status.current_version}</Typography.Text>
        {status.install_kind === "source" ? (
          <Tag className="ml-2" color="default">
            源码运行（更新请 git pull）
          </Tag>
        ) : null}
      </div>
      <div className="text-sm">
        最新版本：
        {status.latest ? (
          <>
            <Typography.Text code>{status.latest.tag}</Typography.Text>
            {status.latest.html_url ? (
              <Typography.Link className="ml-2" href={status.latest.html_url} target="_blank">
                发布说明
              </Typography.Link>
            ) : null}
            {status.update_available ? (
              <Tag color="blue" className="ml-2">
                可更新
              </Tag>
            ) : (
              <Tag color="green" className="ml-2">
                已是最新
              </Tag>
            )}
          </>
        ) : (
          <Typography.Text type="secondary">尚未检查到发布版本</Typography.Text>
        )}
      </div>
      <div className="text-xs text-neutral-500">
        上次检查：{status.last_checked_at ? new Date(status.last_checked_at).toLocaleString() : "从未"}
      </div>
      {status.last_error ? (
        <Alert
          type="warning"
          showIcon
          className="rounded-xl"
          message={`上次检查失败（${status.last_error.error_code}）`}
          description={status.last_error.message}
        />
      ) : null}
      <Space>
        <Button icon={<SyncOutlined />} size="small" loading={checking} onClick={() => void onCheck()}>
          检查更新
        </Button>
        {status.update_available && status.latest ? (
          <Button
            type="primary"
            size="small"
            icon={<CloudDownloadOutlined />}
            loading={applying}
            disabled={status.install_kind === "source"}
            onClick={() => {
              setApplying(true);
              void runUpdateFlow(status).finally(() => {
                if (mounted.current) setApplying(false);
              });
            }}
          >
            立即更新到 {status.latest.tag}
          </Button>
        ) : null}
      </Space>
    </div>
  );
}
