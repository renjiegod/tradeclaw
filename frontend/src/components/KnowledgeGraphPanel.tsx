import {
  ApartmentOutlined,
  ReloadOutlined,
  SyncOutlined,
} from "@ant-design/icons";
import {
  Button,
  Card,
  Empty,
  Input,
  Segmented,
  Spin,
  Switch,
  Tag,
  Typography,
  message,
} from "antd";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  ApiError,
  getKnowledgeGraph,
  getKnowledgeGraphLayout,
  saveKnowledgeGraphLayout,
  syncKnowledgeGraph,
} from "../api";
import type { KgEdge, KgNode, KnowledgeGraphNeighborhood } from "../types";
import { KnowledgeGraphEditingActions } from "./KnowledgeGraphEditingActions";
import { ManualRelationActions } from "./ManualRelationActions";
import { layoutNeighborhood } from "./knowledgeGraphLayout";

const SVG_WIDTH = 760;
const SVG_HEIGHT = 520;

/**
 * 节点类型 → 颜色。Categorical 调色板按固定顺序分配给固定类型（永不因
 * 出现顺序轮换），已通过 dataviz 六项校验（light surface #fffdf9）：
 * 亮度带 / 彩度下限 / CVD 相邻分离 / 正常视觉下限 / 对比度全 PASS。
 * 每个节点都直接标注名称（secondary encoding），身份从不只靠颜色。
 */
const NODE_TYPE_STYLES: Record<string, { color: string; label: string }> = {
  symbol: { color: "#b26a1f", label: "个股" },
  theme: { color: "#b8508f", label: "题材" },
  cycle: { color: "#3b6fd4", label: "周期月" },
  role: { color: "#2f8f6b", label: "角色" },
  playbook: { color: "#6b7f2e", label: "战法" },
  signal: { color: "#7b5fc0", label: "信号" },
};
const FALLBACK_NODE_STYLE = { color: "#8a7a63", label: "其他" };

const RELATION_LABELS: Record<string, string> = {
  has_role: "担任角色",
  traded_in: "交易于",
  signals: "决策信号",
  belongs_to_theme: "属于题材",
  leads_theme: "题材龙头",
  uses_playbook: "使用战法",
  linked_with: "个股联动",
  observed_in: "活跃于周期",
};

function nodeStyle(nodeType: string) {
  return NODE_TYPE_STYLES[nodeType] ?? FALLBACK_NODE_STYLE;
}

function nodeLabel(node: KgNode): string {
  if (node.display_name && node.display_name !== node.name) {
    return node.display_name;
  }
  return node.name;
}

function relationLabel(relation: string): string {
  return RELATION_LABELS[relation] ?? relation;
}

function provenanceLabel(provenance: KgEdge["provenance"]): string {
  if (provenance === "llm") return "LLM 观点";
  if (provenance === "manual") return "人工确认";
  return "硬数据";
}

function formatWindow(edge: KgEdge): string {
  const day = (value: string | null) => (value ? value.slice(0, 10) : null);
  const start = day(edge.valid_at);
  const end = day(edge.invalid_at);
  if (start && end) return start === end ? start : `${start} → ${end}`;
  if (start) return `${start} 起`;
  if (end) return `至 ${end}`;
  return "时间未知";
}

/**
 * 知识图谱面板 — 知识库页的「图谱」tab。
 *
 * 输入实体（代码 / 名称 / 角色词 / YYYY-MM / 信号 id）查询其 N 跳邻域，
 * 左侧 SVG 力导向图（节点色 = 实体类型；实线 = 硬数据投影、虚线 = LLM
 * 观点候选、灰 = 已失效历史），右侧按关系分组的事实句列表（时间窗 /
 * provenance / confidence / 来源）。「同步投影」按钮幂等重建确定性投影
 * ——查不到刚写入的数据时先同步再查。纯 SVG + 自写确定性布局，无图库
 * 依赖（与本项目「无 chart 依赖」偏好一致，布局见
 * {@link layoutNeighborhood}）。
 */
export function KnowledgeGraphPanel() {
  const [entity, setEntity] = useState("");
  const [hops, setHops] = useState<number>(1);
  const [includeExpired, setIncludeExpired] = useState(false);
  const [data, setData] = useState<KnowledgeGraphNeighborhood | null>(null);
  const [loading, setLoading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [notFoundEntity, setNotFoundEntity] = useState<string | null>(null);
  const [hoveredEdgeId, setHoveredEdgeId] = useState<string | null>(null);
  const [positionOverrides, setPositionOverrides] = useState<
    Map<string, { x: number; y: number }>
  >(new Map());
  const [lockedIds, setLockedIds] = useState<Set<string>>(new Set());
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [highlightIds, setHighlightIds] = useState<Set<string>>(new Set());
  const [savingLayout, setSavingLayout] = useState(false);
  const dragRef = useRef<{
    nodeId: string;
    pointerId: number;
    originX: number;
    originY: number;
    startX: number;
    startY: number;
  } | null>(null);

  const load = useCallback(
    async (query: string, nextHops: number, nextExpired: boolean) => {
      const text = query.trim();
      if (!text) return;
      setLoading(true);
      setNotFoundEntity(null);
      try {
        const res = await getKnowledgeGraph(text, {
          hops: nextHops,
          includeExpired: nextExpired,
        });
        setData(res);
        setPositionOverrides(new Map());
        setSelectedIds(new Set());
        setHighlightIds(new Set());
        try {
          const layoutRes = await getKnowledgeGraphLayout(res.center.id);
          if (layoutRes.layout) {
            setPositionOverrides(
              new Map(Object.entries(layoutRes.layout.positions)),
            );
            setLockedIds(new Set(layoutRes.layout.locked_ids));
            setHighlightIds(new Set(layoutRes.layout.highlight_ids));
          } else {
            setLockedIds(new Set([res.center.id]));
          }
        } catch {
          setLockedIds(new Set([res.center.id]));
        }
      } catch (error: unknown) {
        if (error instanceof ApiError && error.status === 404) {
          setData(null);
          setNotFoundEntity(text);
        } else {
          const msg = error instanceof Error ? error.message : String(error);
          message.error(`加载知识图谱失败：${msg}`);
        }
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  const runSync = useCallback(async () => {
    setSyncing(true);
    try {
      const res = await syncKnowledgeGraph();
      if (res.skipped) {
        message.info("图谱已是最新（所有来源自上次同步未变化）");
      } else {
        const applied = res.apply;
        message.success(
          `图谱同步完成：边 +${applied?.edges_created ?? 0}` +
            `（失效 ${applied?.edges_expired ?? 0}）· ` +
            `共 ${res.counts?.nodes ?? "?"} 节点 / ${res.counts?.active_edges ?? "?"} 有效边`,
        );
      }
      // 同步后若已有查询上下文则自动重查（覆盖 not-found 重试场景）。
      const retry = entity.trim() || notFoundEntity;
      if (retry) {
        await load(retry, hops, includeExpired);
      }
    } catch (error: unknown) {
      const msg = error instanceof Error ? error.message : String(error);
      message.error(`图谱同步失败：${msg}`);
    } finally {
      setSyncing(false);
    }
  }, [entity, notFoundEntity, hops, includeExpired, load]);

  const positions = useMemo(() => {
    if (!data) return new Map<string, { x: number; y: number }>();
    return layoutNeighborhood(data.nodes, data.edges, {
      width: SVG_WIDTH,
      height: SVG_HEIGHT,
      centerId: data.center.id,
      lockedIds,
      seedPositions: positionOverrides,
    });
  }, [data, lockedIds, positionOverrides]);

  useEffect(() => {
    if (!data) return;
    if (selectedIds.size === 0) {
      setHighlightIds(new Set());
      return;
    }
    const next = new Set<string>();
    for (const id of selectedIds) {
      next.add(id);
      for (const edge of data.edges) {
        if (edge.src_id === id || edge.dst_id === id) {
          next.add(edge.src_id);
          next.add(edge.dst_id);
        }
      }
    }
    setHighlightIds(next);
  }, [data, selectedIds]);

  const nodesById = useMemo(() => {
    const map = new Map<string, KgNode>();
    for (const node of data?.nodes ?? []) map.set(node.id, node);
    return map;
  }, [data]);

  const presentTypes = useMemo(() => {
    const seen = new Set<string>();
    for (const node of data?.nodes ?? []) seen.add(node.node_type);
    // 图例顺序跟随固定的类型→颜色分配顺序，而不是数据出现顺序。
    return Object.keys(NODE_TYPE_STYLES)
      .filter((t) => seen.has(t))
      .concat([...seen].filter((t) => !(t in NODE_TYPE_STYLES)));
  }, [data]);

  const factGroups = useMemo(() => {
    const groups = new Map<string, KgEdge[]>();
    for (const edge of data?.edges ?? []) {
      const list = groups.get(edge.relation) ?? [];
      list.push(edge);
      groups.set(edge.relation, list);
    }
    return [...groups.entries()];
  }, [data]);

  const onSearch = (value: string) => {
    setEntity(value);
    void load(value, hops, includeExpired);
  };

  const reQuery = (nextHops: number, nextExpired: boolean) => {
    const current = (entity || data?.center.name || "").trim();
    if (current) void load(current, nextHops, nextExpired);
  };

  const reloadCurrent = useCallback(async () => {
    const current = (entity || data?.center.name || "").trim();
    if (current) {
      await load(current, hops, includeExpired);
    }
  }, [data?.center.name, entity, hops, includeExpired, load]);

  const saveLayout = useCallback(async () => {
    if (!data) return;
    setSavingLayout(true);
    try {
      const payload: Record<string, { x: number; y: number }> = {};
      for (const [id, pos] of positions.entries()) {
        payload[id] = pos;
      }
      await saveKnowledgeGraphLayout(
        data.center.id,
        payload,
        [...lockedIds],
        [...highlightIds],
        data.revision,
      );
      message.success("画布布局已保存");
      await reloadCurrent();
    } catch (error: unknown) {
      const detail = error instanceof Error ? error.message : String(error);
      message.error(`保存布局失败：${detail}`);
    } finally {
      setSavingLayout(false);
    }
  }, [data, highlightIds, lockedIds, positions, reloadCurrent]);

  return (
    <Card
      className="!border !border-shell-line !bg-card-bg shadow-shell-card"
      title={
        <div className="flex flex-col">
          <Typography.Text strong>知识图谱</Typography.Text>
          <Typography.Text type="secondary" className="!text-xs !font-normal">
            个股 ↔ 角色 ↔ 题材 ↔ 周期 ↔ 交易 ↔ 信号 · 事实带时间窗与来源
          </Typography.Text>
        </div>
      }
      extra={
        <div className="flex items-center gap-2">
          <KnowledgeGraphEditingActions data={data} onChanged={reloadCurrent} />
          <Button
            size="small"
            disabled={!data}
            loading={savingLayout}
            onClick={() => void saveLayout()}
            data-testid="kg-save-layout"
          >
            保存布局
          </Button>
          <Button
            size="small"
            icon={<SyncOutlined />}
            loading={syncing}
            onClick={() => void runSync()}
            data-testid="kg-sync"
          >
            同步投影
          </Button>
          <Button
            size="small"
            icon={<ReloadOutlined />}
            loading={loading}
            disabled={!entity.trim() && !data}
            onClick={() => reQuery(hops, includeExpired)}
            data-testid="kg-refresh"
          >
            刷新
          </Button>
        </div>
      }
      data-testid="knowledge-graph-panel"
    >
      <div className="flex flex-col gap-4">
        {/* 查询控制行 */}
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <Input.Search
            className="max-w-xs"
            placeholder="股票代码 / 名称 / 角色词 / YYYY-MM / 信号 id"
            allowClear
            enterButton="查询"
            value={entity}
            onChange={(e) => setEntity(e.target.value)}
            onSearch={onSearch}
            loading={loading}
            data-testid="kg-entity-input"
          />
          <span className="flex items-center gap-2 text-xs">
            <Typography.Text type="secondary" className="!text-xs">
              跳数
            </Typography.Text>
            <Segmented
              size="small"
              options={[1, 2, 3]}
              value={hops}
              onChange={(value) => {
                const next = Number(value);
                setHops(next);
                reQuery(next, includeExpired);
              }}
              data-testid="kg-hops"
            />
          </span>
          <span className="flex items-center gap-2 text-xs">
            <Typography.Text type="secondary" className="!text-xs">
              含已失效历史
            </Typography.Text>
            <Switch
              size="small"
              checked={includeExpired}
              onChange={(checked) => {
                setIncludeExpired(checked);
                reQuery(hops, checked);
              }}
              data-testid="kg-include-expired"
            />
          </span>
        </div>

        {loading ? (
          <div className="flex min-h-[240px] items-center justify-center">
            <Spin />
          </div>
        ) : notFoundEntity ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            data-testid="kg-not-found"
            description={
              <span>
                图谱里没有「{notFoundEntity}」——若数据是新写入的，先
                <Button
                  type="link"
                  size="small"
                  className="!px-1"
                  onClick={() => void runSync()}
                >
                  同步投影
                </Button>
                再查；也可换股票代码 / 全名重试。
              </span>
            }
          />
        ) : !data ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="输入实体开始探索：一只票的历史角色、题材归属、交易盈亏与相关信号"
            data-testid="kg-empty"
          />
        ) : (
          <div className="flex flex-col gap-4 lg:flex-row">
            {/* 左：SVG 力导向子图 */}
            <div className="min-w-0 flex-1">
              <svg
                viewBox={`0 0 ${SVG_WIDTH} ${SVG_HEIGHT}`}
                className="h-auto w-full rounded-card border border-shell-line"
                role="img"
                aria-label={`${nodeLabel(data.center)} 的知识图谱邻域`}
                data-testid="kg-svg"
                onPointerMove={(event) => {
                  const drag = dragRef.current;
                  if (!drag || event.pointerId !== drag.pointerId) return;
                  const svg = event.currentTarget;
                  const rect = svg.getBoundingClientRect();
                  const scaleX = SVG_WIDTH / rect.width;
                  const scaleY = SVG_HEIGHT / rect.height;
                  const dx = (event.clientX - drag.originX) * scaleX;
                  const dy = (event.clientY - drag.originY) * scaleY;
                  setPositionOverrides((prev) => {
                    const next = new Map(prev);
                    next.set(drag.nodeId, {
                      x: drag.startX + dx,
                      y: drag.startY + dy,
                    });
                    return next;
                  });
                  setLockedIds((prev) => new Set(prev).add(drag.nodeId));
                }}
                onPointerUp={(event) => {
                  if (dragRef.current?.pointerId === event.pointerId) {
                    dragRef.current = null;
                  }
                }}
                onPointerLeave={() => {
                  dragRef.current = null;
                }}
              >
                {data.edges.map((edge) => {
                  const a = positions.get(edge.src_id);
                  const b = positions.get(edge.dst_id);
                  if (!a || !b) return null;
                  const expired = edge.expired_at != null;
                  const hovered = hoveredEdgeId === edge.id;
                  const highlighted =
                    highlightIds.has(edge.src_id) && highlightIds.has(edge.dst_id);
                  return (
                    <g key={edge.id}>
                      <line
                        x1={a.x}
                        y1={a.y}
                        x2={b.x}
                        y2={b.y}
                        stroke={
                          highlighted
                            ? "#c45c26"
                            : expired
                              ? "#b3a68f"
                              : "#8a7a63"
                        }
                        strokeWidth={highlighted || hovered ? 3 : 2}
                        strokeOpacity={expired ? 0.45 : highlighted ? 0.95 : 0.75}
                        strokeDasharray={edge.provenance === "llm" ? "6 4" : undefined}
                        onMouseEnter={() => setHoveredEdgeId(edge.id)}
                        onMouseLeave={() => setHoveredEdgeId(null)}
                        data-testid={`kg-edge-${edge.id}`}
                      >
                        <title>{`${relationLabel(edge.relation)}：${edge.fact}`}</title>
                      </line>
                    </g>
                  );
                })}
                {data.nodes.map((node) => {
                  const p = positions.get(node.id);
                  if (!p) return null;
                  const isCenter = node.id === data.center.id;
                  const style = nodeStyle(node.node_type);
                  const label = nodeLabel(node);
                  const selected = selectedIds.has(node.id);
                  const locked = lockedIds.has(node.id);
                  const highlighted = highlightIds.has(node.id);
                  return (
                    <g
                      key={node.id}
                      transform={`translate(${p.x}, ${p.y})`}
                      className="cursor-pointer"
                      onClick={(event) => {
                        if (event.shiftKey) {
                          setSelectedIds((prev) => {
                            const next = new Set(prev);
                            if (next.has(node.id)) next.delete(node.id);
                            else next.add(node.id);
                            return next;
                          });
                          return;
                        }
                        if (!isCenter) {
                          setEntity(node.name);
                          void load(node.name, hops, includeExpired);
                        }
                      }}
                      onPointerDown={(event) => {
                        if (event.button !== 0 || event.shiftKey) return;
                        event.preventDefault();
                        event.currentTarget.setPointerCapture(event.pointerId);
                        dragRef.current = {
                          nodeId: node.id,
                          pointerId: event.pointerId,
                          originX: event.clientX,
                          originY: event.clientY,
                          startX: p.x,
                          startY: p.y,
                        };
                      }}
                      data-testid={`kg-node-${node.id}`}
                    >
                      <title>{`${style.label}：${label}（${node.name}）`}</title>
                      <circle
                        r={isCenter ? 15 : 10}
                        fill={style.color}
                        stroke={
                          selected || highlighted
                            ? "#c45c26"
                            : locked
                              ? "#2f8f6b"
                              : "#fffdf9"
                        }
                        strokeWidth={selected || highlighted || locked ? 3 : 2}
                      />
                      <text
                        y={isCenter ? 30 : 24}
                        textAnchor="middle"
                        fontSize={12}
                        fill="#4a3f33"
                        fontWeight={isCenter ? 600 : 400}
                      >
                        {label.length > 10 ? `${label.slice(0, 10)}…` : label}
                      </text>
                    </g>
                  );
                })}
              </svg>

              {/* 图例：节点类型（颜色）+ 边语义（线型），身份不只靠颜色。 */}
              <div
                className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1"
                data-testid="kg-legend"
              >
                {presentTypes.map((type) => {
                  const style = nodeStyle(type);
                  return (
                    <span key={type} className="flex items-center gap-1.5 text-xs">
                      <span
                        className="inline-block h-3 w-3 rounded-full"
                        style={{ backgroundColor: style.color }}
                      />
                      <Typography.Text type="secondary" className="!text-xs">
                        {style.label}
                      </Typography.Text>
                    </span>
                  );
                })}
                <span className="flex items-center gap-1.5 text-xs">
                  <svg width="26" height="8" aria-hidden="true">
                    <line x1="0" y1="4" x2="26" y2="4" stroke="#8a7a63" strokeWidth="2" />
                  </svg>
                  <Typography.Text type="secondary" className="!text-xs">
                    硬数据
                  </Typography.Text>
                </span>
                <span className="flex items-center gap-1.5 text-xs">
                  <svg width="26" height="8" aria-hidden="true">
                    <line
                      x1="0"
                      y1="4"
                      x2="26"
                      y2="4"
                      stroke="#8a7a63"
                      strokeWidth="2"
                      strokeDasharray="5 3"
                    />
                  </svg>
                  <Typography.Text type="secondary" className="!text-xs">
                    LLM 观点
                  </Typography.Text>
                </span>
                {includeExpired ? (
                  <span className="flex items-center gap-1.5 text-xs">
                    <svg width="26" height="8" aria-hidden="true">
                      <line
                        x1="0"
                        y1="4"
                        x2="26"
                        y2="4"
                        stroke="#b3a68f"
                        strokeWidth="2"
                        strokeOpacity="0.6"
                      />
                    </svg>
                    <Typography.Text type="secondary" className="!text-xs">
                      已失效
                    </Typography.Text>
                  </span>
                ) : null}
              </div>

              {data.candidates.length > 0 ? (
                <Typography.Text
                  type="secondary"
                  className="mt-1 block !text-xs"
                  data-testid="kg-candidates"
                >
                  同名候选：
                  {data.candidates.slice(0, 4).map((c) => (
                    <Button
                      key={c.id}
                      type="link"
                      size="small"
                      className="!px-1 !text-xs"
                      onClick={() => {
                        setEntity(c.name);
                        void load(c.name, hops, includeExpired);
                      }}
                    >
                      {nodeLabel(c)}（{nodeStyle(c.node_type).label}）
                    </Button>
                  ))}
                </Typography.Text>
              ) : null}
            </div>

            {/* 右：按关系分组的事实列表 */}
            <div
              className="flex max-h-[560px] w-full flex-col gap-3 overflow-y-auto lg:w-96"
              data-testid="kg-facts"
            >
              {factGroups.length === 0 ? (
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description="这个实体暂无关联事实——先同步投影，或等每日复盘积累"
                />
              ) : (
                factGroups.map(([relation, edges]) => (
                  <div key={relation} className="flex flex-col gap-2">
                    <Typography.Text strong className="!text-sm">
                      <ApartmentOutlined className="mr-1 text-shell-accent" />
                      {relationLabel(relation)}
                      <Typography.Text type="secondary" className="ml-1 !text-xs">
                        {edges.length}
                      </Typography.Text>
                    </Typography.Text>
                    {edges.map((edge) => {
                      const expired = edge.expired_at != null;
                      return (
                        <div
                          key={edge.id}
                          className={`rounded-card border border-shell-line p-2 ${
                            expired ? "opacity-60" : ""
                          } ${hoveredEdgeId === edge.id ? "!border-shell-accent" : ""}`}
                          onMouseEnter={() => setHoveredEdgeId(edge.id)}
                          onMouseLeave={() => setHoveredEdgeId(null)}
                          data-testid="kg-fact-item"
                        >
                          <Typography.Paragraph
                            className={`!mb-1 !text-[13px] ${expired ? "line-through" : ""}`}
                          >
                            {edge.fact}
                          </Typography.Paragraph>
                          <div className="flex flex-wrap items-center gap-1">
                            <Tag className="!text-[11px]">
                              {provenanceLabel(edge.provenance)}
                            </Tag>
                            {edge.confidence != null ? (
                              <Tag className="!text-[11px]">
                                conf {edge.confidence.toFixed(2)}
                              </Tag>
                            ) : null}
                            <Tag className="!text-[11px]">{formatWindow(edge)}</Tag>
                            {expired ? (
                              <Tag color="default" className="!text-[11px]">
                                已失效 {edge.expired_at?.slice(0, 10)}
                              </Tag>
                            ) : null}
                            {edge.source_ref ? (
                              <Typography.Text type="secondary" className="!text-[11px]">
                                {edge.source_ref}
                              </Typography.Text>
                            ) : null}
                            {!expired ? (
                              <ManualRelationActions
                                edge={edge}
                                revision={data.revision}
                                onChanged={reloadCurrent}
                              />
                            ) : null}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                ))
              )}
              <Typography.Text type="secondary" className="!text-[11px]">
                LLM 观点为复盘日记的自动抽取候选（按 confidence
                加权参考）；仅描述历史认知，非预测、非买卖建议。
              </Typography.Text>
            </div>
          </div>
        )}
      </div>
    </Card>
  );
}

export default KnowledgeGraphPanel;
