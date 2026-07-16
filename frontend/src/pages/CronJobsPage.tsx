import React from "react";
import type { Agent, CronJob } from "../types";
import {
  listAssistantAgents,
  listCronJobs,
  deleteCronJob,
  pauseCronJob,
  resumeCronJob,
  triggerCronJob,
} from "../api";
import { Table, Button, Space, Tag, Popconfirm, message, Select } from "antd";
import { describeCron } from "../components/parseCron";
import { CronJobFormModal } from "../components/CronJobFormModal";
import { CronJobRunHistoryModal } from "../components/CronJobRunHistoryModal";
import { usePageRefreshToken } from "../pageRefreshContext";

const ALL_AGENTS = "__all_agents__";

const STATUS_COLORS: Record<string, string> = {
  success: "green",
  error: "red",
  agent_failed: "red",
  pre_failed: "red",
  running: "blue",
  skipped: "orange",
  cancelled: "gray",
  // Derived states surfaced by the backend's ``effective_status`` field
  // for freshly-created and disabled jobs.
  waiting: "blue",
  paused: "orange",
};

export function CronJobsPage() {
  const pageRefreshToken = usePageRefreshToken();
  const [agents, setAgents] = React.useState<Agent[]>([]);
  const [selectedAgentId, setSelectedAgentId] = React.useState<string>(ALL_AGENTS);
  const [jobs, setJobs] = React.useState<CronJob[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [editingJob, setEditingJob] = React.useState<CronJob | undefined>(undefined);
  const [showForm, setShowForm] = React.useState(false);
  const [historyJob, setHistoryJob] = React.useState<CronJob | undefined>(undefined);

  const loadAgents = React.useCallback(async () => {
    const result = await listAssistantAgents({ include_inactive: true });
    setAgents(result.items);
  }, []);

  React.useEffect(() => { void loadAgents(); }, [loadAgents, pageRefreshToken]);

  const loadJobs = React.useCallback(async () => {
    const targetAgents = selectedAgentId === ALL_AGENTS
      ? agents
      : agents.filter((agent) => agent.id === selectedAgentId);

    if (targetAgents.length === 0) {
      setJobs([]);
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const settled = await Promise.allSettled(
        targetAgents.map(async (agent) => {
          const result = await listCronJobs(agent.id);
          return result.items;
        }),
      );
      const mergedJobs = settled.flatMap((result) => result.status === "fulfilled" ? result.value : []);
      setJobs(sortCronJobs(mergedJobs, agents));
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  }, [agents, selectedAgentId]);

  React.useEffect(() => { void loadJobs(); }, [loadJobs, pageRefreshToken]);

  const handleSaved = (_job: CronJob) => {
    setShowForm(false);
    setEditingJob(undefined);
    void loadJobs();
  };

  const handleDelete = async (job: CronJob) => {
    await deleteCronJob(job.agent_id, job.id);
    message.success("已删除");
    void loadJobs();
  };

  // NOTE: strategy signal push lives on Tasks now (see TaskTriggersPanel /
  // TriggerFormModal). This page only manages agent reminders / chat replies.

  const handlePause = async (job: CronJob) => {
    await pauseCronJob(job.agent_id, job.id);
    void loadJobs();
  };

  const handleResume = async (job: CronJob) => {
    await resumeCronJob(job.agent_id, job.id);
    void loadJobs();
  };

  const handleTrigger = async (job: CronJob) => {
    try {
      const result = await triggerCronJob(job.agent_id, job.id);
      message.success(`已触发（run id：${result.cron_job_run_id}）`);
    } catch (err) {
      message.error(`触发失败：${err instanceof Error ? err.message : String(err)}`);
    }
  };

  const columns = [
    { title: "Name", dataIndex: "name", key: "name" },
    {
      title: "Agent",
      render: (_: unknown, record: CronJob) => {
        const agent = agents.find(a => a.id === record.agent_id);
        return agent?.name ?? record.agent_id;
      },
      key: "agent",
    },
    {
      title: "Schedule",
      render: (_: unknown, record: CronJob) => describeCron(record.cron_expression),
      key: "schedule",
    },
    { title: "Timezone", dataIndex: "timezone", key: "timezone" },
    {
      title: "Kind",
      key: "task_kind",
      render: (_: unknown, record: CronJob) => {
        // Reminders are the supported kind here. Older strategy-push /
        // pre-action rows still render so operators can find and migrate
        // them, but new rows are always agent_chat_reply reminders.
        if (record.task_kind === "agent_chat_reply") return <Tag color="purple">提醒</Tag>;
        if (record.task_kind) return <Tag color="orange">{record.task_kind}</Tag>;
        if (record.pre_action?.kind) {
          return (
            <Space size={4}>
              <Tag>legacy</Tag>
              <Tag>{record.pre_action.kind}</Tag>
            </Space>
          );
        }
        return <Tag>legacy</Tag>;
      },
    },
    {
      title: "Status",
      dataIndex: "effective_status",
      key: "effective_status",
      render: (status: string) => (
        <Tag color={STATUS_COLORS[status] || "gray"}>{status}</Tag>
      ),
    },
    {
      title: "Last Run",
      dataIndex: "last_run_at",
      key: "last_run_at",
      render: (v: string | null) => v ? new Date(v).toLocaleString() : "—",
    },
    {
      title: "Actions",
      key: "actions",
      render: (_: unknown, record: CronJob) => (
        <Space>
          <Button size="small" onClick={() => { setEditingJob(record); setShowForm(true); }}>Edit</Button>
          <Button size="small" onClick={() => void handleTrigger(record)}>Run</Button>
          <Button size="small" onClick={() => setHistoryJob(record)}>History</Button>
          {record.enabled
            ? <Button size="small" onClick={() => void handlePause(record)}>Pause</Button>
            : <Button size="small" onClick={() => void handleResume(record)}>Resume</Button>
          }
          <Popconfirm title="Delete this reminder?" onConfirm={() => void handleDelete(record)}>
            <Button size="small" danger>Delete</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 16, gap: 16, alignItems: "center" }}>
        <h2 style={{ margin: 0 }}>提醒</h2>
        <Space>
          <span style={{ color: "#666" }}>Agent:</span>
          <Select
            style={{ width: 220 }}
            value={selectedAgentId}
            onChange={(val) => { setSelectedAgentId(val); }}
            options={[
              { label: "All agents", value: ALL_AGENTS },
              ...agents.map((agent) => ({ label: agent.name, value: agent.id })),
            ]}
          />
          <Button
            type="primary"
            disabled={selectedAgentId === ALL_AGENTS}
            onClick={() => { setEditingJob(undefined); setShowForm(true); }}
          >
            New Reminder
          </Button>
        </Space>
      </div>

      <Table
        dataSource={jobs}
        columns={columns}
        rowKey="id"
        loading={loading}
        pagination={false}
      />

      {showForm && (editingJob?.agent_id || selectedAgentId !== ALL_AGENTS) && (
        <CronJobFormModal
          agentId={editingJob?.agent_id ?? selectedAgentId}
          job={editingJob}
          onSaved={handleSaved}
          onClose={() => { setShowForm(false); setEditingJob(undefined); }}
        />
      )}

      {historyJob && (
        <CronJobRunHistoryModal
          jobId={historyJob.id}
          jobName={historyJob.name}
          onClose={() => setHistoryJob(undefined)}
        />
      )}
    </div>
  );
}

function sortCronJobs(jobs: CronJob[], agents: Agent[]): CronJob[] {
  const agentNameById = new Map(agents.map((agent) => [agent.id, agent.name]));
  return [...jobs].sort((left, right) => {
    const leftAgentName = agentNameById.get(left.agent_id) ?? left.agent_id;
    const rightAgentName = agentNameById.get(right.agent_id) ?? right.agent_id;
    const agentCompare = leftAgentName.localeCompare(rightAgentName, undefined, { sensitivity: "base" });
    if (agentCompare !== 0) return agentCompare;
    const nameCompare = left.name.localeCompare(right.name, undefined, { sensitivity: "base" });
    if (nameCompare !== 0) return nameCompare;
    return left.id.localeCompare(right.id, undefined, { sensitivity: "base" });
  });
}
