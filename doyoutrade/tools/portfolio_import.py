"""Assistant tools for portfolio import (功能 6 — 图片 / CSV 智能导入持仓).

Two ``OperationHandler`` tools:

- ``import_positions_from_image`` — vision extraction of positions from a
  brokerage screenshot inside a registered sandbox. Requires a wired
  multimodal ``model_adapter``; unwired runtimes fail with a structured
  ``portfolio_import_unwired``.
- ``import_trades_csv`` — broker-statement CSV import into the knowledge
  ``trades/<broker>/<YYYY-MM>.csv`` partition (pure local, no model).

NOTE 待集成: these tools are NOT yet registered in
``doyoutrade/tools/__init__.py``'s ``build_default_tool_registry`` (that file
is owned by a parallel change). Registration is listed as a pending
integration item in the delivery notes.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from doyoutrade.debug import emit_debug_event
from doyoutrade.persistence.strategy_storage import SandboxViolation
from doyoutrade.tools import OperationHandler, ToolResult
from doyoutrade.tools._prose import append_json_payload, format_error_text, format_unknown_args
from doyoutrade.tools._sandbox import register_knowledge_sandbox, resolve_path

logger = logging.getLogger(__name__)

#: File-extension → MIME fallback used when the caller omits ``mime_type``.
#: The extractor still magic-sniffs the content, so a wrong extension is
#: caught as ``image_mime_mismatch`` rather than trusted.
_EXT_MIME: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


class _PortfolioImportToolBase(OperationHandler):
    """Shared contract/debug-event scaffolding for the two import tools."""

    category = "portfolio"

    async def _run_contract(
        self, kwargs: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, ToolResult | None]:
        """Run the standard kwargs contract + coercion; return (kwargs, error_result)."""
        base_payload = {
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
            return None, ToolResult(text=text, is_error=True)
        kwargs = contract.kwargs

        coercion = self._apply_schema_coercion(kwargs)
        if coercion.error is not None:
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {**base_payload, "error": coercion.error},
            )
            return None, ToolResult(
                text=format_error_text(
                    str(coercion.error.get("error_code") or "coercion_error"),
                    str(coercion.error.get("error") or "input coercion failed"),
                ),
                is_error=True,
            )
        return coercion.kwargs, None

    async def _fail(
        self, error_code: str, message: str, *, hint: str | None = None, **event_extra: Any
    ) -> ToolResult:
        await emit_debug_event(
            f"operation_{self.name}.failed",
            {
                "tool": self.name,
                "error_code": error_code,
                "message": message,
                **({"hint": hint} if hint else {}),
                **event_extra,
            },
        )
        return ToolResult(
            text=format_error_text(error_code, message, hint),
            is_error=True,
        )

    def _resolve_sandbox_file(self, file_path: Any) -> Path | dict[str, Any]:
        """Resolve *file_path* inside a registered sandbox; error dict on failure."""
        if not isinstance(file_path, str) or not file_path.strip():
            return {
                "error_code": "validation_error",
                "message": (
                    "file_path must be a non-empty string, got "
                    f"{type(file_path).__name__}: {file_path!r}"
                ),
            }
        register_knowledge_sandbox()  # KB root is always an acceptable source dir
        try:
            resolved = resolve_path(file_path.strip())
        except SandboxViolation as exc:
            logger.warning(
                "%s: sandbox violation for file_path=%r (%s): %s",
                self.name, file_path, type(exc).__name__, exc,
            )
            return {
                "error_code": "sandbox_violation",
                "message": str(exc),
                "hint": "file must live inside a registered sandbox root "
                "(e.g. ~/.doyoutrade/knowledge); copy it there first",
            }
        if not resolved.is_file():
            return {
                "error_code": "file_not_found",
                "message": f"no file at {resolved}",
            }
        return resolved


class ImportPositionsFromImageTool(_PortfolioImportToolBase):
    name = "import_positions_from_image"
    description = (
        "从证券账户持仓截图（PNG/JPEG/WEBP/GIF，≤8MB，沙盒内路径）中用多模态模型"
        "提取持仓列表（name/symbol/quantity/cost_price/current_price），"
        "股票名称自动解析为规范代码；解析不出的保留原名并标 symbol_unresolved。"
    )
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "file_path": {
                "type": "string",
                "description": "沙盒内的截图绝对路径（如 ~/.doyoutrade/knowledge 下）。",
            },
            "mime_type": {
                "type": "string",
                "enum": ["image/png", "image/jpeg", "image/webp", "image/gif"],
                "description": "图片 MIME 类型；省略时按扩展名推断并按魔数校验。",
            },
        },
        "required": ["file_path"],
    }

    def __init__(
        self,
        model_adapter: Any | None = None,
        instrument_catalog_repository: Any | None = None,
        model_adapter_factory: Any | None = None,
    ) -> None:
        self._model_adapter = model_adapter
        self._instrument_catalog_repository = instrument_catalog_repository
        # Lazy alternative to a concrete adapter: an async callable
        # ``(route_name | None) -> adapter`` (AssistantService's
        # ``ModelAdapterFactory``). Resolved on first use with the default
        # route and cached; resolution failure is surfaced structurally.
        self._model_adapter_factory = model_adapter_factory

    async def _resolve_adapter(self) -> Any | dict[str, Any]:
        if self._model_adapter is not None:
            return self._model_adapter
        if self._model_adapter_factory is None:
            return {
                "error_code": "portfolio_import_unwired",
                "message": (
                    "this runtime has no multimodal model adapter wired into "
                    "import_positions_from_image"
                ),
                "hint": "wire model_adapter=... or model_adapter_factory=... "
                "when building the tool registry",
            }
        try:
            adapter = await self._model_adapter_factory(None)
        except Exception as exc:  # noqa: BLE001 - surfaced structurally below
            logger.warning(
                "%s: model adapter factory failed (%s): %s",
                self.name, type(exc).__name__, exc,
            )
            return {
                "error_code": "model_adapter_unavailable",
                "message": (
                    "resolving the default model route failed: "
                    f"{type(exc).__name__}: {exc}"
                ),
                "hint": "check model routes (doyoutrade-cli route list) and provider keys",
            }
        self._model_adapter = adapter
        return adapter

    async def execute(self, **kwargs: Any) -> ToolResult:
        await emit_debug_event(
            f"operation_{self.name}.request",
            {"tool": self.name, "input_keys": sorted(kwargs.keys())},
        )
        kwargs2, err = await self._run_contract(kwargs)
        if err is not None:
            return err
        assert kwargs2 is not None

        adapter = await self._resolve_adapter()
        if isinstance(adapter, dict):
            return await self._fail(
                str(adapter["error_code"]),
                str(adapter["message"]),
                hint=adapter.get("hint"),
            )

        resolved = self._resolve_sandbox_file(kwargs2.get("file_path"))
        if isinstance(resolved, dict):
            return await self._fail(
                str(resolved["error_code"]),
                str(resolved["message"]),
                hint=resolved.get("hint"),
            )

        mime_type = kwargs2.get("mime_type")
        if mime_type is None:
            mime_type = _EXT_MIME.get(resolved.suffix.lower())
            if mime_type is None:
                return await self._fail(
                    "unsupported_image_type",
                    f"cannot infer MIME type from extension {resolved.suffix!r}; "
                    "pass mime_type explicitly",
                    hint="supported: image/png, image/jpeg, image/webp, image/gif",
                )

        try:
            image_bytes = resolved.read_bytes()
        except OSError as exc:
            logger.warning(
                "%s: read failed %s (%s): %s",
                self.name, resolved, type(exc).__name__, exc,
            )
            return await self._fail(
                "file_read_failed",
                f"could not read {resolved}: {type(exc).__name__}: {exc}",
            )

        from doyoutrade.portfolio_import.image_extractor import extract_positions_from_image

        result = await extract_positions_from_image(
            image_bytes,
            str(mime_type),
            adapter=adapter,
            instrument_catalog_repository=self._instrument_catalog_repository,
        )
        if result.get("status") != "ok":
            return await self._fail(
                str(result.get("error_code") or "portfolio_import_failed"),
                str(result.get("message") or "position extraction failed"),
                raw_text_head=result.get("raw_text"),
            )

        await emit_debug_event(
            f"operation_{self.name}.created",
            {
                "tool": self.name,
                "position_count": len(result["positions"]),
                "unresolved_count": len(result["unresolved"]),
            },
        )
        summary = (
            f"从截图提取到 {len(result['positions'])} 条持仓"
            + (f"，其中 {len(result['unresolved'])} 条名称/代码待人工确认" if result["unresolved"] else "")
            + "。"
        )
        return ToolResult(text=append_json_payload(summary, result))


class ImportTradesCsvTool(_PortfolioImportToolBase):
    name = "import_trades_csv"
    description = (
        "导入券商交割单 CSV（沙盒内路径）到私有知识库 trades/<broker>/<YYYY-MM>.csv，"
        "自动识别华泰/国君等券商列名、按月分文件、重复成交去重追加，"
        "并刷新知识库索引。"
    )
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "file_path": {
                "type": "string",
                "description": "沙盒内的交割单 CSV 绝对路径。",
            },
            "broker": {
                "type": "string",
                "description": "券商名（作为 trades/ 下的目录名，如 'huatai' / '华泰'）。",
            },
        },
        "required": ["file_path", "broker"],
    }

    async def execute(self, **kwargs: Any) -> ToolResult:
        await emit_debug_event(
            f"operation_{self.name}.request",
            {"tool": self.name, "input_keys": sorted(kwargs.keys())},
        )
        kwargs2, err = await self._run_contract(kwargs)
        if err is not None:
            return err
        assert kwargs2 is not None

        resolved = self._resolve_sandbox_file(kwargs2.get("file_path"))
        if isinstance(resolved, dict):
            return await self._fail(
                str(resolved["error_code"]),
                str(resolved["message"]),
                hint=resolved.get("hint"),
            )

        from doyoutrade.portfolio_import.csv_import import import_trades_csv

        result = import_trades_csv(resolved, broker=str(kwargs2.get("broker") or ""))
        if result.get("status") != "ok":
            return await self._fail(
                str(result.get("error_code") or "csv_import_failed"),
                str(result.get("message") or "CSV import failed"),
                unparsed_count=len(result.get("unparsed") or []),
            )

        await emit_debug_event(
            f"operation_{self.name}.created",
            {
                "tool": self.name,
                "broker": result.get("broker"),
                "appended_total": result.get("appended_total"),
                "duplicates_skipped": result.get("duplicates_skipped"),
                "unparsed_count": len(result.get("unparsed") or []),
                "attribution_readable": result.get("attribution_readable"),
            },
        )
        summary = (
            f"已导入 {result['appended_total']} 条成交到 {', '.join(result['written']) or '（无新文件）'}；"
            f"重复跳过 {result['duplicates_skipped']} 条，"
            f"未解析 {len(result.get('unparsed') or [])} 条。"
        )
        return ToolResult(text=append_json_payload(summary, result))


__all__ = ["ImportPositionsFromImageTool", "ImportTradesCsvTool"]
