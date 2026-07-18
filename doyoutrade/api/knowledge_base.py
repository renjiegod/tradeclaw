"""Read-only HTTP access to the user's private knowledge base.

The knowledge base at ``~/.doyoutrade/knowledge`` is otherwise agent-sandbox-only
(see ``doyoutrade/tools/_sandbox.py`` + the ``doyoutrade-knowledge`` skill). This
router exposes two read-only surfaces, both path-sandboxed and size-capped:

1. **Journal reader** (``GET /knowledge/journals`` + ``GET /knowledge/journal``)
   — the original ``journal/``-only reader used by the task-detail "复盘" tab.
2. **Full-base browser** (``GET /knowledge/index`` + ``GET /knowledge/file``)
   — the top-level Knowledge page: a structured index of every partition
   (powered by ``doyoutrade.knowledge.index``) + a reader for any ``.md`` /
   ``.csv`` file under one of the partitions.
3. **Structured partition feeds** — ``GET /knowledge/sentiment-timeline`` /
   ``symbol-roles`` / ``trade-attribution`` / ``playbook``: parsed, front-end
   ready projections of specific partitions.

Deliberate scope limits (the KB is private memory — see the skill's "Privacy
boundary"):
- **KB files remain read-only**: graph DB edits use audited changesets; local
  users apply directly while Agent proposals require one-time human approval.
- **fixed partition allowlist**: ``cycles`` / ``symbols`` / ``trades`` /
  ``journal`` / ``playbook`` / ``backtests`` — every ``/knowledge/file`` path is
  sandboxed to ``<kb_root>/<partition>/`` and rejected on traversal / symlink
  escape.
- **suffix allowlist**: ``.md`` / ``.markdown`` / ``.csv`` only.
- size-capped; path-traversal / null-byte / absolute paths rejected.

When a partition directory does not exist yet the relevant endpoint returns an
empty / ``root_exists: false`` result rather than erroring — a fresh KB is a
legitimate "nothing here yet" state, not a failure.
"""

from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import yaml
from fastapi import APIRouter, HTTPException, Query, Request

from doyoutrade.api._skill_paths import SkillPathError, resolve_inside
from doyoutrade.knowledge.index import _extract_title

logger = logging.getLogger(__name__)

#: Journals are markdown; never serve other file types even within journal/.
_JOURNAL_SUFFIXES = {".md", ".markdown"}
#: Same cap as the skills file API; a journal over this is a misuse — edit locally.
MAX_JOURNAL_BYTES = 1 << 20  # 1 MiB
#: The knowledge-base partitions a browser file read may target, in the
#: canonical display order kept by ``doyoutrade.knowledge.index``.
_KB_PARTITIONS: tuple[str, ...] = (
    "cycles",
    "symbols",
    "trades",
    "journal",
    "playbook",
    "backtests",
)
#: Suffixes the full-browser file reader will serve (markdown + broker CSV).
_KB_FILE_SUFFIXES = {".md", ".markdown", ".csv"}
#: Size cap for the full-browser file reader (same envelope as journals).
MAX_KB_FILE_BYTES = 2 << 20  # 2 MiB
#: Cap on CSV rows returned by the browser (trades exports can be huge); the
#: frontend renders the rest client-side-paginated, so this is a safety ceiling.
_MAX_CSV_ROWS = 5000

#: Playbook front-matter fields projected onto the API surface. ``tags`` is a
#: list (defaults to ``[]``); the rest are scalars (default ``None``). Kept as a
#: fixed projection so hand-edited extra keys don't leak into the feed.
_PLAYBOOK_FM_DELIM = "---"

KnowledgeRootResolver = Callable[[], Path]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _journal_root(kb_root_resolver: KnowledgeRootResolver) -> Path:
    return kb_root_resolver().expanduser() / "journal"


def _partition_root(kb_root_resolver: KnowledgeRootResolver, partition: str) -> Path:
    """Resolve ``<kb_root>/<partition>`` after validating the partition name."""

    if partition not in _KB_PARTITIONS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown partition {partition!r}; one of: {', '.join(_KB_PARTITIONS)}",
        )
    return kb_root_resolver().expanduser() / partition


def _parse_csv_rows(content: str) -> tuple[list[str], list[list[str]]]:
    """Parse CSV text into ``(columns, rows)`` with a hard row ceiling.

    Uses ``csv.reader``; the first record is the header. Returns ``([], [])``
    for an empty file. Rows beyond ``_MAX_CSV_ROWS`` are dropped and the count
    is surfaced by the caller so the UI can warn it is truncated.
    """

    reader = csv.reader(content.splitlines())
    records = list(reader)
    if not records:
        return [], []
    columns = [c.strip() for c in records[0]]
    # Broker exports often ship a UTF-8 BOM on the first header cell; strip it
    # so the UI table header doesn't render the invisible ``\ufeff`` prefix.
    if columns and columns[0].startswith("\ufeff"):
        columns[0] = columns[0][1:]
    rows = [r for r in records[1:][:_MAX_CSV_ROWS] if r]
    return columns, rows


def _decode_playbook(data: bytes) -> str:
    """Decode a playbook markdown file, tolerating legacy CJK encodings.

    Mirrors the knowledge index's peek decoder (utf-8 → gbk → gb2312 →
    utf-8/ignore) so a playbook authored in a legacy editor still parses. Unlike
    the index this decodes the *whole* file (front-matter can extend past the
    2 KB title peek), but playbooks are small prose notes so this stays cheap.
    """

    for enc in ("utf-8", "gbk", "gb2312"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="ignore")


def _parse_playbook_frontmatter(text: str, rel: str) -> dict[str, Any]:
    """Parse a playbook's YAML front-matter into the fixed field projection.

    Returns ``{"pattern", "stage", "summary", "tags"}`` where the three scalars
    default to ``None`` and ``tags`` defaults to ``[]``. When there is no
    front-matter block the defaults are returned as-is. **Broken YAML is
    loud-skipped** (``logger.info`` with the rel path + error) rather than
    crashing — the file's title / path / mtime still surface, only its
    structured fields fall back to the defaults (§错误可见性: no silent drop,
    surfaced with a hint).

    ``tags`` is coerced to a list of strings; a scalar ``tags:`` value is
    wrapped into a one-element list, a non-list/non-scalar (e.g. a dict) falls
    back to ``[]`` with a loud skip. Scalar fields that arrive as a non-scalar
    (list/dict) are stringified defensively so the API surface stays typed.
    """

    defaults: dict[str, Any] = {
        "pattern": None,
        "stage": None,
        "summary": None,
        "tags": [],
    }

    lines = text.splitlines()
    if not lines or lines[0].strip() != _PLAYBOOK_FM_DELIM:
        return defaults
    try:
        close_idx = lines.index(_PLAYBOOK_FM_DELIM, 1)
    except ValueError:
        # Opened but never closed — a malformed block. Loud-skip the fields.
        logger.info(
            "knowledge playbook %s: unterminated front-matter block; "
            "structured fields omitted (hint: close the leading '---' block)",
            rel,
        )
        return defaults

    block = "\n".join(lines[1:close_idx])
    try:
        meta = yaml.safe_load(block)
    except yaml.YAMLError as exc:
        logger.info(
            "knowledge playbook %s: bad front-matter YAML (%s): %s; "
            "structured fields omitted (hint: fix the YAML front-matter)",
            rel, type(exc).__name__, exc,
        )
        return defaults
    if meta is None:
        return defaults
    if not isinstance(meta, dict):
        logger.info(
            "knowledge playbook %s: front-matter is not a mapping (got %s); "
            "structured fields omitted (hint: use 'key: value' front-matter)",
            rel, type(meta).__name__,
        )
        return defaults

    out = dict(defaults)
    for field in ("pattern", "stage", "summary"):
        val = meta.get(field)
        if val is None:
            continue
        # Scalars only for these; stringify a stray list/dict defensively.
        out[field] = val if isinstance(val, str) else str(val)

    tags = meta.get("tags")
    if isinstance(tags, list):
        out["tags"] = [t if isinstance(t, str) else str(t) for t in tags]
    elif isinstance(tags, str):
        out["tags"] = [tags]
    elif tags is not None:
        logger.info(
            "knowledge playbook %s: 'tags' is neither list nor scalar (got %s); "
            "using empty tags (hint: write tags as a YAML list)",
            rel, type(tags).__name__,
        )
    return out


def build_knowledge_router(
    kb_root_resolver: KnowledgeRootResolver,
    *,
    knowledge_graph_repository=None,
) -> APIRouter:
    """Build the ``/knowledge`` router.

    ``kb_root_resolver`` returns the absolute KB root (``knowledge_root`` from
    ``doyoutrade.tools._sandbox``); the journal partition is ``<root>/journal``.

    文件面保持只读（KB 写入一律走 agent 沙箱）。``knowledge_graph_repository``
    （可选，``SqlAlchemyKnowledgeGraphRepository``）装配后追加图谱查询、同步
    与 audited changeset 编辑面；这些写入只修改 DB 图谱，不破坏 KB 文件只读
    边界。未装配时相关端点返回 503（结构化拒绝，不静默消失）。
    """

    router = APIRouter()

    @router.get("/knowledge/journals")
    async def list_journals() -> dict:
        """List 复盘 journal markdown files under ``journal/`` (recursive).

        Returns newest-first (paths are ``YYYY/YYYY-MM-DD.md`` so lexical-desc
        ≈ date-desc). Empty + ``root_exists: false`` when the dir is absent.
        """
        root = _journal_root(kb_root_resolver)
        if not root.is_dir():
            return {"items": [], "root_exists": False}
        items: list[dict] = []
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in _JOURNAL_SUFFIXES:
                continue
            try:
                stat = path.stat()
                rel = path.relative_to(root).as_posix()
            except (OSError, ValueError) as exc:  # pragma: no cover - defensive
                logger.warning("knowledge journal list: skipping %s: %s", path, exc)
                continue
            items.append(
                {
                    "path": rel,
                    "title": path.stem,
                    "size": stat.st_size,
                    "mtime": _iso(stat.st_mtime),
                }
            )
        items.sort(key=lambda it: it["path"], reverse=True)
        return {"items": items, "root_exists": True}

    @router.get("/knowledge/journal")
    async def read_journal(path: str = Query(..., description="journal-relative .md path")) -> dict:
        """Read one journal markdown file (sandboxed to ``journal/``)."""
        root = _journal_root(kb_root_resolver)
        try:
            target = resolve_inside(root, path)
        except SkillPathError as exc:
            # Path traversal / absolute / null — reject without touching disk.
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if target.suffix.lower() not in _JOURNAL_SUFFIXES:
            raise HTTPException(status_code=400, detail=f"not a journal markdown file: {path!r}")
        if not target.is_file():
            raise HTTPException(status_code=404, detail=f"journal not found: {path!r}")
        try:
            stat = target.stat()
            if stat.st_size > MAX_JOURNAL_BYTES:
                raise HTTPException(status_code=413, detail="journal too large; read it locally")
            content = target.read_text(encoding="utf-8")
        except HTTPException:
            raise
        except OSError as exc:
            logger.error("knowledge journal read failed path=%s: %s", path, exc)
            raise HTTPException(status_code=500, detail="failed to read journal") from exc
        return {
            "path": path,
            "title": target.stem,
            "content": content,
            "size": stat.st_size,
            "mtime": _iso(stat.st_mtime),
        }

    # ------------------------------------------------------------------
    # Full-base browser (top-level Knowledge page)
    # ------------------------------------------------------------------

    @router.get("/knowledge/index")
    async def knowledge_index(self_partition: str | None = Query(
        None, alias="partition", description="optional single partition to scope to"
    )) -> dict:
        """Return the structured knowledge-base index (the navigation map).

        Mirrors ``doyoutrade.knowledge.build_knowledge_index``: every partition
        grouped by month / year / strategy, one title per file, with ⭐ overview
        flags and ⚠️ weak-title flags. Optional ``?partition=`` scopes to one
        partition. Fresh-generated on every call (never a stale snapshot).
        """
        from doyoutrade.knowledge import build_knowledge_index

        index = build_knowledge_index(kb_root_resolver())
        if self_partition is not None:
            if self_partition not in _KB_PARTITIONS:
                raise HTTPException(
                    status_code=400,
                    detail=f"unknown partition {self_partition!r}; one of: {', '.join(_KB_PARTITIONS)}",
                )
            kept = tuple(p for p in index.partitions if p.name == self_partition)
            import dataclasses
            index = dataclasses.replace(
                index,
                partitions=kept,
                total_files=sum(p.file_count for p in kept),
            )

        partitions_out: list[dict[str, Any]] = []
        for partition in index.partitions:
            groups_out = [
                {
                    "name": g.name,
                    "entries": [
                        {
                            "rel_path": e.rel_path,
                            "title": e.title,
                            "is_overview": e.is_overview,
                            "weak": e.weak,
                            "suffix": Path(e.rel_path).suffix.lower(),
                        }
                        for e in g.entries
                    ],
                }
                for g in partition.groups
            ]
            partitions_out.append({
                "name": partition.name,
                "label": partition.label,
                "file_count": partition.file_count,
                "groups": groups_out,
            })

        return {
            "root_exists": index.root_exists,
            "total_files": index.total_files,
            "weak_title_count": len(index.weak_titles),
            "skipped_count": len(index.skipped),
            "weak_titles": list(index.weak_titles),
            "generated_at": index.generated_at.isoformat(),
            "partitions": partitions_out,
        }

    @router.get("/knowledge/file")
    async def read_knowledge_file(
        partition: str = Query(..., description="one of cycles/symbols/trades/journal/playbook/backtests"),
        path: str = Query(..., description="partition-relative .md / .csv path"),
    ) -> dict:
        """Read one file from any partition (markdown content or parsed CSV).

        Sandboxed to ``<kb_root>/<partition>/``; suffix allowlist
        ``.md`` / ``.markdown`` / ``.csv``; size-capped. Markdown is returned
        as raw text (``kind: "markdown"``); CSV is parsed into
        ``columns`` + ``rows`` (``kind: "csv"``, row-capped at
        :data:`_MAX_CSV_ROWS` with ``truncated`` flag).
        """
        root = _partition_root(kb_root_resolver, partition)
        try:
            target = resolve_inside(root, path)
        except SkillPathError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        suffix = target.suffix.lower()
        if suffix not in _KB_FILE_SUFFIXES:
            raise HTTPException(
                status_code=400,
                detail=f"unsupported file type {suffix!r}; allowed: {', '.join(sorted(_KB_FILE_SUFFIXES))}",
            )
        if not target.is_file():
            raise HTTPException(status_code=404, detail=f"file not found: {partition}/{path}")

        try:
            stat = target.stat()
            if stat.st_size > MAX_KB_FILE_BYTES:
                raise HTTPException(status_code=413, detail="file too large; read it locally")
            raw = target.read_text(encoding="utf-8")
        except HTTPException:
            raise
        except OSError as exc:
            logger.error("knowledge file read failed %s/%s: %s", partition, path, exc)
            raise HTTPException(status_code=500, detail="failed to read file") from exc

        base = {
            "partition": partition,
            "path": path,
            "title": target.stem,
            "size": stat.st_size,
            "mtime": _iso(stat.st_mtime),
            "suffix": suffix,
        }
        if suffix in _JOURNAL_SUFFIXES:
            return {**base, "kind": "markdown", "content": raw}
        # CSV → parsed table.
        columns, rows = _parse_csv_rows(raw)
        truncated = len(rows) >= _MAX_CSV_ROWS
        return {
            **base,
            "kind": "csv",
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
        }

    # ------------------------------------------------------------------
    # Sentiment cycle timeline (读 cycles/*/_sentiment.jsonl)
    # ------------------------------------------------------------------

    @router.get("/knowledge/sentiment-timeline")
    async def sentiment_timeline(
        months: int = Query(
            3,
            ge=1,
            le=60,
            description="How many trailing calendar months of 情绪 rows to return.",
        )
    ) -> dict:
        """Return the merged 情绪周期 (sentiment-cycle) timeline for the frontend.

        Reads every ``cycles/<YYYY-MM>/_sentiment.jsonl`` (each a per-trading-day
        row the daily review appends), merges + sorts them ascending by ``date``,
        and keeps the most recent ``months`` calendar months. Returns
        ``{"items": [{date, label, limit_up_count, limit_down_count,
        broken_board_count, broken_board_rate, max_streak}]}``.

        A fresh KB with no logs returns ``{"items": []}`` (not an error), and
        malformed rows are skipped rather than crashing — the read layer
        (:func:`doyoutrade.knowledge.review.read_sentiment_timeline`) owns that
        discipline.
        """
        from doyoutrade.knowledge.review import read_sentiment_timeline

        # Anchor the read to the same KB root this router was built with (the
        # app passes ``knowledge_root``; tests pass a temp dir), so the timeline
        # and the file/index endpoints all resolve the same base. A fresh KB
        # with no logs is a clean empty list, not an error.
        try:
            return read_sentiment_timeline(
                months=months, root=kb_root_resolver().expanduser()
            )
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 — surfaced, not swallowed
            logger.error(
                "knowledge sentiment-timeline read failed months=%s (%s): %s",
                months, type(exc).__name__, exc,
            )
            raise HTTPException(
                status_code=500, detail="failed to read sentiment timeline"
            ) from exc

    # ------------------------------------------------------------------
    # Symbol role cards (读 symbols/roles.jsonl)
    # ------------------------------------------------------------------

    @router.get("/knowledge/symbol-roles")
    async def symbol_roles() -> dict:
        """Return the structured 个股角色卡 (symbol role cards) for the frontend.

        Reads ``symbols/roles.jsonl`` (an append-only role log the agent writes
        on explicit request), de-duplicates by ``symbol`` (last-wins — a later
        append supersedes an earlier one), and returns
        ``{"items": [{symbol, name, role, note, strategy_hint, updated_at}]}``
        sorted newest-first by ``updated_at``. This is the structured companion
        to the prose ``symbols/roles.md`` narrative index (both coexist).

        A fresh KB with no role log returns ``{"items": []}`` (not an error);
        malformed rows are skipped rather than crashing — the read layer
        (:func:`doyoutrade.knowledge.roles.read_symbol_roles`) owns that
        discipline. A file present but unreadable surfaces as a 500 (not
        swallowed into an empty list).
        """
        from doyoutrade.knowledge.roles import read_symbol_roles

        # Anchor the read to the same KB root this router was built with (the
        # app passes ``knowledge_root``; tests pass a temp dir) so roles resolve
        # the same base as the file/index endpoints.
        try:
            return read_symbol_roles(root=kb_root_resolver().expanduser())
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 — surfaced, not swallowed
            logger.error(
                "knowledge symbol-roles read failed (%s): %s",
                type(exc).__name__, exc,
            )
            raise HTTPException(
                status_code=500, detail="failed to read symbol roles"
            ) from exc

    # ------------------------------------------------------------------
    # 交割单归因 (trade attribution) — FIFO round-trip P&L over trades/*.csv
    # ------------------------------------------------------------------

    @router.get("/knowledge/trade-attribution")
    async def trade_attribution(
        months: int | None = Query(
            None,
            ge=1,
            le=120,
            description="Keep only round-trips closed within the most recent N "
            "calendar months (self-relative to the newest close). Omit for all.",
        )
    ) -> dict:
        """Return FIFO round-trip P&L attribution over the ``trades/`` CSVs.

        Reads every broker-exported ``trades/**/*.csv`` (kept verbatim; columns
        are normalised on read across broker formats), FIFO-pairs each symbol's
        buys/sells into round-trips, and rolls up win rate / realised P&L /
        profit factor / hold days / best-worst / per-symbol stats. Returns
        ``{summary, round_trips, by_symbol, unparsed}`` — money as decimal
        strings; ``unparsed`` surfaces any file / row that could not be parsed.

        A fresh KB with no ``trades/`` returns a structured empty result (zeroed
        summary, empty lists), not an error. A read failure surfaces as a 500
        (not swallowed into an empty result). The read layer
        (:func:`doyoutrade.knowledge.attribution.read_trade_attribution`) owns the
        parse-tolerance / loud-skip discipline.
        """
        from doyoutrade.knowledge.attribution import read_trade_attribution

        # Anchor the read to the same KB root this router was built with (the
        # app passes ``knowledge_root``; tests pass a temp dir).
        try:
            return read_trade_attribution(
                months=months, root=kb_root_resolver().expanduser()
            )
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 — surfaced, not swallowed
            logger.error(
                "knowledge trade-attribution read failed months=%s (%s): %s",
                months, type(exc).__name__, exc,
            )
            raise HTTPException(
                status_code=500, detail="failed to read trade attribution"
            ) from exc

    # ------------------------------------------------------------------
    # 打板模式库 / 战法总结 (playbook) — 遍历 playbook/**/*.md
    # ------------------------------------------------------------------

    @router.get("/knowledge/playbook")
    async def playbook() -> dict:
        """Return the 打板模式库 / 战法总结 (playbook) entries for the frontend.

        Walks every ``playbook/**/*.md``, and for each file parses:

        - ``title`` — first ``# `` heading (or YAML ``summary:`` front-matter),
          via the same extractor the knowledge index uses (so map + this feed
          agree on titles);
        - YAML front-matter fields ``pattern`` (打法名) / ``stage`` (适用情绪阶段)
          / ``tags`` (数组) / ``summary`` (一句话) — each ``None`` / ``[]`` when
          absent;
        - ``path`` (playbook-relative) + ``updated_at`` (file mtime → ISO).

        Returns ``{"items": [{path, title, summary, pattern, stage, tags,
        updated_at}]}`` sorted ``updated_at`` descending. A fresh KB with no
        ``playbook/`` returns ``{"items": []}`` (not an error). A file with
        broken front-matter is **loud-skipped for the front-matter fields only**
        (``logger.info`` — title / path / mtime still surface); a file that
        cannot be read at all is loud-skipped entirely rather than crashing the
        feed (§错误可见性). A hard read failure of the directory surfaces as 500.
        """
        root = kb_root_resolver().expanduser() / "playbook"
        if not root.is_dir():
            return {"items": []}

        try:
            paths = sorted(p for p in root.rglob("*.md") if p.is_file())
        except OSError as exc:
            logger.error(
                "knowledge playbook walk failed root=%s (%s): %s",
                root, type(exc).__name__, exc,
            )
            raise HTTPException(
                status_code=500, detail="failed to read playbook"
            ) from exc

        items: list[dict[str, Any]] = []
        for path in paths:
            try:
                rel = path.relative_to(root).as_posix()
                stat = path.stat()
                raw = path.read_bytes()
            except (OSError, ValueError) as exc:
                # One unreadable file must not take down the whole feed: skip it
                # loudly (never a silent drop — AGENTS.md §错误可见性).
                logger.info(
                    "knowledge playbook skipping unreadable file %s (%s): %s",
                    path, type(exc).__name__, exc,
                )
                continue
            text = _decode_playbook(raw)
            title, _weak = _extract_title(text, fallback_stem=path.stem)
            fm = _parse_playbook_frontmatter(text, rel)
            items.append(
                {
                    "path": rel,
                    "title": title,
                    "summary": fm["summary"],
                    "pattern": fm["pattern"],
                    "stage": fm["stage"],
                    "tags": fm["tags"],
                    "updated_at": _iso(stat.st_mtime),
                }
            )
        items.sort(key=lambda it: str(it["updated_at"] or ""), reverse=True)
        return {"items": items}

    # ---- knowledge graph（kg_nodes / kg_edges 之上的实体关系面） ----------

    @router.get("/knowledge/graph/schema")
    async def graph_schema() -> dict:
        """Return protected system ontology merged with custom definitions."""

        from doyoutrade.knowledge.schema import system_schema_payload

        session_factory = getattr(
            knowledge_graph_repository,
            "session_factory",
            None,
        )
        if session_factory is not None:
            from doyoutrade.knowledge.editing import KnowledgeGraphCommandService

            return await KnowledgeGraphCommandService(
                session_factory
            ).get_schema()
        return system_schema_payload()

    def _require_graph_repo():
        if knowledge_graph_repository is None:
            raise HTTPException(
                status_code=503,
                detail="knowledge graph repository is not wired in this runtime",
            )
        return knowledge_graph_repository

    def _require_graph_editor():
        repo = _require_graph_repo()
        session_factory = getattr(repo, "session_factory", None)
        if session_factory is None:
            raise HTTPException(
                status_code=503,
                detail="knowledge graph editor is not wired in this runtime",
            )
        from doyoutrade.knowledge.editing import KnowledgeGraphCommandService

        return KnowledgeGraphCommandService(session_factory)

    def _graph_edit_error(exc: Exception) -> HTTPException:
        from doyoutrade.knowledge.editing import (
            GraphChangeSetNotFound,
            GraphEditError,
            GraphProposalMismatch,
            GraphRevisionConflict,
            GraphSchemaValidationError,
        )

        if isinstance(exc, GraphChangeSetNotFound):
            status_code = 404
        elif isinstance(exc, (GraphProposalMismatch, GraphRevisionConflict)):
            status_code = 409
        elif isinstance(exc, GraphSchemaValidationError):
            status_code = 422
        elif isinstance(exc, GraphEditError):
            status_code = 400
        else:  # pragma: no cover - only called for GraphEditError subclasses
            status_code = 500
        return HTTPException(
            status_code=status_code,
            detail={
                "error_code": getattr(exc, "error_code", "graph_edit_failed"),
                "message": str(exc),
            },
        )

    def _require_local_graph_mutation(request: Request) -> None:
        """Reject cross-origin or non-loopback graph mutation requests."""

        allowed_hosts = {"127.0.0.1", "::1", "localhost", "testclient"}
        client_host = request.client.host if request.client is not None else None
        if client_host not in allowed_hosts:
            raise HTTPException(
                status_code=403,
                detail="knowledge graph mutations are restricted to local clients",
            )
        origin = request.headers.get("origin")
        if origin and urlparse(origin).hostname not in allowed_hosts:
            raise HTTPException(
                status_code=403,
                detail="knowledge graph mutations reject non-local browser origins",
            )

    @router.post("/knowledge/graph/changes", status_code=201)
    async def apply_graph_change(
        payload: dict[str, Any],
        request: Request,
    ) -> dict:
        """Apply one local-user changeset immediately with optimistic locking."""

        _require_local_graph_mutation(request)
        allowed = {"operations", "summary", "expected_revision"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"unknown fields: {', '.join(unknown)}",
            )
        expected_revision = payload.get("expected_revision")
        if not isinstance(expected_revision, int) or isinstance(
            expected_revision, bool
        ):
            raise HTTPException(
                status_code=400,
                detail="expected_revision must be an integer",
            )
        editor = _require_graph_editor()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            return await editor.apply_local_change(
                payload.get("operations"),
                summary=str(payload.get("summary") or ""),
                expected_revision=expected_revision,
                actor_id="local-user",
                now=now,
            )
        except Exception as exc:
            from doyoutrade.knowledge.editing import GraphEditError

            if isinstance(exc, GraphEditError):
                raise _graph_edit_error(exc) from exc
            raise

    @router.post("/knowledge/graph/change-drafts", status_code=201)
    async def create_graph_change_draft(
        payload: dict[str, Any],
        request: Request,
    ) -> dict:
        """Persist an Agent proposal; this endpoint never mutates graph facts."""

        _require_local_graph_mutation(request)
        allowed = {"operations", "summary", "actor_id"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"unknown fields: {', '.join(unknown)}",
            )
        editor = _require_graph_editor()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            return await editor.create_agent_draft(
                payload.get("operations"),
                summary=str(payload.get("summary") or ""),
                actor_id=str(payload.get("actor_id") or "agent"),
                now=now,
            )
        except Exception as exc:
            from doyoutrade.knowledge.editing import GraphEditError

            if isinstance(exc, GraphEditError):
                raise _graph_edit_error(exc) from exc
            raise

    @router.get("/knowledge/graph/change-sets")
    async def list_graph_change_sets(
        status: str | None = Query(None),
        limit: int = Query(100, ge=1, le=500),
    ) -> dict:
        """List graph history or the pending Agent approval inbox."""

        editor = _require_graph_editor()
        return {"items": await editor.list_change_sets(status=status, limit=limit)}

    def _expected_revision(payload: dict[str, Any]) -> int:
        expected = payload.get("expected_revision")
        if not isinstance(expected, int) or isinstance(expected, bool):
            raise HTTPException(
                status_code=400,
                detail="expected_revision must be an integer",
            )
        return expected

    @router.post("/knowledge/graph/revisions/{revision}/undo")
    async def undo_graph_revision(
        revision: int,
        payload: dict[str, Any],
        request: Request,
    ) -> dict:
        """Create a compensating revision for one manual graph revision."""

        _require_local_graph_mutation(request)
        allowed = {"expected_revision"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"unknown fields: {', '.join(unknown)}",
            )
        editor = _require_graph_editor()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            return await editor.undo_revision(
                revision,
                expected_revision=_expected_revision(payload),
                actor_id="local-user",
                now=now,
            )
        except Exception as exc:
            from doyoutrade.knowledge.editing import GraphEditError

            if isinstance(exc, GraphEditError):
                raise _graph_edit_error(exc) from exc
            raise

    @router.post("/knowledge/graph/revisions/{revision}/redo")
    async def redo_graph_revision(
        revision: int,
        payload: dict[str, Any],
        request: Request,
    ) -> dict:
        """Replay one manual graph revision as a new audited revision."""

        _require_local_graph_mutation(request)
        allowed = {"expected_revision"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"unknown fields: {', '.join(unknown)}",
            )
        editor = _require_graph_editor()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            return await editor.redo_revision(
                revision,
                expected_revision=_expected_revision(payload),
                actor_id="local-user",
                now=now,
            )
        except Exception as exc:
            from doyoutrade.knowledge.editing import GraphEditError

            if isinstance(exc, GraphEditError):
                raise _graph_edit_error(exc) from exc
            raise

    @router.put("/knowledge/graph/schema/{kind}/{key}")
    async def upsert_graph_schema_item(
        kind: str,
        key: str,
        payload: dict[str, Any],
        request: Request,
    ) -> dict:
        """Create or update one custom Schema item as an audited revision."""

        _require_local_graph_mutation(request)
        allowed = {"definition", "expected_revision", "expected_version"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"unknown fields: {', '.join(unknown)}",
            )
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or isinstance(
            expected_version,
            bool,
        ):
            raise HTTPException(
                status_code=400,
                detail="expected_version must be an integer",
            )
        editor = _require_graph_editor()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            return await editor.apply_local_change(
                [
                    {
                        "op": "upsert_schema_item",
                        "kind": kind,
                        "key": key,
                        "expected_version": expected_version,
                        "definition": payload.get("definition"),
                    }
                ],
                summary=f"更新自定义 Schema：{kind}/{key}",
                expected_revision=_expected_revision(payload),
                actor_id="local-user",
                now=now,
            )
        except Exception as exc:
            from doyoutrade.knowledge.editing import GraphEditError

            if isinstance(exc, GraphEditError):
                raise _graph_edit_error(exc) from exc
            raise

    @router.delete("/knowledge/graph/schema/{kind}/{key}")
    async def deprecate_graph_schema_item(
        kind: str,
        key: str,
        payload: dict[str, Any],
        request: Request,
    ) -> dict:
        """Deprecate a custom Schema item; referenced system items are protected."""

        _require_local_graph_mutation(request)
        allowed = {"expected_revision", "expected_version"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"unknown fields: {', '.join(unknown)}",
            )
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or isinstance(
            expected_version,
            bool,
        ):
            raise HTTPException(
                status_code=400,
                detail="expected_version must be an integer",
            )
        editor = _require_graph_editor()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            return await editor.apply_local_change(
                [
                    {
                        "op": "deprecate_schema_item",
                        "kind": kind,
                        "key": key,
                        "expected_version": expected_version,
                    }
                ],
                summary=f"弃用自定义 Schema：{kind}/{key}",
                expected_revision=_expected_revision(payload),
                actor_id="local-user",
                now=now,
            )
        except Exception as exc:
            from doyoutrade.knowledge.editing import GraphEditError

            if isinstance(exc, GraphEditError):
                raise _graph_edit_error(exc) from exc
            raise

    @router.post("/knowledge/graph/change-drafts/{change_set_id}/approve")
    async def approve_graph_change_draft(
        change_set_id: str,
        payload: dict[str, Any],
        request: Request,
    ) -> dict:
        """Human-approve one exact Agent proposal; no approve-always mode."""

        _require_local_graph_mutation(request)
        allowed = {"proposal_hash", "resolver_id"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"unknown fields: {', '.join(unknown)}",
            )
        editor = _require_graph_editor()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            return await editor.approve_draft(
                change_set_id,
                proposal_hash=str(payload.get("proposal_hash") or ""),
                resolver_id=str(payload.get("resolver_id") or "local-user"),
                decision_source="web",
                now=now,
            )
        except Exception as exc:
            from doyoutrade.knowledge.editing import GraphEditError

            if isinstance(exc, GraphEditError):
                raise _graph_edit_error(exc) from exc
            raise

    @router.post("/knowledge/graph/change-drafts/{change_set_id}/reject")
    async def reject_graph_change_draft(
        change_set_id: str,
        payload: dict[str, Any],
        request: Request,
    ) -> dict:
        """Human-reject one exact Agent proposal without mutating graph facts."""

        _require_local_graph_mutation(request)
        allowed = {"proposal_hash", "resolver_id", "reason"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"unknown fields: {', '.join(unknown)}",
            )
        editor = _require_graph_editor()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            return await editor.reject_draft(
                change_set_id,
                proposal_hash=str(payload.get("proposal_hash") or ""),
                resolver_id=str(payload.get("resolver_id") or "local-user"),
                decision_source="web",
                reason=str(payload.get("reason") or ""),
                now=now,
            )
        except Exception as exc:
            from doyoutrade.knowledge.editing import GraphEditError

            if isinstance(exc, GraphEditError):
                raise _graph_edit_error(exc) from exc
            raise

    def _iso_or_none(value) -> str | None:
        return value.isoformat() if value is not None else None

    def _node_payload(node) -> dict:
        return {
            "id": node.id,
            "node_type": node.node_type,
            "name": node.name,
            "display_name": node.display_name,
            "attrs": node.attrs,
            "status": getattr(node, "status", None) or "active",
            "retired_at": _iso_or_none(getattr(node, "retired_at", None)),
            "redirect_to_id": getattr(node, "redirect_to_id", None),
        }

    def _edge_payload(edge) -> dict:
        return {
            "id": edge.id,
            "src_id": edge.src_id,
            "dst_id": edge.dst_id,
            "relation": edge.relation,
            "fact": edge.fact,
            "attrs": edge.attrs,
            "provenance": edge.provenance,
            "confidence": edge.confidence,
            "source_ref": edge.source_ref,
            "valid_at": _iso_or_none(edge.valid_at),
            "invalid_at": _iso_or_none(edge.invalid_at),
            "created_at": _iso_or_none(edge.created_at),
            "expired_at": _iso_or_none(edge.expired_at),
        }

    @router.get("/knowledge/graph")
    async def graph_neighborhood(
        entity: str = Query(..., min_length=1, description="实体：代码/名称/角色/YYYY-MM/信号 id"),
        hops: int = Query(1, ge=1, le=3),
        include_expired: bool = Query(False),
    ) -> dict:
        """Resolve ``entity`` and return its N-hop neighborhood subgraph."""
        repo = _require_graph_repo()
        try:
            matches = await repo.find_nodes(entity.strip())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not matches:
            raise HTTPException(status_code=404, detail=f"no graph node matches {entity!r}")
        center = matches[0]
        nodes, edges = await repo.neighborhood(
            center.id, hops=hops, include_expired=include_expired
        )
        session_factory = getattr(repo, "session_factory", None)
        revision = 0
        if session_factory is not None:
            from doyoutrade.knowledge.editing import KnowledgeGraphCommandService

            revision = await KnowledgeGraphCommandService(
                session_factory
            ).get_head_revision()
        return {
            "revision": revision,
            "center": _node_payload(center),
            "candidates": [_node_payload(m) for m in matches[1:]],
            "nodes": [_node_payload(n) for n in nodes],
            "edges": [_edge_payload(e) for e in edges],
        }

    @router.post("/knowledge/graph/sync")
    async def graph_sync(request: Request, force: bool = Query(False)) -> dict:
        """Idempotently re-project deterministic sources into the graph."""
        from datetime import datetime, timezone

        from doyoutrade.knowledge.graph import sync_deterministic_projection

        _require_local_graph_mutation(request)
        repo = _require_graph_repo()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            result = await sync_deterministic_projection(
                repo, kb_root_resolver(), now=now, force=force
            )
        except Exception as exc:
            logger.warning(
                "knowledge graph sync failed (%s): %s", type(exc).__name__, exc
            )
            raise HTTPException(
                status_code=500, detail=f"graph sync failed: {type(exc).__name__}"
            ) from exc
        return result

    @router.post("/knowledge/graph/entities", status_code=201)
    async def create_graph_entity(
        payload: dict[str, Any],
        request: Request,
    ) -> dict:
        """Create one entity as an audited local changeset."""

        _require_local_graph_mutation(request)
        editor = _require_graph_editor()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            return await editor.apply_local_change(
                [
                    {
                        "op": "create_entity",
                        "type": payload.get("type"),
                        "name": payload.get("name"),
                        "display_name": payload.get("display_name"),
                        "attrs": payload.get("attrs"),
                    }
                ],
                summary=str(payload.get("summary") or "创建实体"),
                expected_revision=_expected_revision(payload),
                actor_id="local-user",
                now=now,
            )
        except Exception as exc:
            from doyoutrade.knowledge.editing import GraphEditError

            if isinstance(exc, GraphEditError):
                raise _graph_edit_error(exc) from exc
            raise

    @router.patch("/knowledge/graph/entities/{entity_id}")
    async def update_graph_entity(
        entity_id: str,
        payload: dict[str, Any],
        request: Request,
    ) -> dict:
        """Update display name / attrs / type for one entity."""

        _require_local_graph_mutation(request)
        editor = _require_graph_editor()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        operation: dict[str, Any] = {
            "op": "update_entity",
            "entity_id": entity_id,
        }
        for key in ("display_name", "attrs", "type"):
            if key in payload:
                operation[key] = payload[key]
        try:
            return await editor.apply_local_change(
                [operation],
                summary=str(payload.get("summary") or f"更新实体 {entity_id}"),
                expected_revision=_expected_revision(payload),
                actor_id="local-user",
                now=now,
            )
        except Exception as exc:
            from doyoutrade.knowledge.editing import GraphEditError

            if isinstance(exc, GraphEditError):
                raise _graph_edit_error(exc) from exc
            raise

    @router.post("/knowledge/graph/entities/{entity_id}/retire")
    async def retire_graph_entity(
        entity_id: str,
        payload: dict[str, Any],
        request: Request,
    ) -> dict:
        """Retire one entity without hard-deleting history."""

        _require_local_graph_mutation(request)
        editor = _require_graph_editor()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            return await editor.apply_local_change(
                [
                    {
                        "op": "retire_entity",
                        "entity_id": entity_id,
                        "reason": str(payload.get("reason") or ""),
                    }
                ],
                summary=str(payload.get("summary") or f"退役实体 {entity_id}"),
                expected_revision=_expected_revision(payload),
                actor_id="local-user",
                now=now,
            )
        except Exception as exc:
            from doyoutrade.knowledge.editing import GraphEditError

            if isinstance(exc, GraphEditError):
                raise _graph_edit_error(exc) from exc
            raise

    @router.post("/knowledge/graph/entities/merge")
    async def merge_graph_entities(
        payload: dict[str, Any],
        request: Request,
    ) -> dict:
        """Merge loser entities into one survivor with redirect lineage."""

        _require_local_graph_mutation(request)
        editor = _require_graph_editor()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            return await editor.apply_local_change(
                [
                    {
                        "op": "merge_entities",
                        "survivor_id": payload.get("survivor_id"),
                        "merge_ids": payload.get("merge_ids") or [],
                        "reason": str(payload.get("reason") or ""),
                    }
                ],
                summary=str(payload.get("summary") or "合并实体"),
                expected_revision=_expected_revision(payload),
                actor_id="local-user",
                now=now,
            )
        except Exception as exc:
            from doyoutrade.knowledge.editing import GraphEditError

            if isinstance(exc, GraphEditError):
                raise _graph_edit_error(exc) from exc
            raise

    @router.post("/knowledge/graph/entities/{entity_id}/split")
    async def split_graph_entity(
        entity_id: str,
        payload: dict[str, Any],
        request: Request,
    ) -> dict:
        """Split one entity into named parts with optional edge moves."""

        _require_local_graph_mutation(request)
        editor = _require_graph_editor()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            return await editor.apply_local_change(
                [
                    {
                        "op": "split_entity",
                        "source_id": entity_id,
                        "parts": payload.get("parts") or [],
                        "reason": str(payload.get("reason") or ""),
                    }
                ],
                summary=str(payload.get("summary") or f"拆分实体 {entity_id}"),
                expected_revision=_expected_revision(payload),
                actor_id="local-user",
                now=now,
            )
        except Exception as exc:
            from doyoutrade.knowledge.editing import GraphEditError

            if isinstance(exc, GraphEditError):
                raise _graph_edit_error(exc) from exc
            raise

    @router.get("/knowledge/graph/entities/{entity_id}/lineage")
    async def graph_entity_lineage(entity_id: str) -> dict:
        """Return merge/split lineage rows touching one entity."""

        editor = _require_graph_editor()
        return {"items": await editor.list_lineage(entity_id)}

    @router.get("/knowledge/graph/conflicts")
    async def list_graph_conflicts(
        status: str | None = Query(None),
        limit: int = Query(100, ge=1, le=500),
    ) -> dict:
        """List open or resolved graph conflicts."""

        editor = _require_graph_editor()
        return {
            "items": await editor.list_conflicts(status=status, limit=limit),
        }

    @router.post("/knowledge/graph/conflicts/{conflict_id}/resolve")
    async def resolve_graph_conflict(
        conflict_id: str,
        payload: dict[str, Any],
        request: Request,
    ) -> dict:
        """Adjudicate one open conflict as an audited revision."""

        _require_local_graph_mutation(request)
        editor = _require_graph_editor()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            return await editor.apply_local_change(
                [
                    {
                        "op": "resolve_conflict",
                        "conflict_id": conflict_id,
                        "decision": payload.get("decision"),
                        "override": payload.get("override"),
                    }
                ],
                summary=str(payload.get("summary") or f"裁决冲突 {conflict_id}"),
                expected_revision=_expected_revision(payload),
                actor_id="local-user",
                now=now,
            )
        except Exception as exc:
            from doyoutrade.knowledge.editing import GraphEditError

            if isinstance(exc, GraphEditError):
                raise _graph_edit_error(exc) from exc
            raise

    @router.post("/knowledge/graph/relations/override")
    async def override_graph_relation(
        payload: dict[str, Any],
        request: Request,
    ) -> dict:
        """Replace an automatic/LLM fact with a manual overlay."""

        _require_local_graph_mutation(request)
        editor = _require_graph_editor()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        operation = {
            "op": "override_relation",
            "edge_id": payload.get("edge_id"),
            "dedupe_key": payload.get("dedupe_key"),
            "fact": payload.get("fact"),
            "attrs": payload.get("attrs"),
            "confidence": payload.get("confidence"),
            "valid_at": payload.get("valid_at"),
            "invalid_at": payload.get("invalid_at"),
        }
        try:
            return await editor.apply_local_change(
                [operation],
                summary=str(payload.get("summary") or "覆盖自动关系"),
                expected_revision=_expected_revision(payload),
                actor_id="local-user",
                now=now,
            )
        except Exception as exc:
            from doyoutrade.knowledge.editing import GraphEditError

            if isinstance(exc, GraphEditError):
                raise _graph_edit_error(exc) from exc
            raise

    @router.get("/knowledge/graph/evidence")
    async def list_graph_evidence(
        target_kind: str = Query(..., pattern="^(node|edge)$"),
        target_id: str = Query(..., min_length=1),
    ) -> dict:
        """List active evidence attachments for a node or edge."""

        editor = _require_graph_editor()
        return {
            "items": await editor.list_evidence(target_kind, target_id),
        }

    @router.post("/knowledge/graph/evidence", status_code=201)
    async def attach_graph_evidence(
        payload: dict[str, Any],
        request: Request,
    ) -> dict:
        """Attach provenance evidence to a node or edge."""

        _require_local_graph_mutation(request)
        editor = _require_graph_editor()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            return await editor.apply_local_change(
                [
                    {
                        "op": "attach_evidence",
                        "target_kind": payload.get("target_kind"),
                        "target_id": payload.get("target_id"),
                        "kind": payload.get("kind"),
                        "uri": payload.get("uri"),
                        "excerpt": payload.get("excerpt") or "",
                        "attrs": payload.get("attrs"),
                    }
                ],
                summary=str(payload.get("summary") or "绑定溯源附件"),
                expected_revision=_expected_revision(payload),
                actor_id="local-user",
                now=now,
            )
        except Exception as exc:
            from doyoutrade.knowledge.editing import GraphEditError

            if isinstance(exc, GraphEditError):
                raise _graph_edit_error(exc) from exc
            raise

    @router.post("/knowledge/graph/evidence/{evidence_id}/detach")
    async def detach_graph_evidence(
        evidence_id: str,
        payload: dict[str, Any],
        request: Request,
    ) -> dict:
        """Detach one evidence attachment without deleting history."""

        _require_local_graph_mutation(request)
        editor = _require_graph_editor()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            return await editor.apply_local_change(
                [{"op": "detach_evidence", "evidence_id": evidence_id}],
                summary=str(payload.get("summary") or f"解除溯源 {evidence_id}"),
                expected_revision=_expected_revision(payload),
                actor_id="local-user",
                now=now,
            )
        except Exception as exc:
            from doyoutrade.knowledge.editing import GraphEditError

            if isinstance(exc, GraphEditError):
                raise _graph_edit_error(exc) from exc
            raise

    @router.get("/knowledge/graph/layouts")
    async def get_graph_layout(
        scope: str = Query(..., min_length=1),
    ) -> dict:
        """Return the latest saved canvas layout for one neighborhood scope."""

        editor = _require_graph_editor()
        layout = await editor.get_latest_layout(scope)
        return {"layout": layout}

    @router.post("/knowledge/graph/layouts", status_code=201)
    async def save_graph_layout(
        payload: dict[str, Any],
        request: Request,
    ) -> dict:
        """Persist one canvas layout version for a neighborhood scope."""

        _require_local_graph_mutation(request)
        editor = _require_graph_editor()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            return await editor.apply_local_change(
                [
                    {
                        "op": "save_layout",
                        "scope_key": payload.get("scope_key"),
                        "positions": payload.get("positions") or {},
                        "locked_ids": payload.get("locked_ids") or [],
                        "highlight_ids": payload.get("highlight_ids") or [],
                    }
                ],
                summary=str(payload.get("summary") or "保存画布布局"),
                expected_revision=_expected_revision(payload),
                actor_id="local-user",
                now=now,
            )
        except Exception as exc:
            from doyoutrade.knowledge.editing import GraphEditError

            if isinstance(exc, GraphEditError):
                raise _graph_edit_error(exc) from exc
            raise

    return router
