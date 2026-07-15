"""knowledge_index — the navigation map over ``~/.doyoutrade/knowledge``.

Returns a compact, always-fresh index of the user's private knowledge
base: every partition, grouped by month / year / strategy, with a
one-line title per file (extracted from the first heading or YAML
``summary:`` front-matter — no LLM, no content dump).

This is the PageIndex "tree without text" step for a corpus of many
small files. The agent calls this *first* to reason about where to
look, then ``read_file`` the single file it actually needs — instead of
blindly ``list_files`` + per-file ``read_file`` burning tokens. See
``doyoutrade/knowledge/index.py`` for the generator.

Read-only: never writes to disk (the in-process tool always regenerates
so the agent never reasons over a stale snapshot; ``doyoutrade-cli
knowledge index --refresh`` is the persisted-snapshot path).

Error codes (stable skill-doc tokens):

* ``unknown_arguments`` — kwargs outside ``partition``.
* ``unknown_partition`` — ``partition`` not one of the six partitions.
* ``knowledge_root_missing`` — ``~/.doyoutrade/knowledge`` does not exist
  (the tool still returns a helpful empty-map message, not a hard error,
  so a fresh environment doesn't block the agent).
"""

from __future__ import annotations

from typing import Any

from doyoutrade.debug import emit_debug_event
from doyoutrade.knowledge import build_knowledge_index, render_index_markdown
from doyoutrade.tools import OperationHandler, ToolResult
from doyoutrade.tools._prose import format_error_text, format_unknown_args

#: The six canonical partitions. Kept in sync with
#: ``doyoutrade.knowledge.index._PARTITION_SPECS``.
_KNOWN_PARTITIONS = ("cycles", "symbols", "trades", "journal", "playbook", "backtests")


class KnowledgeIndexTool(OperationHandler):
    name = "knowledge_index"
    description = (
        "返回用户私有知识库（~/.doyoutrade/knowledge）的紧凑导航索引："
        "每个分区（cycles / symbols / trades / journal / playbook / backtests）、"
        "按月/年/策略分组、每文件一行标题摘要。这是导航地图，不含正文。"
        "对话涉及某标的的历史/角色、情绪周期/题材、用户持仓/交易历史、"
        "打板战法/模式总结、复盘时，"
        "先调本工具推理定位到具体文件，再用 read_file 读那一篇完整内容——"
        "不要用 list_files 逐个盲读。可选 partition 参数限定单分区。"
    )
    category = "agent"
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "partition": {
                "type": "string",
                "enum": list(_KNOWN_PARTITIONS),
                "description": (
                    "只返回该分区的索引。可选；不传则返回全部六个分区。"
                    "取值：cycles / symbols / trades / journal / playbook / backtests。"
                ),
            },
        },
        "required": [],
    }

    async def execute(self, **kwargs: Any) -> ToolResult:
        base_payload: dict[str, Any] = {
            "tool": self.name,
            "input_keys": sorted(k for k in kwargs.keys() if k != "session_id"),
        }

        contract = self._enforce_kwargs_contract(kwargs)
        if contract.error is not None:
            kind = "rejected" if contract.error_kind == "unknown_arguments" else "failed"
            await emit_debug_event(
                f"operation_{self.name}.{kind}",
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

        partition = contract.kwargs.get("partition")
        if partition is not None and partition not in _KNOWN_PARTITIONS:
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {**base_payload, "error_code": "unknown_partition", "partition": partition},
            )
            return ToolResult(
                text=format_error_text(
                    "unknown_partition",
                    f"unknown partition {partition!r}",
                    f"one of: {', '.join(_KNOWN_PARTITIONS)}",
                ),
                is_error=True,
            )

        await emit_debug_event(
            f"operation_{self.name}.request",
            {**base_payload, "partition": partition},
        )

        # Resolve the knowledge root the same way the file sandbox does
        # (honours DOYOUTRADE_HOME). Imported lazily so this module stays
        # importable from contexts (e.g. ``schema`` introspection) that
        # don't need the sandbox machinery.
        from doyoutrade.tools._sandbox import knowledge_root

        kb_root = knowledge_root()
        try:
            index = build_knowledge_index(kb_root)
        except Exception as exc:
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {**base_payload, "error_code": "index_build_failed",
                 "error_type": type(exc).__name__, "message": str(exc)},
            )
            return ToolResult(
                text=format_error_text(
                    "index_build_failed",
                    f"failed to build knowledge index: {exc}",
                    "check that ~/.doyoutrade/knowledge is readable",
                ),
                is_error=True,
            )

        if partition is not None:
            index = _scope_to_partition(index, partition)

        markdown = render_index_markdown(index)
        await emit_debug_event(
            f"operation_{self.name}.created",
            {
                **base_payload,
                "partition": partition,
                "root_exists": index.root_exists,
                "files": index.total_files,
                "skipped": len(index.skipped),
                "weak_titles": len(index.weak_titles),
            },
        )

        if not index.root_exists:
            # Fresh environment — helpful map (not a hard error) so the
            # agent can still proceed to create the base.
            return ToolResult(
                text=format_error_text(
                    "knowledge_root_missing",
                    f"knowledge base root does not exist: {kb_root}",
                    "this is normal for a fresh environment; create the "
                    "partition directories with write_file as needed",
                ),
                is_error=False,
            )

        return ToolResult(text=markdown, is_error=False)


def _scope_to_partition(index: Any, partition: str) -> Any:
    """Return a copy of ``index`` containing only ``partition``.

    Uses ``dataclasses.replace`` so the rendered markdown keeps the same
    header / counts (re-counted on the single partition).
    """

    import dataclasses

    kept = tuple(p for p in index.partitions if p.name == partition)
    return dataclasses.replace(
        index,
        partitions=kept,
        total_files=sum(p.file_count for p in kept),
    )


__all__ = ["KnowledgeIndexTool"]
