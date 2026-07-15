# Real Trading Query And Prod Gated Execution Design

## Goal

Connect the trading service's read-side endpoints to real QMT trading data in `dev` and `prod` modes while keeping real trade execution blocked outside `prod`.

The service should stop returning hard-coded mock account, position, asset, order, and trade data when running against real QMT.

## Scope

This change covers the trading service behavior behind:

- `POST /api/v1/trading/connect`
- `GET /api/v1/trading/account/{session_id}`
- `GET /api/v1/trading/positions/{session_id}`
- `GET /api/v1/trading/asset/{session_id}`
- `GET /api/v1/trading/orders/{session_id}`
- `GET /api/v1/trading/trades/{session_id}`
- `POST /api/v1/trading/order/{session_id}`
- `POST /api/v1/trading/cancel/{session_id}`

It does not change the REST or gRPC route shapes, API authentication, or the market-data service.

Both REST and gRPC must share the same `TradingService` mode rules, query behavior, and execution gate semantics.

## Recommended Approach

Use one trading service with mode-aware behavior:

- `mock` mode keeps simulated connect/query behavior
- `dev` and `prod` use real `xttrader` for connect and all read-side queries
- only `prod` with `allow_real_trading=true` may execute real order submission or cancellation

Why this approach:

- keeps the current public API stable
- removes the risk of fake read-side trading data in real environments
- preserves a safe non-production workflow where teams can inspect real accounts without sending orders

## Mode Behavior

### `mock`

- `connect` may continue returning a simulated session and account
- `account`, `positions`, `asset`, `orders`, and `trades` may continue using simulated or in-memory values
- `order` and `cancel` stay non-real

### `dev`

- `connect` must establish a real trading context through QMT
- `account`, `positions`, `asset`, `orders`, and `trades` must query real `xttrader` data
- `order` and `cancel` must never execute against QMT
- query failures must raise errors directly instead of falling back to mock data

### `prod`

- same real query behavior as `dev`
- `order` and `cancel` may execute against QMT only when `allow_real_trading=true`

## Session Design

The proxy-owned `session_id` remains the external handle returned to clients.

For real-mode sessions, `_connected_accounts[session_id]` should store enough context for future calls, including:

- account id
- account type
- connected time
- the real xttrader account object or equivalent connection context
- cached account info if the implementation needs it

All later query and trade methods should resolve that stored context first.

This keeps the external session stable even if the underlying QMT APIs expect account objects instead of the proxy session string.

The implementation must not pass the public `session_id` directly into real QMT query or write APIs except as an internal lookup key.

## Real Query Rules

In `dev` and `prod`, read-side trading endpoints must fail fast when any of the following is true:

- `xtquant` is not installed
- trading service initialization failed
- QMT is not available
- the requested session is unknown
- the real account context is missing or invalid
- the underlying `xttrader` query raises an exception
- the returned payload cannot be mapped into the API models

The service must not silently return the old hard-coded sample records in these cases.

Normal empty query results are not errors:

- no positions must return `[]`
- no orders must return `[]`
- no trades must return `[]`

Initialization should be treated as a real prerequisite in `dev` and `prod`.
For this feature, "initialized" means the service has successfully created or attached the real trading backend it needs for connect/query operations.
If that prerequisite is not met, real-mode connect and query requests must fail rather than degrade.

## Data Mapping

The service should map real QMT objects into existing response models:

- `AccountInfo`
- `PositionInfo`
- `AssetInfo`
- `OrderResponse`
- `TradeInfo`

Because broker/QMT builds may expose slightly different field names, the mapper should read defensively for known aliases where practical.

Expected examples:

- position quantity from fields such as `volume` or `total_volume`
- available quantity from fields such as `available_volume` or `can_use_volume`
- cost price from fields such as `cost_price` or `open_price`
- cash from fields such as `cash`
- available cash from fields such as `available_cash`

If a critical field is absent and cannot be inferred safely, the request should fail with a clear service error instead of fabricating values.

`get_orders()` and `get_trades()` must use broker-sourced QMT query results in `dev` and `prod`.
The in-memory `_orders` and `_trades` structures may remain for mock mode or local bookkeeping, but they must not replace broker truth in real query modes.

## Execution Safety

`submit_order()` and `cancel_order()` should keep a strict real-trading gate:

- real execution is allowed only when `mode == prod`
- and `allow_real_trading == true`

In every other case:

- no real QMT write call is allowed
- `submit_order()` may return a mocked order response
- `cancel_order()` may return an intercepted non-real success only if the response remains observably simulated to callers and logs

This preserves the current operational guarantee that non-production environments cannot place or cancel real trades.

In real-write mode, cancellation must be based on the real broker/QMT order identity and must not require the order to exist only in the local in-memory cache.

## Error Handling

All real-mode query failures should raise `TradingServiceException` with actionable messages such as:

- trading backend unavailable
- xttrader not initialized
- account not connected
- failed to query positions from QMT
- failed to map QMT order payload

The existing router-level exception conversion can remain unchanged.

`connect_account()` keeps its current response model contract:

- successful connect returns `ConnectResponse(success=True, ...)`
- expected connect failures return `ConnectResponse(success=False, message=...)`
- unexpected internal failures may still surface as `TradingServiceException`

This keeps connect behavior stable for existing clients while removing fake success in real modes.

Real query methods after a failed or incomplete connect must raise service errors rather than returning simulated data.

## Testing Strategy

Use test-first development.

Add focused service-level tests that verify:

- `mock` mode still supports the current simulated workflow
- `dev` and `prod` queries do not fall back to mock positions/assets/orders/trades when real access is unavailable
- non-`prod` order submission still does not execute real trade calls
- non-`prod` cancel still does not execute real cancel calls
- real-mode query methods correctly map representative QMT objects into API models
- `prod` with `allow_real_trading=true` is the only path that calls the real write-side xttrader methods
- empty real query results map to empty API collections rather than failures
- connect failures in real modes return explicit unsuccessful connect responses instead of fake connected sessions

Where direct QMT integration is impractical in tests, use controllable fake `xttrader` objects and fake result objects to validate branching and mapping behavior.

## Risks

The main implementation risks are:

- mismatched assumptions about actual `xttrader` connect/query signatures
- field-name variation between QMT environments
- accidentally reusing proxy `session_id` where QMT expects an account object
- unintentionally allowing write-side calls in `dev`
- exposing intercepted non-real cancel success in a way clients misread as broker-confirmed cancellation

These risks should be reduced by keeping all real trading gates centralized and by covering mode branching plus mapper behavior with focused tests.
