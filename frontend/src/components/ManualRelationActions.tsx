import { Button, Input, Modal, Popconfirm, message } from "antd";
import { useCallback, useState } from "react";

import { applyKnowledgeGraphChange } from "../api";
import type { KgEdge } from "../types";

type Props = {
  edge: KgEdge;
  revision: number;
  onChanged: () => Promise<void>;
};

export function ManualRelationActions({ edge, revision, onChanged }: Props) {
  const [open, setOpen] = useState(false);
  const [fact, setFact] = useState(edge.fact);
  const [submitting, setSubmitting] = useState(false);
  const isManual = edge.provenance === "manual";

  const revise = useCallback(async () => {
    const text = fact.trim();
    if (!text || text === edge.fact) return;
    setSubmitting(true);
    try {
      if (isManual) {
        await applyKnowledgeGraphChange(
          [{ op: "revise_relation", edge_id: edge.id, fact: text }],
          `修订人工关系：${edge.id}`,
          revision,
        );
        message.success("人工关系已修订");
      } else {
        await applyKnowledgeGraphChange(
          [
            {
              op: "override_relation",
              edge_id: edge.id,
              fact: text,
              confidence: 1,
            },
          ],
          `覆盖自动关系：${edge.id}`,
          revision,
        );
        message.success("已写入人工覆盖，自动投影不会覆盖该事实");
      }
      setOpen(false);
      await onChanged();
    } catch (error: unknown) {
      const detail = error instanceof Error ? error.message : String(error);
      message.error(`关系更新失败：${detail}`);
    } finally {
      setSubmitting(false);
    }
  }, [edge.fact, edge.id, fact, isManual, onChanged, revision]);

  const retract = useCallback(async () => {
    setSubmitting(true);
    try {
      await applyKnowledgeGraphChange(
        [
          {
            op: "retract_relation",
            edge_id: edge.id,
            reason: "本地用户手动失效",
          },
        ],
        `失效人工关系：${edge.id}`,
        revision,
      );
      message.success("人工关系已失效，历史版本仍保留");
      await onChanged();
    } catch (error: unknown) {
      const detail = error instanceof Error ? error.message : String(error);
      message.error(`关系失效失败：${detail}`);
    } finally {
      setSubmitting(false);
    }
  }, [edge.id, onChanged, revision]);

  return (
    <>
      <Button
        type="link"
        size="small"
        className="!h-auto !px-1 !text-xs"
        onClick={() => {
          setFact(edge.fact);
          setOpen(true);
        }}
        data-testid={isManual ? `kg-revise-${edge.id}` : `kg-override-${edge.id}`}
      >
        {isManual ? "修订" : "覆盖"}
      </Button>
      {isManual ? (
        <Popconfirm
          title="确认失效这条人工关系？"
          description="事实历史会保留，可通过 revision 撤销。"
          okText="确认失效"
          cancelText="取消"
          onConfirm={() => void retract()}
        >
          <Button
            type="link"
            size="small"
            danger
            className="!h-auto !px-1 !text-xs"
            loading={submitting}
            data-testid={`kg-retract-${edge.id}`}
          >
            失效
          </Button>
        </Popconfirm>
      ) : null}
      <Modal
        title={isManual ? "修订人工关系" : "覆盖自动关系"}
        open={open}
        onCancel={() => setOpen(false)}
        onOk={() => void revise()}
        confirmLoading={submitting}
        okText={isManual ? "保存修订" : "写入覆盖"}
        okButtonProps={{ "data-testid": `kg-revise-submit-${edge.id}` }}
        destroyOnHidden
      >
        <Input.TextArea
          value={fact}
          onChange={(event) => setFact(event.target.value)}
          autoSize={{ minRows: 3, maxRows: 8 }}
          data-testid={`kg-revise-fact-${edge.id}`}
        />
      </Modal>
    </>
  );
}

export default ManualRelationActions;
