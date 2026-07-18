"""knowledge_graph — 私有知识库之上的实体关系图谱（查询 + 变更提案）。

与 ``knowledge_index``（文件导航地图）互补的第二层检索面：图谱回答
"这个实体和什么有关"（个股↔角色↔周期↔交易↔信号），返回紧凑事实句
（带时间窗 / provenance / source_ref）；agent 顺着 source_ref 再用
``knowledge_index`` + ``read_file`` 钻取原文细节。

两个动作：

* ``action="query"``（默认）—— 按名称/代码解析实体，取 N 跳邻域子图，
  渲染成 markdown。``include_expired=true`` 时附带已失效的历史认知
  （bi-temporal 回溯——"当时怎么看这只票"）。
* ``action="propose"`` —— Agent 只能创建持久化变更草案，图谱不会立即
  改动；本地用户审批完全一致的 proposal hash 后才原子落库。自动来源同步
  由系统任务或本地用户入口触发，不暴露给 Agent。

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

_ACTIONS = ("query", "propose")


class KnowledgeGraphTool(OperationHandler):
    name = "knowledge_graph"
    description = (
        "私有交易知识库的实体关系图谱。action=query（默认）：输入实体"
        "（股票代码/名称/角色/YYYY-MM 周期月/信号 id），返回它的邻域事实——"
        "历史角色、所属周期、交易盈亏、相关决策信号，每条带时间窗和来源，"
        "include_expired=true 可回溯已被推翻的历史认知。"
        "对话涉及『这只票什么来头/我做过它几次/那轮周期我怎么操作的』时，"
        "先查图谱拿关联，再用 knowledge_index + read_file 钻取原文。"
        "action=propose：提交人工关系或 custom Schema 变更草案；只进入待"
        "审批队列，绝不直接写入图谱。"
    )
    category = "agent"
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action": {
                "type": "string",
                "enum": list(_ACTIONS),
                "description": "query=查询实体邻域（默认）；propose=提交待审批变更草案。",
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
            "operations": {
                "type": "array",
                "minItems": 1,
                "maxItems": 100,
                "items": {"type": "object"},
                "description": (
                    "仅 action=propose：create/revise/retract_relation 或 "
                    "upsert/deprecate_schema_item 操作数组；服务端按受保护 "
                    "Schema 校验，草案获人工审批前不会写图。"
                ),
            },
            "summary": {
                "type": "string",
                "description": "仅 action=propose：给审批人的变更摘要。",
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
        if action == "propose":
            return await self._execute_propose(
                base_payload,
                operations=args.get("operations"),
                summary=args.get("summary"),
                actor_id=str(session_id or "agent"),
            )
        return await self._execute_query(
            base_payload,
            entity=args.get("entity"),
            hops=args.get("hops"),
            include_expired=bool(args.get("include_expired")),
        )

    # -- propose ------------------------------------------------------------

    async def _execute_propose(
        self,
        base_payload: dict[str, Any],
        *,
        operations: Any,
        summary: Any,
        actor_id: str,
    ) -> ToolResult:
        from doyoutrade.knowledge.editing import (
            GraphEditError,
            KnowledgeGraphCommandService,
        )

        session_factory = getattr(self._repository, "session_factory", None)
        if session_factory is None:
            return ToolResult(
                text=format_error_text(
                    "knowledge_graph_unwired",
                    "this runtime has no graph editor persistence wiring",
                ),
                is_error=True,
            )
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            result = await KnowledgeGraphCommandService(
                session_factory
            ).create_agent_draft(
                operations,
                summary=str(summary or ""),
                actor_id=actor_id,
                now=now,
            )
        except GraphEditError as exc:
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {
                    **base_payload,
                    "action": "propose",
                    "error_code": exc.error_code,
                    "message": str(exc),
                },
            )
            return ToolResult(
                text=format_error_text(exc.error_code, str(exc)),
                is_error=True,
            )

        await emit_debug_event(
            f"operation_{self.name}.created",
            {
                **base_payload,
                "action": "propose",
                "change_set_id": result["id"],
                "base_revision": result["base_revision"],
                "proposal_hash": result["proposal_hash"],
            },
        )
        return ToolResult(
            text=(
                "知识图谱变更草案已创建，待人工审批。\n"
                f"- change_set_id: {result['id']}\n"
                f"- base_revision: {result['base_revision']}\n"
                f"- proposal_hash: {result['proposal_hash']}\n"
                "审批前图谱事实未发生变化；草案不支持 approve-always。"
            ),
            is_error=False,
        )

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
