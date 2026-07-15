"""Deterministic knowledge-base index generator.

Walks ``~/.doyoutrade/knowledge`` and produces a compact navigation map
(one line per file) without invoking any LLM. The map lets the agent
reason over the *structure* first — "which month / which symbol / which
partition should I open?" — and then ``read_file`` only the single file
it needs. This is the PageIndex "tree without text" idea applied to a
corpus of many small files instead of one long document.

Design rules:

* **Deterministic & cheap.** Summaries come from filename + the file's
  first heading / YAML ``summary:`` front-matter. Markdown titles are
  extracted from the first ~2 KB only; CSV / XLSX broker exports are
  listed by name + size and never parsed (they can be large).
* **Always fresh.** The in-process ``knowledge_index`` tool regenerates
  on every call, so the agent never reasons over a stale map. The CLI
  ``knowledge index --refresh`` persists the same output to
  ``_index.md`` as a human / grep / frontend convenience snapshot.
* **No silent drops.** A file that fails to read is collected into
  ``KnowledgeIndex.skipped`` and surfaced at the bottom of the rendered
  map, never silently ignored.
* **Partition-aware grouping.** ``cycles/`` and ``trades/`` group by
  ``YYYY-MM`` (newest first); ``journal/`` groups by ``YYYY``;
  ``backtests/`` groups by strategy sub-directory; ``symbols/`` and
  ``playbook/`` are flat.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

#: The canonical knowledge-base partitions in display order. Each spec
#: carries a human label and a grouping strategy (see ``_group_files``).
#: ``text_suffix`` is the suffix worth extracting a title from; other
#: files are listed by name only.
_PARTITION_SPECS: tuple[tuple[str, str, str, str], ...] = (
    ("cycles", "情绪周期 / 题材 / 龙头", "month", ".md"),
    ("symbols", "标的角色 + 策略匹配", "flat", ".md"),
    ("trades", "个人交割单（券商导出）", "month", ".csv"),
    ("journal", "复盘日记 / 操作记录", "year", ".md"),
    ("playbook", "打板模式库 / 战法总结", "flat", ".md"),
    ("backtests", "策略回测结果", "dir", ".md"),
)

#: Read at most this many bytes of a markdown file when extracting a
#: title. Keeps the generator O(files) not O(bytes) — a 1 MB journal
#: contributes the same cost as a 2 KB note.
_TITLE_PEEK_BYTES = 2048

#: ``_overview.md`` (and ``_overview`` stem generally) is the
#: partition / group entry point the skill tells the agent to read first.
_OVERVIEW_STEM = "_overview"

_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
_YEAR_RE = re.compile(r"^\d{4}$")

#: Front-matter ``summary: ...`` line (YAML scalar). Bounded so it only
#: matches a top-level key, not a nested occurrence.
_FRONTMATTER_SUMMARY_RE = re.compile(r"^summary:\s*(.+?)\s*$")
_FRONTMATTER_DELIM = "---"


GroupStrategy = Literal["month", "year", "dir", "flat"]


@dataclass(frozen=True)
class IndexEntry:
    """One file in the index."""

    rel_path: str
    title: str
    is_overview: bool = False
    #: ``True`` when the title fell back to the filename stem because the
    #: markdown had no ``# `` heading and no ``summary:`` front-matter. Such
    #: entries degrade the navigation map (the model sees a bare filename
    #: instead of a description) and are flagged ⚠️ so they get fixed. Only
    #: set for markdown files — CSV / XLSX titles are stems by design.
    weak: bool = False


@dataclass(frozen=True)
class IndexGroup:
    """A named bucket of entries (a month, a year, a strategy dir, ...)."""

    name: str
    entries: tuple[IndexEntry, ...]


@dataclass(frozen=True)
class PartitionIndex:
    name: str
    label: str
    groups: tuple[IndexGroup, ...]
    file_count: int


@dataclass(frozen=True)
class KnowledgeIndex:
    kb_root: Path
    generated_at: datetime
    partitions: tuple[PartitionIndex, ...]
    total_files: int
    skipped: tuple[tuple[str, str], ...] = ()
    #: Rel paths of markdown entries whose title fell back to the stem (no
    #: heading / no ``summary:``). Surfaced in the rendered map so they get
    #: a real heading added — keeps the index a useful navigation surface.
    weak_titles: tuple[str, ...] = ()
    root_exists: bool = True


# ---------------------------------------------------------------------------
# Title extraction
# ---------------------------------------------------------------------------


def _decode_peek(data: bytes) -> str:
    """Decode a title-peek buffer, tolerating non-utf-8 markdown.

    Strict-decodes utf-8 first, then gbk / gb2312 for legacy CJK files.
    The final fallback is utf-8 with ``errors="ignore"`` — **never**
    latin-1: latin-1 never raises, so it would silently shadow a split
    multi-byte sequence (the 2 KB peek can land mid-character) and turn
    valid CJK into mojibake. ``errors="ignore"`` just drops the broken
    trailing bytes, which sit at the end of the peek and never reach the
    title (the first heading is at the top of the file).
    """

    for enc in ("utf-8", "gbk", "gb2312"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="ignore")


def _extract_title(text: str, fallback_stem: str) -> tuple[str, bool]:
    """Pick the best one-line title from a markdown peek.

    Returns ``(title, fell_back_to_stem)``. Priority: YAML ``summary:`` →
    first ``# `` heading → the filename stem. ``fell_back_to_stem`` is True
    only in the last case, flagging the entry as a weak index node.

    There is **deliberately no "first prose line" fallback**: the skill
    mandates every ``.md`` start with a ``# `` heading (or ``summary:``
    front-matter), so a prose-first file is a convention violation we want
    surfaced (weak) rather than silently masked with a maybe-vague line.
    """

    lines = text.splitlines()

    # Parse YAML front-matter first — an explicit ``summary:`` override wins
    # over any heading inside the body.
    summary: str | None = None
    body_start = 0
    if lines and lines[0].strip() == _FRONTMATTER_DELIM:
        try:
            close_idx = lines.index(_FRONTMATTER_DELIM, 1)
        except ValueError:
            close_idx = -1
        if close_idx > 0:
            for raw in lines[1:close_idx]:
                m = _FRONTMATTER_SUMMARY_RE.match(raw)
                if m:
                    val = m.group(1).strip().strip("'\"")
                    if val:
                        summary = val
            body_start = close_idx + 1

    if summary:
        return _clean(summary), False

    for raw in lines[body_start:]:
        line = raw.strip()
        if line.startswith("# "):
            return _clean(line[2:].strip()), False

    return _clean(fallback_stem), True


def _clean(title: str) -> str:
    """Collapse whitespace and strip a trailing markdown heading marker."""

    title = re.sub(r"\s+", " ", title).strip()
    return title


def _title_for_markdown(path: Path, rel: str) -> tuple[str, bool]:
    """Read the first bytes of a markdown file and extract ``(title, weak)``."""

    try:
        with path.open("rb") as fh:
            data = fh.read(_TITLE_PEEK_BYTES)
    except OSError as exc:
        logger.warning("knowledge index: cannot read %s: %s", rel, exc)
        raise
    return _extract_title(_decode_peek(data), fallback_stem=path.stem)


# ---------------------------------------------------------------------------
# Partition walking
# ---------------------------------------------------------------------------


def _list_files(root: Path) -> list[Path]:
    """Non-recursive file listing (immediate children only), hidden skipped."""

    if not root.is_dir():
        return []
    out: list[Path] = []
    for child in sorted(root.iterdir()):
        name = child.name
        if name.startswith("."):
            continue
        if child.is_file():
            out.append(child)
    return out


def _entry_for(path: Path, partition_root: Path, text_suffix: str) -> IndexEntry | None:
    """Build an :class:`IndexEntry`, or ``None`` if it must be skipped.

    Read failures are signalled by raising ``OSError``; the caller
    collects them so nothing is silently dropped (AGENTS.md §错误可见性).
    """

    rel = path.relative_to(partition_root).as_posix()
    is_overview = path.stem == _OVERVIEW_STEM
    weak = False
    if path.suffix.lower() == text_suffix and text_suffix == ".md":
        title, weak = _title_for_markdown(path, rel)
    else:
        # CSV / XLSX / other: list by name, do not parse content. These are
        # never "weak" — a stem title is the intended form for data files.
        title = path.stem
    return IndexEntry(rel_path=rel, title=title, is_overview=is_overview, weak=weak)


def _group_files(
    partition_root: Path, strategy: GroupStrategy, text_suffix: str
) -> tuple[tuple[IndexGroup, ...], tuple[tuple[str, str], ...]]:
    """Group a partition's files according to ``strategy``.

    Returns ``(groups, skipped)`` where ``skipped`` is a tuple of
    ``(rel_path, reason)`` for files that could not be titled.
    """

    skipped: list[tuple[str, str]] = []

    if strategy == "flat":
        entries: list[IndexEntry] = []
        for path in _list_files(partition_root):
            try:
                entry = _entry_for(path, partition_root, text_suffix)
            except OSError as exc:
                skipped.append((path.relative_to(partition_root).as_posix(), str(exc)))
                continue
            if entry is not None:
                entries.append(entry)
        entries.sort(key=lambda e: (not e.is_overview, e.rel_path))
        return (
            (IndexGroup(name=partition_root.name, entries=tuple(entries)),)
            if entries
            else (),
            tuple(skipped),
        )

    # Grouped strategies: walk immediate sub-directories.
    grouped: dict[str, list[IndexEntry]] = {}
    loose: list[IndexEntry] = []

    def _sort_key(name: str) -> tuple[int, str]:
        # Newer groups first: month/year sort naturally by descending
        # lexical; everything else after.
        return (-1 if _MONTH_RE.match(name) or _YEAR_RE.match(name) else 0, name)

    child_dirs: list[Path] = []
    if partition_root.is_dir():
        for child in sorted(partition_root.iterdir()):
            if child.name.startswith("."):
                continue
            if child.is_dir():
                child_dirs.append(child)

    for sub in sorted(child_dirs, key=lambda p: _sort_key(p.name), reverse=True):
        sub_entries: list[IndexEntry] = []
        for path in _list_files(sub):
            try:
                entry = _entry_for(path, partition_root, text_suffix)
            except OSError as exc:
                skipped.append((path.relative_to(partition_root).as_posix(), str(exc)))
                continue
            if entry is not None:
                sub_entries.append(entry)
        sub_entries.sort(key=lambda e: (not e.is_overview, e.rel_path))
        if sub_entries:
            grouped[sub.name] = sub_entries

    # Files directly under the partition root (not in a sub-dir).
    for path in _list_files(partition_root):
        try:
            entry = _entry_for(path, partition_root, text_suffix)
        except OSError as exc:
            skipped.append((path.relative_to(partition_root).as_posix(), str(exc)))
            continue
        if entry is not None:
            loose.append(entry)

    groups: list[IndexGroup] = []
    for name in sorted(grouped.keys(), key=_sort_key, reverse=True):
        groups.append(IndexGroup(name=name, entries=tuple(grouped[name])))
    if loose:
        loose.sort(key=lambda e: (not e.is_overview, e.rel_path))
        groups.append(IndexGroup(name="（根目录散文件）", entries=tuple(loose)))

    return tuple(groups), tuple(skipped)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_knowledge_index(kb_root: Path) -> KnowledgeIndex:
    """Walk ``kb_root`` and build a structured index of every partition.

    Missing partitions are simply empty (fresh-KB friendly); the
    ``root_exists`` flag records whether the knowledge base itself exists.
    """

    kb_root = kb_root.expanduser()
    root_exists = kb_root.is_dir()
    partitions: list[PartitionIndex] = []
    all_skipped: list[tuple[str, str]] = []
    all_weak: list[str] = []
    total = 0

    for name, label, strategy, text_suffix in _PARTITION_SPECS:
        partition_root = kb_root / name
        groups, skipped = _group_files(partition_root, strategy, text_suffix)  # type: ignore[arg-type]
        for s in skipped:
            all_skipped.append((f"{name}/{s[0]}", s[1]))
        for g in groups:
            for e in g.entries:
                if e.weak:
                    all_weak.append(f"{name}/{e.rel_path}")
        p_count = sum(len(g.entries) for g in groups)
        total += p_count
        partitions.append(
            PartitionIndex(name=name, label=label, groups=groups, file_count=p_count)
        )

    return KnowledgeIndex(
        kb_root=kb_root,
        generated_at=datetime.now(timezone.utc),
        partitions=tuple(partitions),
        total_files=total,
        skipped=tuple(all_skipped),
        weak_titles=tuple(all_weak),
        root_exists=root_exists,
    )


def render_index_markdown(index: KnowledgeIndex) -> str:
    """Render a :class:`KnowledgeIndex` as a compact markdown map."""

    if not index.root_exists:
        return (
            f"# 知识库索引（knowledge index）\n\n"
            f"> 知识库根目录不存在：`{index.kb_root}`\n"
            f"> 这通常是全新环境；用 in-process 文件工具或 CLI 创建分区目录后即可。\n"
        )

    ts = index.generated_at.astimezone().strftime("%Y-%m-%d %H:%M %z")
    weak_note = (
        f"，⚠️ {len(index.weak_titles)} 个弱标题（无标题行，仅显示文件名，需补 `# ` 标题）"
        if index.weak_titles
        else ""
    )
    lines: list[str] = [
        "# 知识库索引（knowledge index）",
        "",
        f"> 自动生成于 {ts} | 共 **{index.total_files}** 个文件{weak_note}。"
        " 这是导航地图（每文件一行摘要，不含正文）——先在此推理定位，"
        "再用 `read_file` 读具体文件的完整内容。",
        "",
    ]

    for partition in index.partitions:
        if not partition.groups:
            continue
        lines.append(f"## {partition.name}/ — {partition.label}")
        lines.append("")
        for group in partition.groups:
            if group.name and group.name != partition.name:
                lines.append(f"### {group.name}")
                lines.append("")
            for entry in group.entries:
                marker = "⭐ " if entry.is_overview else ("⚠️ " if entry.weak else "")
                lines.append(f"- `{entry.rel_path}` — {marker}{entry.title}")
            lines.append("")

    if index.weak_titles:
        lines.append("## ⚠️ 弱标题文件（缺少 `# ` 标题行 / `summary:`，需补）")
        lines.append("")
        lines.append(
            "这些文件在地图里只显示文件名，无法据此推理定位。给每篇补一个自描述的"
            " `# ` 首行（标的 + 判断 / 主题 + 区间），或在开头加 `summary:` front-matter。"
        )
        lines.append("")
        for rel in index.weak_titles:
            lines.append(f"- `{rel}`")
        lines.append("")

    if index.skipped:
        lines.append("## ⚠️ 跳过的文件（读取失败，需排查）")
        lines.append("")
        for rel, reason in index.skipped:
            lines.append(f"- `{rel}` — {reason}")
        lines.append("")

    if index.total_files == 0 and not index.skipped:
        lines.append(
            "_知识库为空。分区目录（cycles/ symbols/ trades/ journal/ playbook/ backtests/）"
            "下还没有文件。_\n"
        )

    return "\n".join(lines).rstrip() + "\n"


def write_index_file(index: KnowledgeIndex, path: Path | None = None) -> Path:
    """Persist the rendered index to ``<kb_root>/_index.md`` (or ``path``).

    Returns the resolved path written. Used by ``doyoutrade-cli knowledge
    index --refresh``. The in-process ``knowledge_index`` tool does NOT
    call this — it returns a fresh map every call so the agent never
    reasons over a stale snapshot.
    """

    target = (path or (index.kb_root / "_index.md")).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_index_markdown(index), encoding="utf-8")
    return target


__all__ = [
    "GroupStrategy",
    "IndexEntry",
    "IndexGroup",
    "KnowledgeIndex",
    "PartitionIndex",
    "build_knowledge_index",
    "render_index_markdown",
    "write_index_file",
]
