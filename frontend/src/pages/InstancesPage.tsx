import { InstanceTableCard } from "../components/InstanceTableCard";
import { PageIntro } from "../components/PageIntro";
import type { InstanceStatus } from "../types";

type Props = {
  instances: InstanceStatus[];
  loading: boolean;
  onMutated: () => void;
};

export function InstancesPage({ instances, loading, onMutated }: Props) {
  return (
    <>
      <PageIntro title="Agent Instances" description="查看实例状态，并执行启动、暂停和停止操作。" />
      <InstanceTableCard instances={instances} loading={loading} onMutated={onMutated} />
    </>
  );
}
