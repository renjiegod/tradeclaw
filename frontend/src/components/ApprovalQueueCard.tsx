import { Button, Card, Empty, List, Space, Tag, Typography } from "antd";
import { useNavigate } from "react-router-dom";

import { approve, reject } from "../api";
import { PANEL_CARD_CLASSNAME } from "../styles/classNames";
import type { PendingApproval } from "../types";
import { formatDateTimeUtc8, parseBackendDateTime } from "../utils/datetime";

type Props = {
  items: PendingApproval[];
  loading: boolean;
  onMutated: () => void;
};

function formatTs(raw: string | null | undefined): string {
  return formatDateTimeUtc8(raw, "—");
}

/** Format a decimal money string for display only. We deliberately avoid
 * parseFloat for any decision logic to preserve precision; this is purely a
 * cosmetic thousands-separator pass that falls back to the raw string when the
 * value is not a clean decimal. */
function formatNotional(raw: string | null | undefined): string | null {
  if (raw == null || raw === "") return null;
  const match = /^(-?)(\d+)(\.\d+)?$/.exec(raw.trim());
  if (!match) return raw;
  const [, sign, intPart, fracPart = ""] = match;
  const grouped = intPart.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${sign}${grouped}${fracPart}`;
}

const ACTION_META: Record<string, { label: string; color: string }> = {
  buy: { label: "买入", color: "green" },
  sell: { label: "卖出", color: "red" },
};

const STATUS_META: Record<string, { label: string; color: string }> = {
  pending: { label: "待处理", color: "processing" },
  approved: { label: "已同意", color: "success" },
  rejected: { label: "已拒绝", color: "error" },
  expired: { label: "已过期", color: "default" },
};

/** Human-readable countdown / expiry hint based on expires_at. Returns null
 * when there is no expiry to show. */
function expiryHint(expiresAt: string | null | undefined): { text: string; expired: boolean } | null {
  if (expiresAt == null || expiresAt === "") return null;
  // Backend timestamps are naive UTC (no Z/offset). Date.parse() would read
  // them as LOCAL time, so a not-yet-expired pending shows "已过期" in UTC+8
  // zones (the displayed expiry uses parseBackendDateTime → UTC, the countdown
  // must use the SAME parse or the two disagree).
  const expiryMs = parseBackendDateTime(expiresAt).getTime();
  if (Number.isNaN(expiryMs)) return null;
  const remainingMs = expiryMs - Date.now();
  if (remainingMs <= 0) {
    return { text: "已过期", expired: true };
  }
  const totalSeconds = Math.floor(remainingMs / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  const remainingLabel = minutes > 0 ? `${minutes} 分 ${seconds} 秒` : `${seconds} 秒`;
  return { text: `剩余 ${remainingLabel}`, expired: false };
}

function ApprovalListItem({
  item,
  onMutated,
}: {
  item: PendingApproval;
  onMutated: () => void;
}) {
  const navigate = useNavigate();
  const action = (item.action ?? "").toLowerCase();
  const actionMeta = ACTION_META[action];
  const status = (item.status ?? "").toLowerCase();
  const statusMeta = STATUS_META[status];
  const notional = formatNotional(item.notional);
  const expiry = expiryHint(item.expires_at);
  const decidable = status === "" || status === "pending";

  const actions = [];
  if (decidable) {
    actions.push(
      <Button
        key="approve"
        className="rounded-xl"
        type="primary"
        size="small"
        onClick={async () => {
          await approve(item.approval_id);
          onMutated();
        }}
      >
        同意
      </Button>,
      <Button
        key="reject"
        className="rounded-xl"
        danger
        size="small"
        onClick={async () => {
          await reject(item.approval_id);
          onMutated();
        }}
      >
        拒绝
      </Button>,
    );
  }
  if (item.task_id) {
    const taskId = item.task_id;
    actions.push(
      <Button
        key="view-task"
        className="rounded-xl"
        size="small"
        onClick={() => navigate(`/tasks/${encodeURIComponent(taskId)}`)}
      >
        查看任务
      </Button>,
    );
  }

  return (
    <List.Item key={item.approval_id} actions={actions}>
      <Space direction="vertical" size={4} className="w-full">
        <Space size={8} wrap align="center">
          {item.symbol ? (
            <Typography.Text strong>
              {item.symbol_name ? `${item.symbol_name} ` : ""}
              <span className="font-mono">{item.symbol}</span>
            </Typography.Text>
          ) : null}
          {actionMeta ? <Tag color={actionMeta.color}>{actionMeta.label}</Tag> : null}
          {notional ? (
            <Typography.Text>
              名义金额: <span className="font-mono">{notional}</span>
            </Typography.Text>
          ) : null}
          {statusMeta ? <Tag color={statusMeta.color}>{statusMeta.label}</Tag> : null}
          {item.mode ? <Tag color="gold">{item.mode}</Tag> : null}
        </Space>
        {item.rationale ||
        item.signal_tag ||
        item.strategy_tag ||
        item.last_price ||
        item.price_reference ||
        item.order_type ||
        item.direction ? (
          <div className="rounded-lg bg-amber-50/60 px-3 py-2">
            <Typography.Text strong className="text-xs text-amber-700">
              信号
            </Typography.Text>
            <Space size={8} wrap className="ml-2">
              {item.last_price ? (
                <Typography.Text className="text-xs" type="secondary">
                  现价:{" "}
                  <span className="font-mono">
                    {`${item.last_price}${item.pct_change ? ` (${item.pct_change})` : ""}`}
                  </span>
                </Typography.Text>
              ) : null}
              {item.price_reference ? (
                <Typography.Text className="text-xs" type="secondary">
                  限价: <span className="font-mono">{item.price_reference}</span>
                </Typography.Text>
              ) : null}
              {item.order_type || item.tif ? (
                <Typography.Text className="text-xs" type="secondary">
                  订单: <span className="font-mono">{[item.order_type, item.tif].filter(Boolean).join(" · ")}</span>
                </Typography.Text>
              ) : null}
              {item.direction || item.signal_tag ? (
                <Typography.Text className="text-xs" type="secondary">
                  方向: <span className="font-mono">{item.direction || (item.action ?? "")}</span>
                  {item.signal_tag ? <span className="font-mono"> [{item.signal_tag}]</span> : null}
                </Typography.Text>
              ) : null}
              {item.strategy_tag ? (
                <Typography.Text className="text-xs" type="secondary">
                  策略: <span className="font-mono">{item.strategy_tag}</span>
                </Typography.Text>
              ) : null}
              {item.exit_reason ? (
                <Typography.Text className="text-xs" type="secondary">
                  平仓原因: <span className="font-mono">{item.exit_reason}</span>
                </Typography.Text>
              ) : null}
            </Space>
            {item.rationale ? (
              <div className="mt-1 text-xs text-gray-700">理由: {item.rationale}</div>
            ) : null}
          </div>
        ) : null}
        <Typography.Text strong>意图: {item.intent_id}</Typography.Text>
        <Typography.Text className="text-xs" type="secondary">
          请求ID: {item.approval_id}
        </Typography.Text>
        <Typography.Text className="text-xs" type="secondary">
          创建时间: {formatTs(item.created_at)} | 过期时间: {formatTs(item.expires_at)}
          {expiry ? (
            <span className={expiry.expired ? "ml-2 text-red-500" : "ml-2 text-amber-600"}>
              （{expiry.text}）
            </span>
          ) : null}
        </Typography.Text>
        {item.resolver_id || item.decision_source ? (
          <Typography.Text className="text-xs" type="secondary">
            {item.decision_source ? `来源: ${item.decision_source}` : null}
            {item.resolver_id ? `${item.decision_source ? " | " : ""}处理人: ${item.resolver_id}` : null}
          </Typography.Text>
        ) : null}
      </Space>
    </List.Item>
  );
}

export function ApprovalQueueCard({ items, loading, onMutated }: Props) {
  return (
    <Card className={PANEL_CARD_CLASSNAME} title="待处理审批" loading={loading}>
      {items.length === 0 ? (
        <Empty description="暂无待审批请求" />
      ) : (
        <List
          itemLayout="vertical"
          dataSource={items}
          renderItem={(item) => (
            <ApprovalListItem key={item.approval_id} item={item} onMutated={onMutated} />
          )}
        />
      )}
    </Card>
  );
}
