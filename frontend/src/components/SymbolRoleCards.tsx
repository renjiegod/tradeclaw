import { ReloadOutlined } from "@ant-design/icons";
import { Button, Card, Empty, Spin, Tag, Typography, message } from "antd";
import { useCallback, useEffect, useMemo, useState } from "react";

import { getSymbolRoles } from "../api";
import type { SymbolRoleCard } from "../types";

const EMPTY_HINT = "暂无标的角色记录（对话里说「把这票记成龙头」即可添加）";

/** Fallback for any missing / blank authored field. Never fabricate a value. */
const DASH = "—";

/**
 * Per-role visual palette for the role tag. A-share convention: red = 强 / 领涨
 * (龙头), warm tones step down through 龙二 / 中军 / 补涨, grey for 杂毛 and the
 * unknown fallback, blue for 事件型. The keys are the exact role labels the
 * backend stores — anything unrecognised falls back to a neutral grey so a new
 * role never renders as an invisible / transparent tag.
 */
const ROLE_STYLES: Record<string, { color: string; className: string }> = {
  龙头: {
    // 热红 — strongest / leader
    color: "red",
    className: "!border-red-400 !bg-red-50 !text-red-700",
  },
  龙二: {
    // 橙 — second in line
    color: "orange",
    className: "!border-orange-400 !bg-orange-50 !text-orange-700",
  },
  中军: {
    // 琥珀 — main body
    color: "gold",
    className: "!border-amber-400 !bg-amber-50 !text-amber-700",
  },
  补涨: {
    // 浅橙 — laggard catch-up
    color: "volcano",
    className: "!border-orange-300 !bg-orange-50/60 !text-orange-600",
  },
  杂毛: {
    // 灰 — noise / weak
    color: "default",
    className: "!border-neutral-300 !bg-neutral-50 !text-neutral-600",
  },
  事件型: {
    // 蓝 — event-driven
    color: "blue",
    className: "!border-blue-400 !bg-blue-50 !text-blue-700",
  },
};

const FALLBACK_ROLE_STYLE = {
  color: "default",
  className: "!border-neutral-300 !bg-neutral-50 !text-neutral-600",
};

function roleStyleFor(role: string) {
  return ROLE_STYLES[role] ?? FALLBACK_ROLE_STYLE;
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

/**
 * The 个股角色 (per-symbol role) card grid for the Knowledge review workbench.
 * Renders each role the user has tagged into the private knowledge base as one
 * card: symbol + name, a role tag coloured by {@link roleStyleFor}, the note,
 * an optional strategy hint, and the last-updated time. Pure div + Tailwind +
 * AntD — no chart / extra dependency.
 *
 * Data comes from {@link getSymbolRoles}; it never fabricates values — missing
 * fields show ``—`` and an empty base shows a friendly empty state.
 */
export function SymbolRoleCards() {
  const [items, setItems] = useState<SymbolRoleCard[] | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await getSymbolRoles();
      setItems(res.items);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load().catch((error: unknown) => {
      const msg = error instanceof Error ? error.message : String(error);
      message.error(`加载个股角色失败：${msg}`);
    });
  }, [load]);

  const showEmpty = !loading && (!items || items.length === 0);

  const subtitle = useMemo(() => {
    if (!items || items.length === 0) return "对话里给标的打的角色标签";
    return `共 ${items.length} 个标的`;
  }, [items]);

  return (
    <Card
      className="!border !border-shell-line !bg-card-bg shadow-shell-card"
      title={
        <div className="flex flex-col">
          <Typography.Text strong>个股角色</Typography.Text>
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
              message.error(`加载个股角色失败：${msg}`);
            })
          }
        >
          刷新
        </Button>
      }
      data-testid="symbol-role-cards"
    >
      {loading ? (
        <div className="flex min-h-[160px] items-center justify-center">
          <Spin />
        </div>
      ) : showEmpty ? (
        <Empty
          description={EMPTY_HINT}
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          data-testid="symbol-role-empty"
        />
      ) : (
        <div className="flex flex-col gap-3">
          <div
            className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3"
            data-testid="symbol-role-grid"
          >
            {(items ?? []).map((card) => (
              <RoleCard key={card.symbol} card={card} />
            ))}
          </div>

          <Typography.Text type="secondary" className="!text-[11px]">
            仅描述你标注的标的定位，非预测、非买卖建议。
          </Typography.Text>
        </div>
      )}
    </Card>
  );
}

/** One symbol role card. */
function RoleCard({ card }: { card: SymbolRoleCard }) {
  const style = roleStyleFor(card.role);
  const hint = card.strategy_hint?.trim();
  return (
    <div
      className="flex flex-col gap-2 rounded-lg border border-shell-line bg-white/60 p-3 transition-colors hover:bg-white"
      data-testid="symbol-role-card"
      data-symbol={card.symbol}
      data-role={card.role}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="flex flex-col leading-tight">
          <span className="text-base font-semibold text-shell-ink">
            {orDash(card.symbol)}
          </span>
          <span className="text-sm text-shell-muted">{orDash(card.name)}</span>
        </div>
        <Tag
          color={style.color}
          className={`!m-0 !rounded-md !border !px-2 !py-0.5 !text-xs !font-medium ${style.className}`}
          data-testid="symbol-role-tag"
        >
          {orDash(card.role)}
        </Tag>
      </div>

      <div className="flex flex-col gap-1 text-xs">
        <div>
          <span className="text-shell-muted">备注：</span>
          <span className="text-shell-ink">{orDash(card.note)}</span>
        </div>
        {hint ? (
          <div>
            <span className="text-shell-muted">策略建议：</span>
            <span className="text-shell-ink">{hint}</span>
          </div>
        ) : null}
      </div>

      <span className="text-[11px] text-shell-muted">
        更新于 {formatUpdatedAt(card.updated_at)}
      </span>
    </div>
  );
}

export default SymbolRoleCards;
