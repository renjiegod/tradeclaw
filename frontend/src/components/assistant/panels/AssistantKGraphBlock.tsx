// frontend/src/components/assistant/panels/AssistantKGraphBlock.tsx
//
// Agent 面板的知识图谱块：引用式（entity → getKnowledgeGraph 拉 N 跳邻域）
// 或内联式（直接给 nodes/edges）。渲染复用 KnowledgeGraphSvg（确定性布局 + SVG）。

import { Alert, Spin } from "antd";
import { useEffect, useMemo, useState } from "react";

import { getKnowledgeGraph } from "../../../api";
import type { KgEdge, KgNode } from "../../../types";
import { KnowledgeGraphSvg } from "../../KnowledgeGraphSvg";
import type { KGraphBlock, KGraphEdgeLite, KGraphNodeLite } from "./panelSpec";

function liteToNode(node: KGraphNodeLite): KgNode {
  return {
    id: node.id,
    node_type: node.node_type,
    name: node.name,
    display_name: node.display_name ?? null,
    attrs: null,
  };
}

function liteToEdge(edge: KGraphEdgeLite): KgEdge {
  return {
    id: edge.id,
    src_id: edge.src_id,
    dst_id: edge.dst_id,
    relation: edge.relation,
    fact: edge.fact ?? "",
    attrs: null,
    provenance: "deterministic",
    confidence: null,
    source_ref: null,
    valid_at: null,
    invalid_at: null,
    created_at: null,
    expired_at: null,
  };
}

export function AssistantKGraphBlock({ block }: { block: KGraphBlock }) {
  const inlineNodes = block.nodes ?? [];
  const isInline = inlineNodes.length > 0;

  const [fetched, setFetched] = useState<{ nodes: KgNode[]; edges: KgEdge[]; centerId: string } | null>(
    null,
  );
  const [loading, setLoading] = useState(!isInline);
  const [errorMessage, setErrorMessage] = useState("");

  useEffect(() => {
    if (isInline || !block.entity) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setErrorMessage("");
    setFetched(null);
    getKnowledgeGraph(block.entity, { hops: block.hops, includeExpired: block.include_expired })
      .then((result) => {
        if (cancelled) return;
        setFetched({ nodes: result.nodes, edges: result.edges, centerId: result.center.id });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setErrorMessage(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [isInline, block.entity, block.hops, block.include_expired]);

  const inline = useMemo(() => {
    if (!isInline) return null;
    return {
      nodes: inlineNodes.map(liteToNode),
      edges: (block.edges ?? []).map(liteToEdge),
      centerId: block.center_id,
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isInline, block.nodes, block.edges, block.center_id]);

  if (errorMessage) {
    return (
      <Alert
        type="warning"
        showIcon
        message={`知识图谱查询失败${block.entity ? `：${block.entity}` : ""}`}
        description={errorMessage}
      />
    );
  }

  if (loading) {
    return (
      <div className="flex min-h-[160px] items-center justify-center rounded-lg border border-shell-line bg-gray-50">
        <Spin />
      </div>
    );
  }

  const graph = inline ?? fetched;
  if (!graph) {
    return (
      <div className="flex min-h-[120px] items-center justify-center rounded-lg border border-shell-line bg-gray-50 text-sm text-gray-400">
        暂无图谱数据
      </div>
    );
  }

  return (
    <KnowledgeGraphSvg
      nodes={graph.nodes}
      edges={graph.edges}
      centerId={graph.centerId}
      layout={block.layout}
      colorMode={block.color_mode}
      height={block.height}
    />
  );
}
