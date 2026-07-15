import { Empty } from "antd";

/**
 * 连板梯队图: renders a ``{连板高度: 家数}`` distribution as one bar per height,
 * width proportional to that height's 家数. Higher boards are rendered more
 * prominently (deeper red per the A-share red convention). Implemented purely
 * with div + Tailwind — NO new chart dependency.
 *
 * ``ladder`` keys are the consecutive-limit heights as strings ("1", "2", …);
 * we sort them descending (highest board on top) so the 梯队 leader is most
 * visible. Non-numeric / non-positive keys are dropped defensively.
 */
export function LadderChart({ ladder }: { ladder: Record<string, number> }) {
  const entries = Object.entries(ladder ?? {})
    .map(([height, count]) => ({ height: Number(height), count: Number(count) }))
    .filter((e) => Number.isFinite(e.height) && e.height > 0 && Number.isFinite(e.count) && e.count > 0)
    .sort((a, b) => b.height - a.height);

  if (entries.length === 0) {
    return <Empty description="无连板梯队数据" image={Empty.PRESENTED_IMAGE_SIMPLE} />;
  }

  const maxCount = Math.max(...entries.map((e) => e.count));
  const maxHeight = Math.max(...entries.map((e) => e.height));

  return (
    <div className="flex flex-col gap-2" aria-label="连板梯队">
      {entries.map((e) => {
        const widthPct = maxCount > 0 ? Math.max((e.count / maxCount) * 100, 6) : 6;
        // Higher boards get a stronger red; scale lightness by relative height.
        const intensity = maxHeight > 1 ? (e.height - 1) / (maxHeight - 1) : 1;
        const lightness = 68 - Math.round(intensity * 30); // 68% (low) → 38% (high)
        const barColor = `hsl(4, 72%, ${lightness}%)`;
        return (
          <div key={e.height} className="flex items-center gap-3" data-testid="ladder-row">
            <span className="w-14 shrink-0 text-right text-sm font-medium text-shell-ink tabular-nums">
              {e.height} 板
            </span>
            <div className="flex h-7 flex-1 items-center overflow-hidden rounded-md bg-white/50">
              <div
                className="flex h-full items-center rounded-md px-2 text-xs font-semibold text-white transition-all"
                style={{ width: `${widthPct}%`, backgroundColor: barColor, minWidth: 32 }}
              >
                {e.count} 家
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
