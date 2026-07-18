"""知识图谱 — 确定性投影 + 子图渲染。

把知识库中已经结构化的事实投影成 ``kg_nodes`` / ``kg_edges``（存储与写入
语义见 :mod:`doyoutrade.persistence.models` 与
``SqlAlchemyKnowledgeGraphRepository.apply_projection``）。五个来源，全部
**零 LLM 成本**、幂等、可整体重跑：

- ``symbols/roles.jsonl``      → (个股)-[has_role]->(角色)，单值状态组
  ``role|<symbol>``：角色变更时旧边自动置 expired（bi-temporal 失效）。
- ``cycles/*/_sentiment.jsonl`` → 月度周期节点（``cycle``，name=YYYY-MM），
  attrs 聚合当月涨停/炸板/连板高度。
- ``trades/**/*.csv``（经 :func:`read_trade_attribution` FIFO 配对）→
  (个股)-[traded_in]->(周期月)，一笔 round-trip 一条边，盈亏进 attrs。
- ``cycles/_strong_timeline.csv``（或遗留 ``cycles/强势股时间线.csv``）→
  (个股)-[has_role]->(角色) + (个股)-[belongs_to_theme]->(题材) +
  (个股)-[traded_in]->(启动月周期)；启动/高点/退潮等进边 attrs，
  ``source_ref`` 指回 CSV 行。时间线 ``has_role`` **不用**
  ``role|<symbol>`` state_key，避免与 roles.jsonl 抢当前角色。
- ``decision_signals`` 表（调用方传入投影行）→ (信号)-[signals]->(个股)，
  回测 outcome 变化时旧边 expire、新边携带最新验证结果。

增量：每个来源算 content_hash，与 ``kg_source_state`` 水位比对，全部未变
则跳过 apply（apply 本身幂等，跳过只是省工作量——codegraph 的
``files.content_hash`` 思路）。

检索侧 :func:`render_neighborhood_markdown` 把邻域子图渲染成紧凑 markdown
（事实句 + 时间窗 + provenance + source_ref），供 ``knowledge_graph``
in-process 工具直接返回给 agent —— agent 顺着 source_ref 用
``knowledge_index`` / ``read_file`` 钻取原文，两层检索互补。

纪律（§错误可见性）：投影跳过的脏数据一律进 ``warnings``（含 reason 与
原始值）并 ``logger.info``，不静默丢弃；本模块不调 ``datetime.now()``，
``now`` 由调用方传入。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

from doyoutrade.knowledge.attribution import read_trade_attribution
from doyoutrade.knowledge.review import read_sentiment_timeline
from doyoutrade.knowledge.roles import read_symbol_roles
from doyoutrade.knowledge.strong_timeline import read_strong_timeline
from doyoutrade.persistence.repositories import (
    KnowledgeGraphEdgeSnapshot,
    KnowledgeGraphEdgeSpec,
    KnowledgeGraphNodeSnapshot,
    KnowledgeGraphNodeSpec,
)

logger = logging.getLogger(__name__)

# 节点类型 / 关系词表：确定性投影只会产出这些值；LLM 抽取阶段扩词表时在
# 这里集中登记，避免关系词发散到没法查询。
NODE_SYMBOL = "symbol"
NODE_ROLE = "role"
NODE_CYCLE = "cycle"
NODE_SIGNAL = "signal"
NODE_THEME = "theme"

REL_HAS_ROLE = "has_role"
REL_TRADED_IN = "traded_in"
REL_SIGNALS = "signals"
REL_BELONGS_TO_THEME = "belongs_to_theme"

#: kg_source_state 的来源键（``kb:`` = 知识库文件派生，``db:`` = 业务表派生）。
SOURCE_ROLES = "kb:symbols/roles.jsonl"
SOURCE_SENTIMENT = "kb:cycles/_sentiment.jsonl"
SOURCE_TRADES = "kb:trades"
SOURCE_SIGNALS = "db:decision_signals"
#: 稳定来源键（与磁盘文件名解耦；文件可以是规范名或中文遗留名）。
SOURCE_TIMELINE = "kb:cycles/strong_timeline"

#: 情绪时间线读取窗口（月）。取足够大以覆盖整库——投影是全量幂等重建，
#: 不做"最近 N 月"截断（截断属于检索侧的事）。
_SENTIMENT_MONTHS_ALL = 1200


@dataclass
class DeterministicProjection:
    """一次确定性投影的产物（节点/边意图 + 来源水位 + 显式警告）。"""

    nodes: list[KnowledgeGraphNodeSpec] = field(default_factory=list)
    edges: list[KnowledgeGraphEdgeSpec] = field(default_factory=list)
    source_hashes: dict[str, str] = field(default_factory=dict)
    warnings: list[dict[str, Any]] = field(default_factory=list)


def _content_hash(payload: Any) -> str:
    """Stable SHA-256 over a JSON-serialisable payload (source watermark)."""
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _parse_naive_utc(value: Any) -> datetime | None:
    """Parse an ISO datetime / date string into naive-UTC ``datetime``.

    与 persistence 层的 DateTime 口径一致（naive UTC）。解析失败返回
    ``None`` —— 调用方决定是否记 warning（时间缺失是"未知"，不是错误）。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            return datetime.combine(date.fromisoformat(text[:10]), time.min)
        except ValueError:
            return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


class _NodeAccumulator:
    """按自然键合并多来源的节点意图（display_name / attrs 非 None 者胜）。"""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], KnowledgeGraphNodeSpec] = {}

    def add(
        self,
        node_type: str,
        name: str,
        *,
        display_name: str | None = None,
        attrs: dict[str, Any] | None = None,
    ) -> tuple[str, str]:
        key = (node_type, name)
        prior = self._by_key.get(key)
        if prior is None:
            self._by_key[key] = KnowledgeGraphNodeSpec(
                node_type=node_type, name=name, display_name=display_name, attrs=attrs
            )
        else:
            merged_attrs = prior.attrs
            if attrs:
                merged_attrs = {**(prior.attrs or {}), **attrs}
            self._by_key[key] = KnowledgeGraphNodeSpec(
                node_type=node_type,
                name=name,
                display_name=display_name or prior.display_name,
                attrs=merged_attrs,
            )
        return key

    def specs(self) -> list[KnowledgeGraphNodeSpec]:
        return [self._by_key[k] for k in sorted(self._by_key)]


def _project_symbol_roles(
    items: list[dict[str, Any]],
    nodes: _NodeAccumulator,
    edges: list[KnowledgeGraphEdgeSpec],
    warnings: list[dict[str, Any]],
) -> None:
    for item in items:
        symbol = str(item.get("symbol") or "").strip()
        role = str(item.get("role") or "").strip()
        if not symbol or not role:
            warnings.append(
                {
                    "source": SOURCE_ROLES,
                    "reason": "role_row_missing_symbol_or_role",
                    "raw": item,
                }
            )
            logger.info(
                "kg projection skipping role row reason=missing_symbol_or_role raw=%r",
                item,
            )
            continue
        display = item.get("name") if isinstance(item.get("name"), str) else None
        src = nodes.add(NODE_SYMBOL, symbol, display_name=display)
        dst = nodes.add(NODE_ROLE, role)
        note = item.get("note")
        fact = f"{display or symbol}（{symbol}）当前角色：{role}"
        if isinstance(note, str) and note.strip():
            fact += f"——{note.strip()}"
        edges.append(
            KnowledgeGraphEdgeSpec(
                src=src,
                dst=dst,
                relation=REL_HAS_ROLE,
                fact=fact,
                dedupe_key=f"role|{symbol}|{role}",
                state_key=f"role|{symbol}",
                attrs={
                    "note": note,
                    "strategy_hint": item.get("strategy_hint"),
                    "updated_at": item.get("updated_at"),
                },
                valid_at=_parse_naive_utc(item.get("updated_at")),
                source_key=SOURCE_ROLES,
                source_ref=SOURCE_ROLES,
            )
        )


def _project_sentiment(
    rows: list[dict[str, Any]],
    nodes: _NodeAccumulator,
    warnings: list[dict[str, Any]],
) -> None:
    by_month: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        day = str(row.get("date") or "")
        if len(day) < 7:
            warnings.append(
                {"source": SOURCE_SENTIMENT, "reason": "sentiment_row_bad_date", "raw": row}
            )
            logger.info(
                "kg projection skipping sentiment row reason=bad_date raw=%r", row
            )
            continue
        by_month.setdefault(day[:7], []).append(row)

    def _num(value: Any) -> float | None:
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    for month, month_rows in sorted(by_month.items()):
        limit_ups = [v for r in month_rows if (v := _num(r.get("limit_up_count"))) is not None]
        streaks = [v for r in month_rows if (v := _num(r.get("max_streak"))) is not None]
        broken = [v for r in month_rows if (v := _num(r.get("broken_board_rate"))) is not None]
        labels: dict[str, int] = {}
        for r in month_rows:
            label = r.get("label")
            if isinstance(label, str) and label.strip():
                labels[label.strip()] = labels.get(label.strip(), 0) + 1
        nodes.add(
            NODE_CYCLE,
            month,
            display_name=f"{month} 情绪周期",
            attrs={
                "days_recorded": len(month_rows),
                "max_limit_up_count": max(limit_ups) if limit_ups else None,
                "avg_limit_up_count": (
                    round(sum(limit_ups) / len(limit_ups), 2) if limit_ups else None
                ),
                "max_streak": max(streaks) if streaks else None,
                "avg_broken_board_rate": (
                    round(sum(broken) / len(broken), 4) if broken else None
                ),
                "label_counts": labels or None,
            },
        )


def _project_round_trips(
    round_trips: list[dict[str, Any]],
    nodes: _NodeAccumulator,
    edges: list[KnowledgeGraphEdgeSpec],
    warnings: list[dict[str, Any]],
) -> None:
    for rt in round_trips:
        symbol = str(rt.get("symbol") or "").strip()
        close_date = rt.get("close_date")
        if not symbol or not isinstance(close_date, str) or len(close_date) < 7:
            warnings.append(
                {
                    "source": SOURCE_TRADES,
                    "reason": "round_trip_missing_symbol_or_close_date",
                    "raw": {k: rt.get(k) for k in ("symbol", "open_date", "close_date")},
                }
            )
            logger.info(
                "kg projection skipping round trip reason=missing_symbol_or_close_date "
                "symbol=%r close_date=%r", rt.get("symbol"), rt.get("close_date"),
            )
            continue
        month = close_date[:7]
        display = rt.get("name") if isinstance(rt.get("name"), str) else None
        src = nodes.add(NODE_SYMBOL, symbol, display_name=display)
        dst = nodes.add(NODE_CYCLE, month)
        open_date = rt.get("open_date")
        pnl = rt.get("realized_pnl")
        return_pct = rt.get("return_pct")
        hold_days = rt.get("hold_days")
        fact = (
            f"{display or symbol}（{symbol}）{open_date or '?'}→{close_date} "
            f"完成一笔交易：盈亏 {pnl}"
            + (f"（{return_pct}%）" if return_pct is not None else "")
            + (f"，持有 {hold_days} 天" if hold_days is not None else "")
        )
        edges.append(
            KnowledgeGraphEdgeSpec(
                src=src,
                dst=dst,
                relation=REL_TRADED_IN,
                fact=fact,
                dedupe_key=(
                    f"trade|{symbol}|{open_date}|{close_date}|{rt.get('qty')}|{rt.get('buy_cost')}"
                ),
                attrs={
                    "qty": rt.get("qty"),
                    "buy_cost": rt.get("buy_cost"),
                    "sell_proceeds": rt.get("sell_proceeds"),
                    "realized_pnl": pnl,
                    "return_pct": return_pct,
                    "hold_days": hold_days,
                },
                valid_at=_parse_naive_utc(open_date),
                invalid_at=_parse_naive_utc(close_date),
                source_key=SOURCE_TRADES,
                source_ref=SOURCE_TRADES,
            )
        )


def _project_decision_signals(
    rows: list[dict[str, Any]],
    nodes: _NodeAccumulator,
    edges: list[KnowledgeGraphEdgeSpec],
    warnings: list[dict[str, Any]],
) -> None:
    for row in rows:
        signal_id = str(row.get("id") or "").strip()
        symbol = str(row.get("symbol") or "").strip()
        if not signal_id or not symbol:
            warnings.append(
                {
                    "source": SOURCE_SIGNALS,
                    "reason": "signal_row_missing_id_or_symbol",
                    "raw": {k: row.get(k) for k in ("id", "symbol")},
                }
            )
            logger.info(
                "kg projection skipping decision signal reason=missing_id_or_symbol "
                "id=%r symbol=%r", row.get("id"), row.get("symbol"),
            )
            continue
        action = row.get("action")
        created_at = row.get("created_at")
        created_day = ""
        parsed_created = _parse_naive_utc(created_at)
        if parsed_created is not None:
            created_day = parsed_created.date().isoformat()
        outcomes = row.get("outcomes") or []
        outcome_text = ""
        if outcomes:
            parts = [
                f"{o.get('horizon')}:{o.get('outcome')}"
                + (
                    f"({o.get('return_pct'):+.2f}%)"
                    if isinstance(o.get("return_pct"), (int, float))
                    else ""
                )
                for o in outcomes
            ]
            outcome_text = f"；回测验证 {', '.join(parts)}"
        reason = row.get("reason")
        reason_text = ""
        if isinstance(reason, str) and reason.strip():
            snippet = reason.strip()
            reason_text = f"，理由：{snippet[:80]}{'…' if len(snippet) > 80 else ''}"
        src = nodes.add(
            NODE_SIGNAL,
            signal_id,
            display_name=f"{created_day} {action} {symbol}".strip(),
            attrs={
                "action": action,
                "source": row.get("source"),
                "status": row.get("status"),
                "confidence": row.get("confidence"),
                "horizon": row.get("horizon"),
            },
        )
        dst = nodes.add(NODE_SYMBOL, symbol)
        edges.append(
            KnowledgeGraphEdgeSpec(
                src=src,
                dst=dst,
                relation=REL_SIGNALS,
                fact=(
                    f"决策信号 {signal_id}（{row.get('source')}）：{created_day} "
                    f"对 {symbol} 给出 {action}{reason_text}{outcome_text}"
                ),
                dedupe_key=f"signal|{signal_id}",
                attrs={
                    "action": action,
                    "status": row.get("status"),
                    "confidence": row.get("confidence"),
                    "horizon": row.get("horizon"),
                    "outcomes": outcomes or None,
                },
                confidence=(
                    row.get("confidence")
                    if isinstance(row.get("confidence"), (int, float))
                    else None
                ),
                valid_at=parsed_created,
                source_key=SOURCE_SIGNALS,
                source_ref=f"db:decision_signals/{signal_id}",
            )
        )


def _timeline_wave_attrs(item: dict[str, Any]) -> dict[str, Any]:
    """Attrs shared across a timeline wave's projected edges."""
    return {
        "wave_name": item.get("name") or None,
        "start_date": item.get("start_date"),
        "start_price": item.get("start_price"),
        "watch_date": item.get("watch_date"),
        "sell_target": item.get("sell_target"),
        "peak_date": item.get("peak_date"),
        "peak_price": item.get("peak_price"),
        "max_gain_pct": item.get("max_gain_pct"),
        "end_date": item.get("end_date"),
        "ongoing": bool(item.get("ongoing")),
        "rally_trading_days": item.get("rally_trading_days"),
        "calendar_days": item.get("calendar_days"),
        "note": item.get("note") or None,
        "line_number": item.get("line_number"),
    }


def _project_strong_timeline(
    items: list[dict[str, Any]],
    nodes: _NodeAccumulator,
    edges: list[KnowledgeGraphEdgeSpec],
    warnings: list[dict[str, Any]],
) -> None:
    """Project strong-stock timeline rows into role / theme / cycle edges."""
    for item in items:
        symbol = str(item.get("symbol") or "").strip()
        start_date = str(item.get("start_date") or "").strip()
        if not symbol or len(start_date) < 7:
            warnings.append(
                {
                    "source": SOURCE_TIMELINE,
                    "reason": "timeline_item_missing_symbol_or_start",
                    "raw": {
                        k: item.get(k) for k in ("symbol", "start_date", "line_number")
                    },
                }
            )
            continue

        display = item.get("name") if isinstance(item.get("name"), str) else None
        month = start_date[:7]
        relpath = str(item.get("relpath") or "cycles/strong_timeline.csv")
        line_number = item.get("line_number")
        source_ref = f"kb:{relpath}"
        if isinstance(line_number, int):
            source_ref = f"{source_ref}#L{line_number}"

        valid_at = _parse_naive_utc(start_date)
        invalid_at = (
            None
            if item.get("ongoing")
            else _parse_naive_utc(item.get("end_date"))
        )
        wave_attrs = _timeline_wave_attrs(item)
        label = display or symbol
        window = f"{start_date}→{item.get('end_date') or '进行中'}"

        nodes.add(NODE_SYMBOL, symbol, display_name=display)
        nodes.add(NODE_CYCLE, month, display_name=f"{month} 情绪周期")

        role = str(item.get("role") or "").strip()
        if role:
            nodes.add(NODE_ROLE, role)
            edges.append(
                KnowledgeGraphEdgeSpec(
                    src=(NODE_SYMBOL, symbol),
                    dst=(NODE_ROLE, role),
                    relation=REL_HAS_ROLE,
                    fact=(
                        f"{label}（{symbol}）在强势股时间线波次 {window} "
                        f"角色：{role}"
                    ),
                    dedupe_key=f"timeline|role|{symbol}|{start_date}|{role}",
                    # 故意不设 state_key：历史波次角色可与 roles.jsonl 当前角色并存。
                    attrs=wave_attrs,
                    valid_at=valid_at,
                    invalid_at=invalid_at,
                    source_key=SOURCE_TIMELINE,
                    source_ref=source_ref,
                )
            )

        theme = str(item.get("theme") or "").strip()
        if theme:
            nodes.add(NODE_THEME, theme)
            edges.append(
                KnowledgeGraphEdgeSpec(
                    src=(NODE_SYMBOL, symbol),
                    dst=(NODE_THEME, theme),
                    relation=REL_BELONGS_TO_THEME,
                    fact=(
                        f"{label}（{symbol}）在强势股时间线波次 {window} "
                        f"属于题材：{theme}"
                    ),
                    dedupe_key=f"timeline|theme|{symbol}|{start_date}|{theme}",
                    attrs=wave_attrs,
                    valid_at=valid_at,
                    invalid_at=invalid_at,
                    source_key=SOURCE_TIMELINE,
                    source_ref=source_ref,
                )
            )

        peak_bit = ""
        if item.get("peak_date") or item.get("max_gain_pct"):
            peak_bit = (
                f"；高点 {item.get('peak_date') or '?'}"
                f" / 最高涨幅 {item.get('max_gain_pct') or '?'}%"
            )
        edges.append(
            KnowledgeGraphEdgeSpec(
                src=(NODE_SYMBOL, symbol),
                dst=(NODE_CYCLE, month),
                relation=REL_TRADED_IN,
                fact=(
                    f"{label}（{symbol}）强势股时间线主升活跃于 {month}"
                    f"（{window}）{peak_bit}"
                ),
                dedupe_key=f"timeline|cycle|{symbol}|{start_date}|{month}",
                attrs=wave_attrs,
                valid_at=valid_at,
                invalid_at=invalid_at,
                source_key=SOURCE_TIMELINE,
                source_ref=source_ref,
            )
        )


def build_deterministic_projection(
    kb_root: Path,
    *,
    decision_signal_rows: list[dict[str, Any]] | None = None,
) -> DeterministicProjection:
    """Build the full deterministic projection from KB files + signal rows.

    纯函数（除读文件外无副作用）：读 roles / sentiment / trades / strong
    timeline 四个文件来源与调用方传入的 ``decision_signal_rows``，返回
    节点/边意图与每个来源的 content_hash。任何被跳过的脏行都进 ``warnings``。
    """
    projection = DeterministicProjection()
    nodes = _NodeAccumulator()

    role_items = read_symbol_roles(root=kb_root)["items"]
    _project_symbol_roles(role_items, nodes, projection.edges, projection.warnings)
    projection.source_hashes[SOURCE_ROLES] = _content_hash(role_items)

    sentiment_rows = read_sentiment_timeline(_SENTIMENT_MONTHS_ALL, root=kb_root)["items"]
    _project_sentiment(sentiment_rows, nodes, projection.warnings)
    projection.source_hashes[SOURCE_SENTIMENT] = _content_hash(sentiment_rows)

    attribution = read_trade_attribution(root=kb_root)
    round_trips = attribution.get("round_trips") or []
    _project_round_trips(round_trips, nodes, projection.edges, projection.warnings)
    unparsed = attribution.get("unparsed") or []
    if unparsed:
        projection.warnings.append(
            {
                "source": SOURCE_TRADES,
                "reason": "trade_rows_unparsed",
                "count": len(unparsed),
                "hint": "see /knowledge/trade-attribution unparsed detail",
            }
        )
        logger.info(
            "kg projection: %d unparsed trade rows surfaced by attribution", len(unparsed)
        )
    projection.source_hashes[SOURCE_TRADES] = _content_hash(round_trips)

    timeline = read_strong_timeline(root=kb_root)
    projection.warnings.extend(timeline.get("warnings") or [])
    timeline_items = timeline.get("items") or []
    _project_strong_timeline(
        timeline_items, nodes, projection.edges, projection.warnings
    )
    projection.source_hashes[SOURCE_TIMELINE] = _content_hash(
        [
            {
                k: item.get(k)
                for k in (
                    "symbol",
                    "name",
                    "start_date",
                    "start_price",
                    "watch_date",
                    "sell_target",
                    "peak_date",
                    "peak_price",
                    "max_gain_pct",
                    "end_date",
                    "ongoing",
                    "rally_trading_days",
                    "calendar_days",
                    "theme",
                    "note",
                    "role",
                    "line_number",
                    "relpath",
                )
            }
            for item in timeline_items
        ]
    )

    signal_rows = decision_signal_rows or []
    _project_decision_signals(signal_rows, nodes, projection.edges, projection.warnings)
    projection.source_hashes[SOURCE_SIGNALS] = _content_hash(
        [
            {
                k: row.get(k)
                for k in (
                    "id",
                    "symbol",
                    "action",
                    "source",
                    "confidence",
                    "horizon",
                    "reason",
                    "status",
                    "created_at",
                    "outcomes",
                )
            }
            for row in signal_rows
        ]
    )

    projection.nodes = nodes.specs()
    return projection


async def sync_deterministic_projection(
    repository: Any,
    kb_root: Path,
    *,
    now: datetime,
    force: bool = False,
) -> dict[str, Any]:
    """One idempotent sync pass: hash-check sources, apply, advance watermarks.

    ``repository`` 是 ``SqlAlchemyKnowledgeGraphRepository``（鸭子类型以便
    测试注入）。所有来源 hash 均未变且非 ``force`` 时整体跳过（apply 幂等，
    跳过纯属省工作量）。返回结构化统计（skipped / apply stats / warnings /
    图规模），调用方（工具层）转成 debug event。
    """
    signal_rows = await repository.list_decision_signal_projection_rows()
    projection = build_deterministic_projection(
        kb_root, decision_signal_rows=signal_rows
    )

    changed_sources: list[str] = []
    for source, digest in sorted(projection.source_hashes.items()):
        state = await repository.get_source_state(source)
        if state is None or state.content_hash != digest:
            changed_sources.append(source)

    result: dict[str, Any] = {
        "skipped": False,
        "forced": force,
        "changed_sources": changed_sources,
        "projected_nodes": len(projection.nodes),
        "projected_edges": len(projection.edges),
        "warnings": projection.warnings,
    }
    if not changed_sources and not force:
        result["skipped"] = True
        result["counts"] = await repository.counts()
        return result

    apply_stats = await repository.apply_projection(
        projection.nodes,
        projection.edges,
        now=now,
        reconcile_source_keys=set(changed_sources),
        source_hashes=projection.source_hashes,
    )

    result["apply"] = apply_stats
    result["counts"] = await repository.counts()
    return result


# ---------------------------------------------------------------------------
# 子图渲染（agent 工具的输出面）
# ---------------------------------------------------------------------------


def _format_window(edge: KnowledgeGraphEdgeSnapshot) -> str:
    def _d(value: datetime | None) -> str | None:
        return value.date().isoformat() if value is not None else None

    start, end = _d(edge.valid_at), _d(edge.invalid_at)
    if start and end:
        return f"{start}→{end}" if start != end else start
    if start:
        return f"{start} 起"
    if end:
        return f"至 {end}"
    return "时间未知"


def _edge_meta(edge: KnowledgeGraphEdgeSnapshot) -> str:
    parts = [_format_window(edge), edge.provenance]
    if edge.confidence is not None:
        parts.append(f"conf={edge.confidence:.2f}")
    if edge.source_ref:
        parts.append(f"来源 {edge.source_ref}")
    return "｜".join(parts)


def render_neighborhood_markdown(
    center: KnowledgeGraphNodeSnapshot,
    nodes: list[KnowledgeGraphNodeSnapshot],
    edges: list[KnowledgeGraphEdgeSnapshot],
    *,
    include_expired: bool = False,
    truncated: bool = False,
) -> str:
    """Render a neighborhood subgraph as compact agent-facing markdown.

    一条边一行：``- 事实句（时间窗｜provenance｜来源)``，按关系分组；
    ``include_expired`` 时已失效边单列一节（历史认知，不与当前事实混排）。
    截断必须明示（§错误可见性——不静默截断）。
    """
    by_id = {n.id: n for n in nodes}

    def _label(node_id: str) -> str:
        node = by_id.get(node_id)
        if node is None:
            return node_id
        if node.display_name and node.display_name != node.name:
            return f"{node.display_name}({node.name})"
        return node.name

    title = _label(center.id)
    lines = [f"# 知识图谱：{title}（{center.node_type}）", ""]
    if center.attrs:
        compact = json.dumps(center.attrs, ensure_ascii=False, sort_keys=True, default=str)
        lines += [f"节点属性：{compact}", ""]

    active = [e for e in edges if e.expired_at is None]
    expired = [e for e in edges if e.expired_at is not None]

    def _render_group(group_edges: list[KnowledgeGraphEdgeSnapshot]) -> list[str]:
        out: list[str] = []
        by_relation: dict[str, list[KnowledgeGraphEdgeSnapshot]] = {}
        for e in group_edges:
            by_relation.setdefault(e.relation, []).append(e)
        for relation in sorted(by_relation):
            out.append(f"## {relation}")
            for e in by_relation[relation]:
                arrow = f"{_label(e.src_id)} → {_label(e.dst_id)}"
                out.append(f"- {e.fact}")
                out.append(f"  （{arrow}｜{_edge_meta(e)}）")
            out.append("")
        return out

    if active:
        lines += _render_group(active)
    else:
        lines += ["（无当前有效关联——图谱里还没有这个实体的活跃事实）", ""]

    if include_expired and expired:
        lines.append("## 已失效（历史认知，仅供回溯）")
        for e in expired:
            arrow = f"{_label(e.src_id)} → {_label(e.dst_id)}"
            expired_day = e.expired_at.date().isoformat() if e.expired_at else "?"
            lines.append(f"- {e.fact}")
            lines.append(f"  （{arrow}｜{_edge_meta(e)}｜{expired_day} 失效）")
        lines.append("")

    if truncated:
        lines.append(
            "> 注意：邻域边数超出上限，以上为截断结果；可缩小 hops 或指定更具体的实体。"
        )
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "DeterministicProjection",
    "NODE_CYCLE",
    "NODE_ROLE",
    "NODE_SIGNAL",
    "NODE_SYMBOL",
    "NODE_THEME",
    "REL_BELONGS_TO_THEME",
    "REL_HAS_ROLE",
    "REL_SIGNALS",
    "REL_TRADED_IN",
    "SOURCE_ROLES",
    "SOURCE_SENTIMENT",
    "SOURCE_SIGNALS",
    "SOURCE_TIMELINE",
    "SOURCE_TRADES",
    "build_deterministic_projection",
    "render_neighborhood_markdown",
    "sync_deterministic_projection",
]
