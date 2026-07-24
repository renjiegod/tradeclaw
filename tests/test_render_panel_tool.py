"""render_panel 工具回归：声明式面板规范的结构 / 符号校验、错误码稳定性、
JSON 字符串容错、成功回显的极小确认，以及是否注册进默认工具面。

render_panel 是纯 UI 原语——只校验并回显确认，面板规范经 tool.input 到前端
渲染，引用式数据由前端拉取，因此工具本身无任何运行时 wiring。"""

import json
import unittest

from doyoutrade.tools import ToolResult, build_default_tool_registry
from doyoutrade.tools.render_panel import RenderPanelTool


class RenderPanelToolTests(unittest.IsolatedAsyncioTestCase):
    def _tool(self) -> RenderPanelTool:
        return RenderPanelTool()

    async def _run(self, **kwargs) -> ToolResult:
        result = await self._tool().execute(**kwargs)
        assert isinstance(result, ToolResult)
        return result

    # ---- schema posture ------------------------------------------------
    def test_schema_posture(self) -> None:
        tool = self._tool()
        self.assertEqual(tool.name, "render_panel")
        self.assertFalse(tool.parameters["additionalProperties"])
        self.assertEqual(tool.parameters["required"], ["blocks"])
        # 顶层白名单严格，块字段在 blocks[] 内部校验。
        self.assertEqual(
            tool._allowed_top_level_kwargs(), frozenset({"v", "title", "panel_id", "blocks"})
        )

    # ---- success paths -------------------------------------------------
    async def test_kline_reference_ok(self) -> None:
        result = await self._run(
            title="茅台走势",
            blocks=[{"type": "kline", "symbol": "600519.SH", "interval": "1d", "sub_indicator": "MACD"}],
        )
        self.assertFalse(result.is_error)
        payload = json.loads(result.text)
        self.assertEqual(payload["status"], "rendered")
        self.assertEqual(payload["block_count"], 1)
        self.assertEqual(payload["block_types"], ["kline"])

    async def test_chart_line_ok(self) -> None:
        result = await self._run(
            blocks=[
                {
                    "type": "chart",
                    "chart_type": "line",
                    "data": [{"d": "2024-01", "pnl": 1200}],
                    "x_field": "d",
                    "y_fields": ["pnl"],
                }
            ]
        )
        self.assertFalse(result.is_error)

    async def test_chart_pie_ok(self) -> None:
        result = await self._run(
            blocks=[
                {
                    "type": "chart",
                    "chart_type": "pie",
                    "data": [{"sector": "白酒", "w": 0.4}],
                    "category_field": "sector",
                    "value_field": "w",
                }
            ]
        )
        self.assertFalse(result.is_error)

    async def test_kgraph_inline_and_table_statcard_markdown_ok(self) -> None:
        result = await self._run(
            blocks=[
                {
                    "type": "kgraph",
                    "nodes": [{"id": "n1", "name": "茅台"}, {"id": "n2", "name": "白酒"}],
                    "edges": [{"id": "e1", "src_id": "n1", "dst_id": "n2", "relation": "belongs_to_theme"}],
                    "center_id": "n1",
                },
                {"type": "table", "columns": [{"title": "代码", "data_index": "code"}], "rows": [{"code": "600519.SH"}]},
                {"type": "statcard", "metrics": [{"label": "涨幅", "value": "3.2%", "delta_dir": "up"}]},
                {"type": "markdown", "content": "**说明**"},
            ]
        )
        self.assertFalse(result.is_error)
        self.assertEqual(json.loads(result.text)["block_count"], 4)

    async def test_blocks_json_string_coercion(self) -> None:
        result = await self._run(blocks=json.dumps([{"type": "markdown", "content": "hi"}]))
        self.assertFalse(result.is_error)

    # ---- error paths (stable error_code tokens) ------------------------
    async def test_invalid_symbol(self) -> None:
        result = await self._run(blocks=[{"type": "kline", "symbol": "茅台"}])
        self.assertTrue(result.is_error)
        self.assertIn("[error:invalid_symbol]", result.text)

    async def test_unknown_top_level_argument(self) -> None:
        result = await self._run(blocks=[{"type": "markdown", "content": "hi"}], foo=1)
        self.assertTrue(result.is_error)
        self.assertIn("[error:unknown_arguments]", result.text)

    async def test_empty_blocks(self) -> None:
        result = await self._run(blocks=[])
        self.assertTrue(result.is_error)
        self.assertIn("[error:invalid_panel]", result.text)

    async def test_too_many_blocks(self) -> None:
        result = await self._run(blocks=[{"type": "markdown", "content": str(i)} for i in range(13)])
        self.assertTrue(result.is_error)
        self.assertIn("[error:invalid_panel]", result.text)

    async def test_unknown_block_type(self) -> None:
        result = await self._run(blocks=[{"type": "heatmap"}])
        self.assertTrue(result.is_error)
        self.assertIn("[error:invalid_block]", result.text)

    async def test_chart_missing_axis_fields(self) -> None:
        result = await self._run(blocks=[{"type": "chart", "chart_type": "line", "data": [{"a": 1}]}])
        self.assertTrue(result.is_error)
        self.assertIn("[error:invalid_block]", result.text)

    async def test_kgraph_needs_entity_or_inline(self) -> None:
        result = await self._run(blocks=[{"type": "kgraph"}])
        self.assertTrue(result.is_error)
        self.assertIn("[error:invalid_kgraph]", result.text)

    async def test_kgraph_bad_hops(self) -> None:
        result = await self._run(blocks=[{"type": "kgraph", "entity": "茅台", "hops": 9}])
        self.assertTrue(result.is_error)
        self.assertIn("[error:invalid_kgraph]", result.text)

    async def test_table_missing_columns(self) -> None:
        result = await self._run(blocks=[{"type": "table", "rows": [{"a": 1}]}])
        self.assertTrue(result.is_error)
        self.assertIn("[error:invalid_block]", result.text)

    async def test_non_string_enum_fields_return_structured_error_not_typeerror(self) -> None:
        # 模型把标量枚举字段写成 list/dict 时，必须返回结构化错误（且不抛
        # TypeError: unhashable，那样会绕过 .failed 事件与稳定 error_code）。
        cases = [
            ({"type": ["kline"]}, "invalid_block"),
            ({"type": "kline", "symbol": "600519.SH", "interval": ["1d"]}, "invalid_block"),
            ({"type": "kline", "symbol": "600519.SH", "overlays": [{"x": 1}]}, "invalid_block"),
            ({"type": "kline", "symbol": "600519.SH", "main_indicator": {"a": 1}}, "invalid_block"),
            ({"type": "chart", "chart_type": ["line"], "data": [{"a": 1}]}, "invalid_block"),
            ({"type": "kgraph", "entity": "茅台", "layout": ["radial"]}, "invalid_kgraph"),
            ({"type": "kgraph", "entity": "茅台", "color_mode": ["type"]}, "invalid_kgraph"),
        ]
        for block, code in cases:
            with self.subTest(block=block):
                result = await self._run(blocks=[block])
                self.assertTrue(result.is_error, f"{block!r} should be a structured error")
                self.assertIn(f"[error:{code}]", result.text)
                # 绝不能把 unhashable TypeError 透传给模型。
                self.assertNotIn("unhashable", result.text)
                self.assertNotIn("TypeError", result.text)

    # ---- registration --------------------------------------------------
    def test_registered_in_default_registry(self) -> None:
        registry = build_default_tool_registry()
        tool = registry.get("render_panel")
        self.assertIsNotNone(tool)
        self.assertIn("render_panel", registry.names)


if __name__ == "__main__":
    unittest.main()
