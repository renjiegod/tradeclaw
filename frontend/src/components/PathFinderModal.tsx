import { NodeIndexOutlined, RightOutlined } from "@ant-design/icons";
import { Button, Empty, Input, Modal, Segmented, Tag, Typography, message } from "antd";
import { Fragment, type ReactNode, useCallback, useState } from "react";

import { ApiError, findKnowledgeGraphPath } from "../api";
import type { KgEdge, KgNode, KnowledgeGraphPath } from "../types";

type Props = {
  /** 当前中心实体名，作为「起点」默认值。 */
  defaultSource: string;
  includeExpired: boolean;
  relationLabel: (relation: string) => string;
  nodeStyle: (nodeType: string) => { color: string; label: string };
  /** 命中路径后回调，把路径节点/边 id 交给面板做高亮。 */
  onHighlight: (nodeIds: string[], edgeIds: string[]) => void;
};

/**
 * 两实体最短路查找 —— 「这只票和那个题材/周期是怎么连起来的」。
 *
 * 调 ``GET /knowledge/graph/path``（双向 BFS 最短路）；命中后把路径渲染成
 * 有序「节点链」（节点 chip + 关系标签串联），并可一键在主图里高亮该路径。
 * ``found=false`` 显示「N 跳内不可达」；端点解析不出（404）给友好提示。
 * 纯读，不改图。
 */
export function PathFinderModal({
  defaultSource,
  includeExpired,
  relationLabel,
  nodeStyle,
  onHighlight,
}: Props) {
  const [open, setOpen] = useState(false);
  const [source, setSource] = useState(defaultSource);
  const [target, setTarget] = useState("");
  const [maxHops, setMaxHops] = useState(6);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<KnowledgeGraphPath | null>(null);
  const [notFound, setNotFound] = useState<string | null>(null);

  const openModal = useCallback(() => {
    setSource(defaultSource);
    setTarget("");
    setResult(null);
    setNotFound(null);
    setOpen(true);
  }, [defaultSource]);

  const run = useCallback(async () => {
    const src = source.trim();
    const dst = target.trim();
    if (!src || !dst) return;
    setLoading(true);
    setResult(null);
    setNotFound(null);
    try {
      const res = await findKnowledgeGraphPath(src, dst, {
        maxHops,
        includeExpired,
      });
      setResult(res);
    } catch (error: unknown) {
      if (error instanceof ApiError && error.status === 404) {
        setNotFound(
          error.hint ??
            "起点或终点在图谱里找不到——换股票代码 / 全名 / 角色词 / YYYY-MM 重试，或先「同步投影」。",
        );
      } else {
        const detail = error instanceof Error ? error.message : String(error);
        message.error(`寻路失败：${detail}`);
      }
    } finally {
      setLoading(false);
    }
  }, [source, target, maxHops, includeExpired]);

  const highlight = useCallback(() => {
    if (!result || !result.found) return;
    onHighlight(
      result.path_node_ids,
      result.edges.map((e) => e.id),
    );
    setOpen(false);
  }, [result, onHighlight]);

  const chain = result?.found ? renderChain(result, relationLabel, nodeStyle) : null;

  return (
    <>
      <Button
        size="small"
        icon={<NodeIndexOutlined />}
        onClick={openModal}
        data-testid="kg-pathfinder-open"
      >
        寻路
      </Button>
      <Modal
        title="两实体最短路"
        open={open}
        onCancel={() => setOpen(false)}
        footer={null}
        width={640}
        destroyOnHidden
      >
        <div className="flex flex-col gap-3" data-testid="kg-pathfinder">
          <Typography.Text type="secondary" className="!text-xs">
            查两个实体之间最短的关系链——它们是"怎么连起来"的。纯读，不改图。
          </Typography.Text>
          <div className="flex flex-wrap items-center gap-2">
            <Input
              className="max-w-[180px]"
              value={source}
              onChange={(e) => setSource(e.target.value)}
              placeholder="起点：代码 / 名称 / 角色 / YYYY-MM"
              data-testid="kg-pathfinder-source"
            />
            <RightOutlined className="text-shell-line" />
            <Input
              className="max-w-[180px]"
              value={target}
              onChange={(e) => setTarget(e.target.value)}
              onPressEnter={() => void run()}
              placeholder="终点：代码 / 名称 / 角色 / YYYY-MM"
              data-testid="kg-pathfinder-target"
            />
            <span className="flex items-center gap-1 text-xs">
              <Typography.Text type="secondary" className="!text-xs">
                最大跳
              </Typography.Text>
              <Segmented
                size="small"
                options={[4, 6, 8]}
                value={maxHops}
                onChange={(v) => setMaxHops(Number(v))}
              />
            </span>
            <Button
              type="primary"
              size="small"
              loading={loading}
              disabled={!source.trim() || !target.trim()}
              onClick={() => void run()}
              data-testid="kg-pathfinder-run"
            >
              寻路
            </Button>
          </div>

          {notFound ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={notFound}
              data-testid="kg-pathfinder-notfound"
            />
          ) : result == null ? null : !result.found ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              data-testid="kg-pathfinder-unreachable"
              description={`${result.source.display_name || result.source.name} 与 ${
                result.target.display_name || result.target.name
              } 在 ${maxHops} 跳内不可达（可调大最大跳，或先「同步投影」补边）`}
            />
          ) : (
            <div className="flex flex-col gap-3" data-testid="kg-pathfinder-result">
              <Typography.Text type="secondary" className="!text-xs">
                {result.hops} 跳可达
              </Typography.Text>
              <div className="flex flex-wrap items-center gap-1.5">{chain}</div>
              <div>
                <Button
                  size="small"
                  onClick={highlight}
                  data-testid="kg-pathfinder-highlight"
                >
                  在图中高亮路径
                </Button>
              </div>
            </div>
          )}
        </div>
      </Modal>
    </>
  );
}

function renderChain(
  path: KnowledgeGraphPath,
  relationLabel: (relation: string) => string,
  nodeStyle: (nodeType: string) => { color: string; label: string },
) {
  const nodesById = new Map<string, KgNode>();
  for (const node of path.nodes) nodesById.set(node.id, node);

  const edgeBetween = (a: string, b: string): KgEdge | null => {
    for (const edge of path.edges) {
      if (
        (edge.src_id === a && edge.dst_id === b) ||
        (edge.src_id === b && edge.dst_id === a)
      ) {
        return edge;
      }
    }
    return null;
  };

  const chip = (id: string) => {
    const node = nodesById.get(id);
    if (!node) return <Tag key={id}>{id}</Tag>;
    const style = nodeStyle(node.node_type);
    return (
      <Tag key={id} color={style.color} className="!m-0">
        {style.label} · {node.display_name || node.name}
      </Tag>
    );
  };

  const items: ReactNode[] = [];
  path.path_node_ids.forEach((id, index) => {
    items.push(chip(id));
    if (index < path.path_node_ids.length - 1) {
      const next = path.path_node_ids[index + 1];
      const edge = edgeBetween(id, next);
      items.push(
        <span
          key={`rel-${id}-${next}`}
          className="flex items-center gap-0.5 text-xs text-shell-muted"
        >
          <RightOutlined className="!text-[10px]" />
          {edge ? relationLabel(edge.relation) : "关联"}
          <RightOutlined className="!text-[10px]" />
        </span>,
      );
    }
  });
  return items.map((item, index) => <Fragment key={index}>{item}</Fragment>);
}

export default PathFinderModal;
