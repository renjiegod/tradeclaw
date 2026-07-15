import { Card } from "antd";
import type { ReactNode } from "react";

/** Gradient-icon accent for a metric tile. The card chrome stays warm (shell
 * palette); only the small square icon carries the multi-hue accent so the page
 * keeps the reference layout without leaving the warm theme. */
export type MetricTileTone = "violet" | "emerald" | "amber" | "rose" | "sky" | "slate";

const TONE_GRADIENT: Record<MetricTileTone, string> = {
  violet: "from-violet-400 to-violet-600",
  emerald: "from-emerald-400 to-emerald-600",
  amber: "from-amber-400 to-amber-600",
  rose: "from-rose-400 to-rose-600",
  sky: "from-sky-400 to-sky-600",
  slate: "from-slate-400 to-slate-600",
};

type Props = {
  icon: ReactNode;
  tone: MetricTileTone;
  label: string;
  value: ReactNode;
  /** Secondary line under the value, e.g. a signed percent or context note. */
  sub?: ReactNode;
  /** Extra classes for the value (used for sign-aware P&L coloring). */
  valueClassName?: string;
  loading?: boolean;
};

/** One headline metric: a gradient icon tile next to a label + value, in the
 * shell card chrome. Shared by the task-detail summary row so live / paper /
 * signal tasks present the same way. */
export function TaskMetricTile({ icon, tone, label, value, sub, valueClassName, loading = false }: Props) {
  return (
    <Card
      size="small"
      variant="borderless"
      loading={loading}
      className="!rounded-xl !border !border-shell-line !bg-card-bg shadow-shell-card"
    >
      <div className="flex items-center gap-3">
        <div
          className={`flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br ${TONE_GRADIENT[tone]} text-lg text-white shadow-sm`}
          aria-hidden
        >
          {icon}
        </div>
        <div className="min-w-0">
          <div className="truncate text-xs text-shell-muted">{label}</div>
          <div className={`mt-0.5 truncate text-xl font-semibold tabular-nums text-shell-ink ${valueClassName ?? ""}`}>
            {value}
          </div>
          {sub != null ? <div className="truncate text-xs text-shell-muted">{sub}</div> : null}
        </div>
      </div>
    </Card>
  );
}
