# MA Crossover Realtime Quote Logging Design

## Goal

Extend the realtime tick log in `examples/ma_crossover_strategy.py` so each monitored stock prints:

- realtime price
- change percentage
- turnover amount
- volume
- MA values
- current position state

## Scope

This change is limited to the realtime quote logging inside `run_realtime_strategy()`.

It does not change:

- stock screening
- MA calculation
- buy/sell signal detection
- order placement
- position management rules

## Recommended Approach

Keep the existing `[TICK]` log entry and expand it in place.

Why this approach:

- minimal code change
- keeps current log reading habits unchanged
- avoids introducing extra abstraction for a small requirement

## Data Handling

The log formatter should read the extra realtime fields from the incoming quote object:

- `last_price`
- `amount` as the primary turnover field
- `volume`
- `pre_close` for derived change percentage

Change percentage should be computed as:

`(last_price - pre_close) / pre_close * 100`

Rules for change percentage:

- if `pre_close` is missing, zero, or invalid, print `N/A`
- if a raw extra change-percentage field exists on the quote, it may be used as a fallback, but derived calculation from `pre_close` is preferred because `QuoteData` explicitly defines `pre_close`

If any non-essential field is missing, the log should print `N/A` instead of raising formatting errors.

Valid numeric zero values are not treated as missing:

- `amount=0` should print `0.00`
- `volume=0` should print `0`

`last_price` remains required for strategy processing; if it is missing or invalid, the tick should still be skipped as it is today.

## Output Shape

The tick log should remain a single line and follow the current style, expanded to include:

`price | change_pct | amount | volume | MA5 | MA20 | position`

MA labels in the actual implementation should follow the configured strategy periods:

- `MA{SHORT_MA_PERIOD}`
- `MA{LONG_MA_PERIOD}`

Example:

`[TICK #0001] 600519.SH | 价格=1234.56 | 涨跌幅=1.23% | 成交额=4567890.00 | 量=1200 | MA5=1230.12 | MA20=1218.34 | 持仓=无`

## Error Handling

- Missing change percentage: print `N/A`
- Missing turnover amount: print `N/A`
- Missing volume: print `N/A`
- Invalid or non-positive `last_price`: keep current skip behavior
- Zero or invalid `pre_close`: print `N/A` for change percentage

## Test Strategy

Use test-first development.

Add a focused unit test around extracted logging/formatting behavior so the change can be verified without altering the strategy flow.

Place the test under the project's `tests/` directory to keep verification separate from the `examples/` script.

The test should verify:

- normal quote data prints all requested fields
- missing optional quote fields fall back to `N/A`
- valid zero optional values are printed as zero, not `N/A`

To keep existing trade decision flow unchanged:

- extract only the log string formatting into a small helper if needed
- keep MA calculation, signal detection, order placement, and position management logic in their current control flow
- do not change the existing skip rule for invalid `last_price`

## Risk

Main risk is using incorrect quote attribute names for change percentage or turnover amount. The implementation should inspect the SDK models or adapt defensively so optional fields do not break runtime logging.
