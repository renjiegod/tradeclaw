"""knowledge_graph — 私有知识库之上的实体关系图谱（查询 + 同步）。

与 ``knowledge_index``（文件导航地图）互补的第二层检索面：图谱回答
"这个实体和什么有关"（个股↔角色↔周期↔交易↔信号），返回紧凑事实句
（带时间窗 / provenance / source_ref）；agent 顺着 source_ref 再用
``knowledge_index`` + ``read_file`` 钻取原文细节。

两个动作：

* ``action="query"``（默认）—— 按名称/代码解析实体，取 N 跳邻域子图，
  渲染成 markdown。``include_expired=true`` 时附带已失效的历史认知
  （bi-temporal 回溯——"当时怎么看这只票"）。
* ``action="sync"`` —— 幂等地把确定性来源（roles.jsonl / 情绪时间线 /
  交割单归因 / decision_signals）重新投影进图谱。来源 content_hash 均
  未变时快速跳过；``force=true`` 强制重投影。

Error codes (stable skill-doc tokens):

* ``unknown_arguments`` — kwargs outside the declared schema.
* ``validation_error`` — malformed argument values（含 hops 越界）。
* ``missing_entity`` — query 动作缺 ``entity``。
* ``entity_not_found`` — 图谱里找不到该实体（附相近候选与 sync 提示）。
* ``knowledge_graph_unwired`` — runtime 未装配图谱 repository。
* ``knowledge_graph_failed`` — 底层读写异常（带异常类型与消息）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from doyoutrade.debug import emit_debug_event
from doyoutrade.tools import OperationHandler, ToolResult
from doyoutrade.tools._prose import format_error_text, format_unknown_args

_ACTIONS = ("query", "sync")


class KnowledgeGraphTool(OperationHandler):
    name = "knowledge_graph"
    description = (
        "私有交易知识库的实体关系图谱。action=query（默认）：输入实体"
        "（股票代码/名称/角色/YYYY-MM 周期月/信号 id），返回它的邻域事实——"
        "历史角色、所属周期、交易盈亏、相关决策信号，每条带时间窗和来源，"
        "include_expired=true 可回溯已被推翻的历史认知。"
        "对话涉及『这只票什么来头/我做过它几次/那轮周期我怎么操作的』时，"
        "先查图谱拿关联，再用 knowledge_index + read_file 钻取原文。"
        "action=sync：把 roles/情绪时间线/交割单/决策信号幂等投影进图谱"
        "（来源未变化时自动跳过；数据刚更新后建议先 sync 再 query）。"
    )
    category = "agent"
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action": {
                "type": "string",
                "enum": list(_ACTIONS),
                "description": "query=查询实体邻域（默认）；sync=重建确定性投影。",
            },
            "entity": {
                "type": "string",
                "description": (
                    "要查询的实体：股票代码（如 300059）、名称（如 东方财富）、"
                    "角色词（如 龙头）、周期月（如 2026-03）或信号 id。"
                    "action=query 时必填。"
                ),
            },
            "hops": {
                "type": "integer",
                "minimum": 1,
                "maximum": 3,
                "description": "邻域跳数，默认 1；2 可看到『同周期其他交易』级别的关联。",
            },
            "include_expired": {
                "type": "boolean",
                "description": "true 时附带已失效的历史事实（角色变更史等），默认 false。",
            },
            "force": {
                "type": "boolean",
                "description": "仅 action=sync：忽略来源水位强制重投影，默认 false。",
            },
        },
        "required": [],
    }

    def __init__(self, *, knowledge_graph_repository: Any | None = None):
        self._repository = knowledge_graph_repository

    async def execute(self, **kwargs: Any) -> ToolResult:
        session_id = kwargs.pop("session_id", None)
        base_payload: dict[str, Any] = {
            "tool": self.name,
            "input_keys": sorted(kwargs.keys()),
        }

        contract = self._enforce_kwargs_contract(kwargs)
        if contract.error is not None:
            kind = "rejected" if contract.error_kind == "unknown_arguments" else "failed"
            await emit_debug_event(
                f"operation_{self.name}.{kind}",
                {**base_payload, "session_id": session_id, "error": contract.error},
            )
            if contract.error_kind == "unknown_arguments":
                text = format_unknown_args(
                    list(contract.error.get("unknown", [])),
                    sorted(self._allowed_top_level_kwargs()),
                    dict(contract.error.get("suggested_path") or {}),
                )
            else:
                text = format_error_text(
                    "validation_error",
                    str(contract.error.get("message") or "validation failed"),
                )
            return ToolResult(text=text, is_error=True)

        args = contract.kwargs
        action = args.get("action") or "query"
        if action not in _ACTIONS:
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {**base_payload, "error_code": "validation_error", "action": action},
            )
            return ToolResult(
                text=format_error_text(
                    "validation_error",
                    f"action must be one of {list(_ACTIONS)}, got {action!r}",
                ),
                is_error=True,
            )

        if self._repository is None:
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {
                    **base_payload,
                    "error_code": "knowledge_graph_unwired",
                    "hint": (
                        "knowledge_graph needs the graph repository "
                        "(KnowledgeGraphTool(knowledge_graph_repository=...))"
                    ),
                },
            )
            return ToolResult(
                text=format_error_text(
                    "knowledge_graph_unwired",
                    "this runtime has no knowledge-graph persistence wiring; "
                    "graph queries are unavailable here.",
                ),
                is_error=True,
            )

        await emit_debug_event(
            f"operation_{self.name}.request",
            {**base_payload, "session_id": session_id, "action": action},
        )
        if action == "sync":
            return await self._execute_sync(base_payload, force=bool(args.get("force")))
        return await self._execute_query(
            base_payload,
            entity=args.get("entity"),
            hops=args.get("hops"),
            include_expired=bool(args.get("include_expired")),
        )

    # -- sync ---------------------------------------------------------------

    async def _execute_sync(self, base_payload: dict[str, Any], *, force: bool) -> ToolResult:
        from doyoutrade.knowledge.graph import sync_deterministic_projection
        from doyoutrade.tools._sandbox import knowledge_root

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            result = await sync_deterministic_projection(
                self._repository, knowledge_root(), now=now, force=force
            )
        except Exception as exc:
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {
                    **base_payload,
                    "error_code": "knowledge_graph_failed",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
            return ToolResult(
                text=format_error_text(
                    "knowledge_graph_failed",
                    f"projection sync failed ({type(exc).__name__}): {exc}",
                ),
                is_error=True,
            )

        await emit_debug_event(
            f"operation_{self.name}.created",
            {
                **base_payload,
                "action": "sync",
                "skipped": result["skipped"],
                "changed_sources": result["changed_sources"],
                "apply": result.get("apply"),
                "counts": result.get("counts"),
                "warning_count": len(result.get("warnings") or []),
            },
        )

        counts = result.get("counts") or {}
        size_line = (
            f"图谱规模：{counts.get('nodes', '?')} 节点 / "
            f"{counts.get('active_edges', '?')} 有效边 / "
            f"{counts.get('expired_edges', '?')} 历史边"
        )
        if result["skipped"]:
            return ToolResult(
                text=(
                    "知识图谱同步：所有来源自上次同步以来未变化，已跳过（幂等）。\n"
                    f"{size_line}\n如需强制重投影传 force=true。"
                ),
                is_error=False,
            )
        apply_stats = result.get("apply") or {}
        lines = [
            "知识图谱同步完成。",
            f"变更来源：{', '.join(result['changed_sources']) or '(force)'}",
            (
                f"本次写入：节点 +{apply_stats.get('nodes_created', 0)}"
                f"/~{apply_stats.get('nodes_updated', 0)}，"
                f"边 +{apply_stats.get('edges_created', 0)}"
                f"（未变 {apply_stats.get('edges_unchanged', 0)}，"
                f"失效 {apply_stats.get('edges_expired', 0)}）"
            ),
            size_line,
        ]
        warnings = result.get("warnings") or []
        if warnings:
            lines.append(
                f"警告 {len(warnings)} 条（脏行已跳过并记录日志），"
                f"首条：{warnings[0].get('reason')}"
            )
        return ToolResult(text="\n".join(lines), is_error=False)

    # -- query ---------------------------------------------------------------

    async def _execute_query(
        self,
        base_payload: dict[str, Any],
        *,
        entity: Any,
        hops: Any,
        include_expired: bool,
    ) -> ToolResult:
        from doyoutrade.knowledge.graph import render_neighborhood_markdown

        if not isinstance(entity, str) or not entity.strip():
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {**base_payload, "error_code": "missing_entity"},
            )
            return ToolResult(
                text=format_error_text(
                    "missing_entity",
                    "action=query requires a non-empty `entity` "
                    "(股票代码/名称/角色/YYYY-MM/信号 id)",
                ),
                is_error=True,
            )
        hop_count = 1 if hops is None else hops
        if not isinstance(hop_count, int) or isinstance(hop_count, bool) or not 1 <= hop_count <= 3:
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {**base_payload, "error_code": "validation_error", "hops": hops},
            )
            return ToolResult(
                text=format_error_text(
                    "validation_error", f"hops must be an integer in 1..3, got {hops!r}"
                ),
                is_error=True,
            )

        edge_limit = 200
        try:
            matches = await self._repository.find_nodes(entity.strip())
            if not matches:
                await emit_debug_event(
                    f"operation_{self.name}.failed",
                    {**base_payload, "error_code": "entity_not_found", "entity": entity},
                )
                return ToolResult(
                    text=format_error_text(
                        "entity_not_found",
                        f"no graph node matches {entity!r}",
                        "若数据是新写入的，先执行 knowledge_graph(action=\"sync\") "
                        "再查询；也可换股票代码 / 全名重试",
                    ),
                    is_error=True,
                )
            center = matches[0]
            nodes, edges = await self._repository.neighborhood(
                center.id,
                hops=hop_count,
                include_expired=include_expired,
                edge_limit=edge_limit,
            )
        except Exception as exc:
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {
                    **base_payload,
                    "error_code": "knowledge_graph_failed",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
            return ToolResult(
                text=format_error_text(
                    "knowledge_graph_failed",
                    f"graph query failed ({type(exc).__name__}): {exc}",
                ),
                is_error=True,
            )

        markdown = render_neighborhood_markdown(
            center,
            nodes,
            edges,
            include_expired=include_expired,
            truncated=len(edges) >= edge_limit,
        )
        if len(matches) > 1:
            others = ", ".join(
                f"{m.display_name or m.name}({m.node_type})" for m in matches[1:4]
            )
            markdown += f"\n> 同名候选还有：{others}——如指的是它们，请换更精确的名称重查。\n"

        await emit_debug_event(
            f"operation_{self.name}.created",
            {
                **base_payload,
                "action": "query",
                "entity": entity,
                "resolved_node_id": center.id,
                "node_type": center.node_type,
                "hops": hop_count,
                "nodes": len(nodes),
                "edges": len(edges),
                "include_expired": include_expired,
            },
        )
        return ToolResult(text=markdown, is_error=False)


__all__ = ["KnowledgeGraphTool"]
