# Tradeclaw OpenTelemetry Observability Design

## Goal

Introduce a unified logging and tracing foundation based on the OpenTelemetry SDK so that Tradeclaw and the local `qmt_proxy_sdk` emit consistent logs with `trace_id` and `span_id` by default across FastAPI requests, runtime scheduling, worker execution, and transport operations.

## Current State

The codebase currently has two separate observability concepts:

- business trace persistence in `tradeclaw.persistence.trace_store`
- ad hoc Python `logging` usage in a small number of modules

This causes several gaps:

- logs are not consistently emitted in key execution paths
- log records do not include the active trace context
- FastAPI request handling, runtime loop execution, and SDK transport calls are not connected through a shared tracing model
- there is no central initialization point for logging and tracing

## Chosen Approach

Use OpenTelemetry tracing as the source of execution context and keep Python `logging` as the log emission API.

The implementation will:

- initialize a shared `TracerProvider` and root logger from a new `tradeclaw.observability` package
- use `LoggingInstrumentor` to inject active trace metadata into standard logging records
- use a custom console text formatter so all logs consistently print `trace_id` and `span_id`
- instrument FastAPI request entrypoints with OpenTelemetry FastAPI instrumentation
- create manual spans around runtime loop ticks, per-instance scheduling, worker cycles, and SDK transport operations

This is preferred over adopting the OpenTelemetry Logs SDK directly because the current requirement is console-first text logging, the codebase already depends on Python `logging`, and the tracing SDK gives us the context propagation needed for `trace_id` and `span_id` with lower integration cost.

## Non-Goals

This design does not include:

- OTLP exporters or collector integration
- structured JSON logging as the default output format
- replacement of the existing business `trace_store`
- automatic instrumentation for every third-party library in the stack

Those can be added later without changing the public logging facade introduced here.

## Module Design

### `tradeclaw.observability.logging`

Responsibilities:

- expose `get_logger(name)` for application code
- provide a formatter that always renders `trace_id` and `span_id`
- configure the root logger level and console handler
- remain safe when no active span exists by rendering placeholder values such as `-`

### `tradeclaw.observability.tracing`

Responsibilities:

- expose `get_tracer(name)`
- create the shared `TracerProvider`
- register a basic `ConsoleSpanExporter`-free provider for context propagation only
- define helper functions for formatting active span identifiers when needed

### `tradeclaw.observability.init`

Responsibilities:

- provide a single public `initialize_observability(...)` function
- enforce idempotent setup so repeated startup paths do not duplicate handlers or providers
- activate `LoggingInstrumentor`
- optionally instrument FastAPI applications passed to the initializer

## Configuration

Add an `observability` section to application config with these initial fields:

- `service_name`
- `log_level`
- `console_enabled`
- `tracing_enabled`

Defaults should be safe for local development:

- `service_name: tradeclaw`
- `log_level: INFO`
- `console_enabled: true`
- `tracing_enabled: true`

No exporter configuration is required in this change.

## Span Boundaries

### FastAPI

Each HTTP request should start with a request span created by FastAPI instrumentation. Application logs emitted during request handling should inherit the request trace context automatically.

### Runtime Loop

`tradeclaw.api.runtime_loop.RuntimeTickLoop._run()` should create a span for each loop iteration and log:

- tick start
- tick success with executed instance count
- approval expiration count
- loop failure with exception details

### Scheduler

`tradeclaw.runtime.scheduler.RuntimeScheduler.tick_once()` should create a child span for each running instance and log:

- instance execution start
- instance execution success
- instance execution failure and status change to `error`

### Worker

`tradeclaw.core.worker.TradingWorker.run_cycle()` should create a cycle span and emit logs at phase boundaries:

- cycle start with `run_id`
- market/account/universe refresh summaries
- proposal/review/intent counts
- approval decisions
- order submission result
- cycle summary counts

The existing trace store append operations remain unchanged and complementary.

### `qmt_proxy_sdk`

Transport-level spans and logs should cover:

- outbound HTTP requests in `qmt_proxy_sdk.http.AsyncHttpTransport.request`
- request failures and mapped API errors
- WebSocket subscription creation, reconnect attempts, and cleanup in `qmt_proxy_sdk.ws.QuoteStream`

This keeps local SDK diagnostics correlated with the Tradeclaw request or runtime tick that triggered them.

## Error Handling

- instrumentation initialization must be idempotent and defensive
- formatter logic must not crash when a record lacks OpenTelemetry metadata
- exception paths should use `logger.exception(...)` in key runtime paths so stack traces are preserved together with trace identifiers

## Testing Strategy

Tests should verify:

- observability initialization configures logging without duplicate handlers
- formatted log output includes `trace_id` and `span_id`
- worker and runtime loop emit logs while a span is active
- FastAPI requests create active trace context for request logs

The tests should focus on behavior visible from the public integration points rather than internal implementation details of OpenTelemetry.
