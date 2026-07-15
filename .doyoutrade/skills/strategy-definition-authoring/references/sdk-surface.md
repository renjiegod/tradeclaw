# SDK Surface Reference

Exact field shapes of `doyoutrade.strategy_sdk` for the `Strategy` API.
Load when confirming a method signature, the allowed-imports whitelist, or
how to size `startup_history`.

For indicator helpers (`indicators.macd / rsi / adx / …`), see
[`indicators.md`](indicators.md).
For structured error codes returned by `doyoutrade-cli sdk validate` /
`doyoutrade-cli strategy definition update`, see
[`error-codes.md`](error-codes.md).

## Allowed imports

```
__future__, decimal, math, numpy, pandas, doyoutrade.strategy_sdk, typing
```

No stdlib `os`, no networking, no filesystem, no third-party libs. Relative
imports are rejected. The compiler enforces this whitelist;
`doyoutrade-cli sdk validate` surfaces the exact error code
(`disallowed_import`).

### Pre-injected names (no `import` line required)

The compile sandbox pre-populates these symbols, so strategy code can
reference them directly. An explicit `from doyoutrade.strategy_sdk import ...`
is still legal and recommended for clarity.

```
Strategy, Signal, Direction,
DataRequest, BarsRequest, IndexBarsRequest, PeersRequest,
CrossSectionRequest, FundamentalsRequest,
IntParameter, DecimalParameter, CategoricalParameter, BooleanParameter,
StrategyDescriptor, informative, informative_each,
Decimal, decimal_from_number, math, indicators
```

## `Strategy` base class

Required override: `on_bar(self, df, ctx) -> Signal`. Everything else is
optional and has a sensible default.

```python
class Strategy(ABC):
    # --- Class metadata ---
    name: ClassVar[str] = ""
    timeframe: ClassVar[str] = "1d"       # one of:
                                          # "1m"/"5m"/"15m"/"30m"/"60m"/"1d"/"1w"/"1mo"
                                          # (hourly is "60m" not "1h"; monthly is "1mo"; no "4h")
    startup_history: ClassVar[int] = 30   # bars provisioned before populate/on_bar

    # --- Lifecycle hooks (optional) ---
    def on_strategy_start(self, ctx) -> None: ...
    def on_cycle_start(self, ctx) -> None: ...

    # --- Data declaration (optional) ---
    def informative_data(self, ctx) -> Sequence[DataRequest]: ...

    # --- Indicator computation (optional) ---
    def populate_indicators(self, df, ctx) -> pd.DataFrame: ...
    # @informative-decorated methods are also picked up here.

    # --- Required ---
    @abstractmethod
    def on_bar(self, df, ctx) -> Signal: ...
```

## `Signal` (return value of `on_bar`)

- `Signal.buy(*, tag, rationale="", diagnostics={})` — target-state long. `tag` mandatory.
- `Signal.sell(*, tag, rationale="", diagnostics={})` — target-state flat. `tag` mandatory.
- `Signal.target_exposure(*, target, tag, rationale="", diagnostics={})` — explicit post-cycle long exposure as a fraction of equity in `[0, 1]`. `tag` mandatory.
- `Signal.target_quantity(*, quantity, tag, rationale="", diagnostics={})` — explicit post-cycle share inventory in `quantity >= 0`. `tag` mandatory.
- `Signal.hold(*, tag="", rationale="", diagnostics={})` — no opinion; PositionManager preserves current position. `tag` optional.

`tag` is the factor identifier. It flows into `trade_fills.entry_tag` /
`exit_tag` and `strategy_runner_cycle` debug events. Use `"+".join(sorted(...))`
for multi-factor entries (canonical, groupable in analytics).

`Signal.buy` / `Signal.sell` are target-state semantics ("long" vs "flat").
`Signal.target_exposure` is a rebalance semantic: the strategy declares the
desired inventory level after this cycle, and execution computes the delta.
`Signal.target_quantity` is a strict inventory semantic: the strategy
declares the desired total share count after this cycle, and execution buys
or sells only the missing/excess shares.

## `StrategyContext` (the `ctx` argument)

Immutable per cycle. Strategy reads but never mutates these.

| Field | Type | Notes |
|---|---|---|
| `ctx.symbol` | `str` | Current evaluation symbol |
| `ctx.now` | `datetime` | Logical time |
| `ctx.run_id` / `ctx.trace_id` | `str` | Vertical IDs flowing into trace / persistence |
| `ctx.universe` | `tuple[str, ...]` | Full cycle universe |
| `ctx.position` | `PositionView` | Read-only: `.is_long`, `.is_flat`, `.quantity`, `.cost_price`, `.market_price`, `.current_profit` |
| `ctx.account` | `AccountView` | Read-only: `.cash`, `.equity` (Decimal) |
| `ctx.params` | `Mapping[str, Any]` | Cycle parameter overrides (free-form) |
| `ctx.dp` | `DataProvider` | Data access facade (below) |
| `ctx.is_backtest` | `bool` | True in backtest; `ctx.dp.ticker()` raises |

## `ctx.dp` — data access methods

All methods are sync from the strategy's POV. Each call emits an OTel span +
structured debug event automatically. Cross-symbol references MUST be
declared in `informative_data()` first.

```python
ctx.dp.get_bars(symbol=None, *, window, freq="1d", fields=None) -> pd.DataFrame
ctx.dp.get_index_bars(code, *, window, freq="1d") -> pd.DataFrame
ctx.dp.get_industry_members(industry=None, *, top_n=20, rank_by="market_cap") -> list[str]
ctx.dp.get_peer_bars(*, window, top_n=20, industry=None, rank_by="market_cap", freq="1d") -> dict[str, pd.DataFrame]
ctx.dp.get_fundamentals(symbol=None, *, fields) -> Mapping[str, Any]
ctx.dp.ticker(symbol=None) -> dict[str, Any]                  # live only
ctx.dp.orderbook(symbol=None, *, depth=5) -> dict[str, Any]   # live only
```

- `symbol=None` resolves to current `ctx.symbol`. Same for `"$self"`.
- `industry=None` resolves to `$self.industry` (current symbol's industry).
- Insufficient data raises `DataAccessError(error_code='data_insufficient')`.
- Unknown method calls fail at compile time (`unknown_dp_method`).

## `DataRequest` factories (for `informative_data`)

```python
DataRequest.bars(*, symbol, window, freq="1d") -> BarsRequest
DataRequest.index_bars(code, *, window, freq="1d") -> IndexBarsRequest
DataRequest.peers(*, window, top_n=20, industry="$self.industry", rank_by="market_cap", freq="1d") -> PeersRequest
DataRequest.cross_section(*, fields, universe="$cycle") -> CrossSectionRequest
DataRequest.fundamentals(*, fields, symbol="$self") -> FundamentalsRequest
```

Symbolic references:
- `"$self"` — current cycle symbol
- `"$self.industry"` — industry of the current symbol

## Tunable parameters

Declared as class attributes; read at runtime via `self.<name>.value`. The
runner binds cycle-supplied overrides before invoking strategy methods.

```python
fast = IntParameter(5, 30, default=10, step=1, optimize=True)
threshold = DecimalParameter(0.01, 0.10, default=0.03, decimals=3, optimize=True)
mode = CategoricalParameter(["aggressive", "moderate"], default="moderate")
use_trailing = BooleanParameter(default=False)
```

- `optimize=True` → included in hyperopt search space.
- `optimize=False` → tunable constant but not searched.
- Schema is extracted automatically into `StrategyDescriptor.parameter_schema`.

## `@informative` decorators

```python
@informative("1w")
def populate_weekly(self, df, ctx):
    df["ma20"] = df["close"].rolling(20).mean()
    return df

@informative("1d", symbol="600519.SH")
def populate_moutai(self, df, ctx):
    df["rsi"] = indicators.rsi(df["close"], 14)
    return df

@informative_each("1d", symbols=("600519.SH", "000858.SZ"))
def populate_leader(self, df, ctx, symbol):     # extra `symbol` kwarg
    df["rsi"] = indicators.rsi(df["close"], 14)
    return df
```

Column suffix policy:
- `@informative("1w")` → suffix `_1w` (e.g. `ma20_1w`)
- `@informative("1d", symbol="600519.SH")` → suffix `_600519_SH_1d`
- `@informative_each` → one merge per symbol with that symbol's suffix

All symbols referenced via `symbol=` or `symbols=(...)` must ALSO appear in
`informative_data()` via `DataRequest.bars(symbol=...)` so the runner can
prefetch them.

## Worker phase visibility

- `worker.phase.prefetch_informative` — batch fetches all declared DataRequests
- `worker.phase.populate_indicators` — per-symbol vectorized indicator population
- `worker.phase.on_bar` — per-symbol decision
- `strategy.dp.<method>` — each ctx.dp call gets its own span

All carry `run_id` so debug session can replay the full call tree.
