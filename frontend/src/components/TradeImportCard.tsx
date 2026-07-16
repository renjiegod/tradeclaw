import { ImportOutlined, UploadOutlined } from "@ant-design/icons";
import {
  Alert,
  AutoComplete,
  Button,
  Card,
  Popconfirm,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useMemo, useRef, useState } from "react";

import { ApiError, commitTradesCsv, listPortfolioImportBrokers, parseTradesCsv } from "../api";
import type {
  PortfolioImportBrokerItem,
  PortfolioImportCommitResponse,
  PortfolioImportParseRecord,
  PortfolioImportParseResponse,
  TradeAttributionSummary,
  TradeAttributionUnparsed,
} from "../types";
import { formatAmount } from "./StockDetailModal";

/** Fallback for any missing / non-finite value. Never fabricate a number. */
const DASH = "—";

/**
 * Extract a display message + optional repair hint from a thrown error.
 * {@link ApiError} carries the backend's structured ``hint``; anything else
 * falls back to its plain message.
 */
function describeError(error: unknown): { text: string; hint: string | null } {
  if (error instanceof ApiError) {
    return { text: error.message, hint: error.hint };
  }
  return { text: error instanceof Error ? error.message : String(error), hint: null };
}

/** Render a backend decimal money string via 亿/万 formatting, or ``—``. */
function formatMoneyString(value: string | null | undefined): string {
  const raw = value?.trim();
  if (!raw) return DASH;
  const n = Number(raw);
  return Number.isFinite(n) ? formatAmount(n) : DASH;
}

/** Format a ``0..1`` win-rate ratio as a percentage, or ``—`` when null. */
function formatWinRate(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return DASH;
  return `${(value * 100).toFixed(1)}%`;
}

/** Format a plain number (profit factor), or ``—`` when null. */
function formatNumber(value: number | null | undefined, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return DASH;
  return value.toFixed(digits);
}

/**
 * 券商交割单 CSV 导入 card for the Knowledge review workbench. Flow: pick a
 * broker (from ``GET /portfolio/imports/brokers``, free input allowed) → pick a
 * ``.csv`` file → 解析预览 (parse-only, shows the fills table with duplicate
 * marks) → 预演导入 (``dry_run=true``, nothing written) → 正式导入 (writes into
 * ``trades/<broker>/<month>.csv`` and shows the post-import attribution
 * review). A real import without a prior preview / dry-run asks for
 * confirmation first. On successful real import ``onImported`` fires so the
 * parent can refresh the 交割单归因 board.
 */
export function TradeImportCard({ onImported }: { onImported?: () => void }) {
  const [brokers, setBrokers] = useState<PortfolioImportBrokerItem[]>([]);
  const [broker, setBroker] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<PortfolioImportParseResponse | null>(null);
  const [result, setResult] = useState<PortfolioImportCommitResponse | null>(null);
  const [error, setError] = useState<{ text: string; hint: string | null } | null>(null);
  const [parsing, setParsing] = useState(false);
  const [committing, setCommitting] = useState<"dry_run" | "commit" | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    let cancelled = false;
    listPortfolioImportBrokers()
      .then((resp) => {
        if (!cancelled) setBrokers(resp.items);
      })
      .catch((err: unknown) => {
        // Broker presets are a convenience — free input still works, so warn
        // instead of blocking the card.
        const { text } = describeError(err);
        message.warning(`加载券商列表失败（仍可手动输入券商标识）：${text}`);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const brokerOptions = useMemo(
    () =>
      brokers.map((b) => ({
        value: b.broker,
        label: (
          <span className="flex items-center gap-2">
            <span>{b.display_name}</span>
            <span className="text-xs text-shell-muted">{b.broker}</span>
            {b.existing ? <Tag color="blue">已有数据</Tag> : null}
          </span>
        ),
      })),
    [brokers],
  );

  /** Any new file / broker invalidates the previous preview & result. */
  const resetOutputs = () => {
    setPreview(null);
    setResult(null);
    setError(null);
  };

  const ready = Boolean(file && broker.trim());
  // A real import without any prior parse preview or dry-run gets a Popconfirm.
  const verified = preview != null || (result != null && result.dry_run);

  const handleParse = async () => {
    if (!file || !broker.trim()) return;
    setParsing(true);
    setError(null);
    setResult(null);
    try {
      setPreview(await parseTradesCsv(file, broker.trim()));
    } catch (err: unknown) {
      setPreview(null);
      setError(describeError(err));
    } finally {
      setParsing(false);
    }
  };

  const handleCommit = async (dryRun: boolean) => {
    if (!file || !broker.trim()) return;
    setCommitting(dryRun ? "dry_run" : "commit");
    setError(null);
    try {
      const resp = await commitTradesCsv(file, broker.trim(), dryRun);
      setResult(resp);
      if (!dryRun) {
        message.success(`导入完成：新增 ${resp.appended_total} 条成交记录`);
        onImported?.();
      }
    } catch (err: unknown) {
      setError(describeError(err));
    } finally {
      setCommitting(null);
    }
  };

  return (
    <Card
      className="!border !border-shell-line !bg-card-bg shadow-shell-card"
      title={
        <div className="flex flex-col">
          <Typography.Text strong>交割单导入</Typography.Text>
          <Typography.Text type="secondary" className="!text-xs !font-normal">
            上传券商导出的交割单 CSV，预览后导入 knowledge 的 trades/ 分区
          </Typography.Text>
        </div>
      }
      data-testid="trade-import-card"
    >
      <div className="flex flex-col gap-4">
        <Space wrap size={12} align="center">
          <AutoComplete
            value={broker}
            options={brokerOptions}
            onChange={(value) => {
              setBroker(value);
              resetOutputs();
            }}
            placeholder="选择或输入券商标识（如 huatai）"
            style={{ minWidth: 240 }}
            data-testid="trade-import-broker"
          />
          <input
            ref={fileInputRef}
            type="file"
            accept=".csv"
            className="hidden"
            data-testid="trade-import-file-input"
            onChange={(e) => {
              setFile(e.target.files?.[0] ?? null);
              resetOutputs();
              // Allow re-selecting the same file after a failed attempt.
              e.target.value = "";
            }}
          />
          <Button icon={<UploadOutlined />} onClick={() => fileInputRef.current?.click()}>
            选择 CSV 文件
          </Button>
          {file ? (
            <Typography.Text className="!text-xs" data-testid="trade-import-file-name">
              {file.name}
            </Typography.Text>
          ) : null}
          <Button
            type="default"
            loading={parsing}
            disabled={!ready}
            onClick={() => void handleParse()}
            data-testid="trade-import-parse"
          >
            解析预览
          </Button>
          <Button
            loading={committing === "dry_run"}
            disabled={!ready || committing != null}
            onClick={() => void handleCommit(true)}
            data-testid="trade-import-dry-run"
          >
            预演导入
          </Button>
          <Popconfirm
            title="尚未解析预览或预演导入，确定直接正式导入吗？"
            okText="确定导入"
            cancelText="取消"
            disabled={!ready || verified}
            onConfirm={() => void handleCommit(false)}
          >
            <Button
              type="primary"
              icon={<ImportOutlined />}
              loading={committing === "commit"}
              disabled={!ready || committing != null}
              onClick={verified ? () => void handleCommit(false) : undefined}
              data-testid="trade-import-commit"
            >
              正式导入
            </Button>
          </Popconfirm>
        </Space>

        {error ? (
          <Alert
            type="error"
            showIcon
            data-testid="trade-import-error"
            message={error.text}
            description={error.hint ? `提示：${error.hint}` : undefined}
          />
        ) : null}

        {preview ? <PreviewBlock preview={preview} /> : null}

        {result ? <ResultBlock result={result} /> : null}
      </div>
    </Card>
  );
}

/** Honest list of lines / files the parser could not understand. */
function UnparsedAlert({ unparsed, count }: { unparsed: TradeAttributionUnparsed[]; count: number }) {
  return (
    <Alert
      type="warning"
      showIcon
      data-testid="trade-import-unparsed"
      message={`${count} 条内容无法解析，未纳入导入`}
      description={
        unparsed.length > 0 ? (
          <div className="flex flex-col gap-0.5 text-xs">
            {unparsed.map((u, idx) => (
              <div key={`${u.path}-${idx}`} className="flex flex-wrap gap-x-2">
                <span className="font-medium text-shell-ink">{u.path || DASH}</span>
                <span className="text-shell-muted">{u.reason || DASH}</span>
              </div>
            ))}
          </div>
        ) : undefined
      }
    />
  );
}

/** The parse-preview block: count summary line + fills table with duplicate marks. */
function PreviewBlock({ preview }: { preview: PortfolioImportParseResponse }) {
  const columns: ColumnsType<PortfolioImportParseRecord> = [
    { title: "日期", dataIndex: "date", key: "date" },
    { title: "时间", dataIndex: "time", key: "time" },
    { title: "代码", dataIndex: "symbol", key: "symbol" },
    { title: "名称", dataIndex: "name", key: "name" },
    {
      title: "方向",
      dataIndex: "side",
      key: "side",
      render: (side: PortfolioImportParseRecord["side"]) =>
        side === "buy" ? <Tag color="red">买入</Tag> : <Tag color="green">卖出</Tag>,
    },
    { title: "价格", dataIndex: "price", key: "price", align: "right" },
    { title: "数量", dataIndex: "qty", key: "qty", align: "right" },
    { title: "金额", dataIndex: "amount", key: "amount", align: "right" },
    { title: "月份", dataIndex: "month", key: "month" },
    {
      title: "重复",
      dataIndex: "duplicate",
      key: "duplicate",
      render: (dup: boolean) => (dup ? <Tag>重复</Tag> : null),
    },
  ];

  return (
    <div className="flex flex-col gap-3" data-testid="trade-import-preview">
      <Typography.Text data-testid="trade-import-preview-summary">
        共 {preview.fills_total} 条，新增 {preview.new_count}，重复 {preview.duplicate_count}
        ，未解析 {preview.unparsed_count}
      </Typography.Text>

      {preview.records_truncated ? (
        <Alert
          type="info"
          showIcon
          data-testid="trade-import-truncated"
          message="预览记录已截断，仅展示部分行；计数为完整文件的统计。"
        />
      ) : null}

      {preview.unparsed_count > 0 ? (
        <UnparsedAlert unparsed={preview.unparsed} count={preview.unparsed_count} />
      ) : null}

      <Table<PortfolioImportParseRecord & { key: string }>
        size="small"
        columns={columns}
        // A statement can legitimately contain identical fills, so key by the
        // stable row position instead of field values.
        dataSource={preview.records.map((r, idx) => ({ ...r, key: String(idx) }))}
        pagination={preview.records.length > 20 ? { pageSize: 20 } : false}
        scroll={{ x: "max-content" }}
        // Duplicate rows are greyed out — they will be skipped on commit.
        rowClassName={(record) => (record.duplicate ? "opacity-50" : "")}
        data-testid="trade-import-preview-table"
      />
    </div>
  );
}

/** The commit / dry-run result summary + post-import attribution review. */
function ResultBlock({ result }: { result: PortfolioImportCommitResponse }) {
  const writtenEntries = Object.entries(result.written ?? {});
  return (
    <div className="flex flex-col gap-3" data-testid="trade-import-result">
      <Alert
        type={result.dry_run ? "info" : "success"}
        showIcon
        message={
          result.dry_run
            ? `预演完成：将新增 ${result.appended_total} 条，跳过重复 ${result.duplicates_skipped} 条（共 ${result.fills_total} 条），未写入任何文件`
            : `导入成功：新增 ${result.appended_total} 条，跳过重复 ${result.duplicates_skipped} 条（共 ${result.fills_total} 条）`
        }
        description={
          !result.dry_run && writtenEntries.length > 0 ? (
            <div className="flex flex-col gap-0.5 text-xs" data-testid="trade-import-written">
              {writtenEntries.map(([path, count]) => (
                <span key={path}>
                  {path}：+{count} 条
                </span>
              ))}
            </div>
          ) : undefined
        }
      />

      {result.unparsed_count > 0 ? (
        <UnparsedAlert unparsed={result.unparsed} count={result.unparsed_count} />
      ) : null}

      {result.review ? <ReviewBlock review={result.review} /> : null}
    </div>
  );
}

/** Post-import review: affected months + key attribution summary stats. */
function ReviewBlock({
  review,
}: {
  review: NonNullable<PortfolioImportCommitResponse["review"]>;
}) {
  const summary: TradeAttributionSummary | null = review.attribution_summary;
  const stats: { label: string; value: string }[] = summary
    ? [
        { label: "回合数", value: String(summary.round_trips) },
        { label: "胜率", value: formatWinRate(summary.win_rate) },
        { label: "总已实现盈亏", value: formatMoneyString(summary.total_realized_pnl) },
        { label: "盈亏比", value: formatNumber(summary.profit_factor) },
        { label: "未平仓", value: String(summary.open_positions) },
      ]
    : [];

  return (
    <div className="flex flex-col gap-2" data-testid="trade-import-review">
      <Typography.Text strong className="!text-sm">
        导入后复盘
      </Typography.Text>

      <div className="flex flex-wrap items-center gap-1">
        <span className="text-xs text-shell-muted">涉及月份：</span>
        {review.affected_months.length > 0 ? (
          review.affected_months.map((m) => <Tag key={m}>{m}</Tag>)
        ) : (
          <span className="text-xs text-shell-muted">{DASH}</span>
        )}
      </div>

      {review.attribution_error ? (
        <Alert
          type="warning"
          showIcon
          data-testid="trade-import-attribution-error"
          message="导入已完成，但归因刷新失败"
          description={review.attribution_error}
        />
      ) : null}

      {summary ? (
        <div
          className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-5"
          data-testid="trade-import-review-summary"
        >
          {stats.map((s) => (
            <div
              key={s.label}
              className="flex flex-col gap-1 rounded-lg border border-shell-line bg-white/60 p-3"
              data-stat={s.label}
            >
              <span className="text-[11px] text-shell-muted">{s.label}</span>
              <span className="text-base font-semibold text-shell-ink">{s.value}</span>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

export default TradeImportCard;
