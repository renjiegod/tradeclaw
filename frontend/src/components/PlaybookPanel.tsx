import { ReloadOutlined } from "@ant-design/icons";
import { Button, Card, Empty, Modal, Spin, Tag, Typography, message } from "antd";
import { useCallback, useEffect, useMemo, useState } from "react";

import { getKnowledgeFile, getPlaybook } from "../api";
import type { KnowledgeFile, PlaybookEntry } from "../types";
import MarkdownPreview from "./MarkdownPreview";

const EMPTY_HINT = "暂无打板模式（对话里说「把这个打法记进模式库」即可添加）";

/** Fallback for any missing / blank authored field. Never fabricate a value. */
const DASH = "—";

/**
 * Per-stage visual palette for the 情绪阶段 tag. Kept in lock-step with
 * {@link import("./SentimentTimeline").SentimentTimeline}'s LABEL_STYLES:
 * 退潮/低迷 = 冷绿, 中性 = 灰, 发酵/活跃 = 橙, 分歧(加剧) = 琥珀,
 * 高潮/亢奋 = 热红. 全周期 / 未知 fall back to a neutral blue so a new /
 * blank stage never renders as an invisible tag. Both the bare label and the
 * ``退潮/低迷`` compound spelling map to the same style.
 */
const STAGE_STYLES: Record<string, { color: string; className: string }> = {
  "退潮/低迷": {
    // 冷绿 — ebb / weak
    color: "green",
    className: "!border-emerald-300 !bg-emerald-50 !text-emerald-700",
  },
  退潮: {
    color: "green",
    className: "!border-emerald-300 !bg-emerald-50 !text-emerald-700",
  },
  低迷: {
    color: "green",
    className: "!border-emerald-300 !bg-emerald-50 !text-emerald-700",
  },
  中性: {
    // 灰 — neutral
    color: "default",
    className: "!border-neutral-300 !bg-neutral-50 !text-neutral-600",
  },
  "发酵/活跃": {
    // 橙 — fermenting / active
    color: "orange",
    className: "!border-orange-400 !bg-orange-50 !text-orange-700",
  },
  发酵: {
    color: "orange",
    className: "!border-orange-400 !bg-orange-50 !text-orange-700",
  },
  活跃: {
    color: "orange",
    className: "!border-orange-400 !bg-orange-50 !text-orange-700",
  },
  分歧: {
    // 琥珀 — divergence
    color: "gold",
    className: "!border-amber-400 !bg-amber-50 !text-amber-700",
  },
  分歧加剧: {
    color: "gold",
    className: "!border-amber-400 !bg-amber-50 !text-amber-700",
  },
  "高潮/亢奋": {
    // 热红 — climax / euphoric
    color: "red",
    className: "!border-red-400 !bg-red-50 !text-red-700",
  },
  高潮: {
    color: "red",
    className: "!border-red-400 !bg-red-50 !text-red-700",
  },
  亢奋: {
    color: "red",
    className: "!border-red-400 !bg-red-50 !text-red-700",
  },
  全周期: {
    // 蓝 — applies across the whole cycle
    color: "blue",
    className: "!border-blue-400 !bg-blue-50 !text-blue-700",
  },
};

/** 未知 / null / new stage → neutral blue, never an invisible tag. */
const FALLBACK_STAGE_STYLE = {
  color: "blue",
  className: "!border-blue-400 !bg-blue-50 !text-blue-700",
};

function stageStyleFor(stage: string) {
  return STAGE_STYLES[stage] ?? FALLBACK_STAGE_STYLE;
}

/** Trim a possibly-blank authored string, or ``—`` when empty. Never fabricate. */
function orDash(value: string | null | undefined): string {
  const trimmed = value?.trim();
  return trimmed ? trimmed : DASH;
}

/** ``2026-05-30T10:00:00`` → ``2026-05-30 10:00`` (best-effort, never throws). */
function formatUpdatedAt(value: string | null | undefined): string {
  const raw = value?.trim();
  if (!raw) return DASH;
  const parsed = new Date(raw);
  if (Number.isNaN(parsed.getTime())) {
    // Not a parseable date — surface the raw authored string rather than
    // fabricating / dropping it.
    return raw;
  }
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())} ${pad(parsed.getHours())}:${pad(parsed.getMinutes())}`;
}

/** The 打法名: ``pattern`` if authored, otherwise fall back to ``title``. */
function patternLabel(entry: PlaybookEntry): string {
  const pattern = entry.pattern?.trim();
  if (pattern) return pattern;
  return orDash(entry.title);
}

/** Non-empty, trimmed tags only — never fabricate an empty chip. */
function cleanTags(tags: string[] | null | undefined): string[] {
  if (!Array.isArray(tags)) return [];
  return tags.map((t) => t?.trim()).filter((t): t is string => !!t);
}

/**
 * The 打板模式库 (playbook) card grid for the Knowledge review workbench.
 * Renders each 战法 / 打法 the user has summarised into the private knowledge
 * base as one card: the 打法名 (pattern, falling back to title), a 情绪阶段
 * tag coloured by {@link stageStyleFor} in lock-step with the sentiment
 * palette, the 摘要, free-form tags, and the last-updated time. Clicking a card
 * fetches the full markdown via {@link getKnowledgeFile} and renders it in an
 * antd Modal via {@link MarkdownPreview}. Pure div + Tailwind + AntD.
 *
 * Data comes from {@link getPlaybook}; it never fabricates values — missing
 * fields show ``—`` and an empty base shows a friendly empty state.
 */
export function PlaybookPanel() {
  const [items, setItems] = useState<PlaybookEntry[] | null>(null);
  const [loading, setLoading] = useState(true);

  // Full-text modal state.
  const [openEntry, setOpenEntry] = useState<PlaybookEntry | null>(null);
  const [detail, setDetail] = useState<KnowledgeFile | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await getPlaybook();
      setItems(res.items);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load().catch((error: unknown) => {
      const msg = error instanceof Error ? error.message : String(error);
      message.error(`加载打板模式库失败：${msg}`);
    });
  }, [load]);

  const openDetail = useCallback((entry: PlaybookEntry) => {
    setOpenEntry(entry);
    setDetail(null);
    setDetailLoading(true);
    void getKnowledgeFile("playbook", entry.path)
      .then((file) => setDetail(file))
      .catch((error: unknown) => {
        const msg = error instanceof Error ? error.message : String(error);
        message.error(`加载打法全文失败：${msg}`);
      })
      .finally(() => setDetailLoading(false));
  }, []);

  const closeDetail = useCallback(() => {
    setOpenEntry(null);
    setDetail(null);
    setDetailLoading(false);
  }, []);

  const showEmpty = !loading && (!items || items.length === 0);

  const subtitle = useMemo(() => {
    if (!items || items.length === 0) return "对话里沉淀的战法 / 打法总结";
    return `共 ${items.length} 个打法`;
  }, [items]);

  return (
    <Card
      className="!border !border-shell-line !bg-card-bg shadow-shell-card"
      title={
        <div className="flex flex-col">
          <Typography.Text strong>打板模式库</Typography.Text>
          <Typography.Text type="secondary" className="!text-xs !font-normal">
            {subtitle}
          </Typography.Text>
        </div>
      }
      extra={
        <Button
          size="small"
          icon={<ReloadOutlined />}
          loading={loading}
          onClick={() =>
            void load().catch((error: unknown) => {
              const msg = error instanceof Error ? error.message : String(error);
              message.error(`加载打板模式库失败：${msg}`);
            })
          }
        >
          刷新
        </Button>
      }
      data-testid="playbook-panel"
    >
      {loading ? (
        <div className="flex min-h-[160px] items-center justify-center">
          <Spin />
        </div>
      ) : showEmpty ? (
        <Empty
          description={EMPTY_HINT}
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          data-testid="playbook-empty"
        />
      ) : (
        <div className="flex flex-col gap-3">
          <div
            className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3"
            data-testid="playbook-grid"
          >
            {(items ?? []).map((entry) => (
              <PlaybookCard key={entry.path} entry={entry} onOpen={openDetail} />
            ))}
          </div>

          <Typography.Text type="secondary" className="!text-[11px]">
            仅描述你沉淀的交易手法，非预测、非买卖建议。
          </Typography.Text>
        </div>
      )}

      <Modal
        open={openEntry != null}
        onCancel={closeDetail}
        footer={null}
        width={760}
        title={openEntry ? patternLabel(openEntry) : ""}
        destroyOnHidden
        data-testid="playbook-detail-modal"
      >
        {detailLoading ? (
          <div className="flex min-h-[200px] items-center justify-center">
            <Spin />
          </div>
        ) : detail && detail.kind === "markdown" ? (
          <div data-testid="playbook-detail-markdown">
            <MarkdownPreview source={detail.content} stripFrontmatter />
          </div>
        ) : detail ? (
          // Non-markdown file (should not happen for playbook notes, but never
          // fabricate — surface the raw path honestly).
          <Typography.Text type="secondary" data-testid="playbook-detail-fallback">
            无法以 markdown 形式展示 {detail.path}
          </Typography.Text>
        ) : null}
      </Modal>
    </Card>
  );
}

/** One playbook (打法) card. Clicking it opens the full-text modal. */
function PlaybookCard({
  entry,
  onOpen,
}: {
  entry: PlaybookEntry;
  onOpen: (entry: PlaybookEntry) => void;
}) {
  const stage = entry.stage?.trim();
  const stageStyle = stageStyleFor(stage ?? "");
  const tags = cleanTags(entry.tags);

  return (
    <button
      type="button"
      onClick={() => onOpen(entry)}
      className="flex cursor-pointer flex-col gap-2 rounded-lg border border-shell-line bg-white/60 p-3 text-left transition-colors hover:bg-white focus:outline-none focus:ring-2 focus:ring-blue-300"
      data-testid="playbook-card"
      data-path={entry.path}
      data-stage={stage ?? ""}
    >
      <div className="flex items-start justify-between gap-2">
        <span className="text-base font-semibold text-shell-ink">
          {patternLabel(entry)}
        </span>
        <Tag
          color={stageStyle.color}
          className={`!m-0 !rounded-md !border !px-2 !py-0.5 !text-xs !font-medium ${stageStyle.className}`}
          data-testid="playbook-stage-tag"
        >
          {orDash(entry.stage)}
        </Tag>
      </div>

      <div className="text-xs text-shell-ink">{orDash(entry.summary)}</div>

      {tags.length > 0 ? (
        <div className="flex flex-wrap gap-1" data-testid="playbook-tags">
          {tags.map((tag) => (
            <Tag
              key={tag}
              className="!m-0 !rounded !border-neutral-300 !bg-neutral-50 !px-1.5 !py-0 !text-[11px] !text-neutral-600"
              data-testid="playbook-tag"
            >
              {tag}
            </Tag>
          ))}
        </div>
      ) : null}

      <span className="text-[11px] text-shell-muted">
        更新于 {formatUpdatedAt(entry.updated_at)}
      </span>
    </button>
  );
}

export default PlaybookPanel;
