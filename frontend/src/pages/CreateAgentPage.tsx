import { CreateAgentCard } from "../components/CreateAgentCard";
import { PageIntro } from "../components/PageIntro";

type Props = {
  onCreated: () => void;
};

export function CreateAgentPage({ onCreated }: Props) {
  return (
    <>
      <PageIntro title="Create Agent" description="填写实例业务字段，系统字段由后端自动生成并持久化。" />
      <div className="max-w-5xl">
        <CreateAgentCard onCreated={onCreated} />
      </div>
    </>
  );
}
