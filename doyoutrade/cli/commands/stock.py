"""`doyoutrade-cli stock ...` subcommands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click

from doyoutrade.cli._api import invoke_api
from doyoutrade.cli._invoke import read_session_meta
from doyoutrade.cli.main import run_async_command


@click.group()
def stock() -> None:
    """Stock symbol / instrument / screening commands."""


@stock.command("lookup")
@click.argument("query")
@click.option("--limit", "limit", type=int, default=20, show_default=True, help="Max matches (1-50).")
@click.option(
    "--source",
    "source",
    default="local_catalog",
    show_default=True,
    type=click.Choice(["local_catalog", "akshare_a"], case_sensitive=False),
    help=(
        "Listing source. ``local_catalog`` (default) reads the locally-synced "
        "catalog table — no network round-trip and no akshare tqdm progress "
        "bar. ``akshare_a`` queries the live akshare A-share listings (沪/深/京)."
    ),
)
def stock_lookup(query: str, limit: int, source: str) -> None:
    """Resolve a Chinese stock name / code to its canonical CODE.EXCHANGE symbol.

    Examples::

        doyoutrade-cli stock lookup 茅台
        doyoutrade-cli stock lookup 600519 --limit 5
        doyoutrade-cli stock lookup 中天科技 --source akshare_a
    """

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "GET",
            "/instrument-universe/search",
            params={"q": query, "limit": limit, "source": source},
            meta=read_session_meta(),
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


# ---------------------------------------------------------------------------
# stock screen
# ---------------------------------------------------------------------------

# First-column header tokens skipped when a universe file is a CSV with a
# header row. Mirrors ``data_run._UNIVERSE_HEADER_TOKENS`` (kept local so the
# CLI does not import the heavier api.operations module just for a constant).
_UNIVERSE_HEADER_TOKENS = frozenset(
    {
        "symbol",
        "symbols",
        "code",
        "codes",
        "ticker",
        "stock",
        "stock_code",
        "sec_code",
        "secid",
        "instrument",
    }
)


def _read_universe_file(path: str) -> list[str]:
    """Read one symbol per line; strip blanks and comments (# ...).

    Tolerates CSV exports (takes the first column) and a single leading
    header row so a ``symbol,name`` pandas dump does not blow up downstream.
    """

    p = Path(path).expanduser()
    if not p.exists():
        raise click.BadParameter(f"universe file not found: {p}", param_hint="--universe-file")
    if p.is_dir():
        raise click.BadParameter(f"--universe-file must be a file, got directory: {p}")
    symbols: list[str] = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Accept CSV exports: take the first column as the symbol.
        candidate = line.split(",", 1)[0].strip() if "," in line else line
        if not candidate:
            continue
        # Skip a single leading CSV header row (``symbol,name`` / ``code`` …).
        if not symbols and candidate.lower() in _UNIVERSE_HEADER_TOKENS:
            continue
        symbols.append(candidate)
    if not symbols:
        raise click.BadParameter(
            f"--universe-file {p} contained no symbols (only blanks/comments)",
            param_hint="--universe-file",
        )
    # de-dup, preserve order
    return list(dict.fromkeys(symbols))


@stock.command("screen")
@click.option(
    "--universe-file",
    "universe_file",
    required=True,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="Path to a file with one CODE.EXCHANGE symbol per line (# comments allowed).",
)
@click.option(
    "--asof",
    default=None,
    help="Decision date YYYY-MM-DD (default: today).",
)
@click.option(
    "--interval",
    default="1d",
    show_default=True,
    type=click.Choice(["1d"]),
    help="Bar interval. v1 supports 1d only.",
)
@click.option(
    "--data-source",
    "data_source",
    default="auto",
    show_default=True,
    type=click.Choice(["auto", "qmt", "akshare", "tushare", "baostock", "mootdx"], case_sensitive=False),
    help="Market data provider (same set as ``data run``).",
)
# --- conditions ------------------------------------------------------------
@click.option("--patterns", default=None, help="Comma-separated pattern names; e.g. hammer,bullish_engulfing.")
@click.option("--pattern-window", "pattern_window", type=int, default=None, help="Pattern detection window (default 10).")
@click.option("--rsi-period", "rsi_period", type=int, default=None, help="RSI period (default 14).")
@click.option("--rsi-min", "rsi_min", type=float, default=None, help="Match when RSI >= this value.")
@click.option("--rsi-max", "rsi_max", type=float, default=None, help="Match when RSI <= this value.")
@click.option(
    "--ma-cross",
    "ma_cross",
    default=None,
    help="MA cross within --cross-window bars; format 'golden:fast,slow' or 'death:fast,slow'.",
)
@click.option("--cross-window", "cross_window", type=int, default=None, help="Bars to look back for MA / MACD cross (default 3).")
@click.option("--price-above-ma", "price_above_ma", type=int, default=None, help="Match when close > SMA(N) at asof.")
@click.option("--price-below-ma", "price_below_ma", type=int, default=None, help="Match when close < SMA(N) at asof.")
@click.option("--pct-change-lookback", "pct_change_lookback", type=int, default=None, help="Lookback in bars for pct change.")
@click.option("--pct-change-min", "pct_change_min", type=float, default=None, help="Min lookback-window return (e.g. 0.05).")
@click.option("--pct-change-max", "pct_change_max", type=float, default=None, help="Max lookback-window return.")
@click.option("--volume-ratio-lookback", "volume_ratio_lookback", type=int, default=None, help="Bars to average volume.")
@click.option("--volume-ratio-min", "volume_ratio_min", type=float, default=None, help="Match when today_vol / avg_vol >= this.")
@click.option("--close-at-high-window", "close_at_high_window", type=int, default=None, help="Match when close equals N-bar high.")
@click.option("--close-at-low-window", "close_at_low_window", type=int, default=None, help="Match when close equals N-bar low.")
@click.option(
    "--bollinger",
    type=click.Choice(["upper_break", "lower_break"], case_sensitive=False),
    default=None,
    help="Match when close breaks the upper / lower Bollinger band.",
)
@click.option("--bollinger-window", "bollinger_window", type=int, default=None, help="Bollinger window (default 20).")
@click.option("--adx-period", "adx_period", type=int, default=None, help="ADX period (default 14).")
@click.option("--adx-min", "adx_min", type=float, default=None, help="Match when ADX >= this value.")
@click.option(
    "--macd",
    type=click.Choice(
        ["golden_cross", "death_cross", "cross_zero_up", "cross_zero_down"],
        case_sensitive=False,
    ),
    default=None,
    help="MACD trigger to match within --cross-window bars.",
)
@click.option(
    "--kdj",
    type=click.Choice(["golden_cross", "death_cross"], case_sensitive=False),
    default=None,
    help="KDJ K/D cross to match within --cross-window bars.",
)
@click.option("--kdj-n", "kdj_n", type=int, default=None, help="KDJ RSV window N (default 9).")
@click.option("--cci-period", "cci_period", type=int, default=None, help="CCI period (default 20).")
@click.option("--cci-min", "cci_min", type=float, default=None, help="Match when CCI >= this value.")
@click.option("--cci-max", "cci_max", type=float, default=None, help="Match when CCI <= this value.")
@click.option("--williams-period", "williams_period", type=int, default=None, help="Williams %R period (default 14).")
@click.option("--williams-min", "williams_min", type=float, default=None, help="Match when Williams %R >= this value (-100..0).")
@click.option("--williams-max", "williams_max", type=float, default=None, help="Match when Williams %R <= this value (-100..0).")
@click.option(
    "--keltner",
    type=click.Choice(["upper_break", "lower_break"], case_sensitive=False),
    default=None,
    help="Match when close breaks the upper / lower Keltner channel.",
)
@click.option(
    "--donchian",
    type=click.Choice(["upper_break", "lower_break"], case_sensitive=False),
    default=None,
    help="Match when close hits the N-bar Donchian upper / lower band.",
)
@click.option("--donchian-window", "donchian_window", type=int, default=None, help="Donchian window (default 20).")
@click.option("--cmf-min", "cmf_min", type=float, default=None, help="Match when CMF >= this value (capital inflow).")
@click.option("--cmf-period", "cmf_period", type=int, default=None, help="CMF period (default 20).")
@click.option("--roc-period", "roc_period", type=int, default=None, help="ROC period (default 12).")
@click.option("--roc-min", "roc_min", type=float, default=None, help="Match when ROC(%) >= this value.")
@click.option("--roc-max", "roc_max", type=float, default=None, help="Match when ROC(%) <= this value.")
@click.option(
    "--ma-above-ma",
    "ma_above_ma",
    default=None,
    help="Match when SMA(fast) > SMA(slow) at asof; format 'fast,slow' (e.g. '20,60').",
)
@click.option(
    "--ma-slope-min",
    "ma_slope_min",
    default=None,
    help="Match when SMA slope over a lookback >= min; format 'period,lookback,min_slope' (e.g. '20,5,0').",
)
@click.option("--avg-amount-lookback", "avg_amount_lookback", type=int, default=None, help="Bars to average turnover (amount).")
@click.option("--avg-amount-min", "avg_amount_min", type=float, default=None, help="Match when mean turnover over the window >= this (currency).")
@click.option("--min-float-mv", "min_float_mv", type=float, default=None, help="Match when float market cap (流通市值, currency; 100亿=1e10) >= this.")
@click.option("--max-float-mv", "max_float_mv", type=float, default=None, help="Match when float market cap <= this.")
@click.option("--exclude-suspended", "exclude_suspended", is_flag=True, default=False, help="Drop symbols halted (停牌) as of --asof.")
@click.option(
    "--limit-up-approx",
    "limit_up_approx",
    is_flag=True,
    default=False,
    help="Match approximate limit-up at asof (board limit pct + close equals high).",
)
@click.option(
    "--limit-down-approx",
    "limit_down_approx",
    is_flag=True,
    default=False,
    help="Match approximate limit-down at asof (board limit pct + close equals low).",
)
# --- code-screen mode (compiled Strategy SDK scorer) -----------------------
@click.option("--scorer-file", "scorer_file", default=None, type=click.Path(exists=True, dir_okay=False), help="Code-screen: single-file Strategy SDK scorer (class Strategy). BUY signal = match.")
@click.option("--by-strategy", "by_strategy", default=None, help="Code-screen: a persisted strategy definition id (sd-…) to evaluate over the universe.")
@click.option("--signal-direction", "signal_direction", default=None, type=click.Choice(["buy", "sell", "hold", "any"], case_sensitive=False), help="Code-screen: which signal direction counts as a match (default buy).")
@click.option("--rank-by-diagnostic", "rank_by_diagnostic", default=None, help="Code-screen: order matches by this Signal.diagnostics key (desc).")
# --- ranking & output ------------------------------------------------------
@click.option(
    "--rank-by",
    "rank_by",
    type=click.Choice(["rsi", "adx", "cci", "roc", "macd_hist", "avg_amount"], case_sensitive=False),
    default=None,
    help="Compute this metric for every matched symbol and order by it (strongest-first). Pair with --top-k.",
)
@click.option(
    "--rank-order",
    "rank_order",
    type=click.Choice(["asc", "desc"], case_sensitive=False),
    default=None,
    help="Ranking direction when --rank-by is set (default desc = strongest first).",
)
@click.option("--top-k", "top_k", type=int, default=None, help="Return at most N matched symbols (top-N after ranking).")
@click.option("--sort-by", "sort_by", default=None, help="Column to sort matched rows by (overrides --rank-by ordering).")
@click.option("--sort-desc/--sort-asc", "sort_desc", default=False, help="Sort direction (default ascending).")
@click.option(
    "--output",
    "output",
    default=None,
    type=click.Path(dir_okay=False, writable=True),
    help="Explicit CSV output path (default: ~/.doyoutrade/assistant/artifacts/screener_<asof>_<ts>.csv).",
)
def stock_screen(
    universe_file: str,
    asof: str | None,
    interval: str,
    data_source: str,
    patterns: str | None,
    pattern_window: int | None,
    rsi_period: int | None,
    rsi_min: float | None,
    rsi_max: float | None,
    ma_cross: str | None,
    cross_window: int | None,
    price_above_ma: int | None,
    price_below_ma: int | None,
    pct_change_lookback: int | None,
    pct_change_min: float | None,
    pct_change_max: float | None,
    volume_ratio_lookback: int | None,
    volume_ratio_min: float | None,
    close_at_high_window: int | None,
    close_at_low_window: int | None,
    bollinger: str | None,
    bollinger_window: int | None,
    adx_period: int | None,
    adx_min: float | None,
    macd: str | None,
    kdj: str | None,
    kdj_n: int | None,
    cci_period: int | None,
    cci_min: float | None,
    cci_max: float | None,
    williams_period: int | None,
    williams_min: float | None,
    williams_max: float | None,
    keltner: str | None,
    donchian: str | None,
    donchian_window: int | None,
    cmf_min: float | None,
    cmf_period: int | None,
    roc_period: int | None,
    roc_min: float | None,
    roc_max: float | None,
    ma_above_ma: str | None,
    ma_slope_min: str | None,
    avg_amount_lookback: int | None,
    avg_amount_min: float | None,
    min_float_mv: float | None,
    max_float_mv: float | None,
    exclude_suspended: bool,
    limit_up_approx: bool,
    limit_down_approx: bool,
    scorer_file: str | None,
    by_strategy: str | None,
    signal_direction: str | None,
    rank_by_diagnostic: str | None,
    rank_by: str | None,
    rank_order: str | None,
    top_k: int | None,
    sort_by: str | None,
    sort_desc: bool,
    output: str | None,
) -> None:
    """Screen a list of symbols against a whitelist of technical conditions.

    Conditions are AND-combined. A symbol matches when every active flag's
    predicate is satisfied at (or within the configured window before)
    ``--asof``. Result is written as CSV to the artifacts dir; the envelope
    returns ``result_path`` and a 10-row preview.

    Examples::

        doyoutrade-cli stock screen \\
          --universe-file /tmp/syms.txt \\
          --asof 2026-05-26 \\
          --rsi-max 30 --rsi-period 14 \\
          --patterns hammer,bullish_engulfing \\
          --ma-cross golden:20,60 --cross-window 3 \\
          --pct-change-lookback 5 --pct-change-min 0.05 \\
          --top-k 50 --sort-by rsi --sort-desc
    """

    universe = _read_universe_file(universe_file)
    payload: dict[str, Any] = {
        "universe": universe,
        "interval": interval,
        "data_source": data_source.lower(),
        "sort_desc": bool(sort_desc),
    }
    if asof:
        payload["asof"] = asof
    if patterns:
        payload["patterns"] = patterns
    if pattern_window is not None:
        payload["pattern_window"] = pattern_window
    if rsi_period is not None:
        payload["rsi_period"] = rsi_period
    if rsi_min is not None:
        payload["rsi_min"] = rsi_min
    if rsi_max is not None:
        payload["rsi_max"] = rsi_max
    if ma_cross:
        payload["ma_cross"] = ma_cross
    if cross_window is not None:
        payload["cross_window"] = cross_window
    if price_above_ma is not None:
        payload["price_above_ma"] = price_above_ma
    if price_below_ma is not None:
        payload["price_below_ma"] = price_below_ma
    if pct_change_lookback is not None:
        payload["pct_change_lookback"] = pct_change_lookback
    if pct_change_min is not None:
        payload["pct_change_min"] = pct_change_min
    if pct_change_max is not None:
        payload["pct_change_max"] = pct_change_max
    if volume_ratio_lookback is not None:
        payload["volume_ratio_lookback"] = volume_ratio_lookback
    if volume_ratio_min is not None:
        payload["volume_ratio_min"] = volume_ratio_min
    if close_at_high_window is not None:
        payload["close_at_high_window"] = close_at_high_window
    if close_at_low_window is not None:
        payload["close_at_low_window"] = close_at_low_window
    if bollinger:
        payload["bollinger"] = bollinger.lower()
    if bollinger_window is not None:
        payload["bollinger_window"] = bollinger_window
    if adx_period is not None:
        payload["adx_period"] = adx_period
    if adx_min is not None:
        payload["adx_min"] = adx_min
    if macd:
        payload["macd"] = macd.lower()
    if kdj:
        payload["kdj"] = kdj.lower()
    if kdj_n is not None:
        payload["kdj_n"] = kdj_n
    if cci_period is not None:
        payload["cci_period"] = cci_period
    if cci_min is not None:
        payload["cci_min"] = cci_min
    if cci_max is not None:
        payload["cci_max"] = cci_max
    if williams_period is not None:
        payload["williams_period"] = williams_period
    if williams_min is not None:
        payload["williams_min"] = williams_min
    if williams_max is not None:
        payload["williams_max"] = williams_max
    if keltner:
        payload["keltner"] = keltner.lower()
    if donchian:
        payload["donchian"] = donchian.lower()
    if donchian_window is not None:
        payload["donchian_window"] = donchian_window
    if cmf_min is not None:
        payload["cmf_min"] = cmf_min
    if cmf_period is not None:
        payload["cmf_period"] = cmf_period
    if roc_period is not None:
        payload["roc_period"] = roc_period
    if roc_min is not None:
        payload["roc_min"] = roc_min
    if roc_max is not None:
        payload["roc_max"] = roc_max
    if ma_above_ma:
        payload["ma_above_ma"] = ma_above_ma
    if ma_slope_min:
        payload["ma_slope_min"] = ma_slope_min
    if avg_amount_lookback is not None:
        payload["avg_amount_lookback"] = avg_amount_lookback
    if avg_amount_min is not None:
        payload["avg_amount_min"] = avg_amount_min
    if min_float_mv is not None:
        payload["min_float_mv"] = min_float_mv
    if max_float_mv is not None:
        payload["max_float_mv"] = max_float_mv
    if exclude_suspended:
        payload["exclude_suspended"] = True
    if limit_up_approx:
        payload["limit_up_approx"] = True
    if limit_down_approx:
        payload["limit_down_approx"] = True
    if scorer_file:
        payload["scorer_file"] = scorer_file
    if by_strategy:
        payload["by_strategy"] = by_strategy
    if signal_direction:
        payload["signal_direction"] = signal_direction.lower()
    if rank_by_diagnostic:
        payload["rank_by_diagnostic"] = rank_by_diagnostic
    if rank_by:
        payload["rank_by"] = rank_by.lower()
    if rank_order:
        payload["rank_order"] = rank_order.lower()
    if top_k is not None:
        payload["top_k"] = top_k
    if sort_by:
        payload["sort_by"] = sort_by
    if output:
        payload["output_path"] = output

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "POST",
            "/stock/screen",
            json=payload,
            meta=read_session_meta(),
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


__all__ = ["stock"]
