"""Pure compute for the backtest task summary.

The functions here have **no I/O**. They are deliberately easy to unit-test:
the platform service feeds in already-collected accumulators (equity history,
fills buffer, final account snapshot) and gets back a frozen ``BacktestSummary``
plus a JSON-ready dict from ``summary_to_json``.

Money values are kept as :class:`decimal.Decimal` internally and serialized to
fixed-point strings on the way out (per ``AGENTS.md``).
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Literal, Sequence

from doyoutrade.money.decimal_helpers import decimal_to_json_str


SUMMARY_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FinalPosition:
    """End-of-run snapshot of a non-zero position."""

    symbol: str
    name: str | None
    quantity: int
    available: int | None
    cost_price: Decimal
    last_price: Decimal | None
    market_value: Decimal | None


@dataclass(frozen=True)
class EquityPoint:
    """Per-bar (cycle_time_utc, equity) record."""

    t: datetime
    equity: Decimal


@dataclass(frozen=True)
class FillRecord:
    """A single executed fill from ``cycle_runs.details.fills``."""

    symbol: str
    side: Literal["buy", "sell"]
    quantity: int  # always > 0
    price: Decimal
    timestamp: datetime
    intent_id: str | None
    cycle_run_id: str
    #: A-share transaction fee (元) for this fill. Defaults to 0 so fee-free
    #: runs reconstruct exactly as before; populated when a fee model was
    #: active during the run (parsed back in _backtest_fill_record_from_details).
    fee: Decimal = Decimal("0")
    #: Exit categorization for SELL fills (one of strategy_sdk.signal.ExitReason).
    #: ``None`` on buys and on exits that did not categorize — those round-trips
    #: simply don't appear in the ``by_exit_reason`` breakdown.
    exit_reason: str | None = None
    #: Factor identifier from ``Signal.tag`` — entry_tag on BUY fills, exit_tag on
    #: SELL fills. The entry_tag rides the FIFO lot to its closed round-trips and
    #: powers the ``by_tag`` attribution. ``None`` when the fill carried no tag.
    entry_tag: str | None = None
    exit_tag: str | None = None


@dataclass(frozen=True)
class SymbolStat:
    """Per-symbol breakdown of closed FIFO round-trips.

    Open lots are excluded — this is closed-trade-only so PnL/win-rate are
    fully realized. Holding period uses the trading-day calendar (with the
    same natural-day fallback as the top-level metric).
    """

    symbol: str
    trade_count_closed: int
    pnl: Decimal
    win_rate: Decimal
    win_rate_sample_size: int
    avg_holding_trading_days: Decimal


@dataclass(frozen=True)
class ExitReasonStat:
    """Per-exit-reason breakdown of closed FIFO round-trips.

    Mirrors :class:`SymbolStat` but groups by ``_ClosedTrade.exit_reason``
    (signal / stop_loss / take_profit / trailing_stop / roi / circuit_breaker).
    Only round-trips whose closing SELL carried an ``exit_reason`` appear —
    uncategorized exits are absent (no "unknown" bucket), so the block is
    empty on runs where no exit was categorized. Closed-trade-only so PnL /
    win-rate are realized.
    """

    exit_reason: str
    trade_count_closed: int
    pnl: Decimal
    win_rate: Decimal
    win_rate_sample_size: int
    avg_holding_trading_days: Decimal


@dataclass(frozen=True)
class TagStat:
    """Per-entry-tag breakdown of closed FIFO round-trips.

    Mirrors :class:`SymbolStat` but groups by ``_ClosedTrade.entry_tag`` (the
    factor identifier from ``Signal.tag`` on the buy that opened the lot), so an
    author can see which factor combination actually carried the PnL. Only
    round-trips whose entry lot carried a tag appear — untagged entries are
    absent (no synthetic bucket). Closed-trade-only so PnL / win-rate are realized.
    """

    tag: str
    trade_count_closed: int
    pnl: Decimal
    win_rate: Decimal
    win_rate_sample_size: int
    avg_holding_trading_days: Decimal


# ---------------------------------------------------------------------------
# Output dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BacktestSummary:
    schema_version: int

    run_id: str
    # The persistent backtest job id (``runs.run_id`` / ``btjob-...``) — what
    # the agent receives back from ``run_strategy_backtest``. ``run_id`` above
    # is the *final cycle_run_id* the loop wrote, kept for back-compat with
    # consumers that already key off it. Use ``backtest_job_id`` for any
    # cross-reference back to the originating run row.
    backtest_job_id: str | None
    completed_at: datetime
    range_start_utc: datetime
    range_end_utc: datetime
    bar_interval: str

    starting_equity: Decimal
    ending_equity: Decimal
    return_pct: Decimal

    final_cash: Decimal
    final_market_value: Decimal
    final_positions: tuple[FinalPosition, ...]

    # FIFO-based diagnostic counts (kept for traceability):
    # ``trade_count_closed`` = closed FIFO round-trips, ``trade_count_open`` =
    # number of *symbols* with at least one un-matched lot left.
    trade_count_closed: int
    trade_count_open: int

    # ``fills_count`` is the user-facing 总成交笔数: every executed buy AND sell
    # is counted once. KPI strip / overview both surface this directly because
    # users expect 「交易次数」 to map to executions, not to FIFO round-trips.
    fills_count: int

    # ``win_rate`` is mark-to-market: closed FIFO trades plus every still-open
    # FIFO lot whose symbol has a ``last_price`` in ``final_positions``. The
    # sample size is exposed so the UI can render 「—」 when the denominator is
    # zero (no closed trades AND no priced open lots).
    win_rate: Decimal
    win_rate_sample_size: int

    # ``avg_holding_trading_days`` averages over closed trades AND every still
    # open lot (open lots use ``range_end_utc`` as their virtual exit time).
    # Sample size is reported alongside so the UI can show 「—」 when zero.
    avg_holding_trading_days: Decimal
    avg_holding_sample_size: int

    max_drawdown_pct: Decimal
    max_drawdown_peak_equity: Decimal
    max_drawdown_trough_equity: Decimal
    max_drawdown_peak_at: datetime | None
    max_drawdown_trough_at: datetime | None

    equity_curve: tuple[EquityPoint, ...]
    equity_curve_meta_downsampled: bool
    equity_curve_meta_raw_length: int

    # Diagnostic-only attribute. ``True`` when ``trading_dates`` was empty and
    # the holding-period metric fell back to natural-day differencing.
    used_natural_day_holding: bool = False

    # --- Risk-adjusted return metrics (additive, schema_version=1 compatible) ---
    # All percent fields are bare percent strings on the JSON wire (e.g. ``"8.75"``
    # for 8.75%), matching ``return_pct``. ``None`` values serialize to JSON null
    # — used when the sample is too small or the denominator is zero.
    annual_return_pct: Decimal | None = None
    volatility_annual_pct: Decimal | None = None
    sharpe: Decimal | None = None
    sortino: Decimal | None = None
    calmar: Decimal | None = None

    # --- Closed-trade aggregates (None when there are no closed trades) ---
    profit_factor: Decimal | None = None
    avg_win_pnl: Decimal | None = None
    avg_loss_pnl: Decimal | None = None  # negative or zero
    profit_loss_ratio: Decimal | None = None
    max_consecutive_losses: int = 0

    # --- Total A-share transaction fees (元) charged across all fills ---
    # 0 when no fee model was active (default). Realized PnL and the equity
    # curve are already net of this; surfaced so reports can show the drag.
    total_fees: Decimal = Decimal("0")

    # --- Per-symbol breakdown of closed trades, ordered by descending |pnl| ---
    by_symbol: tuple[SymbolStat, ...] = ()

    # --- Per-exit-reason breakdown of closed trades, ordered by descending
    # |pnl|. Empty when no closed round-trip carried an exit_reason (the
    # default for strategies that don't categorize exits / no exit engine). ---
    by_exit_reason: tuple[ExitReasonStat, ...] = ()

    # --- Per-entry-tag (factor) breakdown of closed trades, ordered by
    # descending |pnl|. Empty when no closed round-trip's entry lot carried a
    # tag (e.g. fills predating tag persistence). ---
    by_tag: tuple[TagStat, ...] = ()

    # --- Warmup diagnostics (additive; ``None`` when the strategy /
    # data-stack did not report them). Kept on the summary for
    # retrospective analysis only; not used to fire an anomaly. The
    # previous ``warmup_insufficient`` flag derived from
    # ``bars_total < startup_history`` — those two numbers measure
    # different things (report-window length vs strategy warmup
    # requirement) and the predicate produced false positives any time
    # the user asked for a window shorter than ``startup_history``. The
    # truthful preload-failure signal lives on the SDK runner's
    # per-cycle ``strategy_base_history_insufficient`` debug event.
    startup_history: int | None = None
    bars_total: int | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


@dataclass
class _Lot:
    qty_remaining: int
    entry_price: Decimal
    entry_time: datetime
    #: Buy-side fee amortized per share (0 when no fee model was active), so a
    #: partially-closed lot only charges the entry fee for the shares it closes.
    entry_fee_per_share: Decimal = Decimal("0")
    #: Factor tag from the buy fill (``Signal.tag`` → entry_tag), carried to the
    #: closed round-trips this lot produces so ``by_tag`` can attribute by factor.
    entry_tag: str | None = None


@dataclass(frozen=True)
class _ClosedTrade:
    symbol: str
    qty: int
    entry_time: datetime
    exit_time: datetime
    pnl: Decimal
    #: Exit categorization carried from the closing SELL fill (``None`` when
    #: the exit was uncategorized). With partial exits, each lot a single sell
    #: fill closes becomes its own ``_ClosedTrade`` sharing this reason —
    #: by_exit_reason counts FIFO round-trips, matching by_symbol semantics.
    exit_reason: str | None = None
    #: Entry factor tag carried from the matched buy lot (``None`` when untagged).
    #: Powers ``by_tag`` attribution (which factor opened the round-trip).
    entry_tag: str | None = None


def _date_iso(ts: datetime) -> str:
    """Return ``YYYY-MM-DD`` portion of ``ts`` (UTC)."""

    if ts.tzinfo is None:
        return ts.date().isoformat()
    return ts.astimezone(timezone.utc).date().isoformat()


def _holding_trading_days(
    entry_time: datetime, exit_time: datetime, calendar: Sequence[str]
) -> int:
    if not calendar:
        return (exit_time.date() - entry_time.date()).days
    e_idx = bisect_left(calendar, _date_iso(entry_time))
    x_idx = bisect_left(calendar, _date_iso(exit_time))
    return max(0, x_idx - e_idx)


def _fifo_match(fills: Sequence[FillRecord]) -> tuple[list[_ClosedTrade], dict[str, list[_Lot]]]:
    by_symbol: dict[str, list[FillRecord]] = defaultdict(list)
    for f in fills:
        by_symbol[f.symbol].append(f)

    closed: list[_ClosedTrade] = []
    open_lots: dict[str, list[_Lot]] = {}
    for symbol, syms in by_symbol.items():
        syms_sorted = sorted(syms, key=lambda f: (f.timestamp, f.cycle_run_id))
        lots: deque[_Lot] = deque()
        for fill in syms_sorted:
            if fill.quantity <= 0:
                raise ValueError(
                    f"FIFO matching encountered non-positive quantity for {symbol}"
                )
            if fill.side == "buy":
                buy_qty = int(fill.quantity)
                lots.append(
                    _Lot(
                        qty_remaining=buy_qty,
                        entry_price=Decimal(fill.price),
                        entry_time=fill.timestamp,
                        # Amortize the buy fee across the lot's shares (0 when fee-free).
                        entry_fee_per_share=(
                            Decimal(fill.fee) / Decimal(buy_qty) if buy_qty > 0 else Decimal("0")
                        ),
                        entry_tag=fill.entry_tag,
                    )
                )
            elif fill.side == "sell":
                remaining = int(fill.quantity)
                # Amortize the sell fee across this fill's shares so a fill that
                # closes lots from several buys charges its fee proportionally.
                sell_qty = int(fill.quantity)
                sell_fee_per_share = (
                    Decimal(fill.fee) / Decimal(sell_qty) if sell_qty > 0 else Decimal("0")
                )
                while remaining > 0:
                    if not lots:
                        raise ValueError(
                            f"FIFO matching encountered short-sell on {symbol}"
                        )
                    lot = lots[0]
                    take = min(lot.qty_remaining, remaining)
                    # Full-口径 realized PnL: gross price move minus the entry
                    # and exit fees apportioned to the closed quantity. Reconciles
                    # with the ledger, which deducted both fees from cash.
                    gross = Decimal(take) * (Decimal(fill.price) - lot.entry_price)
                    fee_charge = Decimal(take) * (lot.entry_fee_per_share + sell_fee_per_share)
                    pnl = gross - fee_charge
                    closed.append(
                        _ClosedTrade(
                            symbol=symbol,
                            qty=take,
                            entry_time=lot.entry_time,
                            exit_time=fill.timestamp,
                            pnl=pnl,
                            exit_reason=fill.exit_reason,
                            entry_tag=lot.entry_tag,
                        )
                    )
                    lot.qty_remaining -= take
                    remaining -= take
                    if lot.qty_remaining == 0:
                        lots.popleft()
            else:
                raise ValueError(
                    f"unknown fill side {fill.side!r} on symbol {symbol}"
                )
        if lots:
            open_lots[symbol] = list(lots)
    return closed, open_lots


def _max_drawdown(
    eq: Sequence[EquityPoint],
) -> tuple[Decimal, Decimal, Decimal, datetime | None, datetime | None]:
    """Return ``(pct, peak_equity, trough_equity, peak_at, trough_at)``.

    ``pct`` is an absolute percent (e.g. ``Decimal("8.75")`` for 8.75%). When
    multiple drawdown windows tie on percentage, the **earliest** peak is kept
    for determinism.
    """

    if not eq:
        return Decimal("0"), Decimal("0"), Decimal("0"), None, None

    best_pct = Decimal("0")
    best_peak_eq = Decimal("0")
    best_trough_eq = Decimal("0")
    best_peak_at: datetime | None = None
    best_trough_at: datetime | None = None

    running_peak_eq = eq[0].equity
    running_peak_at = eq[0].t
    for point in eq:
        if point.equity > running_peak_eq:
            running_peak_eq = point.equity
            running_peak_at = point.t
        if running_peak_eq > 0:
            dd = (running_peak_eq - point.equity) / running_peak_eq * Decimal(100)
            # Strict ``>`` keeps the earliest peak when ties exist.
            if dd > best_pct:
                best_pct = dd
                best_peak_eq = running_peak_eq
                best_peak_at = running_peak_at
                best_trough_eq = point.equity
                best_trough_at = point.t

    return best_pct, best_peak_eq, best_trough_eq, best_peak_at, best_trough_at


def _evenly_spaced_indices(n: int, k: int) -> list[int]:
    """Pick ``k`` indices in ``[0, n-1]`` including both endpoints."""

    if n <= 0 or k <= 0:
        return []
    if k >= n:
        return list(range(n))
    if k == 1:
        return [n - 1]
    return [round(i * (n - 1) / (k - 1)) for i in range(k)]


def _downsample_curve(
    eq: Sequence[EquityPoint], max_points: int
) -> tuple[tuple[EquityPoint, ...], bool, int]:
    raw_length = len(eq)
    if raw_length == 0:
        return (), False, 0
    if raw_length <= max_points:
        return tuple(eq), False, raw_length
    indices = _evenly_spaced_indices(raw_length, max_points)
    # Deduplicate while preserving order in case ``round`` collapses adjacent
    # indices (very small max_points + very large series).
    seen: set[int] = set()
    picked: list[EquityPoint] = []
    for idx in indices:
        if idx not in seen:
            seen.add(idx)
            picked.append(eq[idx])
    return tuple(picked), True, raw_length


def _float_to_decimal_pct(x: float, places: int = 6) -> Decimal | None:
    """Quantize a float (already in percent units) to a Decimal with ``places``
    decimal places. Returns ``None`` on non-finite input. Use float math only
    to compute the value, never to carry it across module boundaries — this
    bridge ensures the JSON wire stays in decimal-string form.
    """

    if not math.isfinite(x):
        return None
    quant = Decimal(10) ** -places
    return Decimal(str(x)).quantize(quant)


def _float_to_decimal_ratio(x: float, places: int = 6) -> Decimal | None:
    """Same as ``_float_to_decimal_pct`` but for unitless ratios (sharpe,
    profit_factor). Distinct name keeps caller intent obvious.
    """

    if not math.isfinite(x):
        return None
    quant = Decimal(10) ** -places
    return Decimal(str(x)).quantize(quant)


def _period_returns(eq: Sequence[EquityPoint]) -> list[float]:
    """Simple per-bar returns ``(P_t - P_{t-1}) / P_{t-1}`` as floats.

    Drops bars where ``P_{t-1} <= 0`` to avoid division blowups; this matches
    the existing ``return_pct`` behaviour which already treats zero starting
    equity as a degenerate edge.
    """

    out: list[float] = []
    for i in range(1, len(eq)):
        prev = float(eq[i - 1].equity)
        cur = float(eq[i].equity)
        if prev <= 0:
            continue
        out.append((cur - prev) / prev)
    return out


def _annualization_factor(
    range_start: datetime, range_end: datetime, n_bars: int
) -> float | None:
    """Bars-per-year factor used to annualize Sharpe / volatility.

    Derived from the actual elapsed wall-clock duration so it works regardless
    of bar_interval (1d, 1h, 5min...). ``None`` when the range is non-positive
    or there are fewer than 2 bars (annualization would be meaningless).
    """

    if n_bars < 2:
        return None
    if range_end <= range_start:
        return None
    seconds = (range_end - range_start).total_seconds()
    if seconds <= 0:
        return None
    years = seconds / (365.25 * 24 * 3600)
    if years <= 0:
        return None
    # ``n_bars - 1`` returns over ``years`` years → bars/year.
    return (n_bars - 1) / years


def _compute_risk_metrics(
    *,
    equity_history: Sequence[EquityPoint],
    range_start: datetime,
    range_end: datetime,
    starting_equity: Decimal,
    ending_equity: Decimal,
    max_drawdown_pct: Decimal,
) -> dict[str, Decimal | None]:
    """Compute annualized return, volatility, Sharpe, Sortino, Calmar.

    All inputs are already-collected accumulators; no I/O. Float math is used
    internally (Decimal has no native sqrt and these are derived diagnostics,
    not money), then bridged to Decimal at the boundary.
    """

    out: dict[str, Decimal | None] = {
        "annual_return_pct": None,
        "volatility_annual_pct": None,
        "sharpe": None,
        "sortino": None,
        "calmar": None,
    }

    if starting_equity <= 0 or len(equity_history) < 2:
        return out
    seconds = (range_end - range_start).total_seconds()
    if seconds <= 0:
        return out
    years = seconds / (365.25 * 24 * 3600)
    if years <= 0:
        return out

    start_f = float(starting_equity)
    end_f = float(ending_equity)
    if start_f <= 0:
        return out

    # CAGR: (end / start) ** (1 / years) - 1, in percent.
    try:
        cagr = (end_f / start_f) ** (1.0 / years) - 1.0
    except (OverflowError, ValueError):
        cagr = float("nan")
    out["annual_return_pct"] = _float_to_decimal_pct(cagr * 100.0)

    returns = _period_returns(equity_history)
    af = _annualization_factor(range_start, range_end, len(equity_history))
    if not returns or af is None or af <= 0:
        # CAGR is still useful even without sub-period returns; bail on the rest.
        # Calmar uses CAGR and MDD only, so try it before returning.
        out["calmar"] = _calmar(out["annual_return_pct"], max_drawdown_pct)
        return out

    n = len(returns)
    mean = sum(returns) / n
    # Sample stdev (ddof=1) — matches numpy/pandas default; with n=1 we fall back to 0.
    if n >= 2:
        variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
        stdev = math.sqrt(variance)
    else:
        stdev = 0.0

    vol_annual = stdev * math.sqrt(af)
    out["volatility_annual_pct"] = _float_to_decimal_pct(vol_annual * 100.0)

    if stdev > 0:
        sharpe = (mean * af) / vol_annual  # = mean/stdev * sqrt(af)
        out["sharpe"] = _float_to_decimal_ratio(sharpe)

    # Sortino: only downside dispersion, target return = 0.
    downside_sq = [r * r for r in returns if r < 0]
    if downside_sq:
        downside_std = math.sqrt(sum(downside_sq) / len(downside_sq))
        if downside_std > 0:
            sortino = (mean * af) / (downside_std * math.sqrt(af))
            out["sortino"] = _float_to_decimal_ratio(sortino)

    out["calmar"] = _calmar(out["annual_return_pct"], max_drawdown_pct)
    return out


def _calmar(
    annual_return_pct: Decimal | None, max_drawdown_pct: Decimal
) -> Decimal | None:
    """Calmar = annualized_return / |max_drawdown|. ``None`` when either input
    is missing or max drawdown is zero (would divide by zero).
    """

    if annual_return_pct is None or max_drawdown_pct <= 0:
        return None
    return (annual_return_pct / max_drawdown_pct).quantize(Decimal("0.000001"))


def _compute_trade_aggregates(
    closed: Sequence[_ClosedTrade],
) -> dict[str, Decimal | int | None]:
    """Profit factor, avg win/loss PnL, profit/loss ratio, max consecutive
    losses. Closed-trade-only (open lots excluded so values are realized).

    Closed trades are walked in the order ``_fifo_match`` produced them, which
    matches FIFO exit_time order within each symbol but interleaves across
    symbols by ``defaultdict`` insertion order. For the consecutive-losses
    metric we re-sort by ``exit_time`` to get a deterministic timeline across
    the whole run.
    """

    out: dict[str, Decimal | int | None] = {
        "profit_factor": None,
        "avg_win_pnl": None,
        "avg_loss_pnl": None,
        "profit_loss_ratio": None,
        "max_consecutive_losses": 0,
    }
    if not closed:
        return out

    wins = [t.pnl for t in closed if t.pnl > 0]
    losses = [t.pnl for t in closed if t.pnl < 0]

    gross_profit = sum(wins, Decimal("0"))
    gross_loss = sum((-l for l in losses), Decimal("0"))  # positive magnitude

    if gross_loss > 0:
        out["profit_factor"] = (gross_profit / gross_loss).quantize(Decimal("0.000001"))
    elif gross_profit > 0:
        # All trades profitable — profit_factor is mathematically infinite; we
        # leave it as ``None`` so the agent narrates "no losing trades" rather
        # than emitting a misleading numeric.
        out["profit_factor"] = None

    if wins:
        avg_win = sum(wins, Decimal("0")) / Decimal(len(wins))
        out["avg_win_pnl"] = avg_win.quantize(Decimal("0.0001"))
    if losses:
        avg_loss = sum(losses, Decimal("0")) / Decimal(len(losses))  # negative
        out["avg_loss_pnl"] = avg_loss.quantize(Decimal("0.0001"))

    avg_win = out["avg_win_pnl"]
    avg_loss = out["avg_loss_pnl"]
    if isinstance(avg_win, Decimal) and isinstance(avg_loss, Decimal) and avg_loss < 0:
        out["profit_loss_ratio"] = (avg_win / -avg_loss).quantize(Decimal("0.000001"))

    timeline = sorted(closed, key=lambda t: (t.exit_time, t.symbol))
    streak = 0
    best = 0
    for t in timeline:
        if t.pnl < 0:
            streak += 1
            best = max(best, streak)
        else:
            streak = 0
    out["max_consecutive_losses"] = int(best)

    return out


def _compute_by_symbol(
    closed: Sequence[_ClosedTrade], calendar: Sequence[str]
) -> tuple[SymbolStat, ...]:
    """Per-symbol closed-trade aggregates. Sorted by descending ``|pnl|`` so
    the top-impact symbols surface first in any UI that truncates.
    """

    if not closed:
        return ()

    by_sym: dict[str, list[_ClosedTrade]] = defaultdict(list)
    for t in closed:
        by_sym[t.symbol].append(t)

    stats: list[SymbolStat] = []
    for symbol, trades in by_sym.items():
        n = len(trades)
        pnl = sum((t.pnl for t in trades), Decimal("0"))
        wins = sum(1 for t in trades if t.pnl > 0)
        win_rate = Decimal(wins) / Decimal(n) if n > 0 else Decimal("0")
        avg_hold, _ = _avg_holding_trading_days(trades, calendar)
        stats.append(
            SymbolStat(
                symbol=symbol,
                trade_count_closed=n,
                pnl=pnl,
                win_rate=win_rate,
                win_rate_sample_size=n,
                avg_holding_trading_days=avg_hold,
            )
        )

    stats.sort(key=lambda s: (-abs(s.pnl), s.symbol))
    return tuple(stats)


def _compute_by_exit_reason(
    closed: Sequence[_ClosedTrade], calendar: Sequence[str]
) -> tuple[ExitReasonStat, ...]:
    """Per-exit-reason closed-trade aggregates, sorted by descending ``|pnl|``.

    Only round-trips whose closing SELL carried a non-empty ``exit_reason`` are
    grouped — uncategorized exits are dropped (no synthetic bucket), so the
    result is ``()`` when no exit was categorized.
    """

    if not closed:
        return ()

    by_reason: dict[str, list[_ClosedTrade]] = defaultdict(list)
    for t in closed:
        reason = (t.exit_reason or "").strip()
        if not reason:
            continue
        by_reason[reason].append(t)

    if not by_reason:
        return ()

    stats: list[ExitReasonStat] = []
    for reason, trades in by_reason.items():
        n = len(trades)
        pnl = sum((t.pnl for t in trades), Decimal("0"))
        wins = sum(1 for t in trades if t.pnl > 0)
        win_rate = Decimal(wins) / Decimal(n) if n > 0 else Decimal("0")
        avg_hold, _ = _avg_holding_trading_days(trades, calendar)
        stats.append(
            ExitReasonStat(
                exit_reason=reason,
                trade_count_closed=n,
                pnl=pnl,
                win_rate=win_rate,
                win_rate_sample_size=n,
                avg_holding_trading_days=avg_hold,
            )
        )

    stats.sort(key=lambda s: (-abs(s.pnl), s.exit_reason))
    return tuple(stats)


def _compute_by_tag(
    closed: Sequence[_ClosedTrade], calendar: Sequence[str]
) -> tuple[TagStat, ...]:
    """Per-entry-tag closed-trade aggregates, sorted by descending ``|pnl|``.

    Groups by the entry lot's factor tag. Only round-trips whose entry carried a
    non-empty tag are grouped — untagged entries are dropped (no synthetic
    bucket), so the result is ``()`` when no entry was tagged.
    """

    if not closed:
        return ()

    by_tag: dict[str, list[_ClosedTrade]] = defaultdict(list)
    for t in closed:
        tag = (t.entry_tag or "").strip()
        if not tag:
            continue
        by_tag[tag].append(t)

    if not by_tag:
        return ()

    stats: list[TagStat] = []
    for tag, trades in by_tag.items():
        n = len(trades)
        pnl = sum((t.pnl for t in trades), Decimal("0"))
        wins = sum(1 for t in trades if t.pnl > 0)
        win_rate = Decimal(wins) / Decimal(n) if n > 0 else Decimal("0")
        avg_hold, _ = _avg_holding_trading_days(trades, calendar)
        stats.append(
            TagStat(
                tag=tag,
                trade_count_closed=n,
                pnl=pnl,
                win_rate=win_rate,
                win_rate_sample_size=n,
                avg_holding_trading_days=avg_hold,
            )
        )

    stats.sort(key=lambda s: (-abs(s.pnl), s.tag))
    return tuple(stats)


def _avg_holding_trading_days(
    closed: Sequence[_ClosedTrade], calendar: Sequence[str]
) -> tuple[Decimal, bool]:
    """Mean trading-day holding period across closed trades.

    Returns ``(decimal_value, used_natural_day_fallback)``.
    """

    if not closed:
        return Decimal("0"), False
    used_fallback = not calendar
    total = 0
    for t in closed:
        total += _holding_trading_days(t.entry_time, t.exit_time, calendar)
    return (Decimal(total) / Decimal(len(closed))), used_fallback


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def compute_summary(
    *,
    run_id: str,
    range_start_utc: datetime,
    range_end_utc: datetime,
    bar_interval: str,
    starting_equity: Decimal,
    ending_equity: Decimal,
    final_cash: Decimal,
    final_positions: Sequence[FinalPosition],
    equity_history: Sequence[EquityPoint],
    fills: Sequence[FillRecord],
    trading_dates: Sequence[str],
    completed_at: datetime,
    equity_curve_max_points: int = 5000,
    backtest_job_id: str | None = None,
    startup_history: int | None = None,
    bars_total: int | None = None,
) -> BacktestSummary:
    """Build the canonical backtest summary dataclass.

    Pure function; raises :class:`ValueError` on FIFO short-sell.
    """

    closed_trades, open_lots = _fifo_match(fills)

    trade_count_closed = len(closed_trades)
    trade_count_open = sum(
        1 for lots in open_lots.values() if any(lot.qty_remaining > 0 for lot in lots)
    )
    fills_count = sum(1 for f in fills)

    last_price_by_symbol: dict[str, Decimal] = {
        fp.symbol: Decimal(fp.last_price)
        for fp in final_positions
        if fp.last_price is not None
    }

    # Mark-to-market virtual trades: one per still-open lot when the symbol has
    # a known last price. Used both for win-rate and avg-holding sample.
    mtm_open_trades: list[_ClosedTrade] = []
    for symbol, lots in open_lots.items():
        last_price = last_price_by_symbol.get(symbol)
        if last_price is None:
            continue
        for lot in lots:
            if lot.qty_remaining <= 0:
                continue
            mtm_pnl = Decimal(lot.qty_remaining) * (last_price - lot.entry_price)
            mtm_open_trades.append(
                _ClosedTrade(
                    symbol=symbol,
                    qty=lot.qty_remaining,
                    entry_time=lot.entry_time,
                    exit_time=range_end_utc,
                    pnl=mtm_pnl,
                )
            )

    win_rate_sample_size = trade_count_closed + len(mtm_open_trades)
    if win_rate_sample_size > 0:
        wins = sum(1 for t in closed_trades if t.pnl > 0) + sum(
            1 for t in mtm_open_trades if t.pnl > 0
        )
        win_rate = Decimal(wins) / Decimal(win_rate_sample_size)
    else:
        win_rate = Decimal("0")

    # Holding-period sample: closed trades + every open lot regardless of
    # last_price availability (open lots use range_end_utc as exit time).
    holding_trades: list[_ClosedTrade] = list(closed_trades)
    for symbol, lots in open_lots.items():
        for lot in lots:
            if lot.qty_remaining <= 0:
                continue
            holding_trades.append(
                _ClosedTrade(
                    symbol=symbol,
                    qty=lot.qty_remaining,
                    entry_time=lot.entry_time,
                    exit_time=range_end_utc,
                    pnl=Decimal("0"),
                )
            )
    avg_holding, used_natural = _avg_holding_trading_days(holding_trades, trading_dates)
    avg_holding_sample_size = len(holding_trades)

    mdd_pct, mdd_peak_eq, mdd_trough_eq, mdd_peak_at, mdd_trough_at = _max_drawdown(
        equity_history
    )

    if starting_equity == 0:
        return_pct = Decimal("0")
    else:
        return_pct = (
            (Decimal(ending_equity) - Decimal(starting_equity))
            / Decimal(starting_equity)
            * Decimal(100)
        )

    final_market_value = Decimal(ending_equity) - Decimal(final_cash)

    curve, downsampled, raw_length = _downsample_curve(
        equity_history, equity_curve_max_points
    )

    risk = _compute_risk_metrics(
        equity_history=equity_history,
        range_start=range_start_utc,
        range_end=range_end_utc,
        starting_equity=Decimal(starting_equity),
        ending_equity=Decimal(ending_equity),
        max_drawdown_pct=mdd_pct,
    )
    trade_agg = _compute_trade_aggregates(closed_trades)
    by_symbol = _compute_by_symbol(closed_trades, trading_dates)
    by_exit_reason = _compute_by_exit_reason(closed_trades, trading_dates)
    by_tag = _compute_by_tag(closed_trades, trading_dates)

    return BacktestSummary(
        schema_version=SUMMARY_SCHEMA_VERSION,
        run_id=run_id,
        backtest_job_id=backtest_job_id,
        completed_at=completed_at,
        range_start_utc=range_start_utc,
        range_end_utc=range_end_utc,
        bar_interval=bar_interval,
        starting_equity=Decimal(starting_equity),
        ending_equity=Decimal(ending_equity),
        return_pct=return_pct,
        final_cash=Decimal(final_cash),
        final_market_value=final_market_value,
        final_positions=tuple(final_positions),
        trade_count_closed=trade_count_closed,
        trade_count_open=trade_count_open,
        fills_count=fills_count,
        win_rate=win_rate,
        win_rate_sample_size=win_rate_sample_size,
        avg_holding_trading_days=avg_holding,
        avg_holding_sample_size=avg_holding_sample_size,
        max_drawdown_pct=mdd_pct,
        max_drawdown_peak_equity=mdd_peak_eq,
        max_drawdown_trough_equity=mdd_trough_eq,
        max_drawdown_peak_at=mdd_peak_at,
        max_drawdown_trough_at=mdd_trough_at,
        equity_curve=curve,
        equity_curve_meta_downsampled=downsampled,
        equity_curve_meta_raw_length=raw_length,
        used_natural_day_holding=used_natural,
        annual_return_pct=risk["annual_return_pct"],
        volatility_annual_pct=risk["volatility_annual_pct"],
        sharpe=risk["sharpe"],
        sortino=risk["sortino"],
        calmar=risk["calmar"],
        profit_factor=trade_agg["profit_factor"],  # type: ignore[arg-type]
        avg_win_pnl=trade_agg["avg_win_pnl"],  # type: ignore[arg-type]
        avg_loss_pnl=trade_agg["avg_loss_pnl"],  # type: ignore[arg-type]
        profit_loss_ratio=trade_agg["profit_loss_ratio"],  # type: ignore[arg-type]
        max_consecutive_losses=int(trade_agg["max_consecutive_losses"] or 0),
        total_fees=sum((Decimal(f.fee) for f in fills), Decimal("0")),
        by_symbol=by_symbol,
        by_exit_reason=by_exit_reason,
        by_tag=by_tag,
        startup_history=(
            int(startup_history)
            if isinstance(startup_history, int) and not isinstance(startup_history, bool)
            else None
        ),
        bars_total=(
            int(bars_total)
            if isinstance(bars_total, int) and not isinstance(bars_total, bool)
            else None
        ),
    )


def _ts_iso(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts.isoformat() + "Z"


def _opt_decimal_to_json(d: Decimal | None) -> str | None:
    """Serialize an optional Decimal to a JSON string or null.

    Used for risk metrics that can legitimately be undefined (zero MDD →
    Calmar undefined, no losses → profit_factor undefined). Keeping these as
    JSON ``null`` (rather than ``"0"``) lets the agent / UI render 「—」 instead
    of misleading numerics.
    """

    if d is None:
        return None
    return decimal_to_json_str(d)


def _symbol_stat_to_json(s: SymbolStat) -> dict[str, Any]:
    return {
        "symbol": s.symbol,
        "trade_count_closed": int(s.trade_count_closed),
        "pnl": decimal_to_json_str(s.pnl),
        "win_rate": decimal_to_json_str(s.win_rate),
        "win_rate_sample_size": int(s.win_rate_sample_size),
        "avg_holding_trading_days": decimal_to_json_str(s.avg_holding_trading_days),
    }


def _exit_reason_stat_to_json(s: ExitReasonStat) -> dict[str, Any]:
    return {
        "exit_reason": s.exit_reason,
        "trade_count_closed": int(s.trade_count_closed),
        "pnl": decimal_to_json_str(s.pnl),
        "win_rate": decimal_to_json_str(s.win_rate),
        "win_rate_sample_size": int(s.win_rate_sample_size),
        "avg_holding_trading_days": decimal_to_json_str(s.avg_holding_trading_days),
    }


def _tag_stat_to_json(s: TagStat) -> dict[str, Any]:
    return {
        "tag": s.tag,
        "trade_count_closed": int(s.trade_count_closed),
        "pnl": decimal_to_json_str(s.pnl),
        "win_rate": decimal_to_json_str(s.win_rate),
        "win_rate_sample_size": int(s.win_rate_sample_size),
        "avg_holding_trading_days": decimal_to_json_str(s.avg_holding_trading_days),
    }


def _position_to_json(
    pos: FinalPosition, *, ending_equity: Decimal | None = None
) -> dict[str, Any]:
    weight_pct: Decimal | None = None
    if pos.market_value is not None and ending_equity is not None:
        try:
            equity = Decimal(ending_equity)
            if equity != 0:
                weight_pct = (
                    Decimal(pos.market_value) / equity * Decimal(100)
                ).quantize(Decimal("0.0001"))
        except Exception:
            weight_pct = None
    return {
        "symbol": pos.symbol,
        "name": pos.name,
        "quantity": int(pos.quantity),
        "available": (None if pos.available is None else int(pos.available)),
        "cost_price": decimal_to_json_str(Decimal(pos.cost_price)),
        "last_price": (
            None if pos.last_price is None else decimal_to_json_str(Decimal(pos.last_price))
        ),
        "market_value": (
            None
            if pos.market_value is None
            else decimal_to_json_str(Decimal(pos.market_value))
        ),
        "weight_pct": (
            None if weight_pct is None else decimal_to_json_str(weight_pct)
        ),
    }


def summary_to_json(summary: BacktestSummary) -> dict[str, Any]:
    """Serialize the summary into the JSON shape persisted under ``tasks.backtest_summary``.

    Field ordering is deliberate: dense scalar metrics — including the
    risk-adjusted block (``sharpe`` / ``sortino`` / ``calmar`` / ...) and
    ``by_symbol`` — come **before** the bulky ``equity_curve`` array. Tool
    results travel through ``doyoutrade.assistant.context_compaction.micro``
    which truncates anything over the per-agent budget (4000 chars by
    default); putting the curve last means truncation, if it kicks in,
    only chops the curve's tail. Every actionable metric survives.

    Front-ends and tests should never depend on JSON key ordering for
    semantics — but the size-aware compactor *does*, so we keep this
    explicit.
    """

    return {
        # --- identity / context window ---
        "schema_version": int(summary.schema_version),
        "run_id": str(summary.run_id),
        "backtest_job_id": (
            str(summary.backtest_job_id) if summary.backtest_job_id is not None else None
        ),
        "completed_at": _ts_iso(summary.completed_at),
        "range_start_utc": _ts_iso(summary.range_start_utc),
        "range_end_utc": _ts_iso(summary.range_end_utc),
        "bar_interval": str(summary.bar_interval),
        # --- Warmup diagnostics (lift above the bulky tail so anomaly
        # detection can read them even after the compactor chops the
        # equity_curve). ``None`` here is wire-side ``null`` — the field
        # always exists so frontend types stay stable. ---
        "startup_history": (
            int(summary.startup_history) if summary.startup_history is not None else None
        ),
        "bars_total": (
            int(summary.bars_total) if summary.bars_total is not None else None
        ),
        # --- headline money + headline return ---
        "starting_equity": decimal_to_json_str(summary.starting_equity),
        "ending_equity": decimal_to_json_str(summary.ending_equity),
        "return_pct": decimal_to_json_str(summary.return_pct),
        "annual_return_pct": _opt_decimal_to_json(summary.annual_return_pct),
        "final_cash": decimal_to_json_str(summary.final_cash),
        "final_market_value": decimal_to_json_str(summary.final_market_value),
        # --- risk-adjusted return (lift above curve so it survives truncation) ---
        "sharpe": _opt_decimal_to_json(summary.sharpe),
        "sortino": _opt_decimal_to_json(summary.sortino),
        "calmar": _opt_decimal_to_json(summary.calmar),
        "volatility_annual_pct": _opt_decimal_to_json(summary.volatility_annual_pct),
        # --- drawdown ---
        "max_drawdown_pct": decimal_to_json_str(summary.max_drawdown_pct),
        "max_drawdown_peak_equity": decimal_to_json_str(summary.max_drawdown_peak_equity),
        "max_drawdown_trough_equity": decimal_to_json_str(summary.max_drawdown_trough_equity),
        "max_drawdown_peak_at": _ts_iso(summary.max_drawdown_peak_at),
        "max_drawdown_trough_at": _ts_iso(summary.max_drawdown_trough_at),
        # --- trade statistics ---
        "fills_count": int(summary.fills_count),
        "trade_count_closed": int(summary.trade_count_closed),
        "trade_count_open": int(summary.trade_count_open),
        "win_rate": decimal_to_json_str(summary.win_rate),
        "win_rate_sample_size": int(summary.win_rate_sample_size),
        "avg_holding_trading_days": decimal_to_json_str(summary.avg_holding_trading_days),
        "avg_holding_sample_size": int(summary.avg_holding_sample_size),
        "profit_factor": _opt_decimal_to_json(summary.profit_factor),
        "avg_win_pnl": _opt_decimal_to_json(summary.avg_win_pnl),
        "avg_loss_pnl": _opt_decimal_to_json(summary.avg_loss_pnl),
        "profit_loss_ratio": _opt_decimal_to_json(summary.profit_loss_ratio),
        "max_consecutive_losses": int(summary.max_consecutive_losses),
        "total_fees": decimal_to_json_str(summary.total_fees),
        # --- by-symbol breakdown (still scalar-per-symbol; size scales with universe) ---
        "by_symbol": [_symbol_stat_to_json(s) for s in summary.by_symbol],
        # --- by-exit-reason breakdown (empty list when no exit was categorized) ---
        "by_exit_reason": [
            _exit_reason_stat_to_json(s) for s in summary.by_exit_reason
        ],
        # --- by-entry-tag (factor) breakdown (empty list when no entry was tagged) ---
        "by_tag": [_tag_stat_to_json(s) for s in summary.by_tag],
        # --- positions snapshot (one row per still-open symbol) ---
        "final_positions": [
            _position_to_json(p, ending_equity=summary.ending_equity)
            for p in summary.final_positions
        ],
        # --- bulky tail: equity curve goes last so truncation only hits here ---
        "equity_curve_meta": {
            "downsampled": bool(summary.equity_curve_meta_downsampled),
            "raw_length": int(summary.equity_curve_meta_raw_length),
        },
        "equity_curve": [
            {"t": _ts_iso(p.t), "equity": decimal_to_json_str(p.equity)}
            for p in summary.equity_curve
        ],
    }


_MISSING = "—"


def _fmt_pct(value: Any) -> str:
    """Render a bare percent string as ``"x.yz%"``; ``None``/missing → 「—」."""

    if value is None:
        return _MISSING
    text = str(value).strip()
    if not text:
        return _MISSING
    return f"{text}%"


def _fmt_ratio(value: Any) -> str:
    """Render a unitless ratio string as-is; ``None``/missing → 「—」."""

    if value is None:
        return _MISSING
    text = str(value).strip()
    return text or _MISSING


def _fmt_money(value: Any) -> str:
    """Render a money string as-is; ``None``/missing → 「—」."""

    if value is None:
        return _MISSING
    text = str(value).strip()
    return text or _MISSING


def _fmt_int(value: Any) -> str:
    if value is None:
        return _MISSING
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return _MISSING


def _fmt_ts(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return _MISSING
    # ISO string like "2026-04-22T00:00:00Z" — keep date+time, drop trailing Z's
    # millis if any for readability. Cheap, deterministic.
    return value.replace("T", " ").rstrip("Z").rstrip()


def _safe_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _detect_anomalies(summary: dict[str, Any]) -> list[str]:
    """Apply the Hard-Gate checklist directly to the persisted summary.

    Kept inline (vs. a separate skill) so the rules render the same regardless
    of who is reading the report. The checks intentionally fail open — any
    field we cannot parse is treated as "no signal" rather than fabricating
    a warning.
    """

    flags: list[str] = []

    closed = _safe_decimal(summary.get("trade_count_closed"))
    open_ = _safe_decimal(summary.get("trade_count_open"))
    closed_int = int(closed) if closed is not None else None

    # Previously this branch fired a dedicated ``warmup_insufficient``
    # anomaly when ``bars_total < startup_history``, on the assumption
    # that "report window shorter than warmup requirement" meant the
    # data-layer preload had failed. That predicate is wrong: the SDK
    # runner re-fetches ``startup_history`` bars at every cycle anchored
    # at the cycle's ``as_of`` (see ``strategy_sdk/runner.py``), and the
    # backtest cache (``expanded_backtest_bar_range``) widens the preload
    # window by ``startup_history × 1.7`` calendar days before
    # ``range_start`` to serve those fetches. So a 1-month report
    # (~19 trading days) with ``startup_history = 40`` is fully warmed
    # — the comparison only flagged the *shape* of the request, not any
    # real failure, and the hint that followed pushed agents to extend
    # ``--range-start`` (breaking the user's reporting-window intent).
    #
    # The truthful preload-failure signal is the runner's
    # ``strategy_base_history_insufficient`` debug event, which fires
    # only when the data source actually returned fewer than
    # ``startup_history`` bars. Operators consult the debug session for
    # that. The summary report falls through to the generic "零交易"
    # flag below, which is the right hint for "signal stayed flat".
    # Total trade activity = closed round-trips + still-open lots. A run
    # with at least one still-open position is NOT "zero trades" — the
    # strategy entered and just hasn't exited yet. The pre-fix branch
    # flagged ``closed == 0`` regardless of ``open``, so a single-trade
    # buy-and-hold run (request1.json turn 4: closed=0, open=1, +35%
    # return) misleadingly tripped the "零交易：信号始终为 0" hint and
    # pushed the operator toward "rewrite the entry condition" when the
    # actual story is "entered once and is still in the trade".
    open_int = int(open_) if open_ is not None else None
    total_activity = (closed_int or 0) + (open_int or 0)
    if total_activity == 0:
        flags.append("零交易：检查信号是否始终为 0 或入场条件是否过严")

    if open_ is not None and open_ > 0:
        flags.append(
            f"收盘前仍有 {int(open_)} 个标的未平仓 — 检查退出 / 强平逻辑是否覆盖区间末尾"
        )

    final_market_value = _safe_decimal(summary.get("final_market_value"))
    ending_equity = _safe_decimal(summary.get("ending_equity"))
    # Utilization hint: only meaningful when the strategy actually deployed
    # capital at some point. When ``total_activity == 0`` the "零交易"
    # hint above already explains the high-cash end state; double-flagging
    # the same root cause adds noise without information.
    if (
        final_market_value is not None
        and ending_equity is not None
        and ending_equity > 0
        and final_market_value > 0
        and (final_market_value / ending_equity) < Decimal("0.5")
        and total_activity > 0
    ):
        flags.append("资金利用率 < 50%：仓位过小或信号过稀")

    # Pattern: target_state signal that behaves like an event encoding.
    # The MACD-cross-only bug (production, tmp/error_request.json) lands
    # here: 1-day average holding + ≤2 closed trades inside a short range.
    # `generate` should return *target state* (1=want held, 0=want flat);
    # when authors instead encode "1 on the cross bar, 0 elsewhere", the
    # runner sees a hold→flat diff on the very next cycle and closes the
    # trade after a single bar. Surface it explicitly so the reviewer
    # cannot mistake "+7% in a one-day swing" for strategy quality.
    avg_holding = _safe_decimal(summary.get("avg_holding_trading_days"))
    if (
        avg_holding is not None
        and avg_holding > 0
        and avg_holding <= Decimal("1")
        and closed_int is not None
        and 0 < closed_int <= 2
    ):
        flags.append(
            "平均持仓 ≤ 1 个交易日且成交极少：信号可能编码为事件（金叉发 1 / 平日发 0），"
            "而非 target_state；比较 *水平* (例 `hist > 0`) 而非 *事件* (`crossed_above`)，"
            "并通过 `indicators.signal_from(...)` lift 成 0/1。"
        )

    return flags


def _render_by_symbol_section(
    by_symbol: list[dict[str, Any]], *, top_n: int = 5
) -> list[str]:
    if not by_symbol:
        return []
    lines = ["### 按标的拆解（按 |PnL| 排序）", "", "| 标的 | 笔数 | PnL | 胜率 | 平均持仓 |", "|---|---|---|---|---|"]
    for entry in by_symbol[:top_n]:
        win_rate = entry.get("win_rate")
        win_rate_text: str
        try:
            win_rate_text = f"{(Decimal(str(win_rate)) * 100).quantize(Decimal('0.01'))}%"
        except Exception:
            win_rate_text = _MISSING
        lines.append(
            "| {symbol} | {n} | {pnl} | {win_rate} | {hold} |".format(
                symbol=entry.get("symbol", _MISSING),
                n=_fmt_int(entry.get("trade_count_closed")),
                pnl=_fmt_money(entry.get("pnl")),
                win_rate=win_rate_text,
                hold=_fmt_money(entry.get("avg_holding_trading_days")),
            )
        )
    if len(by_symbol) > top_n:
        lines.append("")
        lines.append(f"_另有 {len(by_symbol) - top_n} 个标的未展示。_")
    return lines


def _render_by_exit_reason_section(
    by_exit_reason: list[dict[str, Any]],
) -> list[str]:
    if not by_exit_reason:
        return []
    lines = [
        "### 按退出原因拆解（按 |PnL| 排序）",
        "",
        "| 退出原因 | 笔数 | PnL | 胜率 | 平均持仓 |",
        "|---|---|---|---|---|",
    ]
    for entry in by_exit_reason:
        win_rate = entry.get("win_rate")
        try:
            win_rate_text = f"{(Decimal(str(win_rate)) * 100).quantize(Decimal('0.01'))}%"
        except Exception:
            win_rate_text = _MISSING
        lines.append(
            "| {reason} | {n} | {pnl} | {win_rate} | {hold} |".format(
                reason=entry.get("exit_reason", _MISSING),
                n=_fmt_int(entry.get("trade_count_closed")),
                pnl=_fmt_money(entry.get("pnl")),
                win_rate=win_rate_text,
                hold=_fmt_money(entry.get("avg_holding_trading_days")),
            )
        )
    return lines


def _render_by_tag_section(
    by_tag: list[dict[str, Any]],
) -> list[str]:
    if not by_tag:
        return []
    lines = [
        "### 按入场因子拆解（按 |PnL| 排序）",
        "",
        "| 入场因子 | 笔数 | PnL | 胜率 | 平均持仓 |",
        "|---|---|---|---|---|",
    ]
    for entry in by_tag:
        win_rate = entry.get("win_rate")
        try:
            win_rate_text = f"{(Decimal(str(win_rate)) * 100).quantize(Decimal('0.01'))}%"
        except Exception:
            win_rate_text = _MISSING
        lines.append(
            "| {tag} | {n} | {pnl} | {win_rate} | {hold} |".format(
                tag=entry.get("tag", _MISSING),
                n=_fmt_int(entry.get("trade_count_closed")),
                pnl=_fmt_money(entry.get("pnl")),
                win_rate=win_rate_text,
                hold=_fmt_money(entry.get("avg_holding_trading_days")),
            )
        )
    return lines


def _render_final_positions_section(
    positions: list[dict[str, Any]],
) -> list[str]:
    if not positions:
        return []
    lines = ["### 仍持仓"]
    for pos in positions:
        lines.append(
            "- `{symbol}` · 数量 {qty} · 成本 {cost} · 现价 {last} · 市值 {mv} · 仓位占比 {weight}".format(
                symbol=pos.get("symbol", _MISSING),
                qty=_fmt_int(pos.get("quantity")),
                cost=_fmt_money(pos.get("cost_price")),
                last=_fmt_money(pos.get("last_price")),
                mv=_fmt_money(pos.get("market_value")),
                weight=_fmt_pct(pos.get("weight_pct")),
            )
        )
    return lines


def render_summary_markdown(summary: dict[str, Any]) -> str:
    """Render a persisted ``backtest_summary`` dict as a markdown report.

    Input is the same dict shape ``summary_to_json`` produces (or the trimmed
    ``summary_for_agent_view`` of it — both work). Output is a self-contained
    markdown block suitable for direct inclusion in ``ToolResult.text``: the
    agent can forward it to the user with no further processing.

    Pure / deterministic — easy to unit-test and the single source of truth
    for "how a backtest is summarized to a human". null fields render as
    「—」 instead of being silently coerced to ``0``.
    """

    if not isinstance(summary, dict):
        return ""

    run_id = summary.get("run_id") or _MISSING
    report_id = summary.get("backtest_job_id") or run_id
    lines: list[str] = []
    lines.append(f"## 回测报告 · `{report_id}`")
    lines.append("")
    lines.append(
        "**区间** {start} → {end} · **bar** `{bar}` · **完成于** {at}".format(
            start=_fmt_ts(summary.get("range_start_utc")),
            end=_fmt_ts(summary.get("range_end_utc")),
            bar=summary.get("bar_interval") or _MISSING,
            at=_fmt_ts(summary.get("completed_at")),
        )
    )
    if summary.get("backtest_job_id") and summary.get("backtest_job_id") != run_id:
        lines.append(f"最终 cycle run：`{run_id}`")
    data_provider = summary.get("data_provider")
    data_provider_effective = summary.get("data_provider_effective")
    if data_provider or data_provider_effective:
        requested = data_provider or _MISSING
        effective = data_provider_effective or _MISSING
        if data_provider and data_provider_effective and data_provider != data_provider_effective:
            provider_text = f"数据源：`{requested}` → `{effective}`"
        else:
            provider_text = f"数据源：`{effective}`"
        if str(data_provider_effective or "").strip().lower() == "mock":
            provider_text += "（mock 数据源，仅用于模拟/测试）"
        lines.append(provider_text)

    lines.append("")
    lines.append("### 概览")
    lines.append(
        "- 起始 / 结束权益：{start} → {end}".format(
            start=_fmt_money(summary.get("starting_equity")),
            end=_fmt_money(summary.get("ending_equity")),
        )
    )
    lines.append(
        "- 累计收益：{ret} · 年化：{annual}".format(
            ret=_fmt_pct(summary.get("return_pct")),
            annual=_fmt_pct(summary.get("annual_return_pct")),
        )
    )
    lines.append(
        "- 最大回撤：{dd}（{peak} → {trough}）".format(
            dd=_fmt_pct(summary.get("max_drawdown_pct")),
            peak=_fmt_ts(summary.get("max_drawdown_peak_at")),
            trough=_fmt_ts(summary.get("max_drawdown_trough_at")),
        )
    )

    lines.append("")
    lines.append("### 风险调整")
    lines.append(
        "- Sharpe：{s} · Sortino：{so} · Calmar：{c}".format(
            s=_fmt_ratio(summary.get("sharpe")),
            so=_fmt_ratio(summary.get("sortino")),
            c=_fmt_ratio(summary.get("calmar")),
        )
    )
    lines.append(
        "- 年化波动率：{v}".format(v=_fmt_pct(summary.get("volatility_annual_pct")))
    )

    lines.append("")
    lines.append("### 交易统计")
    lines.append(
        "- 成交笔数：{fills}（平仓 {closed} · 仍持仓 {open}）".format(
            fills=_fmt_int(summary.get("fills_count")),
            closed=_fmt_int(summary.get("trade_count_closed")),
            open=_fmt_int(summary.get("trade_count_open")),
        )
    )
    win_rate = summary.get("win_rate")
    try:
        win_rate_text = (
            f"{(Decimal(str(win_rate)) * 100).quantize(Decimal('0.01'))}%"
            if win_rate is not None
            else _MISSING
        )
    except Exception:
        win_rate_text = _MISSING
    sample = _fmt_int(summary.get("win_rate_sample_size"))
    lines.append(f"- 胜率：{win_rate_text}（样本 {sample}）")
    lines.append(
        "- 平均盈利 / 亏损：{w} / {l} · 盈亏比 {plr}".format(
            w=_fmt_money(summary.get("avg_win_pnl")),
            l=_fmt_money(summary.get("avg_loss_pnl")),
            plr=_fmt_ratio(summary.get("profit_loss_ratio")),
        )
    )
    lines.append(
        "- 盈亏因子：{pf} · 最大连亏：{mcl} 笔".format(
            pf=_fmt_ratio(summary.get("profit_factor")),
            mcl=_fmt_int(summary.get("max_consecutive_losses")),
        )
    )
    lines.append(
        "- 平均持仓：{h} 个交易日".format(
            h=_fmt_money(summary.get("avg_holding_trading_days")),
        )
    )

    by_symbol = summary.get("by_symbol")
    if isinstance(by_symbol, list) and by_symbol:
        lines.append("")
        lines.extend(_render_by_symbol_section(by_symbol))

    by_exit_reason = summary.get("by_exit_reason")
    if isinstance(by_exit_reason, list) and by_exit_reason:
        lines.append("")
        lines.extend(_render_by_exit_reason_section(by_exit_reason))

    by_tag = summary.get("by_tag")
    if isinstance(by_tag, list) and by_tag:
        lines.append("")
        lines.extend(_render_by_tag_section(by_tag))

    final_positions = summary.get("final_positions")
    if isinstance(final_positions, list) and final_positions:
        lines.append("")
        lines.extend(_render_final_positions_section(final_positions))

    anomalies = _detect_anomalies(summary)
    if anomalies:
        lines.append("")
        lines.append("### 异常信号")
        for flag in anomalies:
            lines.append(f"- {flag}")

    return "\n".join(lines)


def summary_for_agent_view(summary: dict[str, Any]) -> dict[str, Any]:
    """Trim a serialized ``BacktestSummary`` for inclusion in an agent tool
    result.

    The persisted ``backtest_summary`` carries ``equity_curve`` (up to 5000
    downsampled points) so the frontend can render charts off
    ``GET /tasks/{task_id}``. Agents don't need the array — they consume
    scalar KPIs / ``by_symbol`` / ``final_positions`` — and dragging it
    through the tool channel risks blowing the per-agent
    ``tool_result_max_chars`` budget (default 4000), which then chops any
    fields that happened to serialize after it.

    This helper drops the curve and leaves a ``dropped_for_agent_view``
    flag on ``equity_curve_meta`` so the agent (or a future debug trace)
    can see the curve was elided, not absent. Front-end consumers go
    through ``GET /tasks/{task_id}`` or ``get_backtest_summary(run_id)``
    and always receive the full curve from the persisted record.

    Pure / non-mutating; the input dict is shallow-copied.
    """

    if not isinstance(summary, dict):
        return summary  # type: ignore[unreachable]
    trimmed: dict[str, Any] = dict(summary)
    trimmed.pop("equity_curve", None)
    meta = trimmed.get("equity_curve_meta")
    if isinstance(meta, dict):
        # Preserve ``downsampled`` / ``raw_length`` so the agent can see how
        # large the original was; layer the breadcrumb on top.
        trimmed["equity_curve_meta"] = {
            **meta,
            "dropped_for_agent_view": True,
        }
    else:
        trimmed["equity_curve_meta"] = {"dropped_for_agent_view": True}
    return trimmed


__all__ = [
    "SUMMARY_SCHEMA_VERSION",
    "BacktestSummary",
    "EquityPoint",
    "ExitReasonStat",
    "FillRecord",
    "FinalPosition",
    "SymbolStat",
    "TagStat",
    "compute_summary",
    "render_summary_markdown",
    "summary_for_agent_view",
    "summary_to_json",
]
