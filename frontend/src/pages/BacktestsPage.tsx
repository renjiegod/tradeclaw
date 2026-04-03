import { EmptyStateCard } from "../components/EmptyStateCard";
import { PageIntro } from "../components/PageIntro";

export function BacktestsPage() {
  return (
    <>
      <PageIntro title="Backtests" description="为回测任务、对比与报告预留固定入口。" />
      <EmptyStateCard
        title="回测功能将在后端支持后接入"
        description="当前版本先保留信息架构位置，避免后续重新组织后台导航。"
      />
    </>
  );
}
