import { EditOutlined, HistoryOutlined, InboxOutlined } from "@ant-design/icons";
import {
  Button,
  Empty,
  Input,
  List,
  Modal,
  Select,
  Space,
  Spin,
  Typography,
  message,
} from "antd";
import { useCallback, useMemo, useState } from "react";

import {
  applyKnowledgeGraphChange,
  approveKnowledgeGraphChange,
  getKnowledgeGraphChangeSets,
  getKnowledgeGraphConflicts,
  getKnowledgeGraphSchema,
  redoKnowledgeGraphRevision,
  rejectKnowledgeGraphChange,
  resolveKnowledgeGraphConflict,
  undoKnowledgeGraphRevision,
} from "../api";
import type {
  KnowledgeGraphChangeSet,
  KnowledgeGraphConflict,
  KnowledgeGraphNeighborhood,
  KnowledgeGraphRelationTypeDefinition,
} from "../types";
import { KnowledgeGraphSchemaManager } from "./KnowledgeGraphSchemaManager";

type Props = {
  data: KnowledgeGraphNeighborhood | null;
  onChanged: () => Promise<void>;
};

function displayNode(data: KnowledgeGraphNeighborhood): string {
  return data.center.display_name || data.center.name;
}

export function KnowledgeGraphEditingActions({ data, onChanged }: Props) {
  const [manualOpen, setManualOpen] = useState(false);
  const [manualLoading, setManualLoading] = useState(false);
  const [manualSubmitting, setManualSubmitting] = useState(false);
  const [relations, setRelations] = useState<KnowledgeGraphRelationTypeDefinition[]>([]);
  const [relationKey, setRelationKey] = useState("");
  const [targetName, setTargetName] = useState("");
  const [fact, setFact] = useState("");

  const [inboxOpen, setInboxOpen] = useState(false);
  const [inboxLoading, setInboxLoading] = useState(false);
  const [pending, setPending] = useState<KnowledgeGraphChangeSet[]>([]);
  const [decidingId, setDecidingId] = useState<string | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [history, setHistory] = useState<KnowledgeGraphChangeSet[]>([]);
  const [historyAction, setHistoryAction] = useState<string | null>(null);

  const [entityOpen, setEntityOpen] = useState(false);
  const [entityDisplayName, setEntityDisplayName] = useState("");
  const [entitySubmitting, setEntitySubmitting] = useState(false);
  const [conflictOpen, setConflictOpen] = useState(false);
  const [conflictLoading, setConflictLoading] = useState(false);
  const [conflicts, setConflicts] = useState<KnowledgeGraphConflict[]>([]);
  const [decidingConflictId, setDecidingConflictId] = useState<string | null>(null);

  const selectedRelation = useMemo(
    () => relations.find((item) => item.key === relationKey) ?? null,
    [relationKey, relations],
  );

  const openManual = useCallback(async () => {
    if (!data) return;
    setManualLoading(true);
    try {
      const schema = await getKnowledgeGraphSchema();
      const available = schema.relation_types.filter(
        (item) => item.source_type === data.center.node_type,
      );
      if (available.length === 0) {
        message.warning(`实体类型 ${data.center.node_type} 暂无可手工创建的关系`);
        return;
      }
      setRelations(available);
      setRelationKey(available[0].key);
      setTargetName("");
      setFact("");
      setManualOpen(true);
    } catch (error: unknown) {
      const text = error instanceof Error ? error.message : String(error);
      message.error(`加载图谱 Schema 失败：${text}`);
    } finally {
      setManualLoading(false);
    }
  }, [data]);

  const submitManual = useCallback(async () => {
    if (!data || !selectedRelation || !targetName.trim() || !fact.trim()) return;
    setManualSubmitting(true);
    try {
      const target = targetName.trim();
      await applyKnowledgeGraphChange(
        [
          {
            op: "create_relation",
            source: {
              type: data.center.node_type,
              name: data.center.name,
              display_name: data.center.display_name,
            },
            relation: selectedRelation.key,
            target: {
              type: selectedRelation.target_type,
              name: target,
            },
            fact: fact.trim(),
            confidence: 1,
          },
        ],
        `手工新增关系：${displayNode(data)} → ${target}`,
        data.revision,
      );
      message.success("手工关系已写入图谱");
      setManualOpen(false);
      await onChanged();
    } catch (error: unknown) {
      const text = error instanceof Error ? error.message : String(error);
      message.error(`手工关系写入失败：${text}`);
    } finally {
      setManualSubmitting(false);
    }
  }, [data, fact, onChanged, selectedRelation, targetName]);

  const loadInbox = useCallback(async () => {
    setInboxLoading(true);
    try {
      const response = await getKnowledgeGraphChangeSets("pending");
      setPending(response.items);
    } catch (error: unknown) {
      const text = error instanceof Error ? error.message : String(error);
      message.error(`加载待审批草案失败：${text}`);
    } finally {
      setInboxLoading(false);
    }
  }, []);

  const openInbox = useCallback(() => {
    setInboxOpen(true);
    void loadInbox();
  }, [loadInbox]);

  const decide = useCallback(
    async (item: KnowledgeGraphChangeSet, decision: "approve" | "reject") => {
      setDecidingId(item.id);
      try {
        if (decision === "approve") {
          await approveKnowledgeGraphChange(item.id, item.proposal_hash);
          message.success("草案已批准并写入图谱");
          await onChanged();
        } else {
          await rejectKnowledgeGraphChange(item.id, item.proposal_hash);
          message.info("草案已拒绝");
        }
        setPending((current) => current.filter((entry) => entry.id !== item.id));
      } catch (error: unknown) {
        const text = error instanceof Error ? error.message : String(error);
        message.error(`审批失败：${text}`);
      } finally {
        setDecidingId(null);
      }
    },
    [onChanged],
  );

  const openHistory = useCallback(async () => {
    setHistoryOpen(true);
    setHistoryLoading(true);
    try {
      const response = await getKnowledgeGraphChangeSets();
      setHistory(response.items);
    } catch (error: unknown) {
      const text = error instanceof Error ? error.message : String(error);
      message.error(`加载图谱历史失败：${text}`);
    } finally {
      setHistoryLoading(false);
    }
  }, []);

  const compensate = useCallback(
    async (item: KnowledgeGraphChangeSet, action: "undo" | "redo") => {
      if (item.revision == null || data == null) return;
      setHistoryAction(`${action}:${item.id}`);
      try {
        if (action === "undo") {
          await undoKnowledgeGraphRevision(item.revision, data.revision);
          message.success(`已撤销 revision ${item.revision}`);
        } else {
          await redoKnowledgeGraphRevision(item.revision, data.revision);
          message.success(`已重做 revision ${item.revision}`);
        }
        setHistoryOpen(false);
        await onChanged();
      } catch (error: unknown) {
        const text = error instanceof Error ? error.message : String(error);
        message.error(`${action === "undo" ? "撤销" : "重做"}失败：${text}`);
      } finally {
        setHistoryAction(null);
      }
    },
    [data, onChanged],
  );

  const openEntityEditor = useCallback(() => {
    if (!data) return;
    setEntityDisplayName(data.center.display_name || data.center.name);
    setEntityOpen(true);
  }, [data]);

  const submitEntityUpdate = useCallback(async () => {
    if (!data) return;
    setEntitySubmitting(true);
    try {
      await applyKnowledgeGraphChange(
        [
          {
            op: "update_entity",
            entity_id: data.center.id,
            display_name: entityDisplayName.trim() || null,
          },
        ],
        `更新实体：${data.center.name}`,
        data.revision,
      );
      message.success("实体属性已更新");
      setEntityOpen(false);
      await onChanged();
    } catch (error: unknown) {
      const detail = error instanceof Error ? error.message : String(error);
      message.error(`实体更新失败：${detail}`);
    } finally {
      setEntitySubmitting(false);
    }
  }, [data, entityDisplayName, onChanged]);

  const retireEntity = useCallback(async () => {
    if (!data) return;
    setEntitySubmitting(true);
    try {
      await applyKnowledgeGraphChange(
        [
          {
            op: "retire_entity",
            entity_id: data.center.id,
            reason: "本地用户退役",
          },
        ],
        `退役实体：${data.center.name}`,
        data.revision,
      );
      message.success("实体已退役");
      setEntityOpen(false);
      await onChanged();
    } catch (error: unknown) {
      const detail = error instanceof Error ? error.message : String(error);
      message.error(`实体退役失败：${detail}`);
    } finally {
      setEntitySubmitting(false);
    }
  }, [data, onChanged]);

  const openConflicts = useCallback(async () => {
    setConflictOpen(true);
    setConflictLoading(true);
    try {
      const response = await getKnowledgeGraphConflicts("open");
      setConflicts(response.items);
    } catch (error: unknown) {
      const detail = error instanceof Error ? error.message : String(error);
      message.error(`加载冲突失败：${detail}`);
    } finally {
      setConflictLoading(false);
    }
  }, []);

  const dismissConflict = useCallback(
    async (conflictId: string) => {
      if (!data) return;
      setDecidingConflictId(conflictId);
      try {
        await resolveKnowledgeGraphConflict(conflictId, "dismiss", data.revision);
        message.success("冲突已忽略");
        const response = await getKnowledgeGraphConflicts("open");
        setConflicts(response.items);
        await onChanged();
      } catch (error: unknown) {
        const detail = error instanceof Error ? error.message : String(error);
        message.error(`裁决失败：${detail}`);
      } finally {
        setDecidingConflictId(null);
      }
    },
    [data, onChanged],
  );

  return (
    <>
      <Button
        size="small"
        icon={<EditOutlined />}
        disabled={!data}
        loading={manualLoading}
        onClick={() => void openManual()}
        data-testid="kg-manual-edit"
      >
        手动标记
      </Button>
      <Button
        size="small"
        disabled={!data}
        onClick={openEntityEditor}
        data-testid="kg-entity-edit"
      >
        编辑实体
      </Button>
      <Button
        size="small"
        icon={<InboxOutlined />}
        onClick={openInbox}
        data-testid="kg-change-inbox"
      >
        待审批
      </Button>
      <Button
        size="small"
        onClick={() => void openConflicts()}
        data-testid="kg-conflict-inbox"
      >
        冲突
      </Button>
      <Button
        size="small"
        icon={<HistoryOutlined />}
        onClick={() => void openHistory()}
        data-testid="kg-change-history"
      >
        历史
      </Button>
      <KnowledgeGraphSchemaManager onChanged={onChanged} />

      <Modal
        title="编辑中心实体"
        open={entityOpen}
        onCancel={() => setEntityOpen(false)}
        footer={[
          <Button key="cancel" onClick={() => setEntityOpen(false)}>
            取消
          </Button>,
          <Button
            key="retire"
            danger
            loading={entitySubmitting}
            onClick={() => void retireEntity()}
            data-testid="kg-entity-retire"
          >
            退役
          </Button>,
          <Button
            key="save"
            type="primary"
            loading={entitySubmitting}
            onClick={() => void submitEntityUpdate()}
            data-testid="kg-entity-save"
          >
            保存
          </Button>,
        ]}
        destroyOnHidden
      >
        <div className="flex flex-col gap-3">
          <div>
            <Typography.Text type="secondary">自然键</Typography.Text>
            <div className="font-medium">
              {data ? `${data.center.node_type} / ${data.center.name}` : "—"}
            </div>
          </div>
          <div>
            <Typography.Text type="secondary">显示名称</Typography.Text>
            <Input
              className="mt-1"
              value={entityDisplayName}
              onChange={(event) => setEntityDisplayName(event.target.value)}
              data-testid="kg-entity-display-name"
            />
          </div>
        </div>
      </Modal>

      <Modal
        title="冲突裁决"
        open={conflictOpen}
        onCancel={() => setConflictOpen(false)}
        footer={null}
        width={720}
        destroyOnHidden
      >
        {conflictLoading ? (
          <Spin />
        ) : conflicts.length === 0 ? (
          <Empty description="暂无待裁决冲突" />
        ) : (
          <List
            dataSource={conflicts}
            renderItem={(item) => (
              <List.Item
                actions={[
                  <Button
                    key="dismiss"
                    size="small"
                    loading={decidingConflictId === item.id}
                    onClick={() => void dismissConflict(item.id)}
                    data-testid={`kg-conflict-dismiss-${item.id}`}
                  >
                    忽略
                  </Button>,
                ]}
              >
                <List.Item.Meta
                  title={`${item.conflict_type} · ${item.subject_key}`}
                  description={item.detected_at}
                />
              </List.Item>
            )}
          />
        )}
      </Modal>

      <Modal
        title="手动新增关系"
        open={manualOpen}
        onCancel={() => setManualOpen(false)}
        footer={[
          <Button key="cancel" onClick={() => setManualOpen(false)}>
            取消
          </Button>,
          <Button
            key="submit"
            type="primary"
            loading={manualSubmitting}
            disabled={!selectedRelation || !targetName.trim() || !fact.trim()}
            onClick={() => void submitManual()}
            data-testid="kg-manual-submit"
          >
            写入图谱
          </Button>,
        ]}
        destroyOnHidden
      >
        <div className="flex flex-col gap-3" data-testid="kg-manual-edit-modal">
          <div>
            <Typography.Text type="secondary">起点实体</Typography.Text>
            <div className="font-medium">
              {data ? `${displayNode(data)}（${data.center.node_type}）` : "—"}
            </div>
          </div>
          <div>
            <Typography.Text type="secondary">关系类型</Typography.Text>
            <Select
              className="mt-1 w-full"
              value={relationKey}
              options={relations.map((item) => ({
                value: item.key,
                label: `${item.label}（${item.key}）`,
              }))}
              onChange={setRelationKey}
            />
          </div>
          <div>
            <Typography.Text type="secondary">
              终点实体（{selectedRelation?.target_type ?? "—"}）
            </Typography.Text>
            <Input
              className="mt-1"
              value={targetName}
              onChange={(event) => setTargetName(event.target.value)}
              placeholder="输入终点实体名称"
              data-testid="kg-manual-target-name"
            />
          </div>
          <div>
            <Typography.Text type="secondary">事实描述</Typography.Text>
            <Input.TextArea
              className="mt-1"
              value={fact}
              onChange={(event) => setFact(event.target.value)}
              placeholder="用一句完整陈述描述该关系"
              autoSize={{ minRows: 2, maxRows: 5 }}
              data-testid="kg-manual-fact"
            />
          </div>
        </div>
      </Modal>

      <Modal
        title="图谱变更历史"
        open={historyOpen}
        onCancel={() => setHistoryOpen(false)}
        footer={null}
        width={760}
        destroyOnHidden
      >
        {historyLoading ? (
          <div className="flex min-h-32 items-center justify-center">
            <Spin />
          </div>
        ) : history.length === 0 ? (
          <Empty description="暂无图谱变更" />
        ) : (
          <List
            dataSource={history}
            renderItem={(item) => {
              const canCompensate =
                item.status === "applied" &&
                item.revision != null &&
                item.actor_type !== "system" &&
                data != null;
              return (
                <List.Item
                  key={item.id}
                  actions={
                    canCompensate
                      ? [
                          <Button
                            key="undo"
                            size="small"
                            loading={historyAction === `undo:${item.id}`}
                            onClick={() => void compensate(item, "undo")}
                            data-testid={`kg-undo-${item.revision}`}
                          >
                            撤销
                          </Button>,
                          <Button
                            key="redo"
                            size="small"
                            loading={historyAction === `redo:${item.id}`}
                            onClick={() => void compensate(item, "redo")}
                            data-testid={`kg-redo-${item.revision}`}
                          >
                            重做
                          </Button>,
                        ]
                      : undefined
                  }
                >
                  <Space direction="vertical" size={2}>
                    <Typography.Text strong>
                      revision {item.revision ?? "—"} · {item.summary || "未命名变更"}
                    </Typography.Text>
                    <Typography.Text type="secondary" className="!text-xs">
                      {item.actor_type} / {item.actor_id} · {item.status}
                    </Typography.Text>
                  </Space>
                </List.Item>
              );
            }}
          />
        )}
      </Modal>

      <Modal
        title="Agent 图谱变更待审批"
        open={inboxOpen}
        onCancel={() => setInboxOpen(false)}
        footer={null}
        width={720}
        destroyOnHidden
      >
        {inboxLoading ? (
          <div className="flex min-h-32 items-center justify-center">
            <Spin />
          </div>
        ) : pending.length === 0 ? (
          <Empty description="暂无待审批草案" />
        ) : (
          <List
            dataSource={pending}
            renderItem={(item) => (
              <List.Item
                key={item.id}
                actions={[
                  <Button
                    key="reject"
                    danger
                    loading={decidingId === item.id}
                    onClick={() => void decide(item, "reject")}
                    data-testid={`kg-reject-${item.id}`}
                  >
                    拒绝
                  </Button>,
                  <Button
                    key="approve"
                    type="primary"
                    loading={decidingId === item.id}
                    onClick={() => void decide(item, "approve")}
                    data-testid={`kg-approve-${item.id}`}
                  >
                    批准并写入
                  </Button>,
                ]}
              >
                <Space direction="vertical" size={4} className="w-full">
                  <Typography.Text strong>{item.summary || "未命名草案"}</Typography.Text>
                  <Typography.Text type="secondary" className="!text-xs">
                    {item.actor_id} · 基于 revision {item.base_revision}
                  </Typography.Text>
                  {(item.operations ?? []).map((operation, index) => (
                    <div
                      key={`${item.id}-${index}`}
                      className="rounded-lg border border-shell-line px-3 py-2"
                    >
                      <Typography.Text>
                        {"fact" in operation && operation.fact
                          ? operation.fact
                          : operation.op === "retract_relation"
                            ? `失效关系 ${operation.edge_id}`
                            : `修订关系 ${operation.edge_id}`}
                      </Typography.Text>
                      {operation.op === "create_relation" ? (
                        <div className="text-xs text-gray-500">
                          {operation.source.name} — {operation.relation} →{" "}
                          {operation.target.name}
                        </div>
                      ) : null}
                    </div>
                  ))}
                </Space>
              </List.Item>
            )}
          />
        )}
      </Modal>
    </>
  );
}

export default KnowledgeGraphEditingActions;
