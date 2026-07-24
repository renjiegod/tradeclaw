"""render_panel — 声明式地把一个可视化面板渲染进当前对话（K线 / 通用图表 /
知识图谱 / 表格 / 指标卡 / Markdown）。

设计（借鉴 modelgo-controller 的 fizz `render_panel`，落到 doyoutrade 原生的
tool_call ↔ content_block 机制上，不引入 CopilotKit / AG-UI）：

- Agent 发一个 ``render_panel`` 工具调用，参数即一份**声明式面板规范**
  （``{title?, panel_id?, blocks:[...]}``）。前端按工具名把这个 tool_call
  渲染成一个醒目的面板组件（``AssistantPanel`` + 块注册表），K线复用
  ``LocalMarketKlineChart``、通用图表用 recharts、知识图谱复用确定性布局
  函数 + SVG、表格 / 指标卡用 antd。
- **引用式数据**：``kline`` / ``kgraph`` 块只带 ``symbol`` / ``entity`` 等
  引用，真正的行情 / 图谱数据由前端复用既有 API（``GET /market/bars`` /
  ``GET /knowledge/graph``）拉取渲染——工具侧**只做结构与符号校验**，不取数、
  无任何运行时 wiring（与 ``ask_user_question`` 同为 UI 原语，但更纯）。

面板规范**从工具调用参数（``tool.input``）**流向前端（``arguments`` 已被
持久化进 ``content_blocks`` 的 tool_call block，并随 ``tool.call`` SSE 事件
下发），因此本工具的**返回值只需一个极小的成功确认**（``{"status":"rendered",
...}``），不回显整份规范——省 token、无 1000 字 preview 截断风险。校验失败时
返回带 ``error_code`` 的结构化错误，模型可据此自我纠正。
"""

from __future__ import annotations

import json
import re
from typing import Any

from doyoutrade.debug import emit_debug_event
from doyoutrade.tools import OperationHandler, ToolResult
from doyoutrade.tools._coercion import SchemaCoercion
from doyoutrade.tools._prose import format_error_text, format_unknown_args

_MAX_BLOCKS = 12
_BLOCK_TYPES = {"kline", "chart", "kgraph", "table", "statcard", "markdown"}
_KLINE_INTERVALS = {"1d", "5m", "60m"}
_MAIN_INDICATORS = {"MA", "BOLL", "none"}
_SUB_INDICATORS = {"MACD", "KDJ", "RSI", "WR", "none"}
_OVERLAY_KINDS = {"backtest_trades", "task_fills", "signals"}
_CHART_TYPES = {"line", "bar", "area", "pie"}
_ADJUSTS = {"qfq", "hfq", "none"}
_KGRAPH_LAYOUTS = {"radial", "force"}
_KGRAPH_COLOR_MODES = {"type", "community"}
# canonical CODE.EXCHANGE（与 stock lookup 产出的规范符号一致；只校验形状，
# 不做名称解析——名称→符号必须由模型先 `stock lookup`）。
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9]{1,15}\.[A-Za-z]{1,6}$")


class RenderPanelTool(OperationHandler):
    name = "render_panel"
    description = (
        "把一个可视化面板渲染进当前对话（在网页控制台里显示为醒目的图形卡片，"
        "不是纯文本）。用于 K 线蜡烛图、通用图表（折线/柱状/面积/饼图）、知识图谱、"
        "表格、指标卡。参数是一份声明式规范：{\"title\":可选标题,\"blocks\":[...]}，"
        "blocks 是自上而下堆叠的块数组（1-12 个）。每个块用 type 区分：\n"
        "- kline（K线，引用式）：{type:'kline',symbol:'600519.SH',interval?:'1d|5m|60m',"
        "start?:'YYYY-MM-DD',end?:'YYYY-MM-DD',adjust?:'qfq|hfq|none',main_indicator?:'MA|BOLL|none',"
        "sub_indicator?:'MACD|KDJ|RSI|WR|none',overlays?:['backtest_trades'|'task_fills'|'signals'],title?}。"
        "symbol 必须是先经 `stock lookup` 得到的规范 CODE.EXCHANGE；数据由前端按本地行情库拉取。\n"
        "- chart（通用图表，内联数据）：{type:'chart',chart_type:'line|bar|area|pie',data:[{...}],"
        "x_field?,y_fields?:[...],series_names?:{key:名},category_field?,value_field?,unit?,stacked?,title?}。"
        "line/bar/area 需要 x_field + y_fields；pie 需要 category_field + value_field。data 请保持精简（数十行内）。\n"
        "- kgraph（知识图谱，引用式或内联）：引用式 {type:'kgraph',entity:'贵州茅台',hops?:1|2|3,"
        "include_expired?:bool,layout?:'radial|force',color_mode?:'type|community',title?}；"
        "或内联 {type:'kgraph',nodes:[{id,name,node_type?}],edges:[{id,src_id,dst_id,relation?,fact?}],center_id?}。\n"
        "- table（表格）：{type:'table',columns:[{title,data_index,align?}],rows:[{...}],title?}。\n"
        "- statcard（指标卡）：{type:'statcard',metrics:[{label,value,unit?,delta?,delta_dir?:'up|down|flat'}],title?}。\n"
        "- markdown（富文本说明）：{type:'markdown',content:'...'}。\n"
        "用于让用户直观看到图形；面板渲染后**继续用文字解读**，不要复述整份规范。"
    )
    category = "render"
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "v": {"type": "integer", "description": "规范版本，当前固定 1，可省略。"},
            "title": {"type": "string", "description": "面板标题。可选。"},
            "panel_id": {
                "type": "string",
                "description": "面板稳定标识（同一逻辑面板多次渲染时复用）。可选。",
            },
            "blocks": {
                "type": "array",
                "minItems": 1,
                "maxItems": _MAX_BLOCKS,
                "description": (
                    "自上而下堆叠的块数组（1-12）。每个块是一个对象，用 type 字段区分："
                    "kline / chart / kgraph / table / statcard / markdown。各块字段见工具描述。"
                ),
                "items": {"type": "object"},
            },
        },
        "required": ["blocks"],
    }
    coercion_rules = (
        SchemaCoercion(field="blocks", declared_type="array", item_type=dict),
    )

    async def execute(self, **kwargs: Any) -> ToolResult:
        base_payload: dict[str, Any] = {
            "tool": self.name,
            "input_keys": sorted(kwargs.keys()),
        }

        contract = self._enforce_kwargs_contract(kwargs)
        if contract.error is not None:
            await emit_debug_event(
                f"operation_{self.name}."
                f"{'rejected' if contract.error_kind == 'unknown_arguments' else 'failed'}",
                {**base_payload, "error": contract.error},
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
        kwargs = contract.kwargs

        coercion = self._apply_schema_coercion(kwargs)
        if coercion.error is not None:
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {**base_payload, "error": coercion.error},
            )
            return ToolResult(
                text=format_error_text(
                    str(coercion.error.get("error_code") or "invalid_blocks_json"),
                    str(coercion.error.get("error") or "input coercion failed"),
                ),
                is_error=True,
            )
        kwargs = coercion.kwargs

        problem = self._validate_spec(kwargs)
        if problem is not None:
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {**base_payload, "error": problem},
            )
            return ToolResult(
                text=format_error_text(
                    str(problem["error_code"]),
                    str(problem["message"]),
                    problem.get("hint"),
                ),
                is_error=True,
            )

        blocks = kwargs["blocks"]
        block_types = [str(block.get("type")) for block in blocks]
        await emit_debug_event(
            f"operation_{self.name}.created",
            {
                **base_payload,
                "panel_id": kwargs.get("panel_id"),
                "block_count": len(blocks),
                "block_types": block_types,
            },
        )
        # 极小的成功确认：面板规范本身通过工具调用参数（tool.input，已持久化 +
        # 随流下发）到达前端，无需在结果里回显整份规范。
        return ToolResult(
            text=json.dumps(
                {
                    "status": "rendered",
                    "panel_id": kwargs.get("panel_id"),
                    "block_count": len(blocks),
                    "block_types": block_types,
                },
                ensure_ascii=False,
            )
        )

    # ---- 结构校验 -----------------------------------------------------
    def _validate_spec(self, kwargs: dict[str, Any]) -> dict[str, Any] | None:
        """返回第一处结构问题（含 error_code / message / hint），全部合法返回 None。"""

        blocks = kwargs.get("blocks")
        if not isinstance(blocks, list) or not blocks:
            return {
                "error_code": "invalid_panel",
                "message": (
                    "blocks 必须是非空数组，"
                    f"got {type(blocks).__name__}: {blocks!r}"
                ),
                "hint": "至少提供一个块，例如 [{'type':'kline','symbol':'600519.SH'}]",
            }
        if len(blocks) > _MAX_BLOCKS:
            return {
                "error_code": "invalid_panel",
                "message": f"blocks 最多 {_MAX_BLOCKS} 个，got {len(blocks)}",
                "hint": "拆成多次 render_panel，或合并相近的块",
            }

        for index, block in enumerate(blocks):
            error = self._validate_block(index, block)
            if error is not None:
                return error
        return None

    def _validate_block(self, index: int, block: Any) -> dict[str, Any] | None:
        where = f"blocks[{index}]"
        if not isinstance(block, dict):
            return {
                "error_code": "invalid_block",
                "message": f"{where} 必须是对象，got {type(block).__name__}",
            }
        block_type = block.get("type")
        # 先确保是字符串再做集合成员判断——模型可能把标量字段写成 list/dict，
        # 直接 `x not in <set>` 会抛 TypeError(unhashable)，绕过结构化错误与
        # .failed 事件（§错误可见性）。下面所有枚举校验同理加 isinstance 守卫。
        if not isinstance(block_type, str) or block_type not in _BLOCK_TYPES:
            return {
                "error_code": "invalid_block",
                "message": f"{where}.type 未知：{block_type!r}",
                "hint": f"type 取值：{', '.join(sorted(_BLOCK_TYPES))}",
            }

        if block_type == "kline":
            return self._validate_kline(where, block)
        if block_type == "chart":
            return self._validate_chart(where, block)
        if block_type == "kgraph":
            return self._validate_kgraph(where, block)
        if block_type == "table":
            return self._validate_table(where, block)
        if block_type == "statcard":
            return self._validate_statcard(where, block)
        if block_type == "markdown":
            content = block.get("content")
            if not isinstance(content, str) or not content.strip():
                return {
                    "error_code": "invalid_block",
                    "message": f"{where}.content 必须是非空字符串",
                }
        return None

    def _validate_kline(self, where: str, block: dict[str, Any]) -> dict[str, Any] | None:
        symbol = block.get("symbol")
        if not isinstance(symbol, str) or not _SYMBOL_RE.match(symbol.strip()):
            return {
                "error_code": "invalid_symbol",
                "message": f"{where}.symbol 必须是规范 CODE.EXCHANGE（如 600519.SH），got {symbol!r}",
                "hint": "先用 `stock lookup` 解析出规范符号，再放进 kline 块；不要凭名称臆造",
            }
        interval = block.get("interval")
        if interval is not None and (not isinstance(interval, str) or interval not in _KLINE_INTERVALS):
            return {
                "error_code": "invalid_block",
                "message": f"{where}.interval 只支持 {sorted(_KLINE_INTERVALS)}，got {interval!r}",
                "hint": "本地行情库仅支持 1d/5m/60m",
            }
        main = block.get("main_indicator")
        if main is not None and (not isinstance(main, str) or main not in _MAIN_INDICATORS):
            return {
                "error_code": "invalid_block",
                "message": f"{where}.main_indicator 取值 {sorted(_MAIN_INDICATORS)}，got {main!r}",
            }
        sub = block.get("sub_indicator")
        if sub is not None and (not isinstance(sub, str) or sub not in _SUB_INDICATORS):
            return {
                "error_code": "invalid_block",
                "message": f"{where}.sub_indicator 取值 {sorted(_SUB_INDICATORS)}，got {sub!r}",
            }
        adjust = block.get("adjust")
        if adjust is not None and (not isinstance(adjust, str) or adjust not in _ADJUSTS):
            return {
                "error_code": "invalid_block",
                "message": f"{where}.adjust 取值 {sorted(_ADJUSTS)}，got {adjust!r}",
            }
        overlays = block.get("overlays")
        if overlays is not None:
            if not isinstance(overlays, list) or any(
                not isinstance(item, str) or item not in _OVERLAY_KINDS for item in overlays
            ):
                return {
                    "error_code": "invalid_block",
                    "message": (
                        f"{where}.overlays 必须是 {sorted(_OVERLAY_KINDS)} 的子集数组，got {overlays!r}"
                    ),
                }
        return None

    def _validate_chart(self, where: str, block: dict[str, Any]) -> dict[str, Any] | None:
        chart_type = block.get("chart_type")
        if not isinstance(chart_type, str) or chart_type not in _CHART_TYPES:
            return {
                "error_code": "invalid_block",
                "message": f"{where}.chart_type 取值 {sorted(_CHART_TYPES)}，got {chart_type!r}",
            }
        data = block.get("data")
        if not isinstance(data, list) or not data:
            return {
                "error_code": "invalid_block",
                "message": f"{where}.data 必须是非空对象数组",
                "hint": "data 是内联数据行，例如 [{'date':'2024-01','pnl':1200}]",
            }
        if any(not isinstance(row, dict) for row in data):
            return {
                "error_code": "invalid_block",
                "message": f"{where}.data 的每一行必须是对象",
            }
        if chart_type == "pie":
            if not _is_nonempty_str(block.get("category_field")) or not _is_nonempty_str(
                block.get("value_field")
            ):
                return {
                    "error_code": "invalid_block",
                    "message": f"{where} 饼图需要 category_field 与 value_field（字段名）",
                }
        else:
            if not _is_nonempty_str(block.get("x_field")):
                return {
                    "error_code": "invalid_block",
                    "message": f"{where} {chart_type} 图需要 x_field（横轴字段名）",
                }
            y_fields = block.get("y_fields")
            if not isinstance(y_fields, list) or not y_fields or any(
                not _is_nonempty_str(field) for field in y_fields
            ):
                return {
                    "error_code": "invalid_block",
                    "message": f"{where} {chart_type} 图需要 y_fields（非空的字段名数组）",
                }
        return None

    def _validate_kgraph(self, where: str, block: dict[str, Any]) -> dict[str, Any] | None:
        has_entity = _is_nonempty_str(block.get("entity"))
        nodes = block.get("nodes")
        edges = block.get("edges")
        has_inline = isinstance(nodes, list) and isinstance(edges, list) and bool(nodes)
        if not has_entity and not has_inline:
            return {
                "error_code": "invalid_kgraph",
                "message": (
                    f"{where} 需要 entity（引用式，前端按 /knowledge/graph 拉取）"
                    "，或 nodes+edges（内联，nodes 非空）"
                ),
                "hint": "引用式示例 {'type':'kgraph','entity':'贵州茅台','hops':2}",
            }
        if has_inline:
            for node_index, node in enumerate(nodes):
                if not isinstance(node, dict) or not _is_nonempty_str(node.get("id")):
                    return {
                        "error_code": "invalid_kgraph",
                        "message": f"{where}.nodes[{node_index}] 必须是带非空 id 的对象",
                    }
            if not isinstance(edges, list):
                return {
                    "error_code": "invalid_kgraph",
                    "message": f"{where}.edges 必须是数组",
                }
            for edge_index, edge in enumerate(edges):
                if (
                    not isinstance(edge, dict)
                    or not _is_nonempty_str(edge.get("src_id"))
                    or not _is_nonempty_str(edge.get("dst_id"))
                ):
                    return {
                        "error_code": "invalid_kgraph",
                        "message": (
                            f"{where}.edges[{edge_index}] 必须是带 src_id / dst_id 的对象"
                        ),
                    }
        hops = block.get("hops")
        if hops is not None and (not isinstance(hops, int) or hops < 1 or hops > 3):
            return {
                "error_code": "invalid_kgraph",
                "message": f"{where}.hops 取值 1-3，got {hops!r}",
            }
        layout = block.get("layout")
        if layout is not None and (not isinstance(layout, str) or layout not in _KGRAPH_LAYOUTS):
            return {
                "error_code": "invalid_kgraph",
                "message": f"{where}.layout 取值 {sorted(_KGRAPH_LAYOUTS)}，got {layout!r}",
            }
        color_mode = block.get("color_mode")
        if color_mode is not None and (
            not isinstance(color_mode, str) or color_mode not in _KGRAPH_COLOR_MODES
        ):
            return {
                "error_code": "invalid_kgraph",
                "message": f"{where}.color_mode 取值 {sorted(_KGRAPH_COLOR_MODES)}，got {color_mode!r}",
            }
        return None

    def _validate_table(self, where: str, block: dict[str, Any]) -> dict[str, Any] | None:
        columns = block.get("columns")
        if not isinstance(columns, list) or not columns:
            return {
                "error_code": "invalid_block",
                "message": f"{where}.columns 必须是非空数组",
            }
        for col_index, column in enumerate(columns):
            if (
                not isinstance(column, dict)
                or not _is_nonempty_str(column.get("title"))
                or not _is_nonempty_str(column.get("data_index"))
            ):
                return {
                    "error_code": "invalid_block",
                    "message": f"{where}.columns[{col_index}] 必须含 title 与 data_index",
                }
        rows = block.get("rows")
        if not isinstance(rows, list):
            return {
                "error_code": "invalid_block",
                "message": f"{where}.rows 必须是数组",
            }
        return None

    def _validate_statcard(self, where: str, block: dict[str, Any]) -> dict[str, Any] | None:
        metrics = block.get("metrics")
        if not isinstance(metrics, list) or not metrics:
            return {
                "error_code": "invalid_block",
                "message": f"{where}.metrics 必须是非空数组",
            }
        for metric_index, metric in enumerate(metrics):
            if not isinstance(metric, dict) or not _is_nonempty_str(metric.get("label")):
                return {
                    "error_code": "invalid_block",
                    "message": f"{where}.metrics[{metric_index}] 必须含非空 label",
                }
            if "value" not in metric:
                return {
                    "error_code": "invalid_block",
                    "message": f"{where}.metrics[{metric_index}] 必须含 value",
                }
        return None


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())
