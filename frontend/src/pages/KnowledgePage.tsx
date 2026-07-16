import { Alert, Space } from "antd";
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
    "你私有知识库（~/.doyoutrade/knowledge）的复盘工作台：顶部是每日复盘累积出的情绪周期时间线与个股角色，中部是券商交割单归因（已实现盈亏回合复盘）与打板模式库（战法 / 打法总结），下方是全库只读文件浏览器（六分区按月/年/策略分组）。写入仍由 agent 把关（对话里说「记到 knowledge 里」即可）。",
};

const PRIVACY_MESSAGE =
  "🔒 你的知识库完全存在本机 ~/.doyoutrade/knowledge，绝不进 git / 会话导出 / 回测报告 / 任何外传通道。这是只属于你的私有交易记忆。";

/**
 * Top-level Knowledge page — a 复盘 (review) workbench over the private
 * knowledge base. Top: a privacy assurance banner + the
 * {@link SentimentTimeline} emotional-cycle color band (recent 3 months).
 * Middle: the {@link SymbolRoleCards} per-symbol role tags. Below: the
 * read-only {@link KnowledgeBrowserPanel} file browser. Reached from the
 * sidebar "知识库" entry under ``/knowledge``.
 */
export function KnowledgePage() {
  // Bump on every successful statement import so the attribution board
  // remounts and refetches (the panel loads on mount; a key bump is the least
  // invasive way to trigger its existing load path).
  const [attributionRefreshKey, setAttributionRefreshKey] = useState(0);

  return (
    <Space direction="vertical" size={16} className="w-full">
      <PageIntro title={INTRO.title} description={INTRO.description} />
      <Alert
        type="info"
        message={PRIVACY_MESSAGE}
        className="!border-shell-line"
        data-testid="knowledge-privacy-banner"
      />
      <SentimentTimeline months={3} />
      <SymbolRoleCards />
      <TradeImportCard onImported={() => setAttributionRefreshKey((k) => k + 1)} />
      <TradeAttributionPanel key={attributionRefreshKey} months={6} />
      <PlaybookPanel />
      <KnowledgeBrowserPanel />
    </Space>
  );
}
