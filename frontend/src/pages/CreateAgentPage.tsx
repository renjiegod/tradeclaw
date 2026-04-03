import { CreateAgentCard } from "../components/CreateAgentCard";
import { PageIntro } from "../components/PageIntro";

type Props = {
  onCreated: () => void;
};

export function CreateAgentPage({ onCreated }: Props) {
  return (
    <>
      <PageIntro title="Create Agent" description="基于模板创建新实例，并预填安全默认配置。" />
      <div className="max-w-3xl">
        <CreateAgentCard onCreated={onCreated} />
      </div>
    </>
  );
}
