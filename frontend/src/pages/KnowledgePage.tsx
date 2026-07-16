import { Tabs } from "antd";
import { useState } from "react";

import { KnowledgeBrowserPanel } from "../components/KnowledgeBrowserPanel";
import { PageIntro } from "../components/PageIntro";
import { PlaybookPanel } from "../components/PlaybookPanel";
import { SentimentTimeline } from "../components/SentimentTimeline";
import { SymbolRoleCards } from "../components/SymbolRoleCards";
import { TradeAttributionPanel } from "../components/TradeAttributionPanel";
import { TradeImportCard } from "../components/TradeImportCard";

const INTRO = {
  title: "知识库",
  description:
    "私有复盘工作台 · 🔒 数据仅存本机 ~/.doyoutrade/knowledge，不进 git、不外传；对话里说「记到 knowledge 里」即可写入。",
};

/**
 * Top-level Knowledge page — a 复盘 (review) workbench over the private
 * knowledge base, organised as tabs so the page stays short: 周期与角色
 * ({@link SentimentTimeline} + {@link SymbolRoleCards}), 交割单
 * ({@link TradeImportCard} + {@link TradeAttributionPanel}), 打板模式库
 * ({@link PlaybookPanel}) and 全库文件 ({@link KnowledgeBrowserPanel}).
 * Reached from the sidebar "知识库" entry under ``/knowledge``.
 */
export function KnowledgePage() {
  // Bump on every successful statement import so the attribution board
  // remounts and refetches (the panel loads on mount; a key bump is the least
  // invasive way to trigger its existing load path).
  const [attributionRefreshKey, setAttributionRefreshKey] = useState(0);

  return (
    <div className="w-full">
      <PageIntro title={INTRO.title} description={INTRO.description} />
      <Tabs
        defaultActiveKey="review"
        data-testid="knowledge-tabs"
        items={[
          {
            key: "review",
            label: "周期与角色",
            children: (
              <div className="flex flex-col gap-4">
                <SentimentTimeline months={3} />
                <SymbolRoleCards />
              </div>
            ),
          },
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
            key: "playbook",
            label: "打板模式库",
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
