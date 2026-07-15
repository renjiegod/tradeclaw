"""Deterministic review-analytics layer for the ``daily_review`` cron task.

The composing agent is *compose-only*: it must not recompute numbers from raw
trades/positions (it has no tools, and even if it did, an LLM re-deriving
胜率 / 集中度 / 手续费率 from a JSON statement is exactly the failure mode that
produced wrong headlines historically). This module is the Python-side
pre-processing layer that mirrors QuantDinger's ``_build_metrics`` +
``_build_rule_review``: turn the gathered statement + KB digest into a compact
``metrics`` dict and a list of rule-based findings, *before* the LLM turn.

Three public callables:

* :func:`build_review_metrics` — aggregate the live statement (cash/positions/
  trades/asset) into a structured metrics payload. Money fields stay as decimal
  strings (same contract as :mod:`doyoutrade.account.statement`); ratios/counts
  are plain numbers.
* :func:`build_rule_diagnostics` — run deterministic rules over the metrics to
  emit findings (单票仓位过重 / 几乎满仓 / 手续费率偏高 / 频繁交易 / 单日大亏 …).
  Each finding is a dict with a stable ``code``, a ``severity``, a Chinese
  ``title`` / ``detail``, and an actionable ``recommendation``. Findings are
  *deterministic conclusions* — the LLM is told to cite, not re-derive, them.
* :func:`build_fallback_journal` — a Python-composed minimal 复盘 used when the
  LLM turn fails or returns empty (P5). The user is never left empty-handed:
  the rule-layer numbers + findings already make a usable summary, and the
  journal still starts with ``# <asof> 复盘`` so the KB index stays clean.

Everything here is pure (no I/O, no LLM, no DB) so it is trivially testable and
can be reused by future review surfaces (signal_composer enrichment, weekly /
monthly review, a future structured ``review_reports`` table).
"""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal
from typing import Any, Iterable

from doyoutrade.money.decimal_helpers import decimal_from_number, decimal_to_json_str

logger = logging.getLogger(__name__)

#: A-share standard all-in fee rate (commission + stamp duty + transfer fee) is
#: roughly 0.025%–0.08% for retail. Anything ≥ 10bps is a yellow flag, ≥ 20bps
#: is a red flag (negotiate commission / reduce churn).
_FEE_RATE_WARN_PCT = 0.10
_FEE_RATE_CRITICAL_PCT = 0.20

#: Single-position concentration thresholds (A-share retail discipline).
_CONCENTRATION_WARN_PCT = 40.0
_CONCENTRATION_CRITICAL_PCT = 60.0

#: Cash ratio thresholds.
_NEAR_FULLY_INVESTED_PCT = 10.0
_NEAR_FLAT_PCT = 90.0

#: Trade-frequency threshold (intraday churn guard).
_HIGH_TRADE_COUNT = 20

#: Position count at which retail attention starts to fragment.
_HIGH_POSITION_COUNT = 10

#: Single-day P&L ratio (vs total asset) at which we flag a notable session.
_BIG_LOSS_PCT = -5.0
_BIG_WIN_PCT = 5.0


def _to_str(value: Any) -> str:
    """Normalise any numeric input to the canonical decimal-string contract.

    Statement money fields already arrive as decimal strings; this helper makes
    the module tolerant to callers passing raw ``Decimal``/``float``/``int``
    (e.g. tests) without changing the output shape.
    """
    return decimal_to_json_str(decimal_from_number(value))


def _safe_pct(numerator: Decimal | None, denominator: Decimal | None) -> float | None:
    """``numerator / denominator * 100`` with full null/zero/NaN discipline.

    Returns ``None`` when either input is missing or the denominator is zero —
    callers (rules, prompt) treat ``None`` as "metric not available" and must
    NOT fabricate a number. Returning ``0.0`` here would silently mask a
    missing denominator as "0% concentration", which is a real silent-bug class
    (AGENTS.md §错误可见性).
    """
    if numerator is None or denominator is None:
        return None
    d = decimal_from_number(denominator)
    if d == 0:
        return None
    return float(decimal_from_number(numerator) / d * Decimal(100))


def _sum_amounts(trades: Iterable[dict[str, Any]], sides: tuple[str, ...]) -> Decimal:
    """Sum ``amount`` over trades whose (normalised) side is in ``sides``.

    Uses :func:`_classify_side` so CN / compact variants (``买入`` / ``B``)
    are bucketed consistently with the per-side counters — without this, a
    trade counted in ``buy_count`` could be silently missing from
    ``buy_amount``, and the two would disagree.
    """
    total = Decimal(0)
    for t in trades:
        if _classify_side(t.get("side")) in sides:
            total += decimal_from_number(t.get("amount") or 0)
    return total


def _classify_side(side: Any) -> str:
    """Normalise broker side strings to ``buy`` / ``sell`` / ``""``.

    QMT / 交割单 export conventions seen in the wild: ``buy``/``sell`` (EN),
    ``买入``/``卖出`` (CN), ``B``/``S`` (compact). Anything else maps to ``""``
    so it is excluded from buy/sell buckets rather than mis-bucketed.
    """
    s = str(side or "").strip().lower()
    if s in ("buy", "b", "买入", "买"):
        return "buy"
    if s in ("sell", "s", "卖出", "卖"):
        return "sell"
    return ""


def _position_weight_rows(
    positions: Iterable[dict[str, Any]],
) -> list[tuple[str, str | None, Decimal]]:
    """Yield ``(symbol, name, market_value)`` for positions with positive MV.

    Defensive against missing/zero market_value rows (mock ledgers keep
    zero-qty placeholders; build_post_cycle_account already drops most of them
    but a defensive re-filter keeps this module honest when called directly
    from tests with hand-built payloads).
    """
    rows: list[tuple[str, str | None, Decimal]] = []
    for p in positions:
        mv_raw = p.get("market_value")
        if mv_raw is None:
            continue
        mv = decimal_from_number(mv_raw)
        if mv <= 0:
            continue
        rows.append((str(p.get("symbol") or ""), p.get("name"), mv))
    return rows


def build_review_metrics(
    statement: dict[str, Any],
    *,
    knowledge: dict[str, Any] | None = None,
    asof: str | None = None,
    lookback_days: int = 1,
) -> dict[str, Any]:
    """Aggregate a gathered account statement into structured review metrics.

    Inputs:
      - ``statement`` — the dict returned by
        :func:`doyoutrade.account.statement.gather_account_statement`. May
        contain ``errors`` (sub-fetch failures); those are propagated as
        ``metrics["errors"]`` so the LLM and downstream UIs can show a
        data-visibility warning, never hide it.
      - ``knowledge`` — the KB digest from
        :func:`doyoutrade.knowledge.review.build_daily_review_knowledge_digest`.
        Only its ``errors`` list is consumed at this layer (propagated); the
        textual digest is left for the LLM to read in the framing.

    Output: a flat dict. Money fields are decimal strings (same contract as the
    statement); ratios are floats (percent, already ×100); counts are ints.
    Missing inputs produce ``None`` rather than a fabricated 0 — every consumer
    (rules, prompt, fallback journal) is written to tolerate ``None``.
    """
    lookback_days = int(lookback_days) if lookback_days else 1
    if lookback_days < 1:
        lookback_days = 1

    account_block = statement.get("account") or {}
    account_cash_block = account_block.get("account") or {}
    asset_block = statement.get("asset") or {}
    positions = account_block.get("positions") or []
    trades = statement.get("trades") or []

    cash = account_cash_block.get("cash")
    equity = account_cash_block.get("equity")
    total_market_value = account_block.get("total_market_value")

    total_asset = asset_block.get("total_asset")
    profit_loss = asset_block.get("profit_loss")
    profit_loss_ratio_raw = asset_block.get("profit_loss_ratio")
    if profit_loss_ratio_raw is None:
        profit_loss_ratio_pct: float | None = None
    else:
        try:
            profit_loss_ratio_pct = float(decimal_from_number(profit_loss_ratio_raw)) * 100.0
        except Exception:  # noqa: BLE001 — surfaced via error list, non-fatal
            logger.warning(
                "review_metrics: profit_loss_ratio unparseable (%r); "
                "treated as None",
                profit_loss_ratio_raw,
            )
            profit_loss_ratio_pct = None

    cash_ratio_pct = _safe_pct(cash, total_asset)
    holding_pnl_pct = _safe_pct(profit_loss, total_asset)

    buy_count = 0
    sell_count = 0
    other_count = 0
    for t in trades:
        cls = _classify_side(t.get("side"))
        if cls == "buy":
            buy_count += 1
        elif cls == "sell":
            sell_count += 1
        else:
            other_count += 1
    buy_amount = _sum_amounts(trades, ("buy",))
    sell_amount = _sum_amounts(trades, ("sell",))
    net_amount = sell_amount - buy_amount
    fee_total = Decimal(0)
    for t in trades:
        fee_total += decimal_from_number(t.get("commission") or 0)

    turnover = buy_amount + sell_amount
    fee_to_turnover_pct: float | None
    if turnover > 0:
        fee_to_turnover_pct = float(fee_total / turnover * Decimal(100))
    else:
        fee_to_turnover_pct = None

    weight_rows = _position_weight_rows(positions)
    total_mv_decimal = decimal_from_number(total_market_value) if total_market_value is not None else Decimal(0)
    if total_mv_decimal <= 0 and weight_rows:
        # Statement total_market_value missing but positions have MV → derive.
        total_mv_decimal = sum((mv for _, _, mv in weight_rows), Decimal(0))
    sorted_positions = sorted(weight_rows, key=lambda r: r[2], reverse=True)
    top_positions: list[dict[str, Any]] = []
    if total_mv_decimal > 0:
        for symbol, name, mv in sorted_positions[:3]:
            weight_pct = float(mv / total_mv_decimal * Decimal(100))
            top_positions.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "market_value": decimal_to_json_str(mv),
                    "weight_pct": round(weight_pct, 2),
                }
            )
    concentration_top1_pct = top_positions[0]["weight_pct"] if len(top_positions) >= 1 else None
    concentration_top3_pct = (
        round(sum(p["weight_pct"] for p in top_positions[:3]), 2)
        if top_positions
        else None
    )

    errors: list[dict[str, str]] = []
    stmt_errors = statement.get("errors") or []
    if isinstance(stmt_errors, list):
        errors.extend(e for e in stmt_errors if isinstance(e, dict))
    if knowledge is not None:
        kb_errors = knowledge.get("errors") or []
        if isinstance(kb_errors, list):
            errors.extend(e for e in kb_errors if isinstance(e, dict))

    return {
        "asof": asof or statement.get("asof"),
        "lookback_days": lookback_days,
        # --- trade flow (today) ---
        "trade_count": len(trades),
        "buy_count": buy_count,
        "sell_count": sell_count,
        "other_side_count": other_count,
        "buy_amount": decimal_to_json_str(buy_amount),
        "sell_amount": decimal_to_json_str(sell_amount),
        "net_amount": decimal_to_json_str(net_amount),
        "fee_total": decimal_to_json_str(fee_total),
        "fee_to_turnover_pct": (
            round(fee_to_turnover_pct, 4) if fee_to_turnover_pct is not None else None
        ),
        # --- account / holdings ---
        "position_count": len(weight_rows),
        "cash": _to_str(cash) if cash is not None else None,
        "equity": _to_str(equity) if equity is not None else None,
        "total_market_value": (
            decimal_to_json_str(total_mv_decimal) if total_mv_decimal > 0 else None
        ),
        "total_asset": _to_str(total_asset) if total_asset is not None else None,
        "profit_loss": _to_str(profit_loss) if profit_loss is not None else None,
        "profit_loss_ratio_pct": (
            round(profit_loss_ratio_pct, 4)
            if profit_loss_ratio_pct is not None
            else None
        ),
        "holding_pnl_pct": (
            round(holding_pnl_pct, 4) if holding_pnl_pct is not None else None
        ),
        "cash_ratio_pct": (
            round(cash_ratio_pct, 4) if cash_ratio_pct is not None else None
        ),
        "concentration_top1_pct": concentration_top1_pct,
        "concentration_top3_pct": concentration_top3_pct,
        "top_positions": top_positions,
        # --- data-visibility propagation (never hidden) ---
        "errors": errors,
    }


def _finding(
    *,
    code: str,
    severity: str,
    title: str,
    detail: str,
    recommendation: str,
) -> dict[str, str]:
    return {
        "code": code,
        "severity": severity,
        "title": title,
        "detail": detail,
        "recommendation": recommendation,
    }


def build_rule_diagnostics(
    metrics: dict[str, Any],
    *,
    statement: dict[str, Any] | None = None,
    knowledge: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Run deterministic rules over :func:`build_review_metrics` output.

    Returns a list of finding dicts (newest-highest priority first). Each rule
    is independent and may emit at most one finding. Severities:
      - ``critical`` — likely discipline breach or account-level risk.
      - ``warn``     — yellow flag worth attention.
      - ``info``     — contextual note, not actionable on its own.

    The LLM composing the 复盘 is told these are authoritative findings; it
    should cite them, not re-derive them, and may add explanation but never
    contradict a ``critical`` finding.
    """
    findings: list[dict[str, str]] = []

    # --- concentration ---
    top1_pct = metrics.get("concentration_top1_pct")
    top_positions = metrics.get("top_positions") or []
    top1_label = ""
    if top_positions:
        first = top_positions[0]
        name = first.get("name") or first.get("symbol") or "?"
        top1_label = f"{name}（{first.get('symbol') or ''}）"
    if top1_pct is not None and top1_pct >= _CONCENTRATION_CRITICAL_PCT:
        findings.append(
            _finding(
                code="concentration_top1_critical",
                severity="critical",
                title="单票仓位严重集中",
                detail=(
                    f"{top1_label or 'TOP1'} 占总市值 {top1_pct:.1f}%，"
                    f"超过 {_CONCENTRATION_CRITICAL_PCT:.0f}% 红线"
                ),
                recommendation=(
                    "明日开盘优先减仓该标的至 30% 以下；检查策略是否对单票"
                    "设有 max_single_order_amount / equity_fraction 上限"
                ),
            )
        )
    elif top1_pct is not None and top1_pct >= _CONCENTRATION_WARN_PCT:
        findings.append(
            _finding(
                code="concentration_top1_high",
                severity="warn",
                title="单票仓位偏重",
                detail=(
                    f"{top1_label or 'TOP1'} 占总市值 {top1_pct:.1f}%，"
                    f"超过 {_CONCENTRATION_WARN_PCT:.0f}% 警戒线"
                ),
                recommendation="明日考虑分批减仓或设置单票最大权重约束",
            )
        )

    # --- cash ratio ---
    cash_ratio = metrics.get("cash_ratio_pct")
    trade_count = int(metrics.get("trade_count") or 0)
    position_count = int(metrics.get("position_count") or 0)
    if cash_ratio is not None and position_count > 0 and cash_ratio < _NEAR_FULLY_INVESTED_PCT:
        findings.append(
            _finding(
                code="near_fully_invested",
                severity="warn",
                title="几乎满仓",
                detail=(
                    f"现金占比仅 {cash_ratio:.1f}%（持仓 {position_count} 只）"
                    f"，低于 {_NEAR_FULLY_INVESTED_PCT:.0f}% 警戒线"
                ),
                recommendation="评估下行风险，预留防御性现金或设置最大仓位上限",
            )
        )
    elif cash_ratio is not None and cash_ratio > _NEAR_FLAT_PCT and trade_count == 0:
        findings.append(
            _finding(
                code="near_flat_no_action",
                severity="info",
                title="空仓观望",
                detail=(
                    f"现金占比 {cash_ratio:.1f}%，今日无成交——可能处于等待信号"
                    "或主动空仓状态"
                ),
                recommendation="确认是否符合既定策略；若长期空仓，复盘信号触发条件",
            )
        )

    # --- fee rate ---
    fee_pct = metrics.get("fee_to_turnover_pct")
    if fee_pct is not None and fee_pct >= _FEE_RATE_CRITICAL_PCT:
        findings.append(
            _finding(
                code="fee_rate_critical",
                severity="critical",
                title="手续费率异常偏高",
                detail=(
                    f"今日手续费率达 {fee_pct:.3f}%（ turnovers 内含佣金+印花税"
                    f"），远超 A 股常规 {_FEE_RATE_WARN_PCT:.2f}%–"
                    f"{_FEE_RATE_CRITICAL_PCT:.2f}%"
                ),
                recommendation="核对券商佣金率；减少小额高频下单；考虑是否触发最低佣金门槛",
            )
        )
    elif fee_pct is not None and fee_pct >= _FEE_RATE_WARN_PCT:
        findings.append(
            _finding(
                code="fee_rate_high",
                severity="warn",
                title="手续费率偏高",
                detail=(
                    f"今日手续费率 {fee_pct:.3f}%，高于 A 股零售常规区间"
                ),
                recommendation="确认佣金费率；合并小单可降低摩擦成本",
            )
        )

    # --- trade frequency ---
    if trade_count >= _HIGH_TRADE_COUNT:
        findings.append(
            _finding(
                code="high_trade_count",
                severity="warn",
                title="成交频繁",
                detail=(
                    f"今日成交 {trade_count} 笔（买 {metrics.get('buy_count')} / "
                    f"卖 {metrics.get('sell_count')}），达到"
                    f"{_HIGH_TRADE_COUNT} 笔警戒线"
                ),
                recommendation="核对是否触发最小佣金下限；评估策略是否过拟合短期波动",
            )
        )

    # --- position fragmentation ---
    if position_count >= _HIGH_POSITION_COUNT:
        findings.append(
            _finding(
                code="position_fragmented",
                severity="info",
                title="持仓分散",
                detail=(
                    f"当前持有 {position_count} 只标的，超过"
                    f"{_HIGH_POSITION_COUNT} 只注意力的经验上限"
                ),
                recommendation="评估是否每只都符合策略；考虑精简至能力圈范围",
            )
        )

    # --- single-day P&L ---
    pnl_pct = metrics.get("holding_pnl_pct")
    if pnl_pct is not None and pnl_pct <= _BIG_LOSS_PCT:
        findings.append(
            _finding(
                code="big_session_loss",
                severity="warn",
                title="单日较大浮亏",
                detail=(
                    f"今日浮亏约 {pnl_pct:.2f}%（相对总资产），低于"
                    f"{_BIG_LOSS_PCT:.0f}% 警戒线"
                ),
                recommendation="复盘亏损来源标的是否触及止损；明日优先处理持续走弱持仓",
            )
        )
    elif pnl_pct is not None and pnl_pct >= _BIG_WIN_PCT:
        findings.append(
            _finding(
                code="big_session_win",
                severity="info",
                title="单日较大浮盈",
                detail=(
                    f"今日浮盈约 {pnl_pct:.2f}%（相对总资产），高于"
                    f"{_BIG_WIN_PCT:.0f}% 标志线"
                ),
                recommendation="避免过度自信；评估是否触及动态止盈或部分兑现",
            )
        )

    # --- data visibility (never silent) ---
    errors = metrics.get("errors") or []
    if errors:
        stages = sorted({str(e.get("stage") or "?") for e in errors if isinstance(e, dict)})
        findings.append(
            _finding(
                code="data_partially_unavailable",
                severity="warn",
                title="复盘数据不完整",
                detail=(
                    "本次复盘的部分数据采集失败（stage: "
                    + ", ".join(stages)
                    + "），相关指标可能缺失"
                ),
                recommendation="检查 hint 字段对应的上游（QMT 连接 / KB 读写）后再下一火次重试",
            )
        )

    severity_rank = {"critical": 0, "warn": 1, "info": 2}
    findings.sort(key=lambda f: (severity_rank.get(f["severity"], 9), f["code"]))
    return findings


def _format_money(value: Any) -> str:
    """Render a (possibly-None) decimal-string money field for prose.

    The fallback journal shows raw decimal strings (e.g. ``"1234.56"``) — same
    contract as the LLM-facing statement. ``None`` becomes ``"—"`` so the
    reader can tell "missing" vs "zero" apart, which a bare ``0`` would
    conflate.
    """
    if value is None:
        return "—"
    return str(value)


def build_fallback_journal(
    asof: str,
    metrics: dict[str, Any],
    diagnostics: list[dict[str, str]],
    *,
    reason: str,
) -> str:
    """Compose a minimal Python-only 复盘 when the LLM turn fails.

    Inputs:
      - ``asof``    — ``YYYY-MM-DD`` (becomes the ``# <asof> 复盘`` title —
        the KB index depends on this exact shape; see
        :func:`doyoutrade.knowledge.review.write_daily_review_journal`).
      - ``metrics`` — output of :func:`build_review_metrics`.
      - ``diagnostics`` — output of :func:`build_rule_diagnostics`.
      - ``reason``  — short machine code explaining why the LLM was bypassed
        (``compose_failed`` / ``empty_reply`` / ``no_api_key`` …). Surfaced in
        the banner so the operator knows this is *not* an AI 复盘.

    The result is plain Markdown, self-contained, and follows the same five
    sections as the LLM framing so downstream tooling (KB index, frontend
    renderer) treats both shapes identically. It never invents numbers —
    missing fields render as ``—``.
    """
    lines: list[str] = []
    lines.append(f"# {asof} 复盘")
    lines.append("")
    lines.append(
        f"> ⚠️ **AI 复盘失败（reason={reason}）**。以下为系统基于已对账指标与规则"
        "引擎生成的兜底复盘；LLM 解读缺失，关键结论以「规则诊断」为准。"
    )
    lines.append("")

    # 1. 账户概览
    lines.append("## 账户概览")
    total_asset = _format_money(metrics.get("total_asset"))
    total_mv = _format_money(metrics.get("total_market_value"))
    cash = _format_money(metrics.get("cash"))
    cash_ratio = metrics.get("cash_ratio_pct")
    cash_ratio_str = f"{cash_ratio:.1f}%" if cash_ratio is not None else "—"
    profit_loss = _format_money(metrics.get("profit_loss"))
    pnl_ratio = metrics.get("profit_loss_ratio_pct")
    pnl_ratio_str = f"{pnl_ratio:.2f}%" if pnl_ratio is not None else "—"
    holding_pnl = metrics.get("holding_pnl_pct")
    holding_pnl_str = f"{holding_pnl:.2f}%" if holding_pnl is not None else "—"
    lines.append(f"- 总资产: {total_asset}")
    lines.append(f"- 持仓市值: {total_mv}")
    lines.append(f"- 现金: {cash}（占比 {cash_ratio_str}）")
    lines.append(f"- 累计盈亏: {profit_loss}（{pnl_ratio_str}）")
    lines.append(f"- 当日浮盈亏占比: {holding_pnl_str}")
    lines.append("")

    # 2. 今日成交
    lines.append("## 今日成交")
    trade_count = int(metrics.get("trade_count") or 0)
    buy_count = int(metrics.get("buy_count") or 0)
    sell_count = int(metrics.get("sell_count") or 0)
    buy_amount = _format_money(metrics.get("buy_amount"))
    sell_amount = _format_money(metrics.get("sell_amount"))
    net_amount = _format_money(metrics.get("net_amount"))
    fee_total = _format_money(metrics.get("fee_total"))
    fee_pct = metrics.get("fee_to_turnover_pct")
    fee_pct_str = f"{fee_pct:.3f}%" if fee_pct is not None else "—"
    lines.append(
        f"- 成交笔数: {trade_count}（买 {buy_count} / 卖 {sell_count}）"
    )
    lines.append(f"- 买入金额: {buy_amount}")
    lines.append(f"- 卖出金额: {sell_amount}")
    lines.append(f"- 净金额: {net_amount}（正=净卖出）")
    lines.append(f"- 手续费: {fee_total}（费率 {fee_pct_str}）")
    lines.append("")

    # 3. 持仓盘点
    lines.append("## 持仓盘点")
    position_count = int(metrics.get("position_count") or 0)
    lines.append(f"- 持仓数量: {position_count}")
    top_positions = metrics.get("top_positions") or []
    if top_positions:
        lines.append("- TOP3 持仓:")
        for p in top_positions:
            name = p.get("name") or p.get("symbol") or "?"
            symbol = p.get("symbol") or ""
            weight = p.get("weight_pct")
            weight_str = f"{weight:.1f}%" if weight is not None else "—"
            lines.append(f"  - {name}（{symbol}）: 权重 {weight_str}")
    else:
        lines.append("- TOP3 持仓: 无")
    lines.append("")

    # 4. 盈亏归因与复盘 → 规则诊断（兜底版无 LLM 归因）
    lines.append("## 盈亏归因与复盘")
    actionable = [d for d in diagnostics if d.get("severity") in ("warn", "critical")]
    if actionable:
        lines.append("> 规则引擎识别到以下要点（确定性结论，非 LLM 解读）:")
        lines.append("")
        for d in actionable:
            sev = d.get("severity") or "info"
            icon = {"critical": "🚨", "warn": "⚠️", "info": "ℹ️"}.get(sev, "•")
            lines.append(f"- {icon} **{d.get('title')}** — {d.get('detail')}")
    else:
        lines.append(
            "> 规则引擎未识别到 warn/critical 级别要点；当前持仓结构与"
            "成交频次处于常规区间。"
        )
    lines.append("")

    # 5. 风险提示与明日计划
    lines.append("## 风险提示与明日计划")
    if actionable:
        for d in actionable:
            rec = d.get("recommendation") or ""
            if rec:
                lines.append(f"- {rec}")
    lines.append(
        "- 本次复盘为系统兜底（AI 解读缺失），下次触发时系统会重试 LLM 解读；"
        "如连续多次兜底，请检查模型路由配置。"
    )
    lines.append("")

    # Structured trailing block (same contract the LLM is asked to emit, so
    # downstream parsers — when P4 lands a review_reports table — can treat
    # AI-composed and fallback journals uniformly).
    lines.append("```json")
    lines.append("{")
    lines.append(
        f'  "source": "fallback",'
        f'  "reason": "{reason}",'
        f'  "ai_status": "fallback"'
    )
    diag_codes = [d.get("code") for d in diagnostics if d.get("code")]
    lines.append(f'  "diagnosis_codes": {diag_codes!r},'.replace("'", '"'))
    lines.append(
        '  "cautions": ["本复盘为系统兜底，未经过 LLM 解读；关键决策前请人工复核指标"]'
    )
    lines.append("}")
    lines.append("")

    return "\n".join(lines).rstrip() + "\n"


#: Matches ```json fenced blocks. We grab ALL of them and take the last (the
#: framing tells the LLM to emit the structured block at the very end, after
#: the Markdown body — an LLM that illustrates a JSON example mid-prose
#: shouldn't shadow the real trailing one).
_TRAILING_JSON_BLOCK_RE = re.compile(
    r"```json\s*\n(?P<body>\{.*?\})\s*\n```",
    re.DOTALL,
)


def parse_trailing_review_json(reply_text: str) -> dict[str, Any] | None:
    """Extract the structured review JSON the LLM was asked to emit at the end.

    The framing instructs the agent to append a fenced ```json block with the
    shape::

        {"source": "llm", "ai_status": "ok", "summary": ...,
         "diagnosis": [...], "recommendations": [...], "cautions": [...]}

    This helper finds the LAST such block and parses it. Returns ``None`` when
    no parseable block is present — callers treat that as "AI review was
    free-form prose only" and fall back to rule-derived structure for the
    persisted row, never failing the fire.

    The parser is defensive: a malformed block is logged (warning level, with
    the failure reason) and yields ``None`` rather than raising, because the
    Markdown body of the reply is still usable as a journal on its own.
    """
    if not reply_text:
        return None
    matches = list(_TRAILING_JSON_BLOCK_RE.finditer(reply_text))
    if not matches:
        return None
    body = matches[-1].group("body")
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        logger.warning(
            "review trailing JSON block unparseable (%s); treated as absent",
            exc,
        )
        return None
    if not isinstance(parsed, dict):
        logger.warning(
            "review trailing JSON block parsed to %s, not dict; treated as absent",
            type(parsed).__name__,
        )
        return None
    return parsed


__all__ = [
    "build_review_metrics",
    "build_rule_diagnostics",
    "build_fallback_journal",
    "parse_trailing_review_json",
]
