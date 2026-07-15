"""Knowledge-base tooling.

The user's private knowledge base at ``~/.doyoutrade/knowledge`` is a flat
directory of markdown / CSV files organised into partitions (``cycles/``,
``symbols/``, ``trades/``, ``journal/``, ``backtests/``). Historically the
agent discovered its contents by ``list_files`` + blind per-file
``read_file`` — token-expensive and slow as the base grows.

This package adds a compact **index layer** (inspired by PageIndex's
"reasoning over a tree structure, then drill down" retrieval): a
deterministic, LLM-free generator that walks the base and produces a
one-line-per-file navigation map. The agent reasons over the map first,
then ``read_file`` only the file it actually needs.
"""

from doyoutrade.knowledge.index import (
    KnowledgeIndex,
    build_knowledge_index,
    render_index_markdown,
    write_index_file,
)

__all__ = [
    "KnowledgeIndex",
    "build_knowledge_index",
    "render_index_markdown",
    "write_index_file",
]
