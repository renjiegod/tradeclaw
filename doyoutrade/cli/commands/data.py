"""`doyoutrade-cli data ...` subcommands — market data lookup and probes."""

from __future__ import annotations

import json
from typing import Any

import click

from doyoutrade.cli._api import invoke_api
from doyoutrade.cli._envelope import EXIT_VALIDATION, error_envelope
from doyoutrade.cli._invoke import read_session_meta
from doyoutrade.cli.main import run_async_command

# Market-data endpoints fan out network fetches over a whole universe
# (hundreds of symbols for ``data run`` / ``fundamentals`` / ``sector`` /
# ``news`` / ``events``). The default 15s HTTP timeout is for snappy control
# calls and trips on batch pulls (tmp/messages.json turn 10: a /data/run over
# a large universe died at 15s while the agent's bash budget was 120s). Give
# these a budget aligned with how long an agent's execute_bash call waits.
_DATA_FETCH_TIMEOUT_SECONDS = 180.0


@click.group()
def data() -> None:
    """Market data commands."""


def _json_object_or_error(value: str | None, *, flag: str, error_code: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if value is None:
        return None, None
    meta = read_session_meta()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        return None, error_envelope(
            error_code=error_code,
            error_type="validation_error",
            message=f"{flag} must be a JSON object: {exc}",
            hint=f"pass {flag} as JSON, e.g. '{{\"rsi\":{{\"period\":21}}}}'",
            meta=meta,
        )
    if not isinstance(parsed, dict):
        return None, error_envelope(
            error_code=error_code,
            error_type="validation_error",
            message=f"{flag} must be a JSON object, got {type(parsed).__name__}",
            hint=f"pass {flag} as a JSON object",
            meta=meta,
        )
    return parsed, None


@data.command("run")
@click.argument("code", required=False)
@click.option(
    "--symbols",
    default=None,
    help=(
        "Multi-symbol input: comma-separated list (e.g. '600519.SH,000001.SZ') "
        "or a JSON array string. Mutually exclusive with the positional "
        "code argument and --universe-file."
    ),
)
@click.option(
    "--universe-file",
    "universe_file",
    default=None,
    type=click.Path(dir_okay=False, readable=True),
    help=(
        "Path to a file with one canonical CODE.EXCHANGE per line "
        "(# comments allowed). Mutually exclusive with the positional code "
        "argument and --symbols."
    ),
)
@click.option(
    "--period",
    default=None,
    help="Relative requested window, e.g. 1m / 3m / 1y. Mutually exclusive with --start / --end.",
)
@click.option(
    "--start",
    "--range-start",
    "range_start",
    default=None,
    help="Inclusive requested start date YYYY-MM-DD.",
)
@click.option(
    "--end",
    "--range-end",
    "range_end",
    default=None,
    help="Inclusive requested end date YYYY-MM-DD.",
)
@click.option("--interval", default="1d", show_default=True, help="Bar interval.")
@click.option(
    "--data-source",
    "data_source",
    default="auto",
    show_default=True,
    type=click.Choice(["auto", "qmt", "akshare", "tushare", "baostock", "mootdx"]),
    help="Provider id.",
)
@click.option(
    "--indicators",
    default=None,
    help='Built-in indicators: comma list, JSON array, or "all".',
)
@click.option(
    "--indicator-params",
    "indicator_params",
    default=None,
    help='JSON object of per-indicator params, e.g. \'{"rsi":{"period":21}}\'.',
)
@click.option(
    "--script",
    default=None,
    help=(
        "Inline Python source defining compute(df, target_df, params) or "
        "assigning a 'result' global. Script runs in an AST-validated "
        "sandbox; imports are restricted to numpy/pandas/decimal/math/"
        "typing/doyoutrade.strategy_sdk."
    ),
)
@click.option(
    "--script-file",
    "script_file",
    default=None,
    type=click.Path(dir_okay=False, readable=True),
    help=(
        "Python script file with the same contract as --script. Mutually "
        "exclusive with --script."
    ),
)
@click.option(
    "--script-params",
    "script_params",
    default=None,
    help="JSON object passed to compute() as the params argument.",
)
@click.option(
    "--script-timeout",
    "script_timeout",
    type=float,
    default=None,
    help=(
        "Per-symbol script execution timeout in seconds (default 10). "
        "Scripts run in a worker thread; on timeout the request returns "
        "script_timeout and the orphan thread is acknowledged but not killed."
    ),
)
@click.option(
    "--warmup-bars",
    "warmup_bars",
    type=int,
    default=None,
    help=(
        "Explicit warm-up bars. Omit for auto-sizing from selected built-ins "
        "and a script's REQUIRED_HISTORY literal. A pure-script run with no "
        "warmup hint anywhere is rejected with script_warmup_unspecified."
    ),
)
@click.option(
    "--tail",
    type=int,
    default=1,
    show_default=True,
    help="Trailing rows to return per indicator column.",
)
def data_run(
    code: str | None,
    symbols: str | None,
    universe_file: str | None,
    period: str | None,
    range_start: str | None,
    range_end: str | None,
    interval: str,
    data_source: str,
    indicators: str | None,
    indicator_params: str | None,
    script: str | None,
    script_file: str | None,
    script_params: str | None,
    script_timeout: float | None,
    warmup_bars: int | None,
    tail: int,
) -> None:
    """Fetch OHLCV for one or many symbols and optionally compute indicators.

    Three symbol-input modes (mutually exclusive):

    * positional ``CODE`` — single symbol, e.g. ``600519.SH``.
    * ``--symbols A.SH,B.SZ`` — short multi-symbol list.
    * ``--universe-file path.txt`` — one CODE.EXCHANGE per line, ``#`` comments.

    Custom scripts run inside an AST-validated sandbox. They receive
    ``df`` (fetch window including warm-up), ``target_df`` (requested
    window), ``params`` (from ``--script-params``), plus ``pd``, ``np``,
    and ``indicators`` from the strategy SDK. Scripts may declare
    ``REQUIRED_HISTORY = <int>`` at top level so warm-up auto-sizing knows
    how much lookback the script needs.

    Failures are reported per symbol in ``symbols[i]``; a partial run
    (some succeeded, some failed) returns ``status: partial`` and a
    non-error envelope.
    """

    async def _run() -> tuple[dict[str, Any], int]:
        parsed_indicator_params, err = _json_object_or_error(
            indicator_params,
            flag="--indicator-params",
            error_code="invalid_indicator_params_json",
        )
        if err is not None:
            return err, EXIT_VALIDATION
        parsed_script_params, err = _json_object_or_error(
            script_params,
            flag="--script-params",
            error_code="invalid_script_params_json",
        )
        if err is not None:
            return err, EXIT_VALIDATION

        body: dict[str, Any] = {
            "interval": interval,
            "data_source": data_source,
            "tail": tail,
        }
        if code is not None:
            body["code"] = code
        if symbols is not None:
            body["symbols"] = symbols
        if universe_file is not None:
            body["universe_file"] = universe_file
        if period is not None:
            body["period"] = period
        if range_start is not None:
            body["start_date"] = range_start
        if range_end is not None:
            body["end_date"] = range_end
        if indicators is not None:
            body["indicators"] = indicators
        if parsed_indicator_params is not None:
            body["indicator_params"] = parsed_indicator_params
        if script is not None:
            body["script"] = script
        if script_file is not None:
            body["script_file"] = script_file
        if parsed_script_params is not None:
            body["script_params"] = parsed_script_params
        if script_timeout is not None:
            body["script_timeout"] = script_timeout
        if warmup_bars is not None:
            body["warmup_bars"] = warmup_bars
        return await invoke_api("POST", "/data/run", json=body, meta=read_session_meta(), timeout_seconds=_DATA_FETCH_TIMEOUT_SECONDS)

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@data.command("sync")
@click.argument("symbol", required=True)
@click.option(
    "--start",
    required=True,
    help="Range start — YYYY-MM-DD for 1d bars; timezone-aware ISO for 5m.",
)
@click.option(
    "--end",
    required=True,
    help="Range end — YYYY-MM-DD for 1d bars; timezone-aware ISO for 5m.",
)
@click.option(
    "--interval",
    default="1d",
    show_default=True,
    type=click.Choice(["1d", "5m"]),
    help="Bar interval to warm into the local warehouse.",
)
@click.option(
    "--mode",
    default="fill_gap",
    show_default=True,
    type=click.Choice(["fill_gap", "force_refresh"]),
    help=(
        "fill_gap = only fetch trading days missing locally; "
        "force_refresh = re-fetch and overwrite the whole range."
    ),
)
@click.option(
    "--provider",
    default=None,
    help=(
        "Override the upstream provider written into the warehouse key "
        "(default: market_data.default_provider). Leave unset so the cached rows "
        "match the key 'stock screen' reads."
    ),
)
@click.option(
    "--adjust",
    default=None,
    help="Override the adjust type for the warehouse key (default: the provider's default).",
)
def data_sync(
    symbol: str,
    start: str,
    end: str,
    interval: str,
    mode: str,
    provider: str | None,
    adjust: str | None,
) -> None:
    """Warm the local ``market_bars`` warehouse for one symbol's range.

    Pre-fetches OHLCV into the local market warehouse so a later
    ``stock screen`` (and backtests / live cycles) reads it locally instead of
    over the network. Short ranges run synchronously and return ``status: ok``
    with ``upserted_count``; large ranges run as a background job and return
    ``status: accepted`` with a ``job_id`` (poll ``GET /market/bars/sync-jobs/{id}``).

    For full-market warming prefer the background sync
    (``market_data.sync_full_market: true``) over looping this per symbol.
    """

    async def _run() -> tuple[dict[str, Any], int]:
        body: dict[str, Any] = {
            "symbol": symbol,
            "interval": interval,
            "start": start,
            "end": end,
            "mode": mode,
        }
        if provider is not None:
            body["provider"] = provider
        if adjust is not None:
            body["adjust"] = adjust
        return await invoke_api(
            "POST",
            "/market/bars/sync-range",
            json=body,
            meta=read_session_meta(),
            timeout_seconds=_DATA_FETCH_TIMEOUT_SECONDS,
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@data.command("news")
@click.argument("code", required=False)
@click.option(
    "--symbols",
    default=None,
    help=(
        "Multi-symbol input: comma-separated list (e.g. '600519.SH,000001.SZ') "
        "or a JSON array string. Mutually exclusive with the positional code "
        "argument and --universe-file."
    ),
)
@click.option(
    "--universe-file",
    "universe_file",
    default=None,
    type=click.Path(dir_okay=False, readable=True),
    help=(
        "Path to a file with one canonical CODE.EXCHANGE per line "
        "(# comments allowed). Mutually exclusive with the positional code "
        "argument and --symbols."
    ),
)
@click.option(
    "--period",
    default=None,
    help="Relative window, e.g. 7d / 1mo / 1y. Mutually exclusive with --start / --end.",
)
@click.option(
    "--start",
    "--range-start",
    "range_start",
    default=None,
    help="Inclusive start date YYYY-MM-DD (filters by publish date).",
)
@click.option(
    "--end",
    "--range-end",
    "range_end",
    default=None,
    help="Inclusive end date YYYY-MM-DD (filters by publish date).",
)
@click.option(
    "--data-source",
    "data_source",
    default="auto",
    show_default=True,
    type=click.Choice(["auto", "akshare"]),
    help="News provider id (akshare only today).",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Max most-recent articles per symbol (default 50).",
)
def data_news(
    code: str | None,
    symbols: str | None,
    universe_file: str | None,
    period: str | None,
    range_start: str | None,
    range_end: str | None,
    data_source: str,
    limit: int | None,
) -> None:
    """Fetch recent news for one or many symbols and persist to local CSV.

    Three symbol-input modes (mutually exclusive):

    * positional ``CODE`` — single symbol, e.g. ``600519.SH``.
    * ``--symbols A.SH,B.SZ`` — short multi-symbol list.
    * ``--universe-file path.txt`` — one CODE.EXCHANGE per line, ``#`` comments.

    Each symbol's articles are filtered to the requested window by publish
    date and written to ``news_<code>.csv`` under the assistant artifacts
    root; the envelope reports ``news_path`` plus a preview of the most
    recent items. The upstream akshare endpoint only returns recent news,
    so a window with no matching articles returns ``news_empty`` for that
    symbol. Per-symbol failures surface in ``symbols[i]``; a partial run
    returns ``status: partial`` and a non-error envelope.
    """

    async def _run() -> tuple[dict[str, Any], int]:
        body: dict[str, Any] = {"data_source": data_source}
        if code is not None:
            body["code"] = code
        if symbols is not None:
            body["symbols"] = symbols
        if universe_file is not None:
            body["universe_file"] = universe_file
        if period is not None:
            body["period"] = period
        if range_start is not None:
            body["start_date"] = range_start
        if range_end is not None:
            body["end_date"] = range_end
        if limit is not None:
            body["limit"] = limit
        return await invoke_api("POST", "/data/news", json=body, meta=read_session_meta(), timeout_seconds=_DATA_FETCH_TIMEOUT_SECONDS)

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@data.command("reports")
@click.argument("code", required=False)
@click.option(
    "--symbols",
    default=None,
    help=(
        "Multi-symbol input: comma-separated list (e.g. '600519.SH,000001.SZ') "
        "or a JSON array string. Mutually exclusive with the positional code "
        "argument and --universe-file."
    ),
)
@click.option(
    "--universe-file",
    "universe_file",
    default=None,
    type=click.Path(dir_okay=False, readable=True),
    help=(
        "Path to a file with one canonical CODE.EXCHANGE per line "
        "(# comments allowed). Mutually exclusive with the positional code "
        "argument and --symbols."
    ),
)
@click.option(
    "--period",
    default=None,
    help="Relative window, e.g. 7d / 1mo / 1y. Mutually exclusive with --start / --end.",
)
@click.option(
    "--start",
    "--range-start",
    "range_start",
    default=None,
    help="Inclusive start date YYYY-MM-DD (filters by report date).",
)
@click.option(
    "--end",
    "--range-end",
    "range_end",
    default=None,
    help="Inclusive end date YYYY-MM-DD (filters by report date).",
)
@click.option(
    "--data-source",
    "data_source",
    default="auto",
    show_default=True,
    type=click.Choice(["auto", "akshare"]),
    help="Research-report provider id (akshare only today).",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Max most-recent reports per symbol (default 50).",
)
def data_reports(
    code: str | None,
    symbols: str | None,
    universe_file: str | None,
    period: str | None,
    range_start: str | None,
    range_end: str | None,
    data_source: str,
    limit: int | None,
) -> None:
    """Fetch brokerage research reports (券商个股研报) for symbols to local CSV.

    Three symbol-input modes (mutually exclusive):

    * positional ``CODE`` — single symbol, e.g. ``600519.SH``.
    * ``--symbols A.SH,B.SZ`` — short multi-symbol list.
    * ``--universe-file path.txt`` — one CODE.EXCHANGE per line, ``#`` comments.

    Each symbol's reports are filtered to the requested window by report
    date and written to ``research_reports_<code>.csv`` under the assistant
    artifacts root; the envelope reports ``reports_path`` plus a preview of
    the most recent items (title / rating / institution). Each report also
    carries analyst EPS / PE forecasts by year and a PDF link. The upstream
    akshare endpoint returns every report it holds for the symbol, so a
    window with no matching reports returns ``research_reports_empty`` for
    that symbol. Per-symbol failures surface in ``symbols[i]``; a partial
    run returns ``status: partial`` and a non-error envelope.
    """

    async def _run() -> tuple[dict[str, Any], int]:
        body: dict[str, Any] = {"data_source": data_source}
        if code is not None:
            body["code"] = code
        if symbols is not None:
            body["symbols"] = symbols
        if universe_file is not None:
            body["universe_file"] = universe_file
        if period is not None:
            body["period"] = period
        if range_start is not None:
            body["start_date"] = range_start
        if range_end is not None:
            body["end_date"] = range_end
        if limit is not None:
            body["limit"] = limit
        return await invoke_api("POST", "/data/reports", json=body, meta=read_session_meta(), timeout_seconds=_DATA_FETCH_TIMEOUT_SECONDS)

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@data.command("breadth")
@click.option(
    "--date",
    "trade_date",
    default=None,
    help="Trading day YYYY-MM-DD (default: today, Asia/Shanghai). Must be a real trading day.",
)
@click.option(
    "--data-source",
    "data_source",
    default="auto",
    show_default=True,
    type=click.Choice(["auto", "akshare"]),
    help="Breadth provider id (akshare only today).",
)
def data_breadth(
    trade_date: str | None,
    data_source: str,
) -> None:
    """Fetch A-share limit-up / down / broken-board breadth for one trading day.

    Pulls the 涨停 / 跌停 / 炸板 pools for the day, aggregates a market
    limit-up panel, a consecutive-limit ladder (连板梯队), and a rule-based
    sentiment thermometer (情绪温度计), and writes each pool to a local CSV
    (``limit_up_pool_<date>.csv`` etc.) under the assistant artifacts root.

    The envelope ``data`` carries ``trade_date`` / ``limit_up_count`` /
    ``limit_down_count`` / ``broken_board_count`` / ``broken_board_rate`` /
    ``max_streak`` / ``ladder`` / ``sentiment`` (label + reason + disclaimer +
    inputs) plus the three CSV paths. The sentiment label is a single-day,
    rule-based, non-predictive description — never investment advice.

    ``--date`` defaults to today (Asia/Shanghai); a non-trading day (or a day
    whose after-hours snapshot hasn't updated) returns ``market_breadth_empty``.
    When one pool fails but others succeed the run returns ``status: partial``
    and names the failed pool in ``data.pool_errors``.
    """

    async def _run() -> tuple[dict[str, Any], int]:
        body: dict[str, Any] = {"data_source": data_source}
        if trade_date is not None:
            body["date"] = trade_date
        return await invoke_api(
            "POST",
            "/data/breadth",
            json=body,
            meta=read_session_meta(),
            timeout_seconds=_DATA_FETCH_TIMEOUT_SECONDS,
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@data.command("lhb")
@click.option(
    "--symbol",
    default=None,
    help=(
        "Canonical CODE.EXCHANGE (e.g. 600519.SH). When given, switches to "
        "per-seat / 游资 detail mode for that name on a single --date (a range "
        "is rejected). Omit for the market-level daily board."
    ),
)
@click.option(
    "--date",
    "trade_date",
    default=None,
    help="Single trading day YYYY-MM-DD. Market mode: mutually exclusive with --start / --end. Seat mode: the only date input. Default: today (Asia/Shanghai).",
)
@click.option(
    "--start",
    "range_start",
    default=None,
    help="Inclusive range start YYYY-MM-DD (with --end). Market mode only.",
)
@click.option(
    "--end",
    "range_end",
    default=None,
    help="Inclusive range end YYYY-MM-DD (with --start). Market mode only.",
)
@click.option(
    "--data-source",
    "data_source",
    default="auto",
    show_default=True,
    type=click.Choice(["auto", "akshare"]),
    help="龙虎榜 provider id (akshare only today).",
)
def data_lhb(
    symbol: str | None,
    trade_date: str | None,
    range_start: str | None,
    range_end: str | None,
    data_source: str,
) -> None:
    """Fetch the A-share 龙虎榜 (dragon-tiger board) — two modes.

    **Market mode** (no ``--symbol``): the exchange's daily large-order /
    abnormal-move disclosure list (``stock_lhb_detail_em``) — a **market-wide
    per-day list**, not a per-symbol series. Pass either ``--date`` (single
    day) OR ``--start`` / ``--end`` (range); both default to today
    (Asia/Shanghai, no client-side trading-calendar). A non-trading window
    returns ``lhb_empty``. Rows are written to ``lhb_<start>_<end>.csv`` with
    the canonical ``symbol`` plus key 中文 fields
    (``code,name,on_date,reason,interpretation,change_pct,close_price,``
    ``net_buy_amount,buy_amount,sell_amount,turnover_rate,circulating_mv``); the
    envelope ``data`` carries ``mode="market"`` / ``start_date`` / ``end_date``
    (``YYYYMMDD``) / ``count`` / ``lhb_path`` / ``latest`` / ``status``.

    **Seat mode** (``--symbol CODE.EXCHANGE --date YYYY-MM-DD``): that name's
    per-营业部 (trading desk) 买入/卖出 席位明细 for a single day
    (``stock_lhb_stock_detail_em``). Each seat is tagged with a best-effort
    游资名 (``hot_money``, from a non-authoritative static starter library) and
    flagged ``is_institution`` for 机构专用 desks. Seat mode requires a single
    ``--date`` (a ``--start``/``--end`` range → ``invalid_date``). A name that
    did NOT make the board that day returns the **distinct** ``lhb_no_seat_data``
    (not ``lhb_fetch_failed``). Seats are written to
    ``lhb_seats_<symbol>_<date>.csv``; the envelope ``data`` carries
    ``mode="seats"`` / ``symbol`` / ``date`` / ``buy_seats`` / ``sell_seats`` /
    ``seats_path`` / ``status``.
    """

    async def _run() -> tuple[dict[str, Any], int]:
        body: dict[str, Any] = {"data_source": data_source}
        if symbol is not None:
            body["symbol"] = symbol
        if trade_date is not None:
            body["date"] = trade_date
        if range_start is not None:
            body["start"] = range_start
        if range_end is not None:
            body["end"] = range_end
        return await invoke_api(
            "POST",
            "/data/lhb",
            json=body,
            meta=read_session_meta(),
            timeout_seconds=_DATA_FETCH_TIMEOUT_SECONDS,
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@data.command("chips")
@click.option(
    "--symbol",
    required=True,
    help="Canonical CODE.EXCHANGE (e.g. 600519.SH).",
)
@click.option(
    "--days",
    type=int,
    default=1,
    show_default=True,
    help="Most recent N trading days of 筹码分布 (1-90). Default 1 (latest day only).",
)
@click.option(
    "--data-source",
    "data_source",
    default="auto",
    show_default=True,
    type=click.Choice(["auto", "akshare"]),
    help="筹码分布 provider id (akshare only today).",
)
def data_chips(
    symbol: str,
    days: int,
    data_source: str,
) -> None:
    """Fetch A-share 筹码分布 (chip distribution / 筹码集中度) for one symbol.

    获利比例 (profit ratio), 平均成本 (avg cost), and the 90%/70% cost-band
    concentration akshare computes from OHLCV + turnover
    (``stock_cyq_em``). A-share individual stocks only — ETFs / indices /
    non-A-share names return the distinct ``chip_distribution_empty`` (never
    a fabricated snapshot). Defaults to the single latest trading day; pass
    ``--days`` > 1 for a short trend window. Rows are written to
    ``chips_<symbol>.csv`` with
    ``symbol,date,profit_ratio,avg_cost,cost_90_low,cost_90_high,``
    ``concentration_90,cost_70_low,cost_70_high,concentration_70,provider``;
    the envelope ``data`` carries ``symbol`` / ``days`` / ``count`` /
    ``chips_path`` / ``latest`` / ``status``.
    """

    async def _run() -> tuple[dict[str, Any], int]:
        body: dict[str, Any] = {"symbol": symbol, "days": days, "data_source": data_source}
        return await invoke_api(
            "POST",
            "/data/chips",
            json=body,
            meta=read_session_meta(),
            timeout_seconds=_DATA_FETCH_TIMEOUT_SECONDS,
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@data.command("fund-flow")
@click.option(
    "--scope",
    default="individual",
    show_default=True,
    type=click.Choice(["individual", "sector"]),
    help="individual = per-stock ranking; sector = per-board ranking.",
)
@click.option(
    "--period",
    default="今日",
    show_default=True,
    type=click.Choice(["今日", "3日", "5日", "10日"]),
    help="Rolling window. sector scope has NO 3日 (rejected with invalid_period).",
)
@click.option(
    "--sector-type",
    "sector_type",
    default=None,
    type=click.Choice(["行业", "概念", "地域"]),
    help="sector scope only: maps to akshare 行业/概念/地域资金流 (default 概念).",
)
@click.option(
    "--top",
    type=int,
    default=None,
    help="Rows previewed under data.latest, ranked by main net inflow desc (default 30). CSV holds the full ranking.",
)
@click.option(
    "--data-source",
    "data_source",
    default="auto",
    show_default=True,
    type=click.Choice(["auto", "akshare"]),
    help="Fund-flow provider id (akshare only today).",
)
def data_fund_flow(
    scope: str,
    period: str,
    sector_type: str | None,
    top: int | None,
    data_source: str,
) -> None:
    """Fetch A-share 资金流排名 (fund-flow ranking) for stocks or sector boards.

    Main / super-large / large / medium / small net inflow ranking over a
    rolling ``--period`` (今日 / 3日 / 5日 / 10日). This is a **market-wide**
    ranking, not a per-symbol series — there is **no `--symbols` and no date**
    (the window is the rolling period).

    * ``--scope individual`` (default) → per-stock ranking.
    * ``--scope sector`` → per-board ranking; ``--period`` allows only
      {今日, 5日, 10日} (**no 3日** → ``invalid_period``) and ``--sector-type``
      (default 概念) maps to akshare 行业/概念/地域资金流.

    Rows are ranked by main net inflow (净额) descending; ``--top`` (default 30)
    picks how many are previewed under ``data.latest`` while the CSV
    (``fund_flow_<scope>_<period>.csv``) holds the full ranking. Envelope
    ``data`` carries ``scope`` / ``period`` / ``sector_type`` (sector only) /
    ``count`` / ``top`` / ``fund_flow_path`` / ``latest`` / ``status``.
    """

    async def _run() -> tuple[dict[str, Any], int]:
        body: dict[str, Any] = {
            "scope": scope,
            "period": period,
            "data_source": data_source,
        }
        if sector_type is not None:
            body["sector_type"] = sector_type
        if top is not None:
            body["top"] = top
        return await invoke_api(
            "POST",
            "/data/fund-flow",
            json=body,
            meta=read_session_meta(),
            timeout_seconds=_DATA_FETCH_TIMEOUT_SECONDS,
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@data.command("sector-heat")
@click.option(
    "--sector-type",
    "sector_type",
    default="concept",
    show_default=True,
    type=click.Choice(["concept", "industry"], case_sensitive=False),
    help="concept = 概念板块; industry = 行业板块.",
)
@click.option(
    "--top",
    type=int,
    default=None,
    help="Boards previewed under data.latest, ranked by 涨跌幅 desc (default 30). CSV holds the full ranking.",
)
@click.option(
    "--data-source",
    "data_source",
    default="auto",
    show_default=True,
    type=click.Choice(["auto", "akshare"]),
    help="Sector-heat provider id (akshare only today).",
)
def data_sector_heat(
    sector_type: str,
    top: int | None,
    data_source: str,
) -> None:
    """Fetch the A-share 题材 / 板块热度榜 (sector-heat ranking) for one board family.

    Pulls the whole-board snapshot the akshare board-name endpoints return —
    板块 涨跌幅 / 总市值 / 换手率 / 上涨·下跌家数 / 领涨股 + 领涨股涨跌幅 — and
    ranks boards by 涨跌幅 descending (板块涨幅榜 = a first-order read of the
    day's 主线 热度). This is a **market-wide** board snapshot, not a per-symbol
    series — there is **no `--symbols` and no date**.

    * ``--sector-type concept`` (default) → 概念板块.
    * ``--sector-type industry`` → 行业板块.

    ``--top`` (default 30) picks how many boards are previewed under
    ``data.latest`` while the CSV (``sector_heat_<sector_type>.csv``) holds the
    full ranking. Envelope ``data`` carries ``sector_type`` / ``count`` /
    ``top`` / ``sector_heat_path`` / ``latest`` / ``status``. An empty board
    list returns ``sector_heat_empty``; a persistent upstream failure returns
    ``sector_heat_fetch_failed``.
    """

    async def _run() -> tuple[dict[str, Any], int]:
        body: dict[str, Any] = {
            "sector_type": sector_type.lower(),
            "data_source": data_source,
        }
        if top is not None:
            body["top"] = top
        return await invoke_api(
            "POST",
            "/data/sector-heat",
            json=body,
            meta=read_session_meta(),
            timeout_seconds=_DATA_FETCH_TIMEOUT_SECONDS,
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@data.command("earnings")
@click.argument("code", required=False)
@click.option(
    "--symbols",
    default=None,
    help=(
        "Multi-symbol input: comma-separated list (e.g. '600519.SH,000001.SZ') "
        "or a JSON array string. Mutually exclusive with the positional code "
        "argument and --universe-file."
    ),
)
@click.option(
    "--universe-file",
    "universe_file",
    default=None,
    type=click.Path(dir_okay=False, readable=True),
    help=(
        "Path to a file with one canonical CODE.EXCHANGE per line "
        "(# comments allowed). Mutually exclusive with the positional code "
        "argument and --symbols."
    ),
)
@click.option(
    "--period",
    default=None,
    help=(
        "Relative report-period window, e.g. 1y (trailing 4 quarters). "
        "Mutually exclusive with --start / --end."
    ),
)
@click.option(
    "--start",
    "--range-start",
    "range_start",
    default=None,
    help="Inclusive start date YYYY-MM-DD (selects quarter-ends inside the window).",
)
@click.option(
    "--end",
    "--range-end",
    "range_end",
    default=None,
    help="Inclusive end date YYYY-MM-DD (selects quarter-ends inside the window).",
)
@click.option(
    "--kind",
    "kind",
    default="both",
    show_default=True,
    type=click.Choice(["forecast", "express", "both"]),
    help="forecast=业绩预告, express=业绩快报, both=both.",
)
@click.option(
    "--data-source",
    "data_source",
    default="auto",
    show_default=True,
    type=click.Choice(["auto", "akshare"]),
    help="Earnings provider id (akshare only today).",
)
def data_earnings(
    code: str | None,
    symbols: str | None,
    universe_file: str | None,
    period: str | None,
    range_start: str | None,
    range_end: str | None,
    kind: str,
    data_source: str,
) -> None:
    """Fetch earnings preannouncements (业绩预告) / express reports (业绩快报).

    Three symbol-input modes (mutually exclusive):

    * positional ``CODE`` — single symbol, e.g. ``600519.SH``.
    * ``--symbols A.SH,B.SZ`` — short multi-symbol list.
    * ``--universe-file path.txt`` — one CODE.EXCHANGE per line, ``#`` comments.

    The window (``--period`` or ``--start`` / ``--end``, default 1y) selects
    which fiscal quarter-ends (03-31 / 06-30 / 09-30 / 12-31) to cover — every
    quarter-end inside the window becomes one report period. ``--kind`` picks
    forecast (业绩预告) / express (业绩快报) / both (default both). Earnings data
    is served full-market per quarter-end, so the provider pulls each period
    once and filters to the requested symbols in memory. Each (symbol, kind)
    pair is written to ``earnings_<kind>_<code>.csv`` under the assistant
    artifacts root. A symbol with no row across all requested kinds × periods
    returns ``earnings_empty`` for that symbol. Per-symbol failures surface in
    ``symbols[i]``; a partial run returns ``status: partial``.
    """

    async def _run() -> tuple[dict[str, Any], int]:
        body: dict[str, Any] = {"data_source": data_source, "kind": kind}
        if code is not None:
            body["code"] = code
        if symbols is not None:
            body["symbols"] = symbols
        if universe_file is not None:
            body["universe_file"] = universe_file
        if period is not None:
            body["period"] = period
        if range_start is not None:
            body["start_date"] = range_start
        if range_end is not None:
            body["end_date"] = range_end
        return await invoke_api("POST", "/data/earnings", json=body, meta=read_session_meta(), timeout_seconds=_DATA_FETCH_TIMEOUT_SECONDS)

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@data.command("sectors")
@click.option(
    "--sector-type",
    "sector_type",
    default=None,
    type=click.Choice(["industry", "concept"], case_sensitive=False),
    help="Restrict to industry or concept boards. Omit to list both.",
)
@click.option(
    "--data-source",
    "data_source",
    default="auto",
    show_default=True,
    type=click.Choice(["auto", "akshare", "qmt"], case_sensitive=False),
    help="Sector source. auto walks akshare → qmt.",
)
@click.option("--limit", type=int, default=None, help="Cap the number of board names returned.")
def data_sectors(
    sector_type: str | None,
    data_source: str,
    limit: int | None,
) -> None:
    """List available sector / industry / concept board names.

    Use this to discover board names, then pass one to ``data sector-members``
    to build a screenable universe. envelope ``data.sectors`` holds the names.
    """

    async def _run() -> tuple[dict[str, Any], int]:
        body: dict[str, Any] = {"data_source": data_source.lower()}
        if sector_type is not None:
            body["sector_type"] = sector_type.lower()
        if limit is not None:
            body["limit"] = limit
        return await invoke_api("POST", "/data/sector", json=body, meta=read_session_meta(), timeout_seconds=_DATA_FETCH_TIMEOUT_SECONDS)

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@data.command("sector-members")
@click.argument("sector_names", required=True)
@click.option(
    "--sector-type",
    "sector_type",
    default=None,
    type=click.Choice(["industry", "concept"], case_sensitive=False),
    help="Disambiguate when a name exists as both an industry and a concept board.",
)
@click.option(
    "--data-source",
    "data_source",
    default="auto",
    show_default=True,
    type=click.Choice(["auto", "akshare", "qmt"], case_sensitive=False),
    help="Sector source. auto walks akshare → qmt.",
)
@click.option("--limit", type=int, default=None, help="Cap members per board.")
@click.option(
    "--output",
    "output",
    default=None,
    type=click.Path(dir_okay=False, writable=True),
    help="Combined universe CSV path (default: artifacts dir).",
)
def data_sector_members(
    sector_names: str,
    sector_type: str | None,
    data_source: str,
    limit: int | None,
    output: str | None,
) -> None:
    """Fetch board constituents and write a screenable universe CSV.

    ``SECTOR_NAMES`` is one or more comma-separated board names (e.g.
    ``白酒`` or ``白酒,半导体``). Each board's members are written to
    ``sector_<name>.csv``; the de-duplicated union of all member codes is
    written to a universe file (``data.universe_path``) ready for
    ``stock screen --universe-file``.
    """

    async def _run() -> tuple[dict[str, Any], int]:
        body: dict[str, Any] = {"data_source": data_source.lower(), "sector_names": sector_names}
        if sector_type is not None:
            body["sector_type"] = sector_type.lower()
        if limit is not None:
            body["limit"] = limit
        if output is not None:
            body["output_path"] = output
        return await invoke_api("POST", "/data/sector", json=body, meta=read_session_meta(), timeout_seconds=_DATA_FETCH_TIMEOUT_SECONDS)

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@data.command("fundamentals")
@click.argument("code", required=False)
@click.option("--symbols", default=None, help="Comma-separated or JSON array of symbols.")
@click.option(
    "--universe-file",
    "universe_file",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="One CODE.EXCHANGE per line (# comments allowed).",
)
@click.option(
    "--data-source",
    "data_source",
    default="auto",
    show_default=True,
    type=click.Choice(["auto", "akshare", "qmt"], case_sensitive=False),
    help="Fundamentals source. auto walks akshare→qmt.",
)
@click.option(
    "--output",
    "output",
    default=None,
    type=click.Path(dir_okay=False, writable=True),
    help="Explicit CSV output path (default: artifacts dir fundamentals.csv).",
)
def data_fundamentals(
    code: str | None,
    symbols: str | None,
    universe_file: str | None,
    data_source: str,
    output: str | None,
) -> None:
    """Fetch float / total market cap + PE / PB and write a CSV.

    Symbol input is `code` / `--symbols` / `--universe-file` (mutually
    exclusive). Market-cap values are in 元 (100亿 = 1e10). akshare serves
    PE/PB; qmt gives float-cap only. `data.fundamentals_path` is the CSV
    (`code,float_mv,total_mv,pe,pb,price`); `data.missing` lists symbols the
    source couldn't serve.
    """

    async def _run() -> tuple[dict[str, Any], int]:
        body: dict[str, Any] = {"data_source": data_source.lower()}
        if code is not None:
            body["code"] = code
        if symbols is not None:
            body["symbols"] = symbols
        if universe_file is not None:
            body["universe_file"] = universe_file
        if output is not None:
            body["output_path"] = output
        return await invoke_api("POST", "/data/fundamentals", json=body, meta=read_session_meta(), timeout_seconds=_DATA_FETCH_TIMEOUT_SECONDS)

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@data.command("events")
@click.argument("code", required=False)
@click.option("--symbols", default=None, help="Comma-separated or JSON array of symbols.")
@click.option(
    "--universe-file",
    "universe_file",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="One CODE.EXCHANGE per line (# comments allowed).",
)
@click.option("--asof", default=None, help="Decision date YYYY-MM-DD (suspension snapshot date). Defaults to today.")
@click.option(
    "--data-source",
    "data_source",
    default="auto",
    show_default=True,
    type=click.Choice(["auto", "akshare"], case_sensitive=False),
    help="Event source (akshare suspension snapshot).",
)
@click.option(
    "--output",
    "output",
    default=None,
    type=click.Path(dir_okay=False, writable=True),
    help="Explicit CSV output path (default: artifacts dir events.csv).",
)
def data_events(
    code: str | None,
    symbols: str | None,
    universe_file: str | None,
    asof: str | None,
    data_source: str,
    output: str | None,
) -> None:
    """Fetch calendar / status events (suspension 停牌) and write a CSV.

    The inspector behind `stock screen --exclude-suspended`. Symbol input is
    `code` / `--symbols` / `--universe-file` (mutually exclusive).
    `data.events_path` is the CSV (`code,event_type,event_date,detail`);
    `data.symbols_with_events` counts how many had events.
    """

    async def _run() -> tuple[dict[str, Any], int]:
        body: dict[str, Any] = {"data_source": data_source.lower()}
        if code is not None:
            body["code"] = code
        if symbols is not None:
            body["symbols"] = symbols
        if universe_file is not None:
            body["universe_file"] = universe_file
        if asof is not None:
            body["asof"] = asof
        if output is not None:
            body["output_path"] = output
        return await invoke_api("POST", "/data/events", json=body, meta=read_session_meta(), timeout_seconds=_DATA_FETCH_TIMEOUT_SECONDS)

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


__all__ = ["data"]
