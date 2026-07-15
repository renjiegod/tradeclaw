import React from "react";
import type { Agent } from "../types";
import { deleteAssistantAgent, cloneAssistantAgent, ApiError } from "../api";
import { Card, Badge, Button, Space, Tag, message } from "antd";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

type Props = {
  agent: Agent;
  onEdit: (agent: Agent) => void;
  onDeleted: () => void;
  onCloneSuccess: (newAgent: Agent) => void;
};

const PREVIEW_CHAR_LIMIT = 240;

function truncate(text: string, limit: number): string {
  if (text.length <= limit) return text;
  return `${text.slice(0, limit).trimEnd()}…`;
}

export const AgentCard: React.FC<Props> = ({ agent, onEdit, onDeleted, onCloneSuccess }) => {
  const [deleting, setDeleting] = React.useState(false);
  const [cloning, setCloning] = React.useState(false);
  const linkedTemplateId = agent.system_prompt_template_id || "";
  // Prefer the backend's live render so the preview reflects current .j2 file
  // contents, not the (possibly empty) snapshot column.
  const livePrompt = (agent.resolved_system_prompt ?? "").trim();
  const rawPrompt = agent.system_prompt.trim();
  const previewSource = livePrompt || rawPrompt;
  const previewText = previewSource ? truncate(previewSource, PREVIEW_CHAR_LIMIT) : "";

  const handleDelete = async () => {
    if (!confirm(`确定删除 Agent「${agent.name}」吗？`)) return;
    setDeleting(true);
    try {
      await deleteAssistantAgent(agent.id);
      onDeleted();
    } catch (err) {
      // A 409 means the agent still has assistant sessions (the backend blocks
      // the delete to protect history). Offer a cascade delete that removes the
      // agent together with its sessions instead of dead-ending in a blocking
      // native alert.
      if (err instanceof ApiError && err.status === 409) {
        const proceed = confirm(
          `${err.message}\n\n将一并删除该 Agent 的全部会话（含消息与事件），该操作不可恢复。是否继续？`,
        );
        if (proceed) {
          try {
            await deleteAssistantAgent(agent.id, { force: true });
            onDeleted();
            return;
          } catch (forceErr) {
            const content = forceErr instanceof Error ? forceErr.message : String(forceErr);
            message.error(`删除失败：${content}`);
          }
        }
      } else {
        const content = err instanceof Error ? err.message : String(err);
        message.error(`删除失败：${content}`);
      }
    } finally {
      setDeleting(false);
    }
  };

  const handleClone = async () => {
    const newName = prompt(`克隆「${agent.name}」——请输入新名称：`);
    if (!newName?.trim()) return;
    setCloning(true);
    try {
      const cloned = await cloneAssistantAgent(agent.id, newName.trim());
      onCloneSuccess(cloned);
    } catch (err) {
      const content = err instanceof Error ? err.message : String(err);
      message.error(`克隆失败：${content}`);
    } finally {
      setCloning(false);
    }
  };

  return (
    <Card
      size="small"
      title={
        <Space size={6}>
          <span>{agent.name}</span>
          {agent.is_builtin && (
            <Tag color="gold" style={{ marginInlineEnd: 0 }}>
              固定主智能体
            </Tag>
          )}
        </Space>
      }
      extra={
        <Badge
          color={agent.status === "active" ? "green" : "gray"}
          text={agent.status}
        />
      }
      actions={[
        <Button key="edit" type="text" onClick={() => onEdit(agent)} disabled={deleting || cloning}>
          编辑
        </Button>,
        <Button key="clone" type="text" onClick={handleClone} loading={cloning} disabled={deleting}>
          克隆
        </Button>,
        !agent.is_builtin && (
          <Button key="delete" type="text" danger onClick={handleDelete} loading={deleting}>
            删除
          </Button>
        ),
      ].filter(Boolean)}
    >
      <div style={{ marginBottom: 12 }}>
        {linkedTemplateId ? (
          <Tag color="blue" style={{ marginBottom: 6 }}>
            已关联 → {linkedTemplateId}
          </Tag>
        ) : (
          <Tag color="default" style={{ marginBottom: 6 }}>
            自定义提示词
          </Tag>
        )}
        {previewText ? (
          <div
            data-testid="agent-card-prompt-preview"
            className="markdown-body"
            style={{
              color: "#666",
              fontSize: 12,
              lineHeight: 1.5,
              maxHeight: 96,
              overflow: "hidden",
            }}
          >
            <Markdown remarkPlugins={[remarkGfm]}>{previewText}</Markdown>
          </div>
        ) : (
          <span style={{ color: "#999", fontSize: 12, fontStyle: "italic" }}>
            （提示词为空）
          </span>
        )}
      </div>
      <Space size="small" className="!gap-2">
        <Badge count={`工具: ${agent.tool_names.length}`} style={{ backgroundColor: "#f0f0f0", color: "#666" }} />
        <Badge count={`Skills: ${agent.skill_names.length}`} style={{ backgroundColor: "#f0f0f0", color: "#666" }} />
        <Badge count={`最大轮数: ${agent.max_turns}`} style={{ backgroundColor: "#f0f0f0", color: "#666" }} />
      </Space>
    </Card>
  );
};
