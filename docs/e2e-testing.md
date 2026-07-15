# End-to-End Testing

DoYouTrade E2E tests live under `tests/e2e`. They are skipped by normal
`make test` unless `DOYOUTRADE_E2E=1` is set.

## Goals

E2E tests validate behavior that unit tests can miss:

- `config.yaml` can bootstrap the real application runtime.
- Runtime tasks can execute through `TradingPlatformService`, `TradingWorker`, strategy, review, risk, execution, persistence, and observability.
- `run_id` stays connected across `cycle_runs`, `debug_sessions`, `debug_session_spans`, and `model_invocations`.
- Debug sessions still run through the real cycle path, and `running` tasks still reject debug runs.
- Amount-like payloads continue through the normal JSON/persistence/debug path instead of bypassing Decimal/string conventions.

## Configuration Loading

E2E config is layered:

1. Repo-root `config.yaml`
2. Optional E2E overlay from `DOYOUTRADE_E2E_CONFIG`, or `tests/e2e/config.yaml` when present
3. Built-in profile overrides selected by `DOYOUTRADE_E2E_PROFILE`

The overlay is a deep merge. You can override only the keys needed for a test
run, such as `database.url`, `data.qmt`, or `observability.tracing_enabled`.
The top-level `e2e` section is consumed by `tests/e2e/support.py` and is not
passed into production config parsing. It can define test-only model routes,
seed symbols, and task defaults such as `e2e.task.data_provider`.

Use `tests/e2e/config.yaml.example` as the template.

## Profiles

`local`

Reads root `config.yaml` plus the optional E2E overlay. This is closest to the
developer machine configuration.

`isolated`

Overrides to a temporary SQLite database, mock data provider, mock account, and
enabled tracing. Market-data storage points at a temporary SQLite file, so the
real market-data runtime (migrations, schema verification, repository) runs
end-to-end without requiring a local TimescaleDB service. This is the
recommended profile after AI-generated code changes because it is fast and
leaves no persistent state.

`live`

Reads root `config.yaml` plus overlay without safety overrides. Use it when you
intentionally want to exercise real QMT/model/database configuration.

## Commands

Fast isolated E2E:

```bash
make test-e2e
```

Equivalent explicit command:

```bash
DOYOUTRADE_E2E=1 DOYOUTRADE_E2E_PROFILE=isolated uv run python -m unittest discover -s tests/e2e -v
```

Run with local or live resources:

```bash
DOYOUTRADE_E2E=1 DOYOUTRADE_E2E_PROFILE=local uv run python -m unittest discover -s tests/e2e -v
DOYOUTRADE_E2E=1 DOYOUTRADE_E2E_PROFILE=live uv run python -m unittest discover -s tests/e2e -v
```

Use a custom overlay:

```bash
DOYOUTRADE_E2E=1 DOYOUTRADE_E2E_CONFIG=/path/to/e2e.yaml uv run python -m unittest discover -s tests/e2e -v
```

## Adding E2E Cases

Prefer adding helpers to `tests/e2e/support.py` instead of duplicating runtime
setup. New E2E cases should normally assert at least one persisted or exported
artifact, not just an in-memory return value.

When a change touches runtime, trace/debug, model invocation, persistence, API,
or money-related paths, include E2E assertions for the relevant cross-table
relationship:

- `cycle_runs.run_id`
- `debug_sessions.run_id`
- `debug_session_spans.trace_id` / `session_id`
- `model_invocations.run_id`
- `trade_fills.cycle_run_id`
- API/debug-view payload shape when the frontend consumes it

For AI-generated implementation work, run unit tests required by `AGENTS.md`
first, then run `make test-e2e` before claiming the runtime path is healthy.
