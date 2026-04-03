import { ApprovalQueueCard } from "../components/ApprovalQueueCard";
import { PageIntro } from "../components/PageIntro";
import type { PendingApproval } from "../types";

type Props = {
  items: PendingApproval[];
  loading: boolean;
  onMutated: () => void;
};

export function ApprovalsPage({ items, loading, onMutated }: Props) {
  return (
    <>
      <PageIntro title="Approvals" description="集中查看待审批请求，快速批准或拒绝风险订单。" />
      <ApprovalQueueCard items={items} loading={loading} onMutated={onMutated} />
    </>
  );
}
