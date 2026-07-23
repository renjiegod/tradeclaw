import { Tabs } from "antd";
import { useState } from "react";

import { KnowledgeBrowserPanel } from "../components/KnowledgeBrowserPanel";
import { KnowledgeGraphPanel } from "../components/KnowledgeGraphPanel";
import { PageIntro } from "../components/PageIntro";
import { PlaybookPanel } from "../components/PlaybookPanel";
import { TradeAttributionPanel } from "../components/TradeAttributionPanel";
import { TradeImportCard } from "../components/TradeImportCard";

const INTRO = {
  title: "知识库",
};

/**
 * Top-level Knowledge page — a 复盘 (review) workbench over the private
 * knowledge base, organised as tabs so the page stays short. Only carriers that
 * are *style-agnostic* live here: 交割单 ({@link TradeImportCard} +
 * {@link TradeAttributionPanel}), 图谱 ({@link KnowledgeGraphPanel}), 战法库
 * ({@link PlaybookPanel} — the generic 战法 / 打法 library) and 全库文件
 * ({@link KnowledgeBrowserPanel}). The market-sentiment cycle and per-symbol
 * roles that used to live here moved to the 市场复盘 page ({@link
 * import("./MarketReviewPage").MarketReviewPage}) and the 个股详情 page
 * ({@link import("./StockDetailPage").StockDetailPage}) respectively, since
 * those are 情绪派 / per-symbol concerns rather than generic knowledge carriers.
 * Reached from the sidebar "知识库" entry under ``/knowledge``.
 */
export function KnowledgePage() {
  // Bump on every successful statement import so the attribution board
  // remounts and refetches (the panel loads on mount; a key bump is the least
  // invasive way to trigger its existing load path).
  const [attributionRefreshKey, setAttributionRefreshKey] = useState(0);

  return (
    <div className="w-full">
      <PageIntro title={INTRO.title} />
      <Tabs
        defaultActiveKey="trades"
        data-testid="knowledge-tabs"
        items={[
          {
            key: "trades",
            label: "交割单",
            children: (
              <div className="flex flex-col gap-4">
                <TradeImportCard
                  onImported={() => setAttributionRefreshKey((k) => k + 1)}
                />
                <TradeAttributionPanel key={attributionRefreshKey} months={6} />
              </div>
            ),
          },
          {
            key: "graph",
            label: "图谱",
            children: <KnowledgeGraphPanel />,
          },
          {
            key: "playbook",
            label: "战法库",
            children: <PlaybookPanel />,
          },
          {
            key: "files",
            label: "全库文件",
            children: <KnowledgeBrowserPanel />,
          },
        ]}
      />
    </div>
  );
}
