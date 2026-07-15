"""Deterministic stock-report context building + template rendering.

The report pipeline is **template-driven, not LLM-driven**: callers supply
structured per-symbol analysis as :class:`ReportItem` rows, wrap them in a
:class:`ReportRequest`, and :func:`render_report` renders a Jinja2 template
under ``doyoutrade/prompts/report/`` (via :func:`doyoutrade.prompts.render.render_prompt`)
into Markdown. The Markdown can then be pushed as text or turned into a PNG
via :mod:`doyoutrade.assistant.rendering.md2img`.

Design notes (mirrors the dsa migration doc, 功能 3):

- Sorting is deterministic: items are ordered by ``score`` descending with
  ``None`` scores last (stable within each group).
- Localised labels are inlined here (``zh`` / ``en``) so templates stay free of
  language conditionals — they only read ``labels.*`` from the context.
- All display formatting (price / change_pct / score) happens in
  :func:`build_context`, never in templates, so tests can assert on exact
  strings and every template gets identical formatting.
- Schema violations raise: an unsupported ``language`` or a non-``ReportItem``
  entry is a programming error upstream, not something to silently coerce
  (CLAUDE.md §错误可见性).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional, Union

from doyoutrade.prompts.render import render_prompt

__all__ = ["ReportItem", "ReportRequest", "build_context", "render_report"]

#: Action buckets the summary bar counts. ``hold`` is accepted on items but is
#: not one of the three headline buckets.
_BUCKETS = ("buy", "watch", "sell")

_LABELS: dict[str, dict[str, str]] = {
    "zh": {
        "as_of": "报告日期",
        "summary": "摘要",
        "buy": "买入",
        "watch": "观察",
        "sell": "卖出",
        "hold": "持有",
        "score": "评分",
        "price": "现价",
        "change_pct": "涨跌幅",
        "trend": "趋势",
        "core_conclusion": "核心结论",
        "key_indicators": "关键指标",
        "battle_plan": "作战计划",
        "entry": "入场",
        "stop_loss": "止损",
        "target": "目标",
        "logic": "逻辑",
        "risks": "风险",
        "news": "相关新闻",
        "no_items": "（本期无入选标的）",
    },
    "en": {
        "as_of": "As of",
        "summary": "Summary",
        "buy": "buy",
        "watch": "watch",
        "sell": "sell",
        "hold": "hold",
        "score": "Score",
        "price": "Price",
        "change_pct": "Change",
        "trend": "Trend",
        "core_conclusion": "Core conclusion",
        "key_indicators": "Key indicators",
        "battle_plan": "Battle plan",
        "entry": "Entry",
        "stop_loss": "Stop loss",
        "target": "Target",
        "logic": "Logic",
        "risks": "Risks",
        "news": "News",
        "no_items": "(no symbols selected this issue)",
    },
}


@dataclass
class ReportItem:
    """One symbol's structured analysis — doyoutrade-native report input.

    Only ``symbol`` is required; every other field degrades to an omitted
    section in the rendered report. ``price`` is display-only (Decimal, float,
    or int — formatted to a string in :func:`build_context`, never used in
    arithmetic here).
    """

    symbol: str
    name: str = ""
    action: str = "watch"  # buy | watch | sell | hold
    score: Optional[float] = None
    price: Union[Decimal, float, int, None] = None
    change_pct: Optional[float] = None
    trend: str = ""
    core_conclusion: str = ""
    key_indicators: dict[str, Any] = field(default_factory=dict)
    battle_plan: Optional[dict[str, Any]] = None  # e.g. {entry, stop_loss, target}
    logic: str = ""
    risks: str = ""
    news: list[str] = field(default_factory=list)


@dataclass
class ReportRequest:
    """A whole report: a titled, dated collection of :class:`ReportItem`."""

    items: list[ReportItem]
    title: str = ""
    as_of: Union[date, datetime, None] = None
    language: str = "zh"  # "zh" | "en"
    summary_only: bool = False


def _format_price(value: Union[Decimal, float, int, None]) -> str:
    if value is None:
        return "—"
    if isinstance(value, Decimal):
        return f"{value:,.2f}"
    if isinstance(value, (int, float)):
        return f"{value:,.2f}"
    raise ValueError(
        f"ReportItem.price must be Decimal/float/int/None, "
        f"got {type(value).__name__}: {value!r}"
    )


def _format_change_pct(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value:+.2f}%"


def _format_score(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value:.1f}"


def _format_as_of(value: Union[date, datetime, None]) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return value.isoformat()


def _sorted_items(items: list[ReportItem]) -> list[ReportItem]:
    """Score descending; ``None`` scores last; stable otherwise."""
    return sorted(
        items,
        key=lambda it: (it.score is None, -(it.score if it.score is not None else 0.0)),
    )


def build_context(request: ReportRequest) -> dict[str, Any]:
    """Turn a :class:`ReportRequest` into the flat dict the templates consume.

    Raises ``ValueError`` on schema violations (unsupported language,
    non-``ReportItem`` entries) rather than coercing — bad inputs must fail
    loudly at build time, not render a half-broken report.
    """
    language = request.language or "zh"
    if language not in _LABELS:
        raise ValueError(
            f"ReportRequest.language must be one of {sorted(_LABELS)}, "
            f"got {language!r}"
        )
    labels = _LABELS[language]

    for it in request.items:
        if not isinstance(it, ReportItem):
            raise ValueError(
                f"ReportRequest.items must contain ReportItem, "
                f"got {type(it).__name__}: {it!r}"
            )

    ordered = _sorted_items(list(request.items))
    counts = {bucket: 0 for bucket in _BUCKETS}
    counts["hold"] = 0
    for it in ordered:
        if it.action in counts:
            counts[it.action] += 1
    counts["total"] = len(ordered)

    items_ctx: list[dict[str, Any]] = []
    for it in ordered:
        plan = it.battle_plan if isinstance(it.battle_plan, dict) else None
        items_ctx.append(
            {
                "symbol": it.symbol,
                "name": it.name,
                "action": it.action,
                "action_label": labels.get(it.action, it.action),
                "score": it.score,
                "score_display": _format_score(it.score),
                "price_display": _format_price(it.price),
                "change_pct_display": _format_change_pct(it.change_pct),
                "trend": it.trend,
                "core_conclusion": it.core_conclusion,
                "key_indicators": dict(it.key_indicators or {}),
                "battle_plan": dict(plan) if plan else None,
                "has_plan": bool(plan),
                "logic": it.logic,
                "risks": it.risks,
                "news": list(it.news or []),
            }
        )

    summary_line = (
        f"{counts['buy']} {labels['buy']} / "
        f"{counts['watch']} {labels['watch']} / "
        f"{counts['sell']} {labels['sell']}"
    )

    return {
        "title": request.title or ("个股研报" if language == "zh" else "Stock Report"),
        "as_of": _format_as_of(request.as_of),
        "language": language,
        "labels": labels,
        "summary_only": bool(request.summary_only),
        "counts": counts,
        "summary_line": summary_line,
        "items": items_ctx,
    }


def render_report(
    request: ReportRequest, *, template: str = "report/markdown.j2"
) -> str:
    """Render ``request`` through a template under ``doyoutrade/prompts/``.

    ``template`` is a path relative to the prompts package (the same contract
    as :func:`doyoutrade.prompts.render.render_prompt`). Default is the full
    Markdown report; ``report/brief.j2`` gives a one-block push summary.
    """
    return render_prompt(template, **build_context(request))
