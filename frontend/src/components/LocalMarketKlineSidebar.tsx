import { Alert, Button, Card, Select, Space, Tag, Typography } from "antd";

import type {
  LocalMarketBarsSnapshot,
  LocalMarketOverlayCandidate,
  LocalMarketSyncJob,
} from "../types";

type OverlayKind = "backtest_trades" | "task_fills" | "signals";

type LocalMarketKlineSidebarProps = {
  snapshot: LocalMarketBarsSnapshot | null;
  syncJob: LocalMarketSyncJob | null;
  syncMessage: string;
  syncingMode: string | null;
  selectedOverlays: Partial<Record<OverlayKind, string>>;
  onOverlayChange: (kind: OverlayKind, value?: string) => void;
  onFillGap: () => void;
  onForceRefresh: () => void;
};

function pctText(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(2)}%`;
}

function numText(value: number | null | undefined, fractionDigits = 2): string {
  if (value == null || Number.isNaN(value)) return "—";
  return value.toLocaleString("zh-CN", { maximumFractionDigits: fractionDigits });
}

function overlayOptions(items: LocalMarketOverlayCandidate[]) {
  return items.map((item) => ({ value: item.id, label: item.label }));
}

function formatDateTime(value: string | null | undefined): string {
  if (!value) return "—";
  const text = value.replace("T", " ");
  return text.endsWith("Z") ? text.slice(0, -1) : text;
}

export function LocalMarketKlineSidebar({
  snapshot,
  syncJob,
  syncMessage,
  syncingMode,
  selectedOverlays,
  onOverlayChange,
  onFillGap,
  onForceRefresh,
}: LocalMarketKlineSidebarProps) {
  const summary = snapshot?.summary;
  const overlayCandidates = snapshot?.available_overlays;
  const coverage = snapshot?.coverage;
  const syncState = snapshot?.sync_state;

  return (
    <div className="flex flex-col gap-3">
      <Card size="small" title="区间摘要">
        <div className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
          <div>最新价</div>
          <div className="text-right">{numText(summary?.latest_close)}</div>
          <div>区间涨跌</div>
          <div className="text-right">{numText(summary?.window_change)} / {pctText(summary?.window_change_pct)}</div>
          <div>振幅</div>
          <div className="text-right">{pctText(summary?.amplitude_pct)}</div>
          <div>最高 / 最低</div>
          <div className="text-right">{numText(summary?.window_high)} / {numText(summary?.window_low)}</div>
          <div>成交量</div>
          <div className="text-right">{numText(summary?.total_volume, 0)}</div>
          <div>成交额</div>
          <div className="text-right">{numText(summary?.total_amount)}</div>
        </div>
      </Card>

      <Card size="small" title="本地覆盖">
        <Space direction="vertical" size={8} className="w-full">
          {syncState?.covered_start && syncState?.covered_end ? (
            <div className="flex items-center gap-2 text-sm">
              <Tag color="blue">数据库总范围</Tag>
              <span>{syncState.covered_start.slice(0, 10)} ~ {syncState.covered_end.slice(0, 10)}</span>
            </div>
          ) : null}
          <Typography.Text type="secondary">
            当前视图: {coverage ? `${coverage.requested_start} ~ ${coverage.requested_end}` : "—"}
          </Typography.Text>
          <div className="flex flex-wrap gap-2">
            {(coverage?.covered_segments ?? []).map((segment, index) => (
              <Tag key={`covered-${index}`} color="green">
                覆盖 {segment.start} ~ {segment.end}
              </Tag>
            ))}
            {(coverage?.missing_segments ?? []).map((segment, index) => (
              <Tag key={`missing-${index}`} color="orange">
                缺口 {segment.start} ~ {segment.end}
              </Tag>
            ))}
            {!coverage?.covered_segments.length && !coverage?.missing_segments.length ? <Tag>暂无覆盖片段</Tag> : null}
          </div>
          {syncState ? (
            <Space direction="vertical" size={6} className="w-full">
              <Typography.Text type="secondary">
                同步状态: {syncState.status} · {syncState.provider}/{syncState.adjust}
              </Typography.Text>
              {syncState.status === "failed" ? (
                <Alert
                  type="error"
                  showIcon
                  message={`自动同步失败 · ${syncState.last_error_code ?? "unknown_error"}`}
                  description={
                    <div className="space-y-1 text-sm">
                      <div>{syncState.last_error_message ?? "未提供错误信息"}</div>
                      <div className="text-xs text-slate-500">
                        最近尝试: {formatDateTime(syncState.last_attempt_at)} · 重试 {syncState.retry_count} 次
                      </div>
                    </div>
                  }
                />
              ) : null}
            </Space>
          ) : null}
        </Space>
      </Card>

      <Card size="small" title="数据操作">
        <Space direction="vertical" size={8} className="w-full">
          <div className="flex gap-2">
            <Button type="primary" loading={syncingMode === "fill_gap"} onClick={onFillGap}>
              补缺口
            </Button>
            <Button danger loading={syncingMode === "force_refresh"} onClick={onForceRefresh}>
              强制重刷
            </Button>
          </div>
          {syncMessage ? <Alert type="info" showIcon message={syncMessage} /> : null}
          {syncJob ? (
            <Alert
              type={syncJob.status === "failed" ? "error" : "info"}
              showIcon
              message={`同步任务 ${syncJob.status}`}
              description={
                syncJob.status === "failed"
                  ? `${syncJob.error_message ?? "同步失败"}${syncJob.hint ? ` · ${syncJob.hint}` : ""}`
                  : `已写入 ${syncJob.upserted_count} 条，范围 ${syncJob.requested_range.start} ~ ${syncJob.requested_range.end}`
              }
            />
          ) : null}
        </Space>
      </Card>

      <Card size="small" title="叠加来源">
        <Space direction="vertical" size={10} className="w-full">
          <div>
            <Typography.Text type="secondary">回测买卖点</Typography.Text>
            <Select
              allowClear
              className="mt-1 w-full"
              placeholder="选择回测 run"
              value={selectedOverlays.backtest_trades}
              options={overlayOptions(overlayCandidates?.backtest_trades ?? [])}
              onChange={(value) => onOverlayChange("backtest_trades", value)}
            />
          </div>
          <div>
            <Typography.Text type="secondary">任务成交</Typography.Text>
            <Select
              allowClear
              className="mt-1 w-full"
              placeholder="选择任务"
              value={selectedOverlays.task_fills}
              options={overlayOptions(overlayCandidates?.task_fills ?? [])}
              onChange={(value) => onOverlayChange("task_fills", value)}
            />
          </div>
          <div>
            <Typography.Text type="secondary">信号点位</Typography.Text>
            <Select
              allowClear
              className="mt-1 w-full"
              placeholder="选择信号源"
              value={selectedOverlays.signals}
              options={overlayOptions(overlayCandidates?.signals ?? [])}
              onChange={(value) => onOverlayChange("signals", value)}
            />
          </div>
        </Space>
      </Card>
    </div>
  );
}
