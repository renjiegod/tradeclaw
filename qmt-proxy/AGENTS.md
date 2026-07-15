# QMT Proxy Agent Notes

## Core Workflow

- Prefer `make` targets and `scripts/make.ps1` over ad-hoc shell commands for backend and web workflows.
- When adding or changing developer commands, keep Windows PowerShell as the primary execution environment.
- After changing `Makefile`, `scripts/make.ps1`, or the web toolchain, verify the real command path instead of relying only on unit tests.

## Windows Shared-Folder Constraints

- This repo is often used from a Windows VM against a shared-folder path such as `Z:\code\qmt-proxy`.
- Do not assume `npm run` from a UNC/shared-folder path is reliable. Prefer the PowerShell wrapper and direct Node CLI entrypoints when needed.
- Treat `web/node_modules/.bin` as fragile on shared folders. Prefer installs that avoid bin-link dependence when possible.
- If Windows reports `EINVAL`, `lstat`, `stat`, or access-denied errors inside `web/node_modules/.bin`, treat it as corrupted install state before changing app code.
- If Windows cannot delete `web/node_modules`, remove it from the host filesystem first, then reinstall from Windows.

## Frontend Toolchain Policy

- Keep the default web toolchain on versions that are known to work on Windows ARM64 in this repo.
- Do not upgrade `vite`, `vitest`, or `@vitejs/plugin-react` across major versions without verifying Windows ARM64 compatibility first.
- In particular, avoid toolchain versions that force `rolldown` native bindings unless they have been explicitly validated in this environment.
- If intentionally changing the supported toolchain major version, update `tests/unit/test_web_toolchain_versions.py` in the same change.

## Frontend Dependency Recovery

- Use `make ui-install` for frontend installs instead of running custom install commands.
- If frontend startup fails with messages like `Cannot find native binding`, `ERR_DLOPEN_FAILED`, or `not a valid Win32 application`, check dependency/runtime compatibility before changing application code.
- If dependency repair is needed, prefer reinstalling `web/node_modules` cleanly rather than patching individual generated files.

## Required Verification

- After changing `Makefile` or `scripts/make.ps1`, run:
  - `powershell -NoProfile -ExecutionPolicy Bypass -File "tests/unit/test_make_start_args.ps1"`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File "tests/unit/test_make_helpers.ps1"`
- After changing frontend toolchain versions, run:
  - `python -m pytest tests/unit/test_web_toolchain_versions.py`
  - `make ui-install`
- After changing the combined dev workflow, run `make dev` and confirm both frontend and backend start successfully.

## Multi-QMT-Terminal (Multi-Client) Constraints

- Trading is per-terminal: route through `app/services/trading_manager.py` `TradingClientManager`, which holds one `TradingService` per `client_id`. Do NOT collapse it back to a single shared `XtQuantTrader` — multiple brokers/terminals must trade in isolation. Keep `TradingService` itself single-terminal; the manager is the only multiplexing layer.
- Market data is single-source by design: `xtdata` is a process-global singleton (one data connection per process), and quotes are broker-agnostic, so only the resolved data-source terminal (`data_source_client_id` > `is_data_source` flag > default) is used. Do not introduce per-terminal `xtdata` connections.
- Terminal selection is via the `X-QMT-Terminal` header (REST) / `x-qmt-terminal` metadata (gRPC), resolved by `app/config.py` `XTQuantConfig` and `app/dependencies.py::get_client_id`. Unknown `client_id` must fail with HTTP 400 + `error_code == "UNKNOWN_TERMINAL"` — never silently fall back to the default terminal.
- Backward compatibility: when `xtquant.clients` is empty, the config synthesizes a single `client_id="default"` terminal from `qmt_userdata_path` + global mode. Keep this fallback intact so existing single-terminal deploys and callers without the header are unaffected.
- When adding any per-client (per-terminal) dimension, keep it consistent across three places: tag diagnostics records/aggregates with `client_id` (`/api/v1/diagnostics/xtdata-ops`, `/api/v1/diagnostics/summary`), and surface the terminal's state in `GET /api/v1/trading/clients` (`loaded` / `initialized` / `init_failure_reason` / `is_default` / `is_data_source`). The SDK (`libs/qmt_proxy_sdk` `AsyncQmtProxyClient(terminal_id=...)`) must keep auto-injecting the `X-QMT-Terminal` header.

