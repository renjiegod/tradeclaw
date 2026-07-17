"""`doyoutrade-cli knowledge ...` subcommands.

``knowledge index`` — the human / script surface for the knowledge-base
navigation map that the in-process ``knowledge_index`` tool generates for
the agent. Generation is a pure local filesystem derivation (no server
business state), so — like ``schema`` — this command builds the envelope
locally instead of going through HTTP.

``knowledge index``           → print the index (envelope ``data.index_markdown``).
``knowledge index --refresh`` → also write ``<kb_root>/_index.md`` to disk.
``knowledge index --partition cycles`` → scope to one partition.

``knowledge graph`` / ``knowledge graph-sync`` — 知识图谱面（kg_nodes /
kg_edges 在 API server 的 DB 里），因此走 HTTP（``invoke_api``），保持
"server 是运行态唯一所有者"的架构规则：

``knowledge graph <entity> [--hops N] [--include-expired]``
    → ``GET /knowledge/graph``（实体邻域子图）。
``knowledge graph-sync [--force]``
    → ``POST /knowledge/graph/sync``（确定性投影幂等重建）。
"""

from __future__ import annotations

from typing import Any

import click

from doyoutrade.cli._envelope import (
    EXIT_OK,
    EXIT_VALIDATION,
    Meta,
    error_envelope,
    success_envelope,
)
from doyoutrade.cli._format import write_envelope
from doyoutrade.cli._invoke import read_session_meta
from doyoutrade.knowledge import build_knowledge_index, render_index_markdown, write_index_file

_KNOWN_PARTITIONS = ("cycles", "symbols", "trades", "journal", "backtests")


@click.group()
def knowledge() -> None:
    """Knowledge-base (``~/.doyoutrade/knowledge``) index + inspection."""


@knowledge.command("index")
@click.option(
    "--partition",
    "partition",
    default=None,
    type=click.Choice(list(_KNOWN_PARTITIONS), case_sensitive=False),
    help="Scope to a single partition (cycles/symbols/trades/journal/backtests).",
)
@click.option(
    "--refresh",
    "refresh",
    is_flag=True,
    default=False,
    help="Also write the rendered index to <kb_root>/_index.md on disk.",
)
def knowledge_index(partition: str | None, refresh: bool) -> None:
    """Print (and optionally persist) the knowledge-base navigation index.

    The index is a compact one-line-per-file map of every partition,
    grouped by month / year / strategy. It is the "reason over structure
    first, then read the one file you need" entry point — the same map
    the in-process ``knowledge_index`` tool generates for the agent.

    Examples::

        doyoutrade-cli knowledge index
        doyoutrade-cli knowledge index --partition cycles
        doyoutrade-cli knowledge index --refresh
    """

    ctx = click.get_current_context()
    fmt = ctx.find_root().obj.get("fmt", "json") if ctx.find_root().obj else "json"
    meta: Meta = read_session_meta()

    # Resolve the KB root via the same resolver the sandbox / tool use so
    # the CLI honours DOYOUTRADE_HOME identically.
    from doyoutrade.tools._sandbox import knowledge_root

    kb_root = knowledge_root()

    try:
        index = build_knowledge_index(kb_root)
    except Exception as exc:
        envelope = error_envelope(
            error_code="index_build_failed",
            error_type=type(exc).__name__,
            message=str(exc) or f"{type(exc).__name__} (no message)",
            meta=meta,
        )
        write_envelope(envelope, fmt=fmt)
        ctx.exit(EXIT_VALIDATION)
        return

    if partition is not None:
        import dataclasses

        kept = tuple(p for p in index.partitions if p.name == partition)
        index = dataclasses.replace(
            index,
            partitions=kept,
            total_files=sum(p.file_count for p in kept),
        )

    markdown = render_index_markdown(index)
    data: dict[str, Any] = {
        "index_markdown": markdown,
        "root_exists": index.root_exists,
        "total_files": index.total_files,
        "skipped_count": len(index.skipped),
        "weak_title_count": len(index.weak_titles),
        "generated_at": index.generated_at.isoformat(),
        "partitions": [
            {"name": p.name, "label": p.label, "file_count": p.file_count}
            for p in index.partitions
        ],
    }

    index_path = None
    if refresh:
        try:
            index_path = str(write_index_file(index))
        except OSError as exc:
            envelope = error_envelope(
                error_code="index_write_failed",
                error_type=type(exc).__name__,
                message=f"failed to write _index.md: {exc}",
                meta=meta,
            )
            write_envelope(envelope, fmt=fmt)
            ctx.exit(EXIT_VALIDATION)
            return
        data["index_path"] = index_path

    summary = (
        f"Knowledge index ({index.total_files} files"
        + (f", partition={partition}" if partition else "")
        + (f", written to {index_path}" if index_path else "")
        + ")."
    )
    envelope = success_envelope(data, summary, meta=meta)
    write_envelope(envelope, fmt=fmt)
    ctx.exit(EXIT_OK)


@knowledge.command("graph")
@click.argument("entity")
@click.option(
    "--hops",
    "hops",
    default=1,
    type=click.IntRange(1, 3),
    show_default=True,
    help="邻域跳数（1-3）。",
)
@click.option(
    "--include-expired",
    "include_expired",
    is_flag=True,
    default=False,
    help="附带已失效的历史事实（角色变更史等 bi-temporal 回溯）。",
)
def knowledge_graph_query(entity: str, hops: int, include_expired: bool) -> None:
    """Query the knowledge graph for one entity's neighborhood subgraph.

    ENTITY 可以是股票代码（300059）、名称（东方财富）、角色词（龙头）、
    周期月（2026-03）或决策信号 id。

    Examples::

        doyoutrade-cli knowledge graph 300059
        doyoutrade-cli knowledge graph 龙头 --hops 2
        doyoutrade-cli knowledge graph 2026-03 --include-expired
    """
    from doyoutrade.cli._api import invoke_api
    from doyoutrade.cli.main import run_async_command

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "GET",
            "/knowledge/graph",
            params={
                "entity": entity,
                "hops": hops,
                "include_expired": include_expired,
            },
            meta=read_session_meta(),
            not_found_error_code="kg_entity_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@knowledge.command("graph-sync")
@click.option(
    "--force",
    "force",
    is_flag=True,
    default=False,
    help="忽略来源 content_hash 水位，强制重投影。",
)
def knowledge_graph_sync(force: bool) -> None:
    """Idempotently re-project deterministic sources into the knowledge graph.

    来源：symbols/roles.jsonl、cycles/*/_sentiment.jsonl、trades/ 交割单
    归因、decision_signals 表。所有来源未变化时快速跳过（``skipped: true``）。
    """
    from doyoutrade.cli._api import invoke_api
    from doyoutrade.cli.main import run_async_command

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "POST",
            "/knowledge/graph/sync",
            params={"force": force},
            meta=read_session_meta(),
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


__all__ = ["knowledge"]
