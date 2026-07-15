"""`doyoutrade-cli knowledge ...` subcommands.

Currently exposes ``knowledge index`` — the human / script surface for
the knowledge-base navigation map that the in-process ``knowledge_index``
tool generates for the agent. Generation is a pure local filesystem
derivation (no server business state), so — like ``schema`` — this
command builds the envelope locally instead of going through HTTP.

``knowledge index``           → print the index (envelope ``data.index_markdown``).
``knowledge index --refresh`` → also write ``<kb_root>/_index.md`` to disk.
``knowledge index --partition cycles`` → scope to one partition.
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


__all__ = ["knowledge"]
