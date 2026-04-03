# OpenTelemetry Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a unified OpenTelemetry-backed observability layer so Tradeclaw and `qmt_proxy_sdk` emit consistent text logs with `trace_id` and `span_id`, and key API/runtime/worker operations run inside explicit spans.

**Architecture:** Introduce a small `tradeclaw.observability` package that owns tracing setup, logging setup, and optional FastAPI instrumentation. Keep Python `logging` as the application logging API, use `LoggingInstrumentor` for trace context injection, and add manual spans around runtime loop, scheduler, worker, and SDK transport boundaries.

**Tech Stack:** Python 3.12, unittest, FastAPI, OpenTelemetry API/SDK, OpenTelemetry FastAPI instrumentation, OpenTelemetry logging instrumentation

---

## File Structure

### Create

- `tradeclaw/observability/__init__.py` - public observability exports
- `tradeclaw/observability/logging.py` - root logger setup, formatter, logger access
- `tradeclaw/observability/tracing.py` - tracer provider setup and tracer access
- `tradeclaw/observability/init.py` - idempotent initialization entrypoint
- `tests/test_observability.py` - observability initialization and logging tests

### Modify

- `pyproject.toml` - add OpenTelemetry dependencies
- `tradeclaw/default_config.yaml` - add observability defaults
- `tradeclaw/config.py` - parse observability settings
- `tradeclaw/api/server.py` - initialize observability and instrument FastAPI
- `tradeclaw/api/app.py` - add request-level logs on key endpoints
- `tradeclaw/api/runtime_loop.py` - add runtime-loop spans and logs
- `tradeclaw/runtime/scheduler.py` - add per-instance spans and logs
- `tradeclaw/core/worker.py` - add cycle/phase spans and logs
- `qmt_proxy_sdk/http.py` - add transport spans and logs
- `qmt_proxy_sdk/ws.py` - replace ad hoc logger with shared facade-compatible usage and add span-aware logs
- `tests/test_runtime_loop.py` - extend runtime loop verification for logging/tracing behavior
- `tests/test_api_app.py` - extend API verification for request-context logging
- `tests/test_worker.py` - extend worker verification for trace-aware logging

### Verification

- `uv sync`
- `uv run python -m unittest tests.test_observability tests.test_worker tests.test_runtime_loop tests.test_api_app`

### Session Git Constraint

- Do not create a git commit unless the user explicitly requests one in this session.
- Use verification output as the checkpoint instead of committing.

## Task 1: Add Failing Observability Tests and Dependencies

**Files:**
- Modify: `pyproject.toml`
- Create: `tests/test_observability.py`
- Modify: `tests/test_worker.py`
- Modify: `tests/test_runtime_loop.py`
- Modify: `tests/test_api_app.py`

- [ ] **Step 1: Write failing tests for initialization and trace-aware log formatting**

Add `tests/test_observability.py` with tests that import the planned observability API and assert:

```python
import io
import logging
import unittest

from tradeclaw.observability import (
    get_logger,
    get_tracer,
    initialize_observability,
    reset_observability,
)


class ObservabilityTests(unittest.TestCase):
    def tearDown(self):
        reset_observability()

    def test_log_records_include_trace_and_span_ids_inside_span(self):
        stream = io.StringIO()
        initialize_observability(service_name="tradeclaw-test", stream=stream)
        logger = get_logger("tests.observability")
        tracer = get_tracer("tests.observability")

        with tracer.start_as_current_span("sample"):
            logger.info("hello")

        output = stream.getvalue()
        self.assertIn("trace_id=", output)
        self.assertIn("span_id=", output)
        self.assertNotIn("trace_id=-", output)
        self.assertNotIn("span_id=-", output)
```

- [ ] **Step 2: Run the focused test command to verify RED**

Run: `uv run python -m unittest tests.test_observability`

Expected: FAIL with `ModuleNotFoundError` or `ImportError` because `tradeclaw.observability` does not exist yet.

- [ ] **Step 3: Add missing OpenTelemetry dependencies**

Update `pyproject.toml` dependencies with:

```toml
"opentelemetry-api>=1.33.1",
"opentelemetry-sdk>=1.33.1",
"opentelemetry-instrumentation-logging>=0.54b1",
"opentelemetry-instrumentation-fastapi>=0.54b1",
```

- [ ] **Step 4: Re-run the focused test command**

Run: `uv sync && uv run python -m unittest tests.test_observability`

Expected: FAIL because the observability package is still missing, but dependency import errors should be gone.

## Task 2: Implement the Shared Observability Package

**Files:**
- Create: `tradeclaw/observability/__init__.py`
- Create: `tradeclaw/observability/logging.py`
- Create: `tradeclaw/observability/tracing.py`
- Create: `tradeclaw/observability/init.py`
- Test: `tests/test_observability.py`

- [ ] **Step 1: Write the minimal observability package to satisfy the failing tests**

Implement:

- `initialize_observability(service_name, log_level="INFO", stream=None, app=None, tracing_enabled=True, console_enabled=True)`
- `reset_observability()`
- `get_logger(name)`
- `get_tracer(name)`

The formatter should render at least:

```text
level=INFO logger=tests.observability trace_id=<value> span_id=<value> message=hello
```

- [ ] **Step 2: Run the focused tests to verify GREEN**

Run: `uv run python -m unittest tests.test_observability`

Expected: PASS.

- [ ] **Step 3: Refactor for idempotence and cleanup safety**

Ensure repeated initialization does not duplicate handlers and reset logic restores a clean global state for tests.

- [ ] **Step 4: Re-run the focused tests**

Run: `uv run python -m unittest tests.test_observability`

Expected: PASS.

## Task 3: Wire Config and API Startup Into Observability

**Files:**
- Modify: `tradeclaw/default_config.yaml`
- Modify: `tradeclaw/config.py`
- Modify: `tradeclaw/api/server.py`
- Modify: `tradeclaw/api/app.py`
- Modify: `tests/test_api_app.py`

- [ ] **Step 1: Write a failing API test for request-context logging**

Extend `tests/test_api_app.py` so a request logs through the shared logger and the captured output contains non-placeholder `trace_id` and `span_id`.

- [ ] **Step 2: Run the focused API test to verify RED**

Run: `uv run python -m unittest tests.test_api_app`

Expected: FAIL because the app does not initialize or use observability yet.

- [ ] **Step 3: Implement config parsing and API startup initialization**

Add the `observability` config model and defaults, initialize observability in `build_api_with_runtime()`, and instrument the FastAPI app from the shared initializer.

- [ ] **Step 4: Re-run the focused API test**

Run: `uv run python -m unittest tests.test_api_app`

Expected: PASS.

## Task 4: Instrument Runtime Loop, Scheduler, and Worker

**Files:**
- Modify: `tradeclaw/api/runtime_loop.py`
- Modify: `tradeclaw/runtime/scheduler.py`
- Modify: `tradeclaw/core/worker.py`
- Modify: `tests/test_runtime_loop.py`
- Modify: `tests/test_worker.py`

- [ ] **Step 1: Write failing tests for runtime and worker logs inside spans**

Extend the tests to assert that:

- runtime loop emits a log when a tick succeeds or fails
- worker cycle emits logs for start and summary
- captured output contains `trace_id=` and `span_id=`

- [ ] **Step 2: Run the focused runtime and worker tests to verify RED**

Run: `uv run python -m unittest tests.test_worker tests.test_runtime_loop`

Expected: FAIL because those modules do not emit the required logs yet.

- [ ] **Step 3: Add spans and logs to runtime loop, scheduler, and worker**

Use the shared tracer and logger to create:

- one runtime tick span per loop iteration
- one instance execution span per running instance
- one worker cycle span plus phase-level span events or child spans

- [ ] **Step 4: Re-run the focused runtime and worker tests**

Run: `uv run python -m unittest tests.test_worker tests.test_runtime_loop`

Expected: PASS.

## Task 5: Instrument `qmt_proxy_sdk` Transport Boundaries

**Files:**
- Modify: `qmt_proxy_sdk/http.py`
- Modify: `qmt_proxy_sdk/ws.py`
- Test: `uv run python -m unittest tests.test_qmt_proxy_client tests.test_qmt_proxy_adapters`

- [ ] **Step 1: Add a failing expectation if existing SDK tests already cover request or stream flows**

If current tests exercise the transport paths, extend them to assert that instrumentation does not break behavior. If they do not, use the existing test suite unchanged as the regression gate.

- [ ] **Step 2: Run the SDK-focused tests to establish baseline**

Run: `uv run python -m unittest tests.test_qmt_proxy_client tests.test_qmt_proxy_adapters`

Expected: PASS before instrumentation changes.

- [ ] **Step 3: Add transport spans and logs**

Instrument HTTP request execution and WebSocket subscription lifecycle with the shared tracer and logger while preserving existing error mapping.

- [ ] **Step 4: Re-run the SDK-focused tests**

Run: `uv run python -m unittest tests.test_qmt_proxy_client tests.test_qmt_proxy_adapters`

Expected: PASS.

## Task 6: Full Focused Verification

**Files:**
- Verify only

- [ ] **Step 1: Run the complete focused verification command**

Run: `uv run python -m unittest tests.test_observability tests.test_worker tests.test_runtime_loop tests.test_api_app tests.test_qmt_proxy_client tests.test_qmt_proxy_adapters`

Expected: PASS with all targeted observability-related regressions covered.

- [ ] **Step 2: Review the diff**

Run: `git diff -- pyproject.toml tradeclaw qmt_proxy_sdk tests docs/superpowers`

Expected: diff only shows the observability package, config wiring, instrumentation, tests, and the design/plan docs for this change.

## Self-Review

- Spec coverage: configuration, initialization, FastAPI, runtime, worker, SDK transport, and tests are all mapped to tasks above.
- Placeholder scan: all tasks name exact files and commands; no TODO markers remain.
- Type consistency: the plan uses a single public observability API centered on `initialize_observability`, `reset_observability`, `get_logger`, and `get_tracer`.
