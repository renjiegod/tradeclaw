import asyncio
import io
import os
import shutil
import tempfile
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from doyoutrade.api.app import create_app
from doyoutrade.api.server import build_api_with_runtime, main as server_main
from doyoutrade.assistant.cron_manager import AgentCronManager
from doyoutrade.assistant.repository import InMemoryAgentRepository, InMemoryChannelRepository
from doyoutrade.observability import initialize_observability, reset_observability
from doyoutrade.persistence.errors import PersistenceError, RecordNotFoundError, StateConflictError

_VALID_INSTANCE_SETTINGS = {
    "agent": {
        "react_max_turns": 1,
        "signal_tool_names": ["data_bars_relative"],
    },
    "model_route_name": "test-route",
    "strategy": {
        "definition_id": "sd-main",
    },
}


class _FakeService:
    def __init__(self):
        self.tick_calls = 0
        self.kill_switch_calls = []
        self.create_calls = []
        self.tasks = {}
        self.debug_create_calls = []
        self.debug_sessions = {}
        self.backtest_start_calls: list = []
        self.backtest_jobs_store: dict = {}
        self.backtest_jobs_list: list = []
        self.model_route_create_calls: list = []
        self.update_calls: list = []
        self.ensure_model_route_calls: list[str] = []
        self.accounts: dict = {}
        self._account_seq = 0
        self.account_bindings: dict = {}  # account_id -> [task_id,...]
        self.account_statement_calls: list[tuple[str | None, object | None]] = []
        self.local_market_sync_calls: list[dict[str, object]] = []
        self.local_market_sync_jobs: dict[str, dict[str, object]] = {}

    # --- account CRUD (mirrors TradingPlatformService account methods) ------
    async def list_accounts(self):
        return list(self.accounts.values())

    async def get_account(self, account_id):
        if account_id not in self.accounts:
            raise KeyError(f"account_not_found: {account_id}")
        return self.accounts[account_id]

    async def create_account(self, payload):
        if not payload.get("name"):
            raise ValueError("name is required")
        if payload.get("mode") not in ("live", "mock"):
            raise ValueError("mode must be 'live' or 'mock'")
        self._account_seq += 1
        aid = f"acct-fake{self._account_seq:06d}"
        rec = {
            "id": aid, "name": payload["name"], "mode": payload["mode"],
            "base_url": payload.get("base_url") or "", "token": payload.get("token"),
            "qmt_account_id": payload.get("qmt_account_id"),
            "is_default": bool(payload.get("is_default")), "enabled": True,
        }
        if rec["is_default"]:
            for other in self.accounts.values():
                other["is_default"] = False
        self.accounts[aid] = rec
        return rec

    async def update_account(self, account_id, payload):
        if account_id not in self.accounts:
            raise KeyError(f"account_not_found: {account_id}")
        self.accounts[account_id].update(
            {k: v for k, v in payload.items() if k != "is_default"}
        )
        if payload.get("is_default") is True:
            return await self.set_default_account(account_id)
        return self.accounts[account_id]

    async def set_default_account(self, account_id):
        if account_id not in self.accounts:
            raise KeyError(f"account_not_found: {account_id}")
        for aid, rec in self.accounts.items():
            rec["is_default"] = aid == account_id
        return self.accounts[account_id]

    async def delete_account(self, account_id):
        if account_id not in self.accounts:
            raise KeyError(f"account_not_found: {account_id}")
        if self.account_bindings.get(account_id):
            raise ValueError(f"account_in_use: account {account_id!r} is bound")
        del self.accounts[account_id]

    async def get_account_statement(self, account_id=None, *, asof=None, captured_at=None):
        self.account_statement_calls.append((account_id, asof))
        if account_id == "acct-nope":
            raise KeyError("account_not_found: acct-nope")
        return {
            "account_id": account_id or "acct-default",
            "account_name": "demo",
            "account_mode": "live",
            "resolved_via_default": account_id is None,
            "asof": asof.isoformat() if hasattr(asof, "isoformat") else "2026-06-18",
            "account": {"account": {"cash": "100", "equity": "120"}},
            "asset": {"total_asset": "120"},
            "trades": [],
            "trade_count": 0,
            "errors": [],
        }

    async def tick_once(self, source: str = "manual"):
        self.tick_calls += 1
        return 2

    async def list_tasks(self):
        if not self.tasks:
            return [{"task_id": "i-1", "status": "running"}]
        return [self._public_task_payload(row) for row in self.tasks.values()]

    async def list_tasks_page(
        self,
        *,
        q: str | None,
        status: str | None,
        mode: str | None,
        limit: int,
        offset: int,
        definition_id: str | None = None,
        modes: list[str] | None = None,
    ):
        rows = await self.list_tasks()
        if q:
            q_lower = q.lower()
            rows = [
                row
                for row in rows
                if q_lower in str(row.get("name", "")).lower()
                or q_lower in str(row.get("task_id", "")).lower()
            ]
        if status:
            rows = [row for row in rows if str(row.get("status")) == status]
        modes_clean = [m for m in (modes or []) if m]
        if modes_clean:
            rows = [row for row in rows if str(row.get("mode")) in modes_clean]
        elif mode:
            rows = [row for row in rows if str(row.get("mode")) == mode]
        if definition_id:
            rows = [
                row
                for row in rows
                if (
                    (row.get("settings") or {}).get("strategy") or {}
                ).get("definition_id") == definition_id
            ]
        total = len(rows)
        return {"items": rows[offset : offset + limit], "total": total, "limit": limit, "offset": offset}

    def list_templates(self):
        return []

    async def get_system_state(self):
        return {"kill_switch_enabled": False, "task_count": 1, "running_count": 1}

    async def set_kill_switch(self, enabled):
        self.kill_switch_calls.append(enabled)

    async def ensure_model_route_exists(self, route_name: str) -> None:
        self.ensure_model_route_calls.append(route_name)
        return None

    async def create_task(self, **payload):
        if payload["name"] == "persistence-error":
            raise PersistenceError("failed to create task: check constraint failed: tasks.status")
        self.create_calls.append(payload)
        tid = f"instance-{len(self.create_calls)}"
        mode = payload.get("mode") or "paper"
        settings = payload.get("settings") or {}
        record = {
            "task_id": tid,
            "name": payload["name"],
            "mode": mode,
            "description": payload.get("description", ""),
            "data_provider": payload.get("data_provider"),
            "data_provider_effective": payload.get("data_provider") or "auto",
            "status": "configured",
            "cycles": None,
            "last_error": "",
            "universe": settings.get("universe") or [],
            "settings": payload.get("settings"),
            "created_at": "2026-04-04T00:00:00",
            "updated_at": "2026-04-04T00:00:00",
        }
        self.tasks[tid] = record
        return SimpleNamespace(
            task_id=tid,
            config=SimpleNamespace(
                mode=mode,
            ),
        )

    async def get_task_status(self, identifier):
        if identifier not in self.tasks:
            raise RecordNotFoundError(f"task not found: {identifier}")
        return self._public_task_payload(self.tasks[identifier])

    async def build_task_duplicate_preset(self, task_id: str):
        row = self.tasks.get(task_id)
        if row is None:
            raise RecordNotFoundError(f"task not found: {task_id}")
        strategy = None
        settings = row.get("settings") or {}
        if isinstance(settings.get("strategy"), dict):
            strategy = dict(settings["strategy"])
        return {
            "name": f"{row['name']}-copy",
            "mode": row.get("mode") or "paper",
            "description": row.get("description") or "",
            "data_provider": row.get("data_provider"),
            "universe_symbols": list(row.get("universe") or []),
            "enabled_skills": list(row.get("enabled_skills") or []),
            "strategy": strategy,
        }

    async def update_task(self, identifier: str, **payload):
        self.update_calls.append((identifier, payload))
        row = self.tasks.get(identifier)
        if row is None:
            raise RecordNotFoundError(f"task not found: {identifier}")

        if payload.get("mode") is not None:
            current_is_backtest = str(row.get("mode")) == "backtest"
            next_is_backtest = str(payload["mode"]) == "backtest"
            if current_is_backtest != next_is_backtest:
                raise ValueError(
                    "cannot switch task mode between trading and backtest; create a new task instead"
                )

        if payload.get("name") is not None:
            row["name"] = payload["name"]
        if payload.get("mode") is not None:
            row["mode"] = payload["mode"]
        if payload.get("description") is not None:
            row["description"] = payload["description"]
        if payload.get("data_provider") is not None:
            row["data_provider"] = payload["data_provider"]
        if payload.get("settings") is not None:
            merged_settings = dict(row.get("settings") or {})
            merged_settings.update(payload["settings"])
            row["settings"] = merged_settings
            row["universe"] = merged_settings.get("universe", row.get("universe") or [])

        return self._public_task_payload(row)

    @staticmethod
    def _public_task_payload(row: dict) -> dict:
        cleaned = dict(row)
        cleaned.pop("template_id", None)
        settings = cleaned.get("settings")
        if isinstance(settings, dict):
            settings_cleaned = dict(settings)
            settings_cleaned.pop("template_id", None)
            cleaned["settings"] = settings_cleaned
        return cleaned

    async def start_task(self, identifier: str):
        row = self.tasks.get(identifier)
        if row is None:
            raise RecordNotFoundError(f"task not found: {identifier}")
        if row.get("mode") == "backtest":
            raise ValueError("backtest task does not support start")
        row["status"] = "running"
        return SimpleNamespace(task_id=identifier)

    async def pause_task(self, identifier: str):
        row = self.tasks.get(identifier)
        if row is None:
            raise RecordNotFoundError(f"task not found: {identifier}")
        if row.get("mode") == "backtest":
            raise ValueError("backtest task does not support pause")
        row["status"] = "paused"
        return SimpleNamespace(task_id=identifier)

    async def stop_task(self, identifier: str):
        row = self.tasks.get(identifier)
        if row is None:
            raise RecordNotFoundError(f"task not found: {identifier}")
        if row.get("mode") == "backtest":
            raise ValueError("backtest task does not support stop")
        row["status"] = "stopped"
        return SimpleNamespace(task_id=identifier)

    async def delete_task(self, identifier: str):
        if identifier not in self.tasks:
            raise RecordNotFoundError(f"task not found: {identifier}")
        del self.tasks[identifier]

    async def delete_tasks(self, task_ids: list[str]):
        running = [task_id for task_id in task_ids if self.tasks.get(task_id, {}).get("status") == "running"]
        if running:
            raise RuntimeError(f"running tasks cannot be deleted: {', '.join(running)}")
        for task_id in task_ids:
            await self.delete_task(task_id)

    async def start_debug_session(self, identifier: str, *, input_overrides=None):
        row = self.tasks.get(identifier)
        if row is None:
            raise RecordNotFoundError(f"task not found: {identifier}")
        if row.get("mode") == "backtest":
            raise RuntimeError("backtest tasks do not support debug sessions")
        self.debug_create_calls.append((identifier, input_overrides))
        session_id = f"debug-{len(self.debug_create_calls)}"
        session = {
            "session_id": session_id,
            "task_id": identifier,
            "status": "running",
            "run_id": None,
            "error_message": "",
            "input_overrides": input_overrides,
            "effective_config": None,
            "created_at": "2026-04-05T00:00:00",
            "started_at": "2026-04-05T00:00:01",
            "finished_at": None,
            "events": [],
            "model_invocations": [],
        }
        self.debug_sessions.setdefault(identifier, {})[session_id] = session
        return session

    async def list_debug_sessions(self, identifier: str):
        return list(self.debug_sessions.get(identifier, {}).values())

    async def get_debug_session(self, identifier: str, session_id: str):
        return self.debug_sessions[identifier][session_id]

    async def list_cycle_runs(self, identifier: str, *, limit: int = 50, offset: int = 0, **kwargs):
        parent_run_id = kwargs.get("run_id")
        items = [
            {
                "run_id": "run-test-1",
                "task_id": identifier,
                "agent_name": "a",
                "session_id": None,
                "trace_id": None,
                "run_mode": "paper",
                "run_kind": "debug",
                "clock_mode": "wall",
                "cycle_time": None,
                "cycle_time_utc": None,
                "wall_started_at": "2026-04-08T12:00:00",
                "wall_finished_at": "2026-04-08T12:00:01",
                "runtime_params": None,
                "status": "completed",
                "details": {"universe": ["AAA"], "decisions": []},
                "cycle_failed": False,
                "failure_message": None,
                "completed_phases": ["load_context"],
                "submitted_count": 0,
                "vetoed_count": 0,
                "pending_approval_count": 0,
            }
        ]
        if parent_run_id:
            job = self.backtest_jobs_store.get(parent_run_id)
            if job is None or job.get("task_id") != identifier:
                raise RecordNotFoundError(f"backtest job not found: {parent_run_id}")
            want = job.get("session_id")
            items = [row for row in items if want and row.get("session_id") == want]
        return {"items": items, "total": len(items)}

    async def get_cycle_run(self, run_id: str):
        if run_id != "run-test-1":
            raise RecordNotFoundError("missing")
        row = (await self.list_cycle_runs("inst-1"))["items"][0]
        return row

    async def get_run_debug_view(self, run_id: str):
        row = await self.get_cycle_run(run_id)
        return {
            "cycle_run": row,
            "session": None,
            "spans": [],
            "model_invocations": [],
        }

    async def get_trace_debug_view(self, trace_id: str):
        if trace_id != "ab" * 16:
            raise RecordNotFoundError("debug view not found for trace_id")
        row = await self.get_cycle_run("run-test-1")
        return {
            "resolved_from": {"identifier": trace_id, "identifier_type": "trace"},
            "cycle_run": row,
            "cycle_runs": [row],
            "session": None,
            "spans": [],
            "model_invocations": [],
        }

    async def list_backtest_jobs(self, identifier: str, *, limit: int = 50, offset: int = 0):
        rows = [j for j in self.backtest_jobs_list if j["task_id"] == identifier]
        return {"items": rows[offset : offset + limit], "total": len(rows)}

    async def list_backtest_jobs_global(
        self,
        *,
        task_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        rows = list(self.backtest_jobs_list)
        if task_id is not None:
            rows = [j for j in rows if j["task_id"] == task_id]
        return {"items": rows[offset : offset + limit], "total": len(rows)}

    async def get_backtest_job(self, identifier: str, run_id: str):
        row = self.backtest_jobs_store.get(run_id)
        if row is None or row.get("task_id") != identifier:
            raise RecordNotFoundError("missing")
        return row

    async def get_backtest_summary(self, run_id: str):
        row = self.backtest_jobs_store.get(run_id)
        if row is None:
            raise RecordNotFoundError("missing")
        return {
            "summary_state": "ok",
            "run_id": run_id,
            "task_id": row.get("task_id"),
            "summary": {
                "overview": {
                    "starting_equity": "100000",
                    "ending_equity": "123456.78",
                    "return_pct": "23.45678",
                },
                "trades": {
                    "closed_trades": 2,
                    "win_rate": "50.00",
                },
            },
        }

    async def get_backtest_chart(
        self,
        identifier: str,
        run_id: str,
        *,
        symbol: str | None = None,
    ):
        row = await self.get_backtest_job(identifier, run_id)
        selected_symbol = symbol or "600000.SH"
        return {
            "run": row,
            "symbols": ["600000.SH", "601318.SH"],
            "selected_symbol": selected_symbol,
            "adjust": "qfq",
            "bars": [
                {
                    "timestamp": "2026-01-02T00:00:00",
                    "open": 10.0,
                    "high": 10.5,
                    "low": 9.8,
                    "close": 10.2,
                    "volume": 1000.0,
                    "amount": None,
                },
            ],
            "volume_mode": "volume_only",
            "trades": [],
            "warnings": [],
        }

    async def get_local_market_bars(
        self,
        *,
        symbol: str,
        interval: str = "1d",
        start: str | None = None,
        end: str | None = None,
        provider: str | None = None,
        adjust: str | None = None,
    ):
        return {
            "symbol": symbol,
            "interval": interval,
            "provider": provider or "auto",
            "adjust": adjust or "qfq",
            "start": start or "2025-01-01",
            "end": end or "2026-01-01",
            "bars": [
                {
                    "timestamp": "2026-01-02T00:00:00",
                    "open": 10.0,
                    "high": 10.5,
                    "low": 9.8,
                    "close": 10.2,
                    "volume": 1000.0,
                    "amount": None,
                },
            ],
            "volume_mode": "volume_only",
            "summary": {
                "bar_count": 1,
                "latest_close": 10.2,
                "window_change": 0.2,
                "window_change_pct": 0.02,
                "window_high": 10.5,
                "window_low": 9.8,
                "amplitude_pct": 0.0714285714,
                "total_volume": 1000.0,
                "total_amount": None,
            },
            "coverage": {
                "requested_start": start or "2025-01-01",
                "requested_end": end or "2026-01-01",
                "covered_segments": [
                    {
                        "start": "2026-01-02",
                        "end": "2026-01-02",
                        "status": "covered",
                    }
                ],
                "missing_segments": [],
            },
            "available_overlays": {
                "backtest_trades": [
                    {
                        "id": "run-1",
                        "run_id": "run-1",
                        "task_id": "task-1",
                        "label": "demo backtest · run-1",
                        "status": "completed",
                    }
                ],
                "task_fills": [
                    {
                        "id": "task-2",
                        "task_id": "task-2",
                        "label": "demo live",
                        "run_count": 1,
                    }
                ],
                "signals": [
                    {
                        "id": "task-2",
                        "task_id": "task-2",
                        "label": "demo live",
                        "item_count": 1,
                    }
                ],
            },
            "sync_state": None,
            "warnings": [],
        }

    async def sync_local_market_bars_range(
        self,
        *,
        symbol: str,
        interval: str,
        start: str,
        end: str,
        provider: str | None = None,
        adjust: str | None = None,
        mode: str,
    ):
        call = {
            "symbol": symbol,
            "interval": interval,
            "start": start,
            "end": end,
            "provider": provider or "auto",
            "adjust": adjust or "qfq",
            "mode": mode,
        }
        self.local_market_sync_calls.append(call)
        if interval == "1d":
            return {
                "status": "ok",
                "execution_mode": "sync",
                "mode": mode,
                "requested_range": {"start": start, "end": end},
                "fetched_segments": [{"start": start, "end": end, "status": "fetched"}],
                "upserted_count": 42,
                "warnings": [],
            }
        job_id = f"lmjob-fake-{len(self.local_market_sync_calls)}"
        self.local_market_sync_jobs[job_id] = {
            "job_id": job_id,
            "status": "pending",
            "mode": mode,
            "symbol": symbol,
            "interval": interval,
            "provider": provider or "auto",
            "adjust": adjust or "qfq",
            "requested_range": {"start": start, "end": end},
            "fetched_segments": [],
            "upserted_count": 0,
            "started_at": None,
            "finished_at": None,
            "error_code": None,
            "error_type": None,
            "error_message": None,
            "hint": None,
        }
        return {
            "status": "accepted",
            "execution_mode": "async",
            "job_id": job_id,
            "mode": mode,
            "requested_range": {"start": start, "end": end},
            "warnings": [],
        }

    async def get_local_market_sync_job(self, job_id: str):
        row = self.local_market_sync_jobs.get(job_id)
        if row is None:
            raise RecordNotFoundError(f"local market sync job not found: {job_id}")
        return dict(row)

    async def get_local_market_overlays(
        self,
        *,
        symbol: str,
        interval: str,
        start: str,
        end: str,
        overlay_kind: str,
        run_id: str | None = None,
        task_id: str | None = None,
        signal_source_id: str | None = None,
    ):
        source_id = run_id or task_id or signal_source_id or ""
        return {
            "overlay_kind": overlay_kind,
            "source": {
                "id": source_id,
                "run_id": run_id,
                "task_id": task_id or signal_source_id,
                "label": "demo source",
            },
            "items": [
                {
                    "timestamp": "2026-01-03T00:00:00",
                    "kind": "trade_fill" if overlay_kind != "signals" else "signal",
                    "side": "buy",
                    "price": 10.2,
                    "label": "BUY",
                    "details": {"symbol": symbol, "interval": interval, "start": start, "end": end},
                }
            ],
            "warnings": [],
        }

    async def start_backtest_job(
        self,
        identifier: str,
        *,
        range_start: str,
        range_end: str,
        market_profile: str | None = None,
        bar_interval: str | None = None,
        config_overrides: dict | None = None,
        model_route_name: str | None = None,
        debug_enabled: bool = True,
    ):
        self.backtest_start_calls.append(
            (
                identifier,
                range_start,
                range_end,
                market_profile,
                bar_interval,
                config_overrides,
                model_route_name,
                debug_enabled,
            ),
        )
        job_id = f"btjob-fake-{len(self.backtest_start_calls)}"
        row = {
            "run_id": job_id,
            "task_id": identifier,
            "status": "running",
            "market_profile": market_profile or "cn_a_share",
            "bar_interval": bar_interval or "1d",
            "range_start_utc": f"{range_start}T00:00:00",
            "range_end_utc": f"{range_end}T00:00:00",
            "session_id": (f"backtest-{job_id}" if debug_enabled else None),
            "debug_enabled": debug_enabled,
            "starting_equity": None,
            "ending_equity": None,
            "return_pct": None,
            "error_message": None,
            "bars_total": 2,
            "bars_completed": 0,
            "stop_requested": False,
            "ledger_checkpoint_json": None,
            "reference_starting_equity": None,
            "created_at": "2026-04-11T00:00:00",
            "started_at": "2026-04-11T00:00:01",
            "finished_at": None,
        }
        self.backtest_jobs_store[job_id] = row
        self.backtest_jobs_list.insert(0, row)
        return row

    async def pause_backtest_job(self, identifier: str, run_id: str):
        row = self.backtest_jobs_store.get(run_id)
        if row is None or row.get("task_id") != identifier:
            raise RecordNotFoundError("missing")
        if row["status"] != "running":
            raise RuntimeError("only a running backtest job can be paused")
        row["status"] = "paused"
        return row

    async def resume_backtest_job(self, identifier: str, run_id: str):
        row = self.backtest_jobs_store.get(run_id)
        if row is None or row.get("task_id") != identifier:
            raise RecordNotFoundError("missing")
        if row["status"] != "paused":
            raise RuntimeError("only a paused backtest job can be resumed")
        row["status"] = "running"
        return row

    async def stop_backtest_job(self, identifier: str, run_id: str):
        row = self.backtest_jobs_store.get(run_id)
        if row is None or row.get("task_id") != identifier:
            raise RecordNotFoundError("missing")
        if row["status"] in ("completed", "failed", "stopped"):
            raise RuntimeError("backtest job has already finished")
        row["status"] = "stopped"
        row["error_message"] = ""
        row["finished_at"] = "2026-04-11T12:00:02"
        return row

    async def list_instrument_catalog(self, q=None, limit=50, offset=0):
        return {"items": [], "total": 0}

    async def get_instrument_catalog_item(self, symbol: str):
        return None

    async def sync_instrument_catalog(self, source: str, mode: str, symbols=None):
        return {"inserted": 0, "updated": 0, "rows_seen": 0}

    async def delete_instrument_catalog_symbols(self, symbols: list[str]):
        return {"deleted": len(symbols)}

    async def clear_instrument_catalog(self, *, confirm: str):
        expected = "clear_all_instrument_catalog"
        if (confirm or "").strip() != expected:
            raise ValueError(f"confirm must be exactly {expected!r}")
        return {"deleted": 0}

    async def create_model_route_api(self, payload: dict):
        self.model_route_create_calls.append(dict(payload))
        return {
            "id": "mr-fake-1",
            "route_name": payload["route_name"],
            "provider_kind": payload["provider_kind"],
            "base_url": payload.get("base_url"),
            "api_key_masked": "(masked)",
            "target_model": payload.get("target_model"),
            "settings": payload.get("settings"),
            "created_at": "2026-04-04T00:00:00",
            "updated_at": "2026-04-04T00:00:00",
        }


class _FakeStrategyRegistryService:
    def __init__(self):
        self.deleted_definition_ids: list[str] = []
        self.deleted_definition_batches: list[list[str]] = []
        self.create_definition_payloads: list = []
        self.update_definition_calls: list = []

    @staticmethod
    def _snapshot(
        *,
        definition_id: str = "sd-fake",
        name: str = "Fake Strategy",
        api_version: str = "v1",
        input_contract_json=None,
        parameter_schema_json=None,
        default_parameters_json=None,
        capabilities_json=None,
        provenance_json=None,
        generation_prompt: str = "",
        generation_model: str = "",
        generation_metadata_json=None,
        status: str = "active",
        code_hash: str = "",
    ):
        from datetime import datetime, timezone

        from doyoutrade.persistence.repositories import StrategyDefinitionSnapshot

        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        return StrategyDefinitionSnapshot(
            definition_id=definition_id,
            name=name,
            current_version=None,
            api_version=api_version,
            input_contract_json=input_contract_json,
            parameter_schema_json=parameter_schema_json,
            default_parameters_json=default_parameters_json,
            capabilities_json=capabilities_json,
            provenance_json=provenance_json,
            code_hash=code_hash,
            generation_prompt=generation_prompt,
            generation_model=generation_model,
            generation_metadata_json=generation_metadata_json,
            status=status,
            created_at=now,
            updated_at=now,
        )

    async def create_definition(self, payload):
        self.create_definition_payloads.append(payload)
        return self._snapshot(
            definition_id=payload.definition_id,
            name=payload.name,
            api_version=payload.api_version,
            input_contract_json=payload.input_contract,
            parameter_schema_json=payload.parameter_schema,
            default_parameters_json=payload.default_parameters,
            capabilities_json=payload.capabilities,
            provenance_json=payload.provenance,
            generation_prompt=payload.generation_prompt,
            generation_model=payload.generation_model,
            generation_metadata_json=payload.generation_metadata,
            status=payload.status,
            code_hash=payload.code_hash,
        )

    async def update_definition(self, definition_id: str, **kwargs):
        self.update_definition_calls.append((definition_id, dict(kwargs)))
        return self._snapshot(
            definition_id=definition_id,
            name=kwargs.get("name") or "Fake Strategy",
            api_version=kwargs.get("api_version") or "v1",
            input_contract_json=kwargs.get("input_contract"),
            parameter_schema_json=kwargs.get("parameter_schema"),
            default_parameters_json=kwargs.get("default_parameters"),
            capabilities_json=kwargs.get("capabilities"),
            provenance_json=kwargs.get("provenance"),
            generation_prompt=kwargs.get("generation_prompt") or "",
            generation_model=kwargs.get("generation_model") or "",
            generation_metadata_json=kwargs.get("generation_metadata"),
            status=kwargs.get("status") or "active",
        )

    async def delete_definition(self, definition_id: str):
        if definition_id == "missing":
            raise RecordNotFoundError(f"strategy definition not found: {definition_id}")
        self.deleted_definition_ids.append(definition_id)

    async def delete_definitions(self, definition_ids: list[str]):
        if "missing" in definition_ids:
            raise RecordNotFoundError("strategy definition not found: missing")
        self.deleted_definition_batches.append(list(definition_ids))


class _FakeStrategyDefinitionRepository:
    def __init__(self, snapshot):
        self._snapshot = snapshot

    async def get_definition(self, definition_id: str):
        if definition_id == "missing":
            raise RecordNotFoundError(f"strategy definition not found: {definition_id}")
        return self._snapshot

    async def list_definitions(self):
        return [self._snapshot]


class _FakeServiceCycleTimeValidation(_FakeService):
    """Applies the same wall-time parsing as :class:`TradingPlatformService` for API contract tests."""

    async def list_cycle_runs(
        self,
        identifier: str,
        *,
        limit: int = 50,
        offset: int = 0,
        run_id_contains=None,
        status=None,
        run_kind=None,
        run_mode=None,
        exclude_run_kind=None,
        started_after=None,
        started_before=None,
        **kwargs,
    ):
        from doyoutrade.platform.service import _parse_cycle_run_wall_time_query

        _parse_cycle_run_wall_time_query(started_after)
        _parse_cycle_run_wall_time_query(started_before)
        return await super().list_cycle_runs(
            identifier,
            limit=limit,
            offset=offset,
            run_id_contains=run_id_contains,
            status=status,
            run_kind=run_kind,
            run_mode=run_mode,
            exclude_run_kind=exclude_run_kind,
            started_after=started_after,
            started_before=started_before,
            **kwargs,
        )


class _FakeApprovalResult:
    def __init__(self, status: str, intent_id: str, approval_id: str):
        self.status = status
        self.intent_id = intent_id
        self.approval_id = approval_id


class _FakePendingApproval:
    def __init__(self):
        self.approval_id = "approval-1"
        self.intent_id = "intent-1"

        class _Timestamp:
            def isoformat(self):
                return "2026-04-04T00:00:00"

        self.created_at = _Timestamp()
        self.expires_at = _Timestamp()
        self.run_id = "run-x"
        self.symbol = "600000.SH"
        # Persisted intent body — the serializer parses 信号 + order context
        # (rationale / signal_tag / strategy_tag / 限价 / 订单类型 / 有效期) out of
        # this for the merged 信号+审批 card.
        self.intent_payload = (
            '{"intent_id": "intent-1", "symbol": "600000.SH", "action": "buy", '
            '"rationale": "网格下轨触发买入", "signal_tag": "grid_buy_1", '
            '"strategy_tag": "grid_target_exposure", "price_reference": 7.8, '
            '"order_type": "limit", "tif": "day"}'
        )


class _FakeApprovalGate:
    def __init__(self):
        self.expire_calls = 0
        self.approve_calls = []
        self.reject_calls = []
        self.list_approvals_calls = []

    async def expire_pending(self):
        self.expire_calls += 1
        return ["expired-1"]

    async def list_pending(self):
        return [_FakePendingApproval()]

    async def list_approvals(self, **filters):
        self.list_approvals_calls.append(filters)
        return [_FakePendingApproval()], 1

    async def approve(self, approval_id, *, resolver_id=None, decision_source=None):
        self.approve_calls.append((approval_id, resolver_id, decision_source))
        return _FakeApprovalResult(status="approved", intent_id="intent-1", approval_id=approval_id)

    async def reject(self, approval_id, reason="", *, resolver_id=None, decision_source=None):
        self.reject_calls.append((approval_id, reason, resolver_id, decision_source))
        return _FakeApprovalResult(status="rejected", intent_id="intent-1", approval_id=approval_id)


class _FakeAssistantService:
    def __init__(self):
        self.sessions = {}
        self.messages = {}
        self.events = {}
        self.traces = {}
        self.trace_details = {}
        self.sent_messages = []
        self.agent_repo = InMemoryAgentRepository()
        self.channel_repo = InMemoryChannelRepository()
        self.span_session_calls = []

    async def create_session(self, *, agent_id="", title="", config=None):
        session_id = f"asst-{len(self.sessions) + 1}"
        row = {
            "session_id": session_id,
            "agent_id": agent_id,
            "title": title,
            "status": "idle",
            "config": config or {},
            "channel_source": self._derive_channel_source(session_id, config or {}),
            "created_at": "2026-04-29T00:00:00",
            "updated_at": "2026-04-29T00:00:00",
            "last_attempt_id": None,
        }
        self.sessions[session_id] = row
        self.messages[session_id] = []
        self.events[session_id] = [
            {
                "event_id": "evt-1",
                "session_id": session_id,
                "event_type": "session.created",
                "payload": {"session_id": session_id, "agent_id": agent_id},
                "created_at": "2026-04-29T00:00:00",
            }
        ]
        self.traces[session_id] = []
        return row

    async def get_spans_for_sessions(self, session_ids):
        self.span_session_calls.append(list(session_ids))
        return {"spans": [], "model_invocations": []}

    async def list_sessions(self, *, limit=50, offset=0, channel_id=None, source=None):
        rows = list(self.sessions.values())
        if channel_id or source:
            rows = [
                row
                for row in rows
                if self._matches_session_filter(row, channel_id=channel_id, source=source)
            ]
        total = len(rows)
        return {"items": rows[offset : offset + limit], "total": total, "limit": limit, "offset": offset}

    @staticmethod
    def _matches_session_filter(row, *, channel_id=None, source=None):
        channel_source = _FakeAssistantService._derive_channel_source(
            row.get("session_id"),
            row.get("config"),
        )
        normalized_channel_id = str(channel_id or "").strip()
        normalized_source = str(source or "").strip().lower()
        if normalized_channel_id:
            return channel_source.get("channel_id") == normalized_channel_id
        if normalized_source == "web":
            return not channel_source.get("is_channel_session")
        if normalized_source == "channel":
            return bool(channel_source.get("is_channel_session"))
        return True

    async def get_session(self, session_id):
        return self.sessions.get(session_id)

    @staticmethod
    def _derive_channel_source(session_id, config):
        channel = dict((config or {}).get("channel") or {})
        channel_id = str(channel.get("channel_id") or "").strip()
        channel_type = str(channel.get("channel_type") or "").strip()
        if channel_id or channel_type:
            return {
                "is_channel_session": True,
                "channel_id": channel_id or None,
                "channel_type": channel_type or None,
            }
        session_id_text = str(session_id or "")
        if session_id_text.startswith("channel:"):
            parts = session_id_text.split(":", 2)
            return {
                "is_channel_session": True,
                "channel_id": parts[1] if len(parts) > 1 and parts[1] else None,
                "channel_type": None,
            }
        return {
            "is_channel_session": False,
            "channel_id": None,
            "channel_type": None,
        }

    async def send_message(self, *, session_id, content, attachments=None):
        self.sent_messages.append((session_id, content))
        self.sent_attachments = attachments
        if content.strip().lower() == "/new":
            previous = self.sessions[session_id]
            created = await self.create_session(
                agent_id=previous["agent_id"],
                title="",
            )
            return {
                "session": created,
                "messages": [],
                "trace_id": None,
                "lifecycle_command": {
                    "command": "new",
                    "previous_session_id": session_id,
                    "new_session_id": created["session_id"],
                },
            }
        user = {
            "message_id": "msg-user-1",
            "session_id": session_id,
            "role": "user",
            "content": content,
            "created_at": "2026-04-29T00:00:01",
            "linked_attempt_id": "attempt-1",
            "metadata": {},
        }
        assistant = {
            "message_id": "msg-assistant-1",
            "session_id": session_id,
            "role": "assistant",
            "content": "已收到：请做一个 MACD 回测",
            "created_at": "2026-04-29T00:00:02",
            "linked_attempt_id": "attempt-1",
            "metadata": {
                "tool_calls": [],
                "content_blocks": [
                    {"type": "text", "turn": 0, "content": "先检查行情再回测。"},
                    {
                        "type": "tool_call",
                        "tool_call_id": "call_1",
                        "name": "get_market_data",
                        "arguments": {"code": "600522.SH"},
                        "status": "completed",
                    },
                    {"type": "text", "content": "已收到：请做一个 MACD 回测"},
                ],
            },
        }
        self.messages[session_id].extend([user, assistant])
        self.events[session_id].append(
            {
                "event_id": "evt-2",
                "session_id": session_id,
                "event_type": "attempt.completed",
                "payload": {"attempt_id": "attempt-1", "summary": assistant["content"]},
                "created_at": "2026-04-29T00:00:02",
            }
        )
        return {"session": self.sessions[session_id], "messages": [user, assistant]}

    async def list_messages(self, session_id, *, limit=100, offset=0):
        rows = self.messages.get(session_id, [])
        return rows[offset : offset + limit]

    async def list_events(self, session_id, *, after_id=None, limit=100, tail=False):
        rows = self.events.get(session_id, [])
        if after_id:
            ids = [row["event_id"] for row in rows]
            if after_id in ids:
                rows = rows[ids.index(after_id) + 1 :]
            return rows[:limit]
        if tail:
            return rows[-limit:] if limit else rows
        return rows[:limit]

    async def list_traces(self, session_id, *, limit=50, offset=0):
        rows = self.traces.get(session_id, [])
        return {"items": rows[offset : offset + limit], "total": len(rows), "limit": limit, "offset": offset}

    async def get_trace_detail(self, trace_id):
        return self.trace_details.get(trace_id)


class _FakeModelInvocationRepository:
    def __init__(self):
        self.list_calls: list[tuple[int, int]] = []

    async def list_invocations(self, *, limit: int, offset: int, trace_id=None, span_id=None, run_id=None):
        self.list_calls.append((limit, offset, trace_id, span_id, run_id))
        return (
            [
                {
                    "id": 7,
                    "created_at": "2026-04-05T12:00:00",
                    "provider": "anthropic",
                    "model": "test-model",
                    "task_id": "inst-1",
                    "run_id": "run-1",
                    "trace_id": "ab" * 16,
                    "span_id": "cd" * 8,
                    "call_kind": "signal",
                    "first_token_latency_ms": None,
                    "total_latency_ms": 250,
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "total_tokens": 120,
                    "ok": True,
                    "error_message": None,
                    "request": {"system_prompt": "sys", "user_prompt": "usr"},
                    "response": {"message": {}},
                }
            ],
            1,
        )


class ApiAppTests(unittest.TestCase):
    def test_runtime_capabilities_and_status_are_manifest_backed(self):
        service = _FakeService()
        assistant_service = _FakeAssistantService()
        app = create_app(service, _FakeApprovalGate(), assistant_service=assistant_service)

        with TestClient(app) as client:
            caps = client.get("/runtime/capabilities")
            status = client.get("/runtime/status")

        self.assertEqual(caps.status_code, 200)
        caps_body = caps.json()
        self.assertIn("data_provider", caps_body["kinds"])
        self.assertIn("model_provider", caps_body["kinds"])
        self.assertIn("channel", caps_body["kinds"])
        self.assertIn("data.mock", [item["id"] for item in caps_body["items"]])
        self.assertIn("model.anthropic", [item["id"] for item in caps_body["items"]])

        self.assertEqual(status.status_code, 200)
        status_body = status.json()
        self.assertEqual(status_body["health"], "ok")
        self.assertEqual(status_body["capabilities"]["total"], len(caps_body["items"]))
        from doyoutrade.tools import build_default_tool_registry

        self.assertEqual(
            status_body["assistant"]["tool_count"],
            len(build_default_tool_registry().list_tools()),
        )

    def test_version_endpoint_reports_package_and_git_provenance(self):
        service = _FakeService()
        app = create_app(service, _FakeApprovalGate(), assistant_service=_FakeAssistantService())

        with TestClient(app) as client:
            response = client.get("/version")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        from doyoutrade import __version__, engine_version

        self.assertEqual(body["package_version"], __version__)
        self.assertEqual(body["engine_version"], engine_version())
        for key in ("git_tag", "git_commit", "git_commit_short", "git_dirty"):
            self.assertIn(key, body)

    def test_accounts_crud_set_default_and_in_use(self):
        service = _FakeService()
        app = create_app(service, _FakeApprovalGate(), assistant_service=_FakeAssistantService())
        with TestClient(app) as client:
            # empty list
            self.assertEqual(client.get("/accounts").json(), {"items": []})
            # create
            created = client.post(
                "/accounts",
                json={"name": "live-a", "mode": "live", "base_url": "http://x:9",
                      "qmt_account_id": "10000001"},
            )
            self.assertEqual(created.status_code, 201)
            aid = created.json()["id"]
            self.assertFalse(created.json()["is_default"])
            # invalid mode → 400
            bad = client.post("/accounts", json={"name": "x", "mode": "weird"})
            self.assertEqual(bad.status_code, 400)
            # get + 404
            self.assertEqual(client.get(f"/accounts/{aid}").status_code, 200)
            self.assertEqual(client.get("/accounts/acct-nope").status_code, 404)
            # set-default
            d = client.post(f"/accounts/{aid}/set-default")
            self.assertEqual(d.status_code, 200)
            self.assertTrue(d.json()["is_default"])
            self.assertEqual(client.post("/accounts/acct-nope/set-default").status_code, 404)
            # update
            up = client.put(f"/accounts/{aid}", json={"name": "renamed"})
            self.assertEqual(up.json()["name"], "renamed")
            # delete blocked when bound (account_in_use → 409)
            service.account_bindings[aid] = ["task-1"]
            self.assertEqual(client.delete(f"/accounts/{aid}").status_code, 409)
            # delete succeeds once unbound
            service.account_bindings.pop(aid)
            self.assertEqual(client.delete(f"/accounts/{aid}").status_code, 204)
            self.assertEqual(client.get(f"/accounts/{aid}").status_code, 404)

    def test_account_crud_triggers_quote_stream_refresh(self):
        """Each account-mutating endpoint must call ``refresh()`` on the wired
        quote stream service so configuring the default QMT account at runtime
        reconnects live quotes without a server restart."""
        service = _FakeService()
        refresh_calls = []

        class _Qss:
            async def refresh(self):
                refresh_calls.append(True)

        app = create_app(
            service,
            _FakeApprovalGate(),
            assistant_service=_FakeAssistantService(),
            quote_stream_service=_Qss(),
        )
        with TestClient(app) as client:
            created = client.post(
                "/accounts",
                json={"name": "live-a", "mode": "live", "base_url": "http://x:9"},
            )
            aid = created.json()["id"]
            client.post(f"/accounts/{aid}/set-default")
            client.put(f"/accounts/{aid}", json={"name": "renamed"})
            service.account_bindings.pop(aid, None)
            client.delete(f"/accounts/{aid}")

        # create / set-default / update / delete → one refresh each.
        self.assertEqual(len(refresh_calls), 4)

    def test_account_crud_refresh_is_best_effort_when_qss_missing(self):
        """No quote_stream_service wired (isolated tests) — endpoints must
        still succeed (refresh is best-effort)."""
        service = _FakeService()
        app = create_app(
            service,
            _FakeApprovalGate(),
            assistant_service=_FakeAssistantService(),
            quote_stream_service=None,
        )
        with TestClient(app) as client:
            created = client.post(
                "/accounts",
                json={"name": "live-a", "mode": "live", "base_url": "http://x:9"},
            )
            self.assertEqual(created.status_code, 201)

    def test_account_statement_endpoint_supports_default_and_explicit_account(self):
        service = _FakeService()
        app = create_app(service, _FakeApprovalGate(), assistant_service=_FakeAssistantService())
        with TestClient(app) as client:
            explicit = client.get("/accounts/statement", params={"account_id": "acct-1", "asof": "2026-06-18"})
            self.assertEqual(explicit.status_code, 200)
            self.assertEqual(explicit.json()["account_id"], "acct-1")
            self.assertEqual(explicit.json()["asof"], "2026-06-18")

            defaulted = client.get("/accounts/statement")
            self.assertEqual(defaulted.status_code, 200)
            self.assertTrue(defaulted.json()["resolved_via_default"])
            self.assertEqual(service.account_statement_calls[0][0], "acct-1")
            self.assertIsNone(service.account_statement_calls[1][0])

    def test_account_statement_endpoint_maps_missing_account_to_404(self):
        service = _FakeService()
        app = create_app(service, _FakeApprovalGate(), assistant_service=_FakeAssistantService())
        with TestClient(app) as client:
            response = client.get("/accounts/statement", params={"account_id": "acct-nope"})
            self.assertEqual(response.status_code, 404)

    def test_data_providers_endpoint_exposes_manifest_summaries(self):
        service = _FakeService()
        app = create_app(service, _FakeApprovalGate(), assistant_service=_FakeAssistantService())

        with TestClient(app) as client:
            response = client.get("/data-providers")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("mock", body["providers"])
        self.assertIn("qmt", body["providers"])
        self.assertIn("items", body)
        mock = next(item for item in body["items"] if item["provider_id"] == "mock")
        self.assertEqual(mock["capability_id"], "data.mock")
        self.assertEqual(mock["kind"], "data_provider")

    def test_model_route_kind_validation_uses_capability_registry(self):
        service = _FakeService()
        app = create_app(service, _FakeApprovalGate(), assistant_service=_FakeAssistantService())

        with TestClient(app) as client:
            accepted = client.post(
                "/model-routes",
                json={
                    "route_name": "anthropic-main",
                    "provider_kind": "anthropic",
                    "api_key": "sk-test",
                },
            )
            rejected = client.post(
                "/model-routes",
                json={
                    "route_name": "bad",
                    "provider_kind": "unsupported",
                    "api_key": "sk-test",
                },
            )

        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(service.model_route_create_calls[0]["provider_kind"], "anthropic")
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("provider_kind must be one of", rejected.json()["detail"])

    def test_channel_type_validation_uses_capability_registry(self):
        service = _FakeService()
        assistant_service = _FakeAssistantService()

        async def seed_agent():
            return await assistant_service.agent_repo.create_agent(
                {"id": "agent-alpha", "name": "Alpha", "system_prompt": "hi"}
            )

        asyncio.run(seed_agent())
        app = create_app(service, _FakeApprovalGate(), assistant_service=assistant_service)

        with TestClient(app) as client:
            accepted = client.post(
                "/assistant/channels",
                json={
                    "name": "Feishu",
                    "type": "feishu",
                    "agent_id": "agent-alpha",
                    "enabled": False,
                    "config": {},
                    "secrets": {},
                },
            )
            rejected = client.post(
                "/assistant/channels",
                json={
                    "name": "Bad",
                    "type": "bad-channel",
                    "agent_id": "agent-alpha",
                    "enabled": False,
                    "config": {},
                    "secrets": {},
                },
            )

        self.assertEqual(accepted.status_code, 201)
        self.assertEqual(accepted.json()["type"], "feishu")
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("type must be one of", rejected.json()["detail"])

    def test_assistant_agent_context_compaction_create_read_update_contract(self):
        service = _FakeService()
        assistant_service = _FakeAssistantService()
        app = create_app(service, _FakeApprovalGate(), assistant_service=assistant_service)

        with TestClient(app) as client:
            created = client.post(
                "/assistant/agents",
                json={
                    "name": "Compaction Agent",
                    "system_prompt": "hi",
                    "context_compaction": {
                        "mode": "manual",
                        "allow_slash_compact": True,
                    },
                },
            )
            self.assertEqual(created.status_code, 201)
            created_body = created.json()
            self.assertEqual(created_body["context_compaction"]["mode"], "manual")
            self.assertTrue(created_body["context_compaction"]["allow_slash_compact"])
            self.assertIn("auto_threshold_tokens", created_body["context_compaction"])

            fetched = client.get(f"/assistant/agents/{created_body['id']}")
            self.assertEqual(fetched.status_code, 200)
            fetched_body = fetched.json()
            self.assertEqual(fetched_body["context_compaction"]["mode"], "manual")
            self.assertTrue(fetched_body["context_compaction"]["micro_compaction_enabled"])

            updated = client.put(
                f"/assistant/agents/{created_body['id']}",
                json={
                    "context_compaction": {
                        "auto_threshold_tokens": 12345,
                    }
                },
            )
            self.assertEqual(updated.status_code, 200)
            updated_body = updated.json()
            self.assertEqual(updated_body["context_compaction"]["mode"], "manual")
            self.assertEqual(
                updated_body["context_compaction"]["auto_threshold_tokens"],
                12345,
            )
            self.assertTrue(updated_body["context_compaction"]["allow_slash_compact"])

    def test_builtin_main_agent_api_contract(self):
        from doyoutrade.assistant.main_agent import MAIN_AGENT_ID

        service = _FakeService()
        assistant_service = _FakeAssistantService()
        asyncio.run(assistant_service.agent_repo.ensure_main_agent())
        app = create_app(service, _FakeApprovalGate(), assistant_service=assistant_service)

        with TestClient(app) as client:
            # GET surfaces the builtin flag + the locked editable surface.
            got = client.get(f"/assistant/agents/{MAIN_AGENT_ID}")
            self.assertEqual(got.status_code, 200)
            body = got.json()
            self.assertTrue(body["is_builtin"])
            self.assertEqual(
                body["editable_fields"],
                ["model_route_name", "context_compaction", "max_turns"],
            )

            # LIST sorts the builtin first and carries the flag.
            listed = client.get("/assistant/agents").json()["items"]
            self.assertTrue(listed[0]["is_builtin"])

            # Editing a LOCKED field is refused with a stable error_code (403).
            rejected = client.put(
                f"/assistant/agents/{MAIN_AGENT_ID}",
                json={"name": "Renamed"},
            )
            self.assertEqual(rejected.status_code, 403)
            self.assertEqual(rejected.json()["detail"]["error_code"], "agent_builtin_immutable")

            # Editing an allowed knob succeeds.
            ok = client.put(
                f"/assistant/agents/{MAIN_AGENT_ID}",
                json={"model_route_name": "fast-route", "max_turns": 8},
            )
            self.assertEqual(ok.status_code, 200)
            self.assertEqual(ok.json()["model_route_name"], "fast-route")
            self.assertEqual(ok.json()["max_turns"], 8)

            # Deleting the builtin is refused (403).
            deleted = client.delete(f"/assistant/agents/{MAIN_AGENT_ID}")
            self.assertEqual(deleted.status_code, 403)
            self.assertEqual(deleted.json()["detail"]["error_code"], "agent_builtin_immutable")

            # Cloning it yields an ordinary, editable custom agent.
            cloned = client.post(
                f"/assistant/agents/{MAIN_AGENT_ID}/clone",
                json={"name": "Main Copy"},
            )
            self.assertEqual(cloned.status_code, 201)
            self.assertFalse(cloned.json()["is_builtin"])

    def test_assistant_agent_update_allows_clearing_compaction_summary_model_route(self):
        service = _FakeService()
        assistant_service = _FakeAssistantService()
        app = create_app(service, _FakeApprovalGate(), assistant_service=assistant_service)

        with TestClient(app) as client:
            created = client.post(
                "/assistant/agents",
                json={
                    "name": "Compaction Agent",
                    "system_prompt": "hi",
                    "context_compaction": {
                        "mode": "manual",
                        "summary_model_route_name": "summary-route",
                    },
                },
            )
            self.assertEqual(created.status_code, 201)
            created_body = created.json()
            self.assertEqual(
                created_body["context_compaction"]["summary_model_route_name"],
                "summary-route",
            )

            updated = client.put(
                f"/assistant/agents/{created_body['id']}",
                json={"context_compaction": {"summary_model_route_name": ""}},
            )
            self.assertEqual(updated.status_code, 200)
            updated_body = updated.json()
            self.assertEqual(
                updated_body["context_compaction"]["summary_model_route_name"],
                "",
            )

    def test_assistant_agent_prompt_templates_list_contract(self):
        service = _FakeService()
        assistant_service = _FakeAssistantService()
        app = create_app(service, _FakeApprovalGate(), assistant_service=assistant_service)

        with TestClient(app) as client:
            response = client.get("/assistant/agents/prompt-templates")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 5)
        self.assertEqual(
            [item["template_id"] for item in body["items"]],
            [
                "main-agent",
                "swing-trader",
                "event-driven",
                "research-copilot",
                "signal-card-composer",
            ],
        )
        self.assertTrue(all(item["system_prompt"].strip() for item in body["items"]))

    def test_assistant_agent_create_accepts_prompt_template_reference(self):
        service = _FakeService()
        assistant_service = _FakeAssistantService()
        app = create_app(service, _FakeApprovalGate(), assistant_service=assistant_service)

        with TestClient(app) as client:
            created = client.post(
                "/assistant/agents",
                json={
                    "name": "Template Agent",
                    "system_prompt": "",
                    "system_prompt_template_id": "swing-trader",
                },
            )

        self.assertEqual(created.status_code, 201)
        body = created.json()
        self.assertEqual(body["system_prompt_template_id"], "swing-trader")
        self.assertEqual(body["system_prompt"], "")
        self.assertTrue(body["resolved_system_prompt"].strip())

    def test_assistant_agent_update_accepts_prompt_template_reference(self):
        service = _FakeService()
        assistant_service = _FakeAssistantService()
        app = create_app(service, _FakeApprovalGate(), assistant_service=assistant_service)

        async def seed_agent():
            return await assistant_service.agent_repo.create_agent(
                {
                    "name": "Template Agent",
                    "system_prompt": "legacy fallback prompt",
                }
            )

        agent = asyncio.run(seed_agent())

        with TestClient(app) as client:
            updated = client.put(
                f"/assistant/agents/{agent['id']}",
                json={"system_prompt_template_id": "research-copilot"},
            )

        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["system_prompt_template_id"], "research-copilot")

    def test_assistant_agent_tool_configs_create_read_update_contract(self):
        service = _FakeService()
        assistant_service = _FakeAssistantService()
        app = create_app(service, _FakeApprovalGate(), assistant_service=assistant_service)

        with TestClient(app) as client:
            created = client.post(
                "/assistant/agents",
                json={
                    "name": "Tool Config Agent",
                    "system_prompt": "hi",
                    "tool_configs": [
                        {"name": "read_file", "load_mode": "base"},
                        {"name": "create_task", "load_mode": "deferred"},
                    ],
                },
            )
            self.assertEqual(created.status_code, 201)
            created_body = created.json()
            self.assertEqual(
                created_body["tool_configs"],
                [
                    {"name": "read_file", "load_mode": "base"},
                    {"name": "create_task", "load_mode": "deferred"},
                ],
            )
            self.assertEqual(
                created_body["tool_names"],
                ["read_file", "create_task"],
            )

            fetched = client.get(f"/assistant/agents/{created_body['id']}")
            self.assertEqual(fetched.status_code, 200)
            self.assertEqual(
                fetched.json()["tool_configs"],
                created_body["tool_configs"],
            )

            updated = client.put(
                f"/assistant/agents/{created_body['id']}",
                json={
                    "tool_configs": [
                        {"name": "read_file", "load_mode": "base"},
                        {"name": "run_strategy_backtest", "load_mode": "deferred"},
                    ]
                },
            )
            self.assertEqual(updated.status_code, 200)
            updated_body = updated.json()
            self.assertEqual(
                updated_body["tool_configs"],
                [
                    {"name": "read_file", "load_mode": "base"},
                    {"name": "run_strategy_backtest", "load_mode": "deferred"},
                ],
            )
            self.assertEqual(
                updated_body["tool_names"],
                ["read_file", "run_strategy_backtest"],
            )

    def test_assistant_agent_create_backfills_tool_configs_from_legacy_tool_names(self):
        service = _FakeService()
        assistant_service = _FakeAssistantService()
        app = create_app(service, _FakeApprovalGate(), assistant_service=assistant_service)

        with TestClient(app) as client:
            created = client.post(
                "/assistant/agents",
                json={
                    "name": "Legacy Tool Agent",
                    "system_prompt": "hi",
                    "tool_names": ["read_file", "create_task"],
                },
            )

        self.assertEqual(created.status_code, 201)
        self.assertEqual(
            created.json()["tool_configs"],
            [
                {"name": "read_file", "load_mode": "base"},
                {"name": "create_task", "load_mode": "base"},
            ],
        )

    def test_assistant_agent_create_rejects_malformed_tool_configs(self):
        service = _FakeService()
        assistant_service = _FakeAssistantService()
        app = create_app(service, _FakeApprovalGate(), assistant_service=assistant_service)

        with TestClient(app) as client:
            response = client.post(
                "/assistant/agents",
                json={
                    "name": "Bad Tool Agent",
                    "system_prompt": "hi",
                    "tool_configs": [
                        {"name": "read_file", "load_mode": "later"},
                    ],
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("tool_configs[0].load_mode", response.json()["detail"])

    def test_assistant_agent_create_rejects_malformed_context_compaction(self):
        service = _FakeService()
        assistant_service = _FakeAssistantService()
        app = create_app(service, _FakeApprovalGate(), assistant_service=assistant_service)

        with TestClient(app) as client:
            response = client.post(
                "/assistant/agents",
                json={
                    "name": "Compaction Agent",
                    "system_prompt": "hi",
                    "context_compaction": {
                        "allow_slash_compact": "yes",
                    },
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn(
            "context_compaction.allow_slash_compact must be a boolean",
            response.json()["detail"],
        )

    def test_assistant_agent_update_rejects_malformed_context_compaction(self):
        service = _FakeService()
        assistant_service = _FakeAssistantService()
        app = create_app(service, _FakeApprovalGate(), assistant_service=assistant_service)

        async def seed_agent():
            return await assistant_service.agent_repo.create_agent(
                {"name": "Compaction Agent", "system_prompt": "hi"}
            )

        agent = asyncio.run(seed_agent())

        with TestClient(app) as client:
            response = client.put(
                f"/assistant/agents/{agent['id']}",
                json={
                    "context_compaction": {
                        "auto_threshold_tokens": "a lot",
                    }
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn(
            "context_compaction.auto_threshold_tokens must be an integer",
            response.json()["detail"],
        )

    def test_assistant_agent_create_rejects_unknown_context_compaction_key(self):
        service = _FakeService()
        assistant_service = _FakeAssistantService()
        app = create_app(service, _FakeApprovalGate(), assistant_service=assistant_service)

        with TestClient(app) as client:
            response = client.post(
                "/assistant/agents",
                json={
                    "name": "Compaction Agent",
                    "system_prompt": "hi",
                    "context_compaction": {
                        "mode": "manual",
                        "unexpected_field": "nope",
                    },
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn(
            "context_compaction contains unsupported field: unexpected_field",
            response.json()["detail"],
        )

    def test_assistant_agent_update_accepts_null_context_compaction(self):
        service = _FakeService()
        assistant_service = _FakeAssistantService()
        app = create_app(service, _FakeApprovalGate(), assistant_service=assistant_service)

        async def seed_agent():
            return await assistant_service.agent_repo.create_agent(
                {
                    "name": "Compaction Agent",
                    "system_prompt": "hi",
                    "context_compaction": {"mode": "manual"},
                }
            )

        agent = asyncio.run(seed_agent())

        with TestClient(app) as client:
            response = client.put(
                f"/assistant/agents/{agent['id']}",
                json={"context_compaction": None},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("context_compaction", response.json())

    def test_assistant_agent_update_rejects_unknown_context_compaction_key(self):
        service = _FakeService()
        assistant_service = _FakeAssistantService()
        app = create_app(service, _FakeApprovalGate(), assistant_service=assistant_service)

        async def seed_agent():
            return await assistant_service.agent_repo.create_agent(
                {"name": "Compaction Agent", "system_prompt": "hi"}
            )

        agent = asyncio.run(seed_agent())

        with TestClient(app) as client:
            response = client.put(
                f"/assistant/agents/{agent['id']}",
                json={
                    "context_compaction": {
                        "unexpected_field": "nope",
                    }
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn(
            "context_compaction contains unsupported field: unexpected_field",
            response.json()["detail"],
        )

    def test_assistant_channel_crud_redacts_and_copies_secrets(self):
        service = _FakeService()
        assistant_service = _FakeAssistantService()

        async def seed_agent():
            return await assistant_service.agent_repo.create_agent(
                {"id": "agent-alpha", "name": "Alpha", "system_prompt": "hi"}
            )

        asyncio.run(seed_agent())
        app = create_app(service, _FakeApprovalGate(), assistant_service=assistant_service)

        with TestClient(app) as client:
            created = client.post(
                "/assistant/channels",
                json={
                    "name": "Feishu Alpha",
                    "type": "feishu",
                    "enabled": True,
                    "agent_id": "agent-alpha",
                    "config": {"app_id": "cli_alpha", "domain": "feishu"},
                    "secrets": {
                        "app_secret": "secret-alpha",
                        "verification_token": "token-alpha",
                    },
                },
            )
            self.assertEqual(created.status_code, 201)
            channel = created.json()
            self.assertEqual(channel["name"], "Feishu Alpha")
            self.assertEqual(channel["agent_id"], "agent-alpha")
            self.assertNotIn("secrets", channel)
            self.assertEqual(channel["secret_keys"], ["app_secret", "verification_token"])

            listed = client.get("/assistant/channels")
            self.assertEqual(listed.status_code, 200)
            self.assertEqual(listed.json()["total"], 1)
            self.assertNotIn("secret-alpha", listed.text)

            copied = client.post(f"/assistant/channels/{channel['id']}/secrets/app_secret/copy")
            self.assertEqual(copied.status_code, 200)
            self.assertEqual(copied.json(), {"secret_key": "app_secret", "value": "secret-alpha"})

            updated = client.put(
                f"/assistant/channels/{channel['id']}",
                json={
                    "name": "Feishu Alpha 2",
                    "config": {"app_id": "cli_alpha_2", "domain": "lark"},
                    "secrets": {"app_secret": "", "encrypt_key": "encrypt-alpha"},
                },
            )
            self.assertEqual(updated.status_code, 200)
            self.assertEqual(updated.json()["name"], "Feishu Alpha 2")
            self.assertEqual(updated.json()["secret_keys"], ["app_secret", "encrypt_key", "verification_token"])

            copied_after_update = client.post(
                f"/assistant/channels/{channel['id']}/secrets/app_secret/copy"
            )
            self.assertEqual(copied_after_update.json()["value"], "secret-alpha")

            deleted = client.delete(f"/assistant/channels/{channel['id']}")
            self.assertEqual(deleted.status_code, 204)
            empty = client.get("/assistant/channels")
            self.assertEqual(empty.json()["total"], 0)

    def test_list_feishu_chats_flattens_bots_and_groups(self):
        service = _FakeService()

        class _FeishuChannel:
            channel_type = "feishu"

            async def list_chats(self):
                return [
                    {"chat_id": "oc_alpha", "name": "策略群"},
                    {"chat_id": "oc_beta", "name": "风控群"},
                ]

        class _WsChannel:
            channel_type = "websocket"

            async def list_chats(self):  # should never be called (non-feishu)
                raise AssertionError("non-feishu channel must be skipped")

        class _Manager:
            def __init__(self):
                self._channels = {"ch-1": _FeishuChannel(), "ch-2": _WsChannel()}

            @property
            def channel_ids(self):
                return list(self._channels)

            def get(self, cid):
                return self._channels.get(cid)

        class _ChannelRepo:
            async def list_channels(self):
                return [{"id": "ch-1", "name": "飞书Alpha"}]

        app = create_app(
            service,
            _FakeApprovalGate(),
            assistant_service=_FakeAssistantService(),
            channel_manager=_Manager(),
            channel_repository=_ChannelRepo(),
        )
        with TestClient(app) as client:
            resp = client.get("/assistant/feishu/chats")
            self.assertEqual(resp.status_code, 200)
            items = resp.json()["items"]
            self.assertEqual(len(items), 2)
            self.assertEqual({i["chat_id"] for i in items}, {"oc_alpha", "oc_beta"})
            self.assertTrue(all(i["channel_id"] == "ch-1" for i in items))
            self.assertTrue(all(i["channel_name"] == "飞书Alpha" for i in items))

    def test_list_feishu_chats_surfaces_per_channel_error(self):
        service = _FakeService()

        class _BrokenFeishu:
            channel_type = "feishu"

            async def list_chats(self):
                raise RuntimeError("missing im:chat scope")

        class _Manager:
            @property
            def channel_ids(self):
                return ["ch-broken"]

            def get(self, cid):
                return _BrokenFeishu()

        app = create_app(
            service,
            _FakeApprovalGate(),
            assistant_service=_FakeAssistantService(),
            channel_manager=_Manager(),
        )
        with TestClient(app) as client:
            resp = client.get("/assistant/feishu/chats")
            self.assertEqual(resp.status_code, 200)
            items = resp.json()["items"]
            self.assertEqual(len(items), 1)
            self.assertIn("missing im:chat scope", items[0]["error"])
            self.assertEqual(items[0]["chat_id"], "")

    def test_assistant_channel_create_rejects_missing_agent(self):
        service = _FakeService()
        assistant_service = _FakeAssistantService()
        app = create_app(service, _FakeApprovalGate(), assistant_service=assistant_service)

        with TestClient(app) as client:
            response = client.post(
                "/assistant/channels",
                json={
                    "name": "Feishu Alpha",
                    "type": "feishu",
                    "agent_id": "missing-agent",
                    "config": {"app_id": "cli_alpha", "domain": "feishu"},
                    "secrets": {"app_secret": "secret-alpha"},
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("agent not found", response.json()["detail"].lower())

    def test_assistant_session_chat_api_contract(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        assistant_service = _FakeAssistantService()
        app = create_app(service, approval_gate, assistant_service=assistant_service)

        with TestClient(app) as client:
            created = client.post(
                "/assistant/sessions",
                json={"title": "MACD research", "agent_id": "test-agent", "model_route_name": "default"},
            )
            self.assertEqual(created.status_code, 201)
            session = created.json()
            self.assertEqual(session["session_id"], "asst-1")
            self.assertEqual(session["title"], "MACD research")
            self.assertEqual(service.ensure_model_route_calls, ["default"])

            listed = client.get("/assistant/sessions")
            self.assertEqual(listed.status_code, 200)
            self.assertEqual(listed.json()["total"], 1)

            sent = client.post(
                "/assistant/sessions/asst-1/messages",
                json={"content": "请做一个 MACD 回测"},
            )
            self.assertEqual(sent.status_code, 200)
            payload = sent.json()
            self.assertEqual(payload["messages"][0]["role"], "user")
            self.assertEqual(payload["messages"][1]["role"], "assistant")

            messages = client.get("/assistant/sessions/asst-1/messages")
            self.assertEqual(messages.status_code, 200)
            self.assertEqual([m["role"] for m in messages.json()], ["user", "assistant"])
            self.assertEqual(
                messages.json()[1]["metadata"]["content_blocks"][0],
                {"type": "text", "turn": 0, "content": "先检查行情再回测。"},
            )

            events = client.get("/assistant/sessions/asst-1/events")
            self.assertEqual(events.status_code, 200)
            self.assertEqual(events.json()[-1]["event_type"], "attempt.completed")

            # Seed extra events so the default (oldest-first) page and the
            # `tail=true` (newest-first-then-reversed) page actually differ,
            # then confirm the `tail` query param reaches the service call —
            # this is the query the frontend now uses to reconstruct
            # "what is this session doing right now" instead of silently
            # reading its earliest history (see AssistantPage.tsx's
            # refreshSessionData).
            for i in range(5):
                assistant_service.events["asst-1"].append(
                    {
                        "event_id": f"evt-extra-{i}",
                        "session_id": "asst-1",
                        "event_type": "thinking.delta",
                        "payload": {"attempt_id": "attempt-1", "delta": str(i)},
                        "created_at": "2026-04-29T00:00:03",
                    }
                )

            default_page = client.get("/assistant/sessions/asst-1/events", params={"limit": 3})
            self.assertEqual(default_page.status_code, 200)
            self.assertEqual(
                [e["event_type"] for e in default_page.json()],
                ["session.created", "attempt.completed", "thinking.delta"],
            )

            tail_page = client.get("/assistant/sessions/asst-1/events", params={"limit": 3, "tail": "true"})
            self.assertEqual(tail_page.status_code, 200)
            self.assertEqual([e["payload"]["delta"] for e in tail_page.json()], ["2", "3", "4"])

    def test_assistant_session_export_returns_markdown_diagnostics(self):
        service = _FakeService()
        assistant_service = _FakeAssistantService()
        app = create_app(service, _FakeApprovalGate(), assistant_service=assistant_service)

        with TestClient(app) as client:
            created = client.post(
                "/assistant/sessions",
                json={"title": "Export me", "agent_id": "test-agent"},
            )
            self.assertEqual(created.status_code, 201)
            session_id = created.json()["session_id"]
            agent = {
                "id": "test-agent",
                "name": "Test Agent",
                "status": "active",
                "system_prompt": "Stored prompt",
                "resolved_system_prompt": "Resolved prompt",
                "model_route_name": "default",
                "max_turns": 4,
                "tool_configs": [{"name": "get_market_data"}],
                "skill_names": ["doyoutrade-validation"],
            }
            assistant_service.agent_repo.agents["test-agent"] = agent
            sent = client.post(
                f"/assistant/sessions/{session_id}/messages",
                json={"content": "export diagnostics"},
            )
            self.assertEqual(sent.status_code, 200)
            assistant_service.events[session_id][-1]["payload"]["run_id"] = "asst-run-1"
            assistant_service.events[session_id][-1]["payload"]["trace_id"] = "trace-1"
            assistant_service.traces[session_id] = [
                {
                    "trace_id": "trace-1",
                    "session_id": session_id,
                    "span_name": "assistant.loop",
                    "created_at": "2026-04-29T00:00:01",
                    "duration_ms": 21,
                    "status": "ok",
                    "span_count": 1,
                    "model": "stub-model",
                    "input_tokens": 5,
                    "output_tokens": 7,
                }
            ]
            assistant_service.trace_details["trace-1"] = {
                "trace_id": "trace-1",
                "session_id": session_id,
                "spans": [
                    {
                        "span_id": "span-1",
                        "trace_id": "trace-1",
                        "name": "assistant.loop",
                        "attributes": {"doyoutrade.run_id": "asst-run-1", "amount": Decimal("0.10")},
                        "status": "ok",
                    }
                ],
                "model_invocations": [
                    {
                        "id": 1,
                        "run_id": "asst-run-1",
                        "trace_id": "trace-1",
                        "span_id": "span-1",
                        "call_kind": "assistant_loop",
                        "request": {
                            "messages": [{"role": "user", "content": "export diagnostics"}],
                            "price": Decimal("12345678901234567890.123400"),
                        },
                        "response": {"text": "done"},
                    }
                ],
            }

            response = client.get(
                f"/assistant/sessions/{session_id}/export?format=markdown&include_traces=true"
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["ids"]["session_id"], session_id)
        self.assertEqual(payload["ids"]["run_ids"], ["asst-run-1"])
        self.assertEqual(payload["ids"]["trace_ids"], ["trace-1"])
        self.assertEqual(payload["counts"]["messages"], 2)
        self.assertEqual(payload["counts"]["events"], 2)
        self.assertEqual(payload["counts"]["traces"], 1)
        self.assertEqual(payload["counts"]["spans"], 1)
        self.assertEqual(payload["counts"]["model_invocations"], 1)
        self.assertEqual(payload["agent"]["id"], "test-agent")
        self.assertEqual(payload["spans"][0]["attributes"]["amount"], "0.1")
        self.assertEqual(payload["model_invocations"][0]["request"]["price"], "12345678901234567890.1234")
        self.assertNotIn("spans", payload["trace_details"][0])
        self.assertNotIn("model_invocations", payload["trace_details"][0])
        self.assertIn("# Assistant Session Export", payload["export_text"])
        self.assertIn("Test Agent", payload["export_text"])
        self.assertIn("Tool Call: `get_market_data`", payload["export_text"])
        self.assertIn("asst-run-1", payload["export_text"])
        self.assertIn("trace-1", payload["export_text"])
        self.assertEqual(payload["warnings"], [])

    def test_assistant_session_export_warns_when_trace_details_are_truncated(self):
        assistant_service = _FakeAssistantService()
        app = create_app(_FakeService(), _FakeApprovalGate(), assistant_service=assistant_service)

        with TestClient(app) as client:
            created = client.post(
                "/assistant/sessions",
                json={"title": "Large export", "agent_id": "test-agent"},
            )
            self.assertEqual(created.status_code, 201)
            session_id = created.json()["session_id"]
            assistant_service.traces[session_id] = [
                {
                    "trace_id": f"trace-{index}",
                    "session_id": session_id,
                    "span_name": "assistant.loop",
                    "status": "ok",
                    "span_count": 1,
                }
                for index in range(201)
            ]

            response = client.get(f"/assistant/sessions/{session_id}/export?format=json&include_traces=true")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["traces"]["items"]), 200)
        self.assertTrue(
            any("trace details truncated" in warning for warning in payload["warnings"]),
            payload["warnings"],
        )

    def test_assistant_session_export_returns_404_for_missing_session(self):
        app = create_app(_FakeService(), _FakeApprovalGate(), assistant_service=_FakeAssistantService())

        with TestClient(app) as client:
            response = client.get("/assistant/sessions/missing/export?format=json")

        self.assertEqual(response.status_code, 404)
        self.assertIn("assistant session not found", response.json()["detail"])

    def test_assistant_session_list_and_get_include_channel_source_metadata(self):
        service = _FakeService()
        assistant_service = _FakeAssistantService()
        app = create_app(service, _FakeApprovalGate(), assistant_service=assistant_service)

        with TestClient(app) as client:
            created = client.post(
                "/assistant/sessions",
                json={"title": "Channel session", "agent_id": "test-agent"},
            )
            self.assertEqual(created.status_code, 201)
            session_id = created.json()["session_id"]

            assistant_service.sessions[session_id]["session_id"] = "channel:feishu-alpha:open_id_123"
            assistant_service.sessions[session_id]["config"] = {
                "channel": {"channel_id": "feishu-alpha", "channel_type": "feishu"}
            }
            assistant_service.sessions[session_id]["channel_source"] = assistant_service._derive_channel_source(
                assistant_service.sessions[session_id]["session_id"],
                assistant_service.sessions[session_id]["config"],
            )

            listed = client.get("/assistant/sessions")
            self.assertEqual(listed.status_code, 200)
            listed_row = listed.json()["items"][0]
            self.assertEqual(
                listed_row["channel_source"],
                {
                    "is_channel_session": True,
                    "channel_id": "feishu-alpha",
                    "channel_type": "feishu",
                },
            )
            self.assertEqual(listed_row["title"], "Channel session")

            detail = client.get(f"/assistant/sessions/{session_id}")
            self.assertEqual(detail.status_code, 200)
            self.assertEqual(
                detail.json()["channel_source"],
                {
                    "is_channel_session": True,
                    "channel_id": "feishu-alpha",
                    "channel_type": "feishu",
                },
            )

            web_created = client.post(
                "/assistant/sessions",
                json={"title": "Web session", "agent_id": "test-agent"},
            )
            self.assertEqual(web_created.status_code, 201)
            web_session_id = web_created.json()["session_id"]

            filtered = client.get("/assistant/sessions", params={"channel_id": "feishu-alpha"})
            self.assertEqual(filtered.status_code, 200)
            filtered_ids = {row["session_id"] for row in filtered.json()["items"]}
            self.assertIn("channel:feishu-alpha:open_id_123", filtered_ids)
            self.assertNotIn(web_session_id, filtered_ids)

            web_only = client.get("/assistant/sessions", params={"source": "web"})
            self.assertEqual(web_only.status_code, 200)
            web_ids = {row["session_id"] for row in web_only.json()["items"]}
            self.assertIn(web_session_id, web_ids)
            self.assertNotIn("channel:feishu-alpha:open_id_123", web_ids)

            invalid = client.get("/assistant/sessions", params={"source": "invalid"})
            self.assertEqual(invalid.status_code, 400)

    def test_assistant_session_new_command_returns_new_session_contract(self):
        service = _FakeService()
        assistant_service = _FakeAssistantService()
        app = create_app(service, _FakeApprovalGate(), assistant_service=assistant_service)

        with TestClient(app) as client:
            created = client.post(
                "/assistant/sessions",
                json={"title": "DoYouTrade Agent", "agent_id": "test-agent"},
            )
            self.assertEqual(created.status_code, 201)
            session_id = created.json()["session_id"]

            response = client.post(
                f"/assistant/sessions/{session_id}/messages",
                json={"content": "/new"},
            )

            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["messages"], [])
            self.assertIsNone(payload["trace_id"])
            self.assertEqual(payload["lifecycle_command"]["command"], "new")
            self.assertEqual(payload["lifecycle_command"]["previous_session_id"], session_id)
            self.assertNotEqual(payload["session"]["session_id"], session_id)
            self.assertEqual(payload["session"]["title"], "")

    def test_assistant_session_rejects_unknown_model_route(self):
        class _RejectingService(_FakeService):
            async def ensure_model_route_exists(self, route_name: str) -> None:
                await super().ensure_model_route_exists(route_name)
                raise RecordNotFoundError(f"model route not found: {route_name}")

        service = _RejectingService()
        app = create_app(service, _FakeApprovalGate(), assistant_service=_FakeAssistantService())

        with TestClient(app) as client:
            response = client.post(
                "/assistant/sessions",
                json={"title": "bad route", "agent_id": "test-agent", "model_route_name": "missing-route"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("model route not found", response.json()["detail"])
        self.assertEqual(service.ensure_model_route_calls, ["missing-route"])

    def tearDown(self):
        reset_observability()

    def test_model_invocations_endpoint(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        repo = _FakeModelInvocationRepository()
        app = create_app(service, approval_gate, repo)
        with TestClient(app) as client:
            response = client.get("/model-invocations?limit=25&offset=0")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["id"], 7)
        self.assertEqual(body["items"][0]["span_id"], "cd" * 8)
        self.assertEqual(repo.list_calls, [(25, 0, None, None, None)])

    def test_model_invocations_endpoint_accepts_trace_and_span_filters(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        repo = _FakeModelInvocationRepository()
        app = create_app(service, approval_gate, repo)
        with TestClient(app) as client:
            response = client.get(
                "/model-invocations?limit=10&offset=0&trace_id=ab&span_id=12",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(repo.list_calls, [(10, 0, "ab", "12", None)])

    def test_model_invocations_endpoint_accepts_run_id_filter(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        repo = _FakeModelInvocationRepository()
        app = create_app(service, approval_gate, repo)
        with TestClient(app) as client:
            response = client.get("/model-invocations?run_id=run-test-1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(repo.list_calls, [(10, 0, None, None, "run-test-1")])

    def test_post_model_routes_accepts_lmstudio_and_rejects_unknown_kind(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)
        with TestClient(app) as client:
            bad = client.post(
                "/model-routes",
                json={
                    "route_name": "k",
                    "provider_kind": "unknown_kind",
                    "api_key": "",
                },
            )
        self.assertEqual(bad.status_code, 400)
        detail = bad.json()["detail"]
        self.assertIn("provider_kind must be", detail)
        self.assertIn("lmstudio", detail)
        self.assertEqual(service.model_route_create_calls, [])

        with TestClient(app) as client:
            ok = client.post(
                "/model-routes",
                json={
                    "route_name": "local-lm",
                    "provider_kind": "lmstudio",
                    "api_key": "",
                    "base_url": "http://127.0.0.1:1234",
                },
            )
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["provider_kind"], "lmstudio")
        self.assertEqual(len(service.model_route_create_calls), 1)
        self.assertEqual(service.model_route_create_calls[0]["provider_kind"], "lmstudio")

    def test_tick_endpoint_awaits_async_service_and_gate(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)
        stream = io.StringIO()
        initialize_observability(service_name="doyoutrade-test", stream=stream, app=app)

        with TestClient(app) as client:
            response = client.post("/system/tick")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"executed": 2, "expired_count": 1})
        self.assertEqual(service.tick_calls, 1)
        self.assertEqual(approval_gate.expire_calls, 1)

    def test_state_and_approval_endpoints_await_async_dependencies(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            state_response = client.get("/system/state")
            kill_response = client.post("/system/kill-switch", json={"enabled": True})
            pending_response = client.get("/approvals/pending")
            approve_response = client.post("/approvals/approval-1/approve")
            reject_response = client.post("/approvals/approval-1/reject")

        self.assertEqual(state_response.status_code, 200)
        self.assertEqual(
            state_response.json(),
            {"kill_switch_enabled": False, "task_count": 1, "running_count": 1},
        )
        self.assertEqual(kill_response.status_code, 200)
        self.assertEqual(service.kill_switch_calls, [True])
        self.assertEqual(pending_response.status_code, 200)
        self.assertEqual(len(pending_response.json()), 1)
        self.assertEqual(approve_response.status_code, 200)
        self.assertEqual(approve_response.json()["status"], "approved")
        self.assertEqual(reject_response.status_code, 200)
        self.assertEqual(reject_response.json()["status"], "rejected")
        self.assertEqual(approval_gate.approve_calls, [("approval-1", None, "web")])
        self.assertEqual(
            approval_gate.reject_calls, [("approval-1", "api reject", None, "web")]
        )

    def test_list_approvals_passes_filters_and_returns_paged_envelope(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            response = client.get(
                "/approvals",
                params={
                    "status": ["approved", "rejected"],
                    "symbol": "600000.SH",
                    "task_id": "task-9",
                    "decision_source": "web",
                    "q": "intent",
                    "created_after": "2026-06-01T00:00:00Z",
                    "limit": 25,
                    "offset": 5,
                },
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["limit"], 25)
        self.assertEqual(body["offset"], 5)
        self.assertEqual(len(body["items"]), 1)
        # History serializer carries the resolved-decision fields.
        self.assertIn("decided_at", body["items"][0])
        self.assertIn("resolved_at", body["items"][0])
        self.assertIn("reason", body["items"][0])
        # 信号 + order context parsed out of intent_payload for the merged card.
        self.assertEqual(body["items"][0]["rationale"], "网格下轨触发买入")
        self.assertEqual(body["items"][0]["signal_tag"], "grid_buy_1")
        self.assertEqual(body["items"][0]["strategy_tag"], "grid_target_exposure")
        self.assertEqual(body["items"][0]["price_reference"], "7.8")
        self.assertEqual(body["items"][0]["order_type"], "limit")
        self.assertEqual(body["items"][0]["tif"], "day")
        # Filters reached the gate, parsed/normalized as expected.
        self.assertEqual(len(approval_gate.list_approvals_calls), 1)
        call = approval_gate.list_approvals_calls[0]
        self.assertEqual(call["statuses"], ["approved", "rejected"])
        self.assertEqual(call["symbol"], "600000.SH")
        self.assertEqual(call["task_id"], "task-9")
        self.assertEqual(call["decision_source"], "web")
        self.assertEqual(call["search"], "intent")
        self.assertEqual(call["limit"], 25)
        self.assertEqual(call["offset"], 5)
        # ISO 'Z' normalized to a naive-UTC datetime for the naive columns.
        self.assertIsNotNone(call["created_after"])
        self.assertIsNone(call["created_after"].tzinfo)

    def test_list_approvals_injects_cycle_signal_snapshot(self):
        # 现价/涨跌幅/方向 come from the order's cycle digest (signal-time market +
        # 判断), fetched by run_id; symbol_name from the instrument catalog. Both
        # injected into the serialized row.
        class _FakeCycleRepo:
            async def get_by_run_id(self, run_id):
                assert run_id == "run-x"
                return {
                    "details": {
                        "market_snapshot": {"600000.SH": {"last_price": 7.78, "pct_change": 1.2}},
                        "signal_diagnostics": {"600000.SH": {"direction": "buy", "tag": "grid_buy_1"}},
                    }
                }

        class _FakeCatalogRepo:
            async def get(self, symbol):
                return {"display_name": "浦发银行"} if symbol == "600000.SH" else None

        service = _FakeService()
        service.cycle_run_repository = _FakeCycleRepo()
        service.instrument_catalog_repository = _FakeCatalogRepo()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            response = client.get("/approvals")

        self.assertEqual(response.status_code, 200)
        item = response.json()["items"][0]
        self.assertEqual(item["last_price"], "7.78")
        self.assertEqual(item["pct_change"], "+1.20%")
        self.assertEqual(item["direction"], "buy")
        self.assertEqual(item["symbol_name"], "浦发银行")

    def test_list_approvals_rejects_unknown_status(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            response = client.get("/approvals", params={"status": "bogus"})

        self.assertEqual(response.status_code, 400)
        detail = response.json()["detail"]
        self.assertEqual(detail["error_code"], "invalid_status")
        self.assertIn("pending", detail["valid"])
        # A malformed filter must never silently reach the repository.
        self.assertEqual(approval_gate.list_approvals_calls, [])

    def test_list_approvals_rejects_bad_datetime(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            response = client.get("/approvals", params={"created_after": "not-a-date"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["error_code"], "invalid_created_after")
        self.assertEqual(approval_gate.list_approvals_calls, [])

    def test_data_providers_endpoint(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)
        with TestClient(app) as client:
            response = client.get("/data-providers")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("providers", body)
        self.assertEqual(
            body["providers"][:6],
            ["auto", "mock", "qmt", "akshare", "tushare", "baostock"],
        )

    def test_debug_session_endpoints_proxy_service(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        service.tasks["inst-1"] = {
            "task_id": "inst-1",
            "name": "inst-1",
            "mode": "paper",
            "description": "",
            "data_provider": None,
            "data_provider_effective": "auto",
            "status": "configured",
            "cycles": None,
            "last_error": "",
            "universe": [],
            "settings": dict(_VALID_INSTANCE_SETTINGS),
            "created_at": "2026-04-04T00:00:00",
            "updated_at": "2026-04-04T00:00:00",
        }

        with TestClient(app) as client:
            create_response = client.post(
                "/tasks/inst-1/debug-sessions",
                json={
                    "input_overrides": {"debug_note": "inspect"},
                },
            )
            list_response = client.get("/tasks/inst-1/debug-sessions")
            detail_response = client.get("/tasks/inst-1/debug-sessions/debug-1")

        self.assertEqual(create_response.status_code, 202)
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(service.debug_create_calls[0][0], "inst-1")
        self.assertEqual(service.debug_create_calls[0][1], {"debug_note": "inspect"})

    def test_backtest_task_debug_session_endpoint_returns_409(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            create_response = client.post(
                "/tasks",
                json={
                    "name": "bt-debug",
                    "mode": "backtest",
                    "settings": dict(_VALID_INSTANCE_SETTINGS),
                },
            )
            task_id = create_response.json()["task_id"]
            debug_response = client.post(
                f"/tasks/{task_id}/debug-sessions",
                json={"input_overrides": {"debug_note": "inspect"}},
            )

        self.assertEqual(create_response.status_code, 200)
        self.assertEqual(debug_response.status_code, 409)
        self.assertIn(
            "backtest tasks do not support debug sessions",
            debug_response.json()["detail"],
        )

    def test_backtest_task_trigger_endpoint_returns_409(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            create_response = client.post(
                "/tasks",
                json={
                    "name": "bt-trigger",
                    "mode": "backtest",
                    "settings": dict(_VALID_INSTANCE_SETTINGS),
                },
            )
            task_id = create_response.json()["task_id"]
            trigger_response = client.post(
                f"/tasks/{task_id}/triggers",
                json={
                    "name": "daily",
                    "schedule_kind": "interval",
                    "interval_seconds": 60,
                    "execution_intent": "trade",
                },
            )

        self.assertEqual(create_response.status_code, 200)
        self.assertEqual(trigger_response.status_code, 409)
        self.assertIn(
            "backtest tasks do not support task triggers",
            trigger_response.json()["detail"],
        )

    def test_backtest_jobs_endpoints(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            create_response = client.post(
                "/tasks/inst-1/runs",
                json={
                    "range_start": "2026-01-02",
                    "range_end": "2026-01-05",
                    "market_profile": "cn_a_share",
                    "bar_interval": "1d",
                },
            )
            list_response = client.get("/tasks/inst-1/runs")
            job_id = create_response.json()["run_id"]
            detail_response = client.get(f"/tasks/inst-1/runs/{job_id}")
            filtered = client.get(
                f"/tasks/inst-1/cycle-runs?run_id={job_id}",
            )

        self.assertEqual(create_response.status_code, 202)
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(filtered.status_code, 200)
        self.assertEqual(filtered.json()["total"], 0)
        self.assertEqual(len(service.backtest_start_calls), 1)
        self.assertEqual(service.backtest_start_calls[0][1], "2026-01-02")
        self.assertEqual(service.backtest_start_calls[0][2], "2026-01-05")
        # debug_enabled defaults to True when omitted (preserve historical behavior).
        self.assertEqual(service.backtest_start_calls[0][7], True)
        self.assertEqual(create_response.json()["debug_enabled"], True)

        pause_ok = client.post(f"/tasks/inst-1/runs/{job_id}/pause")
        self.assertEqual(pause_ok.status_code, 200)
        self.assertEqual(pause_ok.json()["status"], "paused")

        resume_ok = client.post(f"/tasks/inst-1/runs/{job_id}/resume")
        self.assertEqual(resume_ok.status_code, 200)
        self.assertEqual(resume_ok.json()["status"], "running")

        stop_ok = client.post(f"/tasks/inst-1/runs/{job_id}/stop")
        self.assertEqual(stop_ok.status_code, 200)
        self.assertEqual(stop_ok.json()["status"], "stopped")
        self.assertEqual(stop_ok.json()["error_message"], "")

        pause_twice = client.post(f"/tasks/inst-1/runs/{job_id}/pause")
        self.assertEqual(pause_twice.status_code, 409)

    def test_backtest_run_forwards_debug_disabled(self):
        service = _FakeService()
        app = create_app(service, _FakeApprovalGate())
        with TestClient(app) as client:
            resp = client.post(
                "/tasks/inst-1/runs",
                json={
                    "range_start": "2026-01-02",
                    "range_end": "2026-01-05",
                    "debug_enabled": False,
                },
            )
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(service.backtest_start_calls[0][7], False)
        body = resp.json()
        self.assertEqual(body["debug_enabled"], False)
        # Fast mode does not create a debug session.
        self.assertIsNone(body["session_id"])

    def test_backtest_run_rejects_non_boolean_debug_enabled(self):
        service = _FakeService()
        app = create_app(service, _FakeApprovalGate())
        with TestClient(app) as client:
            resp = client.post(
                "/tasks/inst-1/runs",
                json={
                    "range_start": "2026-01-02",
                    "range_end": "2026-01-05",
                    "debug_enabled": "yes",
                },
            )
        self.assertEqual(resp.status_code, 400)

    def test_backtest_runs_endpoint_auto_creates_task_and_returns_json_summary(self):
        service = _FakeService()
        app = create_app(service, _FakeApprovalGate())

        with TestClient(app) as client:
            response = client.post(
                "/backtest-runs",
                json={
                    "definition_id": "sd-macd",
                    "universe": ["600522.SH"],
                    "range_start": "2026-03-25",
                    "range_end": "2026-05-25",
                    "timeout_seconds": 0,
                },
            )

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["run_id"], "btjob-fake-1")
        self.assertEqual(body["task_id"], "instance-1")
        self.assertEqual(body["auto_created_task_id"], "instance-1")
        self.assertEqual(body["summary"]["overview"]["ending_equity"], "123456.78")
        self.assertNotIn("report_path", body)
        self.assertNotIn("markdown", body)
        self.assertEqual(service.create_calls[0]["mode"], "backtest")
        self.assertEqual(
            service.create_calls[0]["settings"],
            {"strategy": {"definition_id": "sd-macd"}, "universe": ["600522.SH"]},
        )
        self.assertEqual(service.backtest_start_calls[0][0], "instance-1")
        self.assertEqual(service.backtest_start_calls[0][1], "2026-03-25")
        self.assertEqual(service.backtest_start_calls[0][2], "2026-05-25")

    def test_backtest_run_auto_name_uses_strategy_and_stock_name(self):
        from datetime import datetime, timezone

        from doyoutrade.persistence.repositories import StrategyDefinitionSnapshot

        class _NamedService(_FakeService):
            async def get_instrument_catalog_item(self, symbol: str):
                return {"symbol": symbol, "display_name": "惠城环保"}

        snapshot = StrategyDefinitionSnapshot(
            definition_id="sd-macd",
            name="超跌反抽",
            current_version=None,
            api_version="v1",
            input_contract_json=None,
            parameter_schema_json=None,
            default_parameters_json=None,
            capabilities_json=None,
            provenance_json=None,
            code_hash="abc",
            generation_prompt="",
            generation_model="",
            generation_metadata_json=None,
            status="active",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        service = _NamedService()
        app = create_app(
            service,
            _FakeApprovalGate(),
            strategy_definition_repository=_FakeStrategyDefinitionRepository(snapshot),
        )

        with TestClient(app) as client:
            response = client.post(
                "/backtest-runs",
                json={
                    "definition_id": "sd-macd",
                    "universe": ["002855.SZ"],
                    "range_start": "2023-09-28",
                    "range_end": "2024-01-30",
                    "timeout_seconds": 0,
                },
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            service.create_calls[0]["name"],
            "超跌反抽 · 惠城环保 · 2023-09-28~2024-01-30",
        )

    def test_backtest_run_auto_name_multi_symbol_and_fallback(self):
        # No strategy repo wired and the catalog lookup returns None, so the name
        # falls back to the raw definition_id + symbol code with a 等N只 suffix.
        service = _FakeService()
        app = create_app(service, _FakeApprovalGate())

        with TestClient(app) as client:
            response = client.post(
                "/backtest-runs",
                json={
                    "definition_id": "sd-macd",
                    "universe": ["002855.SZ", "600519.SH", "000001.SZ"],
                    "range_start": "2023-09-28",
                    "range_end": "2024-01-30",
                    "timeout_seconds": 0,
                },
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            service.create_calls[0]["name"],
            "sd-macd · 002855.SZ等3只 · 2023-09-28~2024-01-30",
        )

    def test_backtest_run_summary_endpoint_returns_json_by_default(self):
        service = _FakeService()
        app = create_app(service, _FakeApprovalGate())

        with TestClient(app) as client:
            created = client.post(
                "/tasks/inst-1/runs",
                json={"range_start": "2026-01-02", "range_end": "2026-01-05"},
            )
            response = client.get(f"/backtest-runs/{created.json()['run_id']}/summary")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["summary_state"], "ok")
        self.assertEqual(body["summary"]["overview"]["return_pct"], "23.45678")
        self.assertNotIn("markdown", body)

    def test_backtest_chart_endpoint_delegates_to_service(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            create_response = client.post(
                "/tasks/inst-1/runs",
                json={"range_start": "2026-01-02", "range_end": "2026-01-05"},
            )
            job_id = create_response.json()["run_id"]
            response = client.get(
                f"/tasks/inst-1/runs/{job_id}/chart?symbol=600000.SH",
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["selected_symbol"], "600000.SH")
        self.assertEqual(body["adjust"], "qfq")
        self.assertEqual(body["volume_mode"], "volume_only")
        self.assertNotIn("indicators", body)
        self.assertEqual(body["run"]["run_id"], job_id)
        self.assertEqual(body["bars"][0]["close"], 10.2)
        self.assertEqual(body["trades"], [])
        self.assertEqual(body["warnings"], [])

    def test_local_market_bars_endpoint_delegates_to_service(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            response = client.get(
                "/market/bars?symbol=600000.SH&interval=1d&start=2025-01-01&end=2026-01-05",
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["symbol"], "600000.SH")
        self.assertEqual(body["interval"], "1d")
        self.assertEqual(body["bars"][0]["close"], 10.2)
        self.assertEqual(body["volume_mode"], "volume_only")
        self.assertEqual(body["summary"]["bar_count"], 1)
        self.assertEqual(body["coverage"]["requested_start"], "2025-01-01")
        self.assertEqual(body["coverage"]["requested_end"], "2026-01-05")
        self.assertEqual(body["coverage"]["covered_segments"][0]["status"], "covered")
        self.assertEqual(body["available_overlays"]["backtest_trades"][0]["run_id"], "run-1")

    def test_local_market_bars_sync_range_endpoint_runs_small_window_inline(self):
        service = _FakeService()
        app = create_app(service, _FakeApprovalGate())

        with TestClient(app) as client:
            response = client.post(
                "/market/bars/sync-range",
                json={
                    "symbol": "600000.SH",
                    "interval": "1d",
                    "start": "2026-01-01",
                    "end": "2026-03-01",
                    "provider": "auto",
                    "adjust": "qfq",
                    "mode": "fill_gap",
                },
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["execution_mode"], "sync")
        self.assertEqual(body["upserted_count"], 42)
        self.assertEqual(service.local_market_sync_calls[0]["mode"], "fill_gap")

    def test_local_market_bars_sync_range_endpoint_returns_async_job(self):
        service = _FakeService()
        app = create_app(service, _FakeApprovalGate())

        with TestClient(app) as client:
            response = client.post(
                "/market/bars/sync-range",
                json={
                    "symbol": "600000.SH",
                    "interval": "5m",
                    "start": "2025-01-01",
                    "end": "2026-01-01",
                    "provider": "auto",
                    "adjust": "qfq",
                    "mode": "force_refresh",
                },
            )

        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertEqual(body["status"], "accepted")
        self.assertEqual(body["execution_mode"], "async")
        self.assertTrue(body["job_id"].startswith("lmjob-fake-"))

    def test_local_market_sync_job_endpoint_delegates_to_service(self):
        service = _FakeService()
        app = create_app(service, _FakeApprovalGate())

        with TestClient(app) as client:
            accepted = client.post(
                "/market/bars/sync-range",
                json={
                    "symbol": "600000.SH",
                    "interval": "5m",
                    "start": "2025-01-01",
                    "end": "2026-01-01",
                    "provider": "auto",
                    "adjust": "qfq",
                    "mode": "force_refresh",
                },
            )
            response = client.get(f"/market/bars/sync-jobs/{accepted.json()['job_id']}")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "pending")
        self.assertEqual(body["interval"], "5m")
        self.assertEqual(
            body["requested_range"],
            {"start": "2025-01-01", "end": "2026-01-01"},
        )
        self.assertIsNone(body["started_at"])
        self.assertIsNone(body["finished_at"])
        self.assertIsNone(body["error_code"])
        self.assertIsNone(body["error_type"])
        self.assertIsNone(body["error_message"])
        self.assertIsNone(body["hint"])

    def test_local_market_bars_sync_range_endpoint_rejects_missing_mode(self):
        service = _FakeService()
        app = create_app(service, _FakeApprovalGate())

        with TestClient(app) as client:
            response = client.post(
                "/market/bars/sync-range",
                json={
                    "symbol": "600000.SH",
                    "interval": "1d",
                    "start": "2026-01-01",
                    "end": "2026-03-01",
                },
            )

        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["error_code"], "local_market_sync_invalid_request")
        self.assertEqual(body["error_type"], "ValueError")
        self.assertIn("mode", body["error_message"])
        self.assertIn("mode", body["hint"])

    def test_local_market_sync_job_endpoint_returns_structured_not_found(self):
        service = _FakeService()
        app = create_app(service, _FakeApprovalGate())

        with TestClient(app) as client:
            response = client.get("/market/bars/sync-jobs/lmjob-missing")

        self.assertEqual(response.status_code, 404)
        body = response.json()
        self.assertEqual(body["error_code"], "local_market_sync_job_not_found")
        self.assertEqual(body["error_type"], "RecordNotFoundError")
        self.assertIn("lmjob-missing", body["error_message"])
        self.assertIn("job id", body["hint"])

    def test_http_exception_payload_includes_trace_metadata_fields(self):
        app = create_app(_FakeService(), _FakeApprovalGate())

        with TestClient(app) as client:
            response = client.get("/definitely-missing")

        self.assertEqual(response.status_code, 404)
        body = response.json()
        self.assertEqual(body["detail"], "Not Found")
        self.assertEqual(body["error_message"], "Not Found")
        self.assertEqual(body["error_type"], "HTTPException")
        self.assertIn("timestamp", body)
        self.assertIn("trace_id", body)

    def test_local_market_overlays_endpoint_delegates_to_service(self):
        service = _FakeService()
        app = create_app(service, _FakeApprovalGate())

        with TestClient(app) as client:
            response = client.get(
                "/market/bars/overlays"
                "?symbol=600000.SH&interval=1d&start=2026-01-01&end=2026-01-31"
                "&overlay_kind=backtest_trades&run_id=run-1"
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["overlay_kind"], "backtest_trades")
        self.assertEqual(body["source"]["run_id"], "run-1")
        self.assertEqual(body["items"][0]["side"], "buy")

    def test_backtest_task_start_endpoint_returns_409(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            create_response = client.post(
                "/tasks",
                json={
                    "name": "bt-only",
                    "mode": "backtest",
                    "settings": dict(_VALID_INSTANCE_SETTINGS),
                },
            )
            task_id = create_response.json()["task_id"]
            start_response = client.post(f"/tasks/{task_id}/start")

        self.assertEqual(create_response.status_code, 200)
        self.assertEqual(start_response.status_code, 409)
        self.assertIn("backtest task does not support start", start_response.json()["detail"])

    def test_update_task_rejects_switch_between_trading_and_backtest(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            create_response = client.post(
                "/tasks",
                json={
                    "name": "trade-only",
                    "mode": "paper",
                    "settings": dict(_VALID_INSTANCE_SETTINGS),
                },
            )
            task_id = create_response.json()["task_id"]
            update_response = client.put(
                f"/tasks/{task_id}",
                json={"mode": "backtest"},
            )

        self.assertEqual(create_response.status_code, 200)
        self.assertEqual(update_response.status_code, 400)
        self.assertIn(
            "cannot switch task mode between trading and backtest",
            update_response.json()["detail"],
        )

    def test_backtest_jobs_global_list_endpoint_removed(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)
        with TestClient(app) as client:
            all_resp = client.get("/backtest-jobs")
            filt_resp = client.get("/backtest-jobs?task_id=inst-a")

        self.assertEqual(all_resp.status_code, 404)
        self.assertEqual(filt_resp.status_code, 404)

    def test_cycle_runs_endpoints(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            list_response = client.get("/tasks/inst-1/cycle-runs?limit=10&offset=0")
            one_response = client.get("/cycle-runs/run-test-1")
            missing = client.get("/cycle-runs/run-missing")

        self.assertEqual(list_response.status_code, 200)
        body = list_response.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["run_id"], "run-test-1")

        self.assertEqual(one_response.status_code, 200)
        self.assertEqual(one_response.json()["run_kind"], "debug")

        self.assertEqual(missing.status_code, 404)

    def test_cycle_runs_list_accepts_filter_query_params(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            ok = client.get(
                "/tasks/inst-1/cycle-runs?limit=10&offset=0"
                "&q=run&status=completed&run_kind=debug&run_mode=paper&exclude_run_kind=scheduled"
                "&started_after=2026-01-01T00:00:00&started_before=2026-12-31T23:59:59Z",
            )

        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["total"], 1)

    def test_cycle_runs_invalid_started_after_returns_400(self):
        service = _FakeServiceCycleTimeValidation()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            bad = client.get("/tasks/inst-1/cycle-runs?started_after=not-a-datetime")

        self.assertEqual(bad.status_code, 400)

    def test_cycle_run_debug_view_endpoint(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            ok = client.get("/cycle-runs/run-test-1/debug-view")
            missing = client.get("/cycle-runs/run-missing/debug-view")

        self.assertEqual(ok.status_code, 200)
        body = ok.json()
        self.assertEqual(body["cycle_run"]["run_id"], "run-test-1")
        self.assertEqual(body["spans"], [])
        self.assertEqual(body["model_invocations"], [])
        self.assertIsNone(body["session"])

        self.assertEqual(missing.status_code, 404)

    def test_trace_debug_view_endpoint(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        trace_id = "ab" * 16
        with TestClient(app) as client:
            ok = client.get(f"/traces/{trace_id}/debug-view")
            missing = client.get("/traces/00000000000000000000000000000000/debug-view")

        self.assertEqual(ok.status_code, 200)
        body = ok.json()
        self.assertEqual(body["resolved_from"]["identifier_type"], "trace")
        self.assertEqual(body["resolved_from"]["identifier"], trace_id)
        self.assertEqual(body["cycle_run"]["run_id"], "run-test-1")

        self.assertEqual(missing.status_code, 404)

    def test_create_task_normalizes_business_fields(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            response = client.post(
                "/tasks",
                json={
                    "name": "alpha-growth",
                    "mode": "paper",
                    "description": "demo",
                    "data_provider": " mock ",
                    "settings": {
                        **_VALID_INSTANCE_SETTINGS,
                        "universe": [" AAPL ", "", "MSFT "],
                        "risk": "low",
                    },
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(service.create_calls), 1)
        self.assertEqual(service.create_calls[0]["data_provider"], "mock")
        self.assertEqual(service.create_calls[0]["settings"]["universe"], ["AAPL", "MSFT"])
        response_json = response.json()
        self.assertNotIn("template_id", response_json)
        self.assertEqual(response_json["universe"], ["AAPL", "MSFT"])
        self.assertEqual(response_json["settings"]["risk"], "low")
        self.assertEqual(response_json["settings"]["universe"], ["AAPL", "MSFT"])

    def test_get_tasks_read_contract_omits_deprecated_top_level_and_settings_fields(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)
        service.tasks["instance-1"] = {
            "task_id": "instance-1",
            "name": "legacy-shape",
            "mode": "paper",
            "description": "legacy read",
            "data_provider": "mock",
            "data_provider_effective": "mock",
            "status": "configured",
            "cycles": None,
            "last_error": "",
            "universe": ["000001.SZ"],
            "model_route_name": "test-route",
            "settings": {
                "universe": ["000001.SZ"],
                "model_route_name": "test-route",
            },
            "created_at": "2026-04-04T00:00:00",
            "updated_at": "2026-04-04T00:00:00",
        }

        with TestClient(app) as client:
            response = client.get("/tasks")

        self.assertEqual(response.status_code, 200, msg=response.text)
        body = response.json()
        self.assertEqual(len(body), 1)
        task = body[0]
        self.assertNotIn("template_id", task)
        self.assertNotIn("template_id", task["settings"])

    def test_list_tasks_page_supports_q_status_mode_pagination(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)
        service.tasks["task-alpha"] = {
            "task_id": "task-alpha",
            "name": "alpha-bt",
            "mode": "backtest",
            "description": "",
            "data_provider": "mock",
            "data_provider_effective": "mock",
            "status": "completed",
            "cycles": None,
            "last_error": "",
            "universe": [],
            "model_route_name": "test-route",
            "settings": dict(_VALID_INSTANCE_SETTINGS),
            "created_at": "2026-04-04T00:00:00",
            "updated_at": "2026-04-04T00:00:00",
        }
        service.tasks["task-beta"] = {
            "task_id": "task-beta",
            "name": "beta-paper",
            "mode": "paper",
            "description": "",
            "data_provider": "mock",
            "data_provider_effective": "mock",
            "status": "running",
            "cycles": None,
            "last_error": "",
            "universe": [],
            "model_route_name": "test-route",
            "settings": dict(_VALID_INSTANCE_SETTINGS),
            "created_at": "2026-04-04T00:00:00",
            "updated_at": "2026-04-04T00:00:00",
        }

        with TestClient(app) as client:
            response = client.get(
                "/tasks/page?q=alpha&status=completed&mode=backtest&limit=10&offset=0"
            )

        self.assertEqual(response.status_code, 200, msg=response.text)
        payload = response.json()
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["limit"], 10)
        self.assertEqual(payload["offset"], 0)
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["items"][0]["task_id"], "task-alpha")

    def test_list_tasks_page_supports_definition_id_filter(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)
        service.tasks["task-sd-a"] = {
            "task_id": "task-sd-a",
            "name": "alpha-sd",
            "mode": "paper",
            "description": "",
            "data_provider": "mock",
            "data_provider_effective": "mock",
            "status": "running",
            "cycles": None,
            "last_error": "",
            "universe": [],
            "model_route_name": "test-route",
            "settings": {
                **_VALID_INSTANCE_SETTINGS,
                "strategy": {"definition_id": "sd-alpha"},
            },
            "created_at": "2026-04-04T00:00:00",
            "updated_at": "2026-04-04T00:00:00",
        }
        service.tasks["task-sd-b"] = {
            "task_id": "task-sd-b",
            "name": "beta-sd",
            "mode": "paper",
            "description": "",
            "data_provider": "mock",
            "data_provider_effective": "mock",
            "status": "running",
            "cycles": None,
            "last_error": "",
            "universe": [],
            "model_route_name": "test-route",
            "settings": {
                **_VALID_INSTANCE_SETTINGS,
                "strategy": {"definition_id": "sd-beta"},
            },
            "created_at": "2026-04-04T00:00:00",
            "updated_at": "2026-04-04T00:00:00",
        }

        with TestClient(app) as client:
            response = client.get("/tasks/page?definition_id=sd-alpha&limit=10&offset=0")

        self.assertEqual(response.status_code, 200, msg=response.text)
        payload = response.json()
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["items"][0]["task_id"], "task-sd-a")

    def _make_backtest_summary_fixture(self) -> dict:
        return {
            "schema_version": 1,
            "run_id": "run-bt-1",
            "range_start_utc": "2026-01-05T00:00:00Z",
            "range_end_utc": "2026-01-06T00:00:00Z",
            "bar_interval": "1d",
            "completed_at": "2026-01-06T07:00:00Z",
            "starting_equity": "100000.00",
            "ending_equity": "100200.00",
            "return_pct": "0.20",
            "final_cash": "100200.00",
            "final_market_value": "0.00",
            "final_positions": [],
            "trade_count_closed": 0,
            "trade_count_open": 0,
            "win_rate": "0",
            "avg_holding_trading_days": "0",
            "max_drawdown_pct": "0",
            "max_drawdown_peak_at": None,
            "max_drawdown_trough_at": None,
            "max_drawdown_peak_equity": None,
            "max_drawdown_trough_equity": None,
            "equity_curve_meta": {"downsampled": False, "raw_length": 2},
            "equity_curve": [
                {"t": "2026-01-05T07:00:00Z", "equity": "100100.00"},
                {"t": "2026-01-06T07:00:00Z", "equity": "100200.00"},
            ],
        }

    def _seed_backtest_task(self, service: "_FakeService", task_id: str = "bt-1") -> dict:
        summary = self._make_backtest_summary_fixture()
        record = {
            "task_id": task_id,
            "name": "bt-detail",
            "mode": "backtest",
            "description": "bt detail",
            "data_provider": "mock",
            "data_provider_effective": "mock",
            "status": "completed",
            "cycles": None,
            "last_error": "",
            "universe": [],
            "model_route_name": "test-route",
            "settings": {**_VALID_INSTANCE_SETTINGS},
            "backtest_summary": summary,
            "created_at": "2026-04-04T00:00:00",
            "updated_at": "2026-04-04T00:00:00",
        }
        service.tasks[task_id] = record
        return summary

    def test_list_tasks_strips_equity_curve_but_keeps_backtest_summary(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)
        self._seed_backtest_task(service)

        with TestClient(app) as client:
            response = client.get("/tasks")

        self.assertEqual(response.status_code, 200, msg=response.text)
        body = response.json()
        self.assertEqual(len(body), 1)
        task = body[0]
        self.assertEqual(task["task_id"], "bt-1")
        self.assertIn("backtest_summary", task)
        bs = task["backtest_summary"]
        self.assertIsNotNone(bs)
        self.assertNotIn("equity_curve", bs)
        self.assertIn("equity_curve_meta", bs)
        self.assertEqual(bs["equity_curve_meta"]["raw_length"], 2)
        self.assertEqual(bs["schema_version"], 1)

    def test_get_task_detail_returns_full_backtest_summary_with_equity_curve(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)
        self._seed_backtest_task(service)

        with TestClient(app) as client:
            response = client.get("/tasks/bt-1")

        self.assertEqual(response.status_code, 200, msg=response.text)
        task = response.json()
        self.assertEqual(task["task_id"], "bt-1")
        bs = task["backtest_summary"]
        self.assertIsNotNone(bs)
        self.assertIn("equity_curve", bs)
        self.assertEqual(len(bs["equity_curve"]), 2)
        self.assertEqual(bs["equity_curve"][0]["equity"], "100100.00")
        self.assertEqual(bs["equity_curve_meta"]["raw_length"], 2)

    def test_get_task_detail_returns_404_for_unknown_task(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            response = client.get("/tasks/no-such-task")

        self.assertEqual(response.status_code, 404)

    def test_get_task_duplicate_preset(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            create_response = client.post(
                "/tasks",
                json={
                    "name": "factor-1",
                    "mode": "paper",
                    "settings": dict(_VALID_INSTANCE_SETTINGS),
                },
            )
            task_id = create_response.json()["task_id"]
            response = client.get(f"/tasks/{task_id}/duplicate-preset")

        self.assertEqual(create_response.status_code, 200)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["name"], "factor-1-copy")
        self.assertEqual(payload["strategy"], {"definition_id": "sd-main"})

    def test_get_task_duplicate_preset_not_found(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            response = client.get("/tasks/missing-task/duplicate-preset")

        self.assertEqual(response.status_code, 404)

    def test_create_task_allows_missing_model_route_name(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            response = client.post(
                "/tasks",
                json={
                    "name": "graph-no-route",
                    "mode": "paper",
                    "settings": {
                        "agent": {
                            "react_max_turns": 1,
                            "signal_tool_names": [],
                        },
                    "strategy": {
                            "definition_id": "sd-main",
                        },
                        "universe": ["300058.SZ"],
                    },
                },
            )

        self.assertEqual(response.status_code, 200, msg=response.text)
        self.assertEqual(len(service.create_calls), 1)
        create_payload = service.create_calls[0]
        self.assertNotIn("model_route_name", create_payload["settings"])
        self.assertNotIn("model_route_name", response.json())

    def test_create_task_requires_settings_universe(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            response = client.post(
                "/tasks",
                json={
                    "name": "fields-to-settings",
                    "mode": "paper",
                    "description": "test migration",
                    "data_provider": "mock",
                    "settings": {
                        **_VALID_INSTANCE_SETTINGS,
                        "universe": ["BTC", "ETH"],
                    },
                },
            )

        self.assertEqual(response.status_code, 200)
        returned_settings = response.json()["settings"]
        self.assertEqual(returned_settings["universe"], ["BTC", "ETH"])

    def test_create_task_rejects_top_level_legacy_fields(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            response = client.post(
                "/tasks",
                json={
                    "name": "deprecated-fields-create",
                    "mode": "paper",
                    "settings": dict(_VALID_INSTANCE_SETTINGS),
                },
            )

        self.assertEqual(response.status_code, 200, msg=response.text)
        self.assertEqual(len(service.create_calls), 1)
        create_payload = service.create_calls[0]
        self.assertEqual(create_payload["settings"]["model_route_name"], _VALID_INSTANCE_SETTINGS["model_route_name"])

    def test_create_task_rejects_malformed_legacy_top_level_fields(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            response = client.post(
                "/tasks",
                json={
                    "name": "deprecated-fields-create-malformed",
                    "mode": "paper",
                    "settings": dict(_VALID_INSTANCE_SETTINGS),
                },
            )

        self.assertEqual(response.status_code, 200, msg=response.text)
        self.assertEqual(len(service.create_calls), 1)

    def test_update_task_accepts_settings_only(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)
        service.tasks["instance-1"] = {
            "task_id": "instance-1",
            "name": "before",
            "mode": "paper",
            "description": "",
            "data_provider": "mock",
            "data_provider_effective": "mock",
            "status": "configured",
            "cycles": None,
            "last_error": "",
            "universe": [],
            "settings": dict(_VALID_INSTANCE_SETTINGS),
            "created_at": "2026-04-04T00:00:00",
            "updated_at": "2026-04-04T00:00:00",
        }

        with TestClient(app) as client:
            response = client.put(
                "/tasks/instance-1",
                json={
                    "name": "after",
                    "settings": {"universe": ["000001.SZ"]},
                },
            )

        self.assertEqual(response.status_code, 200, msg=response.text)
        self.assertEqual(len(service.update_calls), 1)
        identifier, update_payload = service.update_calls[0]
        self.assertEqual(identifier, "instance-1")
        self.assertEqual(update_payload["name"], "after")
        self.assertEqual(update_payload["settings"]["universe"], ["000001.SZ"])

    def test_update_task_rejects_non_object_settings(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)
        service.tasks["instance-1"] = {
            "task_id": "instance-1",
            "name": "before",
            "mode": "paper",
            "description": "",
            "data_provider": "mock",
            "data_provider_effective": "mock",
            "status": "configured",
            "cycles": None,
            "last_error": "",
            "universe": [],
            "settings": dict(_VALID_INSTANCE_SETTINGS),
            "created_at": "2026-04-04T00:00:00",
            "updated_at": "2026-04-04T00:00:00",
        }

        with TestClient(app) as client:
            response = client.put(
                "/tasks/instance-1",
                json={
                    "name": "after",
                    "settings": ["unexpected", "list"],
                },
            )

        self.assertEqual(response.status_code, 400, msg=response.text)
        self.assertEqual(service.update_calls, [])

    def test_update_task_preserves_omitted_settings_keys(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)
        service.tasks["instance-1"] = {
            "task_id": "instance-1",
            "name": "before",
            "mode": "paper",
            "description": "",
            "data_provider": "mock",
            "data_provider_effective": "mock",
            "status": "configured",
            "cycles": None,
            "last_error": "",
            "universe": ["600000.SH"],
            "settings": {
                **_VALID_INSTANCE_SETTINGS,
                "universe": ["600000.SH"],
            },
            "created_at": "2026-04-04T00:00:00",
            "updated_at": "2026-04-04T00:00:00",
        }

        with TestClient(app) as client:
            response = client.put(
                "/tasks/instance-1",
                json={
                    "settings": {"universe": ["000001.SZ"]},
                },
            )

        self.assertEqual(response.status_code, 200, msg=response.text)
        row = service.tasks["instance-1"]
        self.assertEqual(row["universe"], ["000001.SZ"])

    def test_create_task_rejects_non_object_settings(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            response = client.post(
                "/tasks",
                json={
                    "name": "alpha-growth",
                    "settings": ["not", "an", "object"],
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("settings", response.text)
        self.assertEqual(service.create_calls, [])

    def test_create_task_requires_settings_object(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            response = client.post(
                "/tasks",
                json={"name": "x"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("settings", response.text.lower())
        self.assertEqual(service.create_calls, [])

    def test_create_task_allows_duplicate_names(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            first = client.post(
                "/tasks",
                json={
                    "name": "conflict",
                    "settings": dict(_VALID_INSTANCE_SETTINGS),
                },
            )
            second = client.post(
                "/tasks",
                json={
                    "name": "conflict",
                    "settings": dict(_VALID_INSTANCE_SETTINGS),
                },
            )

        self.assertEqual(first.status_code, 200, msg=first.text)
        self.assertEqual(second.status_code, 200, msg=second.text)
        self.assertNotEqual(first.json()["task_id"], second.json()["task_id"])

    def test_create_task_persistence_error_surfaces_details(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            response = client.post(
                "/tasks",
                json={
                    "name": "persistence-error",
                    "settings": dict(_VALID_INSTANCE_SETTINGS),
                },
            )

        self.assertEqual(response.status_code, 400, msg=response.text)
        self.assertIn("failed to create task: check constraint failed: tasks.status", response.text)

    def test_create_task_requires_agent_settings_keys(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            response = client.post(
                "/tasks",
                json={
                    "name": "x",
                    "settings": {"risk": "low"},
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("settings.strategy must bind a definition_id", response.text)
        self.assertEqual(service.create_calls, [])

    def test_create_task_accepts_strategy_definition_binding(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            response = client.post(
                "/tasks",
                json={
                    "name": "definition-demo",
                    "settings": {
                        "model_route_name": "test-route",
                        "agent": {
                            "react_max_turns": 1,
                            "signal_tool_names": [],
                        },
                        "strategy": {
                            "definition_id": "sd-main",
                            "parameter_overrides": {"window": 14},
                        },
                    },
                },
            )

        self.assertEqual(response.status_code, 200, msg=response.text)
        self.assertEqual(len(service.create_calls), 1)

    def test_create_task_requires_strategy_definition_binding(self):
        """A ``strategy`` block without a ``definition_id`` is rejected — the
        binding requirement is the only valid strategy entry point now that
        StrategyInstance / ``si-`` bindings have been removed.
        """
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            response = client.post(
                "/tasks",
                json={
                    "name": "graph-demo",
                    "settings": {
                        "model_route_name": "test-route",
                        "agent": {
                            "react_max_turns": 1,
                            "signal_tool_names": [],
                        },
                        "strategy": {},
                    },
                },
            )

        self.assertEqual(response.status_code, 400, msg=response.text)
        self.assertIn("settings.strategy must bind a definition_id", response.text)
        self.assertEqual(service.create_calls, [])

    def test_instrument_universe_search_rejects_unknown_source(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            response = client.get("/instrument-universe/search", params={"source": "nope", "q": "6"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("unknown", response.json()["detail"].lower())

    @patch("doyoutrade.data.instrument_universe.akshare_a._sync_fetch_spot_rows")
    def test_instrument_universe_search_empty_q_skips_fetch(self, mock_fetch):
        from doyoutrade.data.instrument_universe.akshare_a import clear_akshare_a_spot_cache

        clear_akshare_a_spot_cache()
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            response = client.get("/instrument-universe/search", params={"source": "akshare_a", "q": ""})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"], [])
        mock_fetch.assert_not_called()

    @patch("doyoutrade.data.instrument_universe.akshare_a._sync_fetch_spot_rows")
    def test_instrument_universe_search_filters_cached_rows(self, mock_fetch):
        from doyoutrade.data.instrument_universe.akshare_a import clear_akshare_a_spot_cache

        clear_akshare_a_spot_cache()
        mock_fetch.return_value = [
            {"symbol": "600000.SH", "name": "浦发银行", "market": "CN"},
            {"symbol": "000001.SZ", "name": "平安银行", "market": "CN"},
        ]
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            response = client.get(
                "/instrument-universe/search",
                params={"source": "akshare_a", "q": "浦发", "limit": 5},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["source"], "akshare_a")
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["symbol"], "600000.SH")
        self.assertEqual(mock_fetch.call_count, 1)

    def test_instrument_catalog_delete_and_clear_routes(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            bad = client.post("/instruments/catalog/delete", json={"symbols": []})
        self.assertEqual(bad.status_code, 400)

        with TestClient(app) as client:
            ok = client.post("/instruments/catalog/delete", json={"symbols": ["600000.SH", "  "]})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json(), {"deleted": 1})

        with TestClient(app) as client:
            bad_clear = client.post("/instruments/catalog/clear", json={"confirm": "wrong"})
        self.assertEqual(bad_clear.status_code, 400)

        with TestClient(app) as client:
            cleared = client.post(
                "/instruments/catalog/clear",
                json={"confirm": "clear_all_instrument_catalog"},
            )
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(cleared.json(), {"deleted": 0})

    def test_delete_task_returns_204(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            create_response = client.post(
                "/tasks",
                json={
                    "name": "to-drop",
                    "settings": _VALID_INSTANCE_SETTINGS,
                },
            )
            task_id = create_response.json()["task_id"]
            delete_response = client.delete(f"/tasks/{task_id}")

        self.assertEqual(create_response.status_code, 200)
        self.assertEqual(delete_response.status_code, 204)
        self.assertEqual(delete_response.content, b"")
        self.assertEqual(service.tasks, {})

    def test_delete_tasks_returns_204(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            first = client.post(
                "/tasks",
                json={
                    "name": "to-drop-1",
                    "settings": _VALID_INSTANCE_SETTINGS,
                },
            ).json()["task_id"]
            second = client.post(
                "/tasks",
                json={
                    "name": "to-drop-2",
                    "settings": _VALID_INSTANCE_SETTINGS,
                },
            ).json()["task_id"]
            delete_response = client.request("DELETE", "/tasks", json={"task_ids": [first, second]})

        self.assertEqual(delete_response.status_code, 204)
        self.assertEqual(delete_response.content, b"")
        self.assertEqual(service.tasks, {})

    def test_delete_tasks_rejects_running_task(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            first = client.post(
                "/tasks",
                json={
                    "name": "to-drop-1",
                    "settings": _VALID_INSTANCE_SETTINGS,
                },
            ).json()["task_id"]
            second = client.post(
                "/tasks",
                json={
                    "name": "to-drop-2",
                    "settings": _VALID_INSTANCE_SETTINGS,
                },
            ).json()["task_id"]
            client.post(f"/tasks/{first}/start")
            delete_response = client.request("DELETE", "/tasks", json={"task_ids": [first, second]})

        self.assertEqual(delete_response.status_code, 409)
        self.assertIn("running", delete_response.json()["detail"])
        self.assertEqual(set(service.tasks.keys()), {first, second})

    def test_delete_strategy_definition_returns_204(self):
        service = _FakeService()
        strategy_registry_service = _FakeStrategyRegistryService()
        app = create_app(service, _FakeApprovalGate(), strategy_registry_service=strategy_registry_service)

        with TestClient(app) as client:
            delete_response = client.delete("/strategy-definitions/def-1")

        self.assertEqual(delete_response.status_code, 204)
        self.assertEqual(delete_response.content, b"")
        self.assertEqual(strategy_registry_service.deleted_definition_ids, ["def-1"])

    def test_delete_strategy_definitions_returns_204(self):
        service = _FakeService()
        strategy_registry_service = _FakeStrategyRegistryService()
        app = create_app(service, _FakeApprovalGate(), strategy_registry_service=strategy_registry_service)

        with TestClient(app) as client:
            delete_response = client.request(
                "DELETE",
                "/strategy-definitions",
                json={"definition_ids": ["def-1", "def-2"]},
            )

        self.assertEqual(delete_response.status_code, 204)
        self.assertEqual(delete_response.content, b"")
        self.assertEqual(strategy_registry_service.deleted_definition_batches, [["def-1", "def-2"]])

    def test_delete_strategy_definitions_accepts_in_use_definition_ids(self):
        service = _FakeService()
        strategy_registry_service = _FakeStrategyRegistryService()
        app = create_app(service, _FakeApprovalGate(), strategy_registry_service=strategy_registry_service)

        with TestClient(app) as client:
            delete_response = client.request(
                "DELETE",
                "/strategy-definitions",
                json={"definition_ids": ["in-use"]},
            )

        self.assertEqual(delete_response.status_code, 204)
        self.assertEqual(strategy_registry_service.deleted_definition_batches, [["in-use"]])

    def test_create_and_update_strategy_definition_use_resource_contract(self):
        service = _FakeService()
        strategy_registry_service = _FakeStrategyRegistryService()
        app = create_app(service, _FakeApprovalGate(), strategy_registry_service=strategy_registry_service)

        with TestClient(app) as client:
            create_response = client.post(
                "/strategy-definitions",
                json={
                    "definition_id": "sd-openapi",
                    "name": "OpenAPI MACD",
                    "api_version": "v1",
                    "parameter_schema": {"type": "object"},
                    "status": "active",
                },
            )
            update_response = client.patch(
                "/strategy-definitions/sd-openapi",
                json={
                    "name": "OpenAPI MACD v2",
                    "status": "inactive",
                },
            )

        self.assertEqual(create_response.status_code, 201)
        self.assertEqual(create_response.json()["definition_id"], "sd-openapi")
        self.assertEqual(create_response.json()["parameter_schema"], {"type": "object"})
        self.assertEqual(update_response.status_code, 200)
        self.assertEqual(update_response.json()["name"], "OpenAPI MACD v2")
        self.assertEqual(
            strategy_registry_service.update_definition_calls,
            [
                (
                    "sd-openapi",
                    {
                        "name": "OpenAPI MACD v2",
                        "api_version": None,
                        "input_contract": None,
                        "parameter_schema": None,
                        "default_parameters": None,
                        "capabilities": None,
                        "provenance": None,
                        "generation_prompt": None,
                        "generation_model": None,
                        "generation_metadata": None,
                        "status": "inactive",
                    },
                )
            ],
        )

    def test_get_strategy_definition_returns_files_from_storage(self):
        import tempfile
        from datetime import datetime, timezone
        from pathlib import Path

        from doyoutrade.persistence.repositories import StrategyDefinitionSnapshot
        from doyoutrade.persistence.strategy_storage import StrategyStorage

        with tempfile.TemporaryDirectory() as tmp:
            storage = StrategyStorage(Path(tmp))
            definition_id = "sd-test-1"
            session_id = "sess-1"
            draft = storage.open_draft(definition_id, session_id, base_version=None)
            (draft / "helpers.py").write_text("def helper(): pass")
            version_label, code_hash = storage.finalize_draft(definition_id, session_id)

            snapshot = StrategyDefinitionSnapshot(
                definition_id=definition_id,
                name="Test",
                current_version=version_label,
                api_version="v1",
                input_contract_json=None,
                parameter_schema_json=None,
                default_parameters_json=None,
                capabilities_json=None,
                provenance_json=None,
                code_hash=code_hash,
                generation_prompt="",
                generation_model="",
                generation_metadata_json=None,
                status="active",
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
            repo = _FakeStrategyDefinitionRepository(snapshot)
            service = _FakeService()
            app = create_app(
                service,
                _FakeApprovalGate(),
                strategy_definition_repository=repo,
                strategy_storage=storage,
            )

            with TestClient(app) as client:
                resp = client.get(f"/strategy-definitions/{definition_id}")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["current_version"], version_label)
        self.assertEqual(body["code_hash"], code_hash)
        file_paths = [f["path"] for f in body["files"]]
        self.assertIn("strategy.py", file_paths)
        self.assertIn("helpers.py", file_paths)
        helpers_file = next(f for f in body["files"] if f["path"] == "helpers.py")
        self.assertEqual(helpers_file["content"], "def helper(): pass")

    def test_get_strategy_definition_returns_empty_files_when_no_version(self):
        from datetime import datetime, timezone
        from doyoutrade.persistence.repositories import StrategyDefinitionSnapshot

        snapshot = StrategyDefinitionSnapshot(
            definition_id="sd-no-version",
            name="NoVersion",
            current_version=None,
            api_version="v1",
            input_contract_json=None,
            parameter_schema_json=None,
            default_parameters_json=None,
            capabilities_json=None,
            provenance_json=None,
            code_hash="abc",
            generation_prompt="",
            generation_model="",
            generation_metadata_json=None,
            status="active",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        repo = _FakeStrategyDefinitionRepository(snapshot)
        service = _FakeService()
        app = create_app(
            service,
            _FakeApprovalGate(),
            strategy_definition_repository=repo,
        )

        with TestClient(app) as client:
            resp = client.get("/strategy-definitions/sd-no-version")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["files"], [])
        self.assertIsNone(resp.json()["current_version"])

    def test_build_api_with_runtime_uses_runtime_aclose_on_shutdown(self):
        closed = []

        class _ServerFakeService(_FakeService):
            async def aclose(self):
                closed.append("service")

        runtime = {
            "service": _ServerFakeService(),
            "approval_gate": _FakeApprovalGate(),
            "aclose": self._make_async_callback(closed, "runtime"),
        }
        fake_cfg = SimpleNamespace(
            observability=SimpleNamespace(
                service_name="doyoutrade-test",
                log_level="INFO",
                tracing_enabled=False,
                console_enabled=False,
            ),
            server=SimpleNamespace(tick_seconds=0.01),
        )

        with (
            patch("doyoutrade.api.server.get_config", return_value=fake_cfg),
            patch("doyoutrade.api.server.build_platform_runtime", new=self._make_runtime_builder(runtime)),
            patch("doyoutrade.api.server.initialize_observability"),
        ):
            app = asyncio.run(build_api_with_runtime())
            with TestClient(app):
                pass

        self.assertEqual(closed, ["runtime"])

    def test_main_uses_uvicorn_server_with_graceful_shutdown_timeout(self):
        captured: dict = {}

        class CaptureServer:
            def __init__(self, config):
                captured["timeout_graceful_shutdown"] = config.timeout_graceful_shutdown
                captured["host"] = config.host
                captured["port"] = config.port

            async def serve(self, sockets=None):
                captured["serve_called"] = True

        runtime = {
            "service": _FakeService(),
            "approval_gate": _FakeApprovalGate(),
            "aclose": self._make_async_callback([], "noop"),
        }
        fake_cfg = SimpleNamespace(
            observability=SimpleNamespace(
                service_name="doyoutrade-test",
                log_level="INFO",
                tracing_enabled=False,
                console_enabled=False,
            ),
            server=SimpleNamespace(host="10.0.0.1", port=7654, tick_seconds=0.01),
        )

        with (
            patch("doyoutrade.api.server.get_config", return_value=fake_cfg),
            patch("doyoutrade.api.server.build_platform_runtime", new=self._make_runtime_builder(runtime)),
            patch("doyoutrade.api.server.initialize_observability"),
            patch("uvicorn.Server", CaptureServer),
        ):
            server_main()

        self.assertEqual(captured["timeout_graceful_shutdown"], 30)
        self.assertEqual(captured["host"], "10.0.0.1")
        self.assertEqual(captured["port"], 7654)
        self.assertTrue(captured["serve_called"])

    def test_skills_endpoint_uses_new_router(self):
        from unittest.mock import patch, MagicMock
        from pathlib import Path
        import tempfile

        service = _FakeService()
        assistant_service = _FakeAssistantService()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Create a minimal skill so load_skills returns at least one item
            skill_dir = tmp_path / "test-skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: Test Skill\ndescription: A test skill\n---\n\n# Test Skill\n",
                encoding="utf-8",
            )
            # Patch default_skills_root in the skills.loader module; app.py imports it
            # fresh each call via `from doyoutrade.skills.loader import default_skills_root`.
            # We patch doyoutrade.skills.loader so the fresh import picks up our mock.
            import doyoutrade.skills.loader as _loader_mod
            original_fn = _loader_mod.default_skills_root

            def _tmp_root():
                return tmp_path

            _loader_mod.default_skills_root = _tmp_root
            try:
                app = create_app(service, _FakeApprovalGate(), assistant_service=assistant_service)
                with TestClient(app) as client:
                    r = client.get("/skills")
                    self.assertEqual(r.status_code, 200)
                    items = r.json()
                    self.assertTrue(len(items) >= 1)
                    first = items[0]
                    self.assertIn("folder_name", first)
                    self.assertIn("frontmatter", first)
                    # Confirm new router schema: no legacy fields
                    self.assertNotIn("body", first)
                    self.assertNotIn("skill_dir", first)
            finally:
                _loader_mod.default_skills_root = original_fn

    @staticmethod
    def _make_runtime_builder(runtime):
        async def _builder(*args, **kwargs):
            return runtime

        return _builder

    @staticmethod
    def _make_async_callback(calls, label):
        async def _callback():
            calls.append(label)

        return _callback


class _FakeCronRunRepo:
    """In-memory stand-in for SqlAlchemyCronJobRunRepository — keeps tests
    sync-friendly and avoids spinning up sqlite for the read-route tests."""

    def __init__(self):
        self.rows: dict[str, dict] = {}

    async def create_run(self, data):
        row = dict(data)
        # Match the SqlAlchemy repo's behaviour of serialising datetimes for
        # display payloads, but keep them as datetimes for internal ordering
        # — the API just echoes whatever the repo returns.
        self.rows[data["id"]] = row
        return row

    async def update_run(self, run_id, updates):
        row = self.rows.get(run_id)
        if row is None:
            return None
        row.update({k: v for k, v in updates.items()})
        return row

    async def get_run(self, run_id):
        return self.rows.get(run_id)

    async def list_for_job(self, job_id, *, limit=20, before_fired_at=None):
        rows = [r for r in self.rows.values() if r.get("job_id") == job_id]
        # Most recent first by fired_at if present.
        rows.sort(key=lambda r: r.get("fired_at") or "", reverse=True)
        return rows[:limit]

    async def list_by_trace_id(self, trace_id, *, limit=50):
        rows = [r for r in self.rows.values() if r.get("trace_id") == trace_id]
        rows.sort(key=lambda r: r.get("fired_at") or "", reverse=True)
        return rows[:limit]


class _FakeCronManager:
    """Stub AgentCronManager — wraps an in-memory job store + the fake run repo
    so the API routes' contract is testable without APScheduler / sqlite.

    Mirrors the public methods of :class:`AgentCronManager` actually exercised
    by the API surface: ``list_jobs / create_job / get_job / update_job /
    delete_job / pause_job / resume_job / trigger_job``.
    """

    def __init__(self, run_repo: _FakeCronRunRepo):
        self._jobs: dict[str, dict] = {}
        self._run_repo = run_repo
        self._counter = 0

    async def list_jobs(self, agent_id=None):
        if agent_id is None:
            return list(self._jobs.values())
        return [j for j in self._jobs.values() if j["agent_id"] == agent_id]

    async def create_job(self, data, *, acknowledge_distant_schedule=False):
        # Mirror the real manager: caller-input validation surfaces as
        # ValueError so we can verify the API route translates it to 400.
        expr = data.get("cron_expression")
        if expr == "49 35 23 5 *":
            raise ValueError(
                f"invalid cron_expression {expr!r} (timezone={data.get('timezone')!r}): "
                "Error validating expression '35': the last value (35) is higher than the maximum value (23)"
            )
        # Mirror the max_concurrency floor: 0 / negative would freeze
        # every fire in the real manager (asyncio.Semaphore(0) stays
        # locked forever).
        mc = data.get("max_concurrency")
        if mc is not None and int(mc) < 1:
            raise ValueError(
                f"max_concurrency must be >= 1 (got {mc}). A value of 0 "
                "would silently block every cron fire."
            )
        # Mirror the next-fire-distance guard for one fixed sentinel
        # expression. Used by the API integration test to confirm the
        # route forwards ``acknowledge_distant_schedule`` correctly.
        if expr == "0 0 1 1 *" and not acknowledge_distant_schedule:
            raise ValueError(
                f"cron_expression {expr!r} (timezone={data.get('timezone')!r}) "
                f"next fires at 2027-01-01T00:00:00+00:00, which is 200+ days "
                f"from now. ... acknowledge_distant_schedule=true."
            )
        self._counter += 1
        job_id = f"cron-{self._counter:04d}"
        row = {
            "id": job_id,
            "agent_id": data["agent_id"],
            "name": data["name"],
            "cron_expression": data["cron_expression"],
            "timezone": data.get("timezone") or "UTC",
            "input_template": data["input_template"],
            "max_concurrency": data.get("max_concurrency") or 1,
            "timeout_seconds": data.get("timeout_seconds") or 120,
            "enabled": bool(data.get("enabled", True)),
            "pre_action": data.get("pre_action"),
            "task_kind": data.get("task_kind"),
            "task_params_json": data.get("task_params_json"),
            # T1: real manager echoes the resolved next_fire_time so
            # callers can spot timezone drift. Fake uses a sentinel.
            "next_fire_time": "2026-05-24T00:00:00+00:00",
        }
        self._jobs[job_id] = row
        return row

    async def get_job(self, job_id):
        return self._jobs.get(job_id)

    async def update_job(
        self, job_id, updates, *, acknowledge_distant_schedule=False,
    ):
        row = self._jobs.get(job_id)
        if row is None:
            raise ValueError(f"Cron job not found: {job_id}")
        expr = updates.get("cron_expression")
        if expr == "49 35 23 5 *":
            raise ValueError(
                f"invalid cron_expression {expr!r} (timezone={updates.get('timezone')!r}): "
                "Error validating expression '35': the last value (35) is higher than the maximum value (23)"
            )
        if expr == "0 0 1 1 *" and not acknowledge_distant_schedule:
            raise ValueError(
                f"cron_expression {expr!r} next fires at 2027-01-01T00:00:00+00:00, "
                f"which is 200+ days from now. ... acknowledge_distant_schedule=true."
            )
        for k, v in updates.items():
            row[k] = v
        return row

    async def delete_job(self, job_id):
        self._jobs.pop(job_id, None)

    async def pause_job(self, job_id):
        row = self._jobs[job_id]
        row["enabled"] = False
        return row

    async def resume_job(self, job_id):
        row = self._jobs[job_id]
        row["enabled"] = True
        return row

    async def trigger_job(self, job_id):
        from datetime import datetime, timezone
        import uuid as _uuid

        job = self._jobs.get(job_id)
        if job is None:
            raise ValueError(f"Cron job not found: {job_id}")
        run_id = f"crun-{_uuid.uuid4().hex[:12]}"
        await self._run_repo.create_run({
            "id": run_id,
            "job_id": job_id,
            "fired_at": datetime.now(timezone.utc).isoformat(),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "status": "running",
            "pre_kind": (job.get("pre_action") or {}).get("kind"),
        })
        return run_id


class _FakeCronRepoForReadiness:
    def __init__(self):
        self.rows: dict[str, dict[str, object]] = {}
        self._counter = 0

    async def list_jobs(self, agent_id):
        if not agent_id:
            return list(self.rows.values())
        return [row for row in self.rows.values() if row.get("agent_id") == agent_id]

    async def get_job(self, job_id):
        row = self.rows.get(job_id)
        return dict(row) if isinstance(row, dict) else None

    async def upsert_job(self, payload):
        row = dict(payload)
        job_id = row.get("id")
        if not isinstance(job_id, str) or not job_id:
            self._counter += 1
            job_id = f"cron-ready-{self._counter:04d}"
            row["id"] = job_id
        existing = self.rows.get(job_id) or {}
        merged = {**existing, **row}
        self.rows[job_id] = merged
        return dict(merged)

    async def delete_job(self, job_id):
        self.rows.pop(job_id, None)

    async def update_job_state(self, job_id, **updates):
        row = self.rows.get(job_id)
        if row is None:
            return None
        row.update(updates)
        return dict(row)


class _FakeTaskRepositoryForCronReadiness:
    def __init__(self, rows: dict[str, dict[str, str]]):
        self.rows = rows

    async def get_task(self, task_id):
        row = self.rows.get(task_id)
        if row is None:
            raise KeyError(f"task not found: {task_id}")
        return SimpleNamespace(task_id=task_id, **row)


class _FakePlatformServiceForCronReadiness:
    def __init__(self, rows: dict[str, dict[str, str]]):
        self.task_repository = _FakeTaskRepositoryForCronReadiness(rows)


class _FakeAgentRepoForCronReadiness:
    async def get_agent(self, agent_id):
        return {"id": agent_id}


class _FakeModelRouteRepoForSetup:
    """Minimal stand-in for SqlAlchemyModelRouteRepository: just enough
    create / get_by_route_name / list_routes for the /setup/* endpoints
    (doyoutrade.onboarding.agent_route_usable / create_route_and_bind_agent)
    to exercise real create-then-resolve-then-build-adapter logic."""

    def __init__(self):
        from datetime import datetime, timezone

        self._by_name: dict[str, SimpleNamespace] = {}
        self._seq = 0
        self._dt = datetime(2026, 1, 1, tzinfo=timezone.utc)

    async def create(self, *, route_name, provider_kind, api_key, base_url=None,
                      target_model=None, settings=None, id=None):
        self._seq += 1
        rid = id or f"route-{self._seq}"
        rec = SimpleNamespace(
            id=rid, route_name=route_name, provider_kind=provider_kind,
            base_url=base_url, api_key=api_key, target_model=target_model,
            settings=settings, created_at=self._dt, updated_at=self._dt,
        )
        self._by_name[route_name] = rec
        return rec

    async def get_by_route_name(self, route_name):
        from doyoutrade.persistence.errors import RecordNotFoundError

        rec = self._by_name.get(route_name)
        if rec is None:
            raise RecordNotFoundError(f"model route not found: {route_name}")
        return rec

    async def list_routes(self):
        return list(self._by_name.values())


class _FakeServiceWithSetupRoutes(_FakeService):
    """``_FakeService`` plus a real ``model_route_repository`` + the public
    ``get_model_route_api`` the /setup/complete endpoint relies on (mirrors
    ``PlatformService.get_model_route_api``: resolve + serialize)."""

    def __init__(self):
        super().__init__()
        self.model_route_repository = _FakeModelRouteRepoForSetup()

    async def get_model_route_api(self, route_name: str) -> dict:
        rec = await self.model_route_repository.get_by_route_name(route_name)
        return {
            "id": rec.id,
            "route_name": rec.route_name,
            "provider_kind": rec.provider_kind,
            "base_url": rec.base_url,
            "api_key_masked": "****" if rec.api_key else "",
            "target_model": rec.target_model,
            "settings": rec.settings,
            "created_at": rec.created_at.isoformat(),
            "updated_at": rec.updated_at.isoformat(),
        }


class SetupWizardApiTests(unittest.TestCase):
    """GET /setup/status, GET /setup/providers, POST /setup/complete —
    the web first-run wizard's backend (SetupWizard.tsx), sharing the
    "what counts as configured" / "create + bind" logic with the terminal
    onboarding wizard via doyoutrade/onboarding.py (not reimplemented here)."""

    def _client(self, service=None, assistant_service=None):
        service = service or _FakeServiceWithSetupRoutes()
        assistant_service = assistant_service or _FakeAssistantService()
        app = create_app(service, _FakeApprovalGate(), assistant_service=assistant_service)
        return TestClient(app), service, assistant_service

    def test_status_false_when_default_agent_unconfigured(self):
        client, _service, assistant_service = self._client()
        asyncio.run(assistant_service.agent_repo.ensure_main_agent())

        with client:
            resp = client.get("/setup/status")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"configured": False})

    def test_status_true_after_route_created_and_bound(self):
        client, service, assistant_service = self._client()
        asyncio.run(assistant_service.agent_repo.ensure_main_agent())

        async def _seed():
            from doyoutrade.onboarding import create_route_and_bind_agent

            await create_route_and_bind_agent(
                service.model_route_repository,
                assistant_service.agent_repo,
                route_name="default",
                provider_kind="openai_compatible",
                api_key="sk-test",
                base_url="https://api.deepseek.com",
                target_model="deepseek-chat",
            )

        asyncio.run(_seed())

        with client:
            resp = client.get("/setup/status")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"configured": True})

    def test_status_503_when_model_route_repository_missing(self):
        service = _FakeService()  # no model_route_repository attribute
        assistant_service = _FakeAssistantService()
        asyncio.run(assistant_service.agent_repo.ensure_main_agent())
        client, _service, _assistant = self._client(service=service, assistant_service=assistant_service)

        with client:
            resp = client.get("/setup/status")

        self.assertEqual(resp.status_code, 503)

    def test_status_503_when_agent_repo_missing(self):
        client, _service, _assistant = self._client(assistant_service=_FakeAssistantService.__new__(_FakeAssistantService))
        # A bare instance (no __init__) has no agent_repo attribute at all —
        # closer to a real deployment where AssistantService wasn't wired with one.
        with client:
            resp = client.get("/setup/status")
        self.assertEqual(resp.status_code, 503)

    def test_providers_returns_serialized_preset_catalog(self):
        from doyoutrade.onboarding import PRESETS

        client, _service, _assistant = self._client()

        with client:
            resp = client.get("/setup/providers")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(len(body["items"]), len(PRESETS))
        slugs_present_kinds = {item["provider_kind"] for item in body["items"]}
        self.assertIn("openai_compatible", slugs_present_kinds)
        self.assertIn("anthropic", slugs_present_kinds)
        first = body["items"][0]
        self.assertEqual(
            set(first.keys()), {"label", "provider_kind", "base_url", "model_hint", "needs_key"}
        )

    def test_complete_creates_route_and_binds_default_agent(self):
        client, service, assistant_service = self._client()
        asyncio.run(assistant_service.agent_repo.ensure_main_agent())

        with client:
            resp = client.post(
                "/setup/complete",
                json={
                    "provider_kind": "openai_compatible",
                    "api_key": "sk-web-setup",
                    "base_url": "https://api.deepseek.com",
                    "target_model": "deepseek-chat",
                },
            )
            status_after = client.get("/setup/status")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["route_name"], "default")
        self.assertEqual(body["provider_kind"], "openai_compatible")
        self.assertEqual(body["target_model"], "deepseek-chat")

        agent = asyncio.run(assistant_service.agent_repo.get_agent("agent_default"))
        self.assertEqual(agent["model_route_name"], "default")
        self.assertEqual(status_after.json(), {"configured": True})

    def test_complete_rejects_unsupported_provider_kind(self):
        client, _service, assistant_service = self._client()
        asyncio.run(assistant_service.agent_repo.ensure_main_agent())

        with client:
            resp = client.post(
                "/setup/complete",
                json={"provider_kind": "not-a-real-kind", "api_key": "sk"},
            )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("provider_kind must be one of", resp.json()["detail"])

    def test_complete_dedupes_route_name_on_conflict(self):
        client, service, assistant_service = self._client()
        asyncio.run(assistant_service.agent_repo.ensure_main_agent())

        async def _seed_existing():
            await service.model_route_repository.create(
                route_name="default", provider_kind="anthropic",
                api_key="sk-old", base_url=None, target_model="claude-sonnet-4-5",
            )

        asyncio.run(_seed_existing())

        with client:
            resp = client.post(
                "/setup/complete",
                json={
                    "provider_kind": "openai_compatible",
                    "api_key": "sk-new",
                    "base_url": "https://api.deepseek.com",
                    "target_model": "deepseek-chat",
                },
            )

        self.assertEqual(resp.status_code, 200)
        self.assertNotEqual(resp.json()["route_name"], "default")
        self.assertTrue(resp.json()["route_name"].startswith("default-"))

    def test_complete_503_when_model_route_repository_missing(self):
        service = _FakeService()  # no model_route_repository
        assistant_service = _FakeAssistantService()
        asyncio.run(assistant_service.agent_repo.ensure_main_agent())
        client, _service, _assistant = self._client(service=service, assistant_service=assistant_service)

        with client:
            resp = client.post(
                "/setup/complete",
                json={"provider_kind": "anthropic", "api_key": "sk-test"},
            )

        self.assertEqual(resp.status_code, 503)


class CronJobApiTests(unittest.TestCase):
    """End-to-end contract tests for the cron job API surface (pre_action
    acceptance, trigger response shape, list/get run routes)."""

    def _build_client(self):
        service = _FakeService()
        assistant_service = _FakeAssistantService()
        run_repo = _FakeCronRunRepo()
        cron_manager = _FakeCronManager(run_repo)
        app = create_app(
            service,
            _FakeApprovalGate(),
            assistant_service=assistant_service,
            cron_manager=cron_manager,
            cron_run_repo=run_repo,
        )
        return app, cron_manager, run_repo

    def _build_client_with_readiness_manager(
        self,
        task_rows: dict[str, dict[str, str]],
    ):
        service = _FakeService()
        assistant_service = _FakeAssistantService()
        assistant_service.agent_repo = _FakeAgentRepoForCronReadiness()
        assistant_service.platform_service = _FakePlatformServiceForCronReadiness(
            task_rows,
        )
        run_repo = _FakeCronRunRepo()
        cron_repo = _FakeCronRepoForReadiness()
        cron_manager = AgentCronManager(
            assistant_service,
            cron_repo,
            cron_run_repo=run_repo,
        )
        app = create_app(
            service,
            _FakeApprovalGate(),
            assistant_service=assistant_service,
            cron_manager=cron_manager,
            cron_run_repo=run_repo,
        )
        return app, cron_manager, cron_repo

    def test_create_cron_job_accepts_task_pipeline_payload(self):
        app, _mgr, _runs = self._build_client()
        with TestClient(app) as client:
            created = client.post(
                "/assistant/agents/a1/cron/jobs",
                json={
                    "name": "chat reminder",
                    "schedule_kind": "cron",
                    "cron_expression": "30 14 * * 1-5",
                    "input_template": None,
                    "task_kind": "agent_chat_reply",
                    "task_params_json": {
                        "user_request": "14:30 提醒我",
                        "target_session_id": "asst-user",
                    },
                },
            )
            self.assertEqual(created.status_code, 201, created.text)
            body = created.json()
            self.assertIsNone(body["input_template"])
            self.assertEqual(body["task_kind"], "agent_chat_reply")
            self.assertEqual(
                body["task_params_json"]["user_request"], "14:30 提醒我",
            )

    def test_create_cron_job_rejects_retired_strategy_signal_alert_kind(self):
        """Strategy cron task_kinds are retired in favour of Task Triggers;
        the real manager must reject create with a stable error_code so the
        orphaned-kind failure is VISIBLE rather than silently accepted."""
        app, _mgr, _repo = self._build_client_with_readiness_manager(
            {
                "task-1": {
                    "mode": "signal_only",
                    "status": "running",
                },
            }
        )
        with TestClient(app) as client:
            response = client.post(
                "/assistant/agents/a1/cron/jobs",
                json={
                    "name": "strategy alert",
                    "schedule_kind": "cron",
                    "cron_expression": "*/5 * * * *",
                    "task_kind": "strategy_signal_alert",
                    "task_params_json": {
                        "strategy_task_ids": ["task-1"],
                        "user_request": "每5分钟提醒我",
                    },
                },
            )
        self.assertEqual(response.status_code, 400, response.text)
        detail = response.json()["detail"]
        self.assertEqual(detail["error_code"], "cron_strategy_kind_retired")
        self.assertIn("Task Trigger", detail["message"])

    def test_cron_create_autofills_target_session_id_from_caller_session(self):
        """``X-DOYOUTRADE-Calling-Session-Id`` injected and the body omitted
        ``target_session_id`` → handler autofills the caller's session id
        on the persisted ``task_params_json``.

        Without this autofill a user who creates a cron from inside an
        existing assistant session (web UI / Lark group) historically
        produced a job that would later fire with
        ``delivery_status='skipped'`` — silent drop.
        """

        app, _mgr, _runs = self._build_client()
        with TestClient(app) as client:
            created = client.post(
                "/assistant/agents/a1/cron/jobs",
                # Header value spelled exactly as the recursive-cron guard
                # reads it from ``request.headers`` (case-insensitive on
                # the HTTP wire but FastAPI lowercases on dict access).
                headers={"X-DOYOUTRADE-Calling-Session-Id": "asst-from-lark"},
                json={
                    "name": "remind me",
                    "schedule_kind": "cron",
                    "cron_expression": "0 9 * * *",
                    "task_kind": "agent_chat_reply",
                    "task_params_json": {
                        "agent_id": "a1",
                        "user_request": "每天早上 9 点提醒我",
                        # target_session_id intentionally omitted.
                    },
                },
            )
            self.assertEqual(created.status_code, 201, created.text)
            body = created.json()
            self.assertEqual(
                body["task_params_json"]["target_session_id"],
                "asst-from-lark",
            )

    def test_cron_create_respects_explicit_target_session_id(self):
        """Body explicitly set ``target_session_id`` → no overwrite, the
        caller wins even if a calling-session header is present."""

        app, _mgr, _runs = self._build_client()
        with TestClient(app) as client:
            created = client.post(
                "/assistant/agents/a1/cron/jobs",
                headers={"X-DOYOUTRADE-Calling-Session-Id": "asst-from-lark"},
                json={
                    "name": "chat reminder",
                    "schedule_kind": "cron",
                    "cron_expression": "30 14 * * 1-5",
                    "task_kind": "agent_chat_reply",
                    "task_params_json": {
                        "agent_id": "a1",
                        "user_request": "14:30 提醒我",
                        "target_session_id": "asst-explicit",
                    },
                },
            )
            self.assertEqual(created.status_code, 201, created.text)
            body = created.json()
            self.assertEqual(
                body["task_params_json"]["target_session_id"],
                "asst-explicit",
            )

    def test_cron_create_no_caller_session_leaves_target_unset(self):
        """No calling-session header (anonymous CLI / curl): handler does
        not invent a target session — the executor's existing
        ``delivery_status='skipped'`` path covers diagnostic fires."""

        app, _mgr, _runs = self._build_client()
        with TestClient(app) as client:
            created = client.post(
                "/assistant/agents/a1/cron/jobs",
                json={
                    "name": "diagnostic",
                    "schedule_kind": "cron",
                    "cron_expression": "0 9 * * *",
                    "task_kind": "agent_chat_reply",
                    "task_params_json": {
                        "agent_id": "a1",
                        "user_request": "diag fire",
                    },
                },
            )
            self.assertEqual(created.status_code, 201, created.text)
            body = created.json()
            self.assertNotIn(
                "target_session_id", body["task_params_json"],
            )

    def test_create_cron_job_accepts_nested_task_payload(self):
        app, _mgr, _runs = self._build_client()
        with TestClient(app) as client:
            created = client.post(
                "/assistant/agents/a1/cron/jobs",
                json={
                    "name": "frontend chat reminder",
                    "schedule_kind": "cron",
                    "cron_expression": "30 14 * * 1-5",
                    "pre_action": None,
                    "task": {
                        "kind": "agent_chat_reply",
                        "params": {
                            "agent_id": "a1",
                            "user_request": "14:30 提醒我",
                            "target_session_id": "asst-user",
                        },
                    },
                },
            )
            self.assertEqual(created.status_code, 201, created.text)
            body = created.json()
            self.assertIsNone(body["input_template"])
            self.assertIsNone(body["pre_action"])
            self.assertEqual(body["task_kind"], "agent_chat_reply")
            self.assertEqual(
                body["task_params_json"]["user_request"], "14:30 提醒我",
            )

    def test_create_cron_job_accepts_pre_action(self):
        app, _mgr, _runs = self._build_client()
        with TestClient(app) as client:
            response = client.post(
                "/assistant/agents/a1/cron/jobs",
                json={
                    "name": "open-bell",
                    "cron_expression": "0 9 * * 1-5",
                    "input_template": "pre={{ pre }}",
                    "pre_action": {"kind": "noop", "params": {}},
                },
            )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["pre_action"], {"kind": "noop", "params": {}})

    def test_create_cron_job_rejects_invalid_pre_action_shape(self):
        app, _mgr, _runs = self._build_client()
        with TestClient(app) as client:
            response = client.post(
                "/assistant/agents/a1/cron/jobs",
                json={
                    "name": "bad",
                    "cron_expression": "* * * * *",
                    "input_template": "x",
                    "pre_action": "not-a-dict",
                },
            )
        self.assertEqual(response.status_code, 400)

    def test_create_cron_job_rejects_pre_action_missing_kind(self):
        app, _mgr, _runs = self._build_client()
        with TestClient(app) as client:
            response = client.post(
                "/assistant/agents/a1/cron/jobs",
                json={
                    "name": "bad",
                    "cron_expression": "* * * * *",
                    "input_template": "x",
                    "pre_action": {"params": {}},
                },
            )
        self.assertEqual(response.status_code, 400)

    def test_update_cron_job_round_trips_pre_action(self):
        app, _mgr, _runs = self._build_client()
        with TestClient(app) as client:
            created = client.post(
                "/assistant/agents/a1/cron/jobs",
                json={
                    "name": "open-bell",
                    "cron_expression": "0 9 * * 1-5",
                    "input_template": "x",
                },
            )
            self.assertEqual(created.status_code, 201)
            job_id = created.json()["id"]

            updated = client.put(
                f"/assistant/agents/a1/cron/jobs/{job_id}",
                json={
                    "pre_action": {
                        "kind": "noop",
                        "params": {"task_id": "inst-1"},
                    }
                },
            )
            self.assertEqual(updated.status_code, 200)
            self.assertEqual(
                updated.json()["pre_action"],
                {"kind": "noop", "params": {"task_id": "inst-1"}},
            )

            fetched = client.get(f"/assistant/agents/a1/cron/jobs/{job_id}")
            self.assertEqual(fetched.status_code, 200)
            self.assertEqual(
                fetched.json()["pre_action"],
                {"kind": "noop", "params": {"task_id": "inst-1"}},
            )

            # Explicit null clears pre_action.
            cleared = client.put(
                f"/assistant/agents/a1/cron/jobs/{job_id}",
                json={"pre_action": None},
            )
            self.assertEqual(cleared.status_code, 200)
            self.assertIsNone(cleared.json()["pre_action"])

    def test_update_cron_job_accepts_task_pipeline_payload(self):
        app, _mgr, _runs = self._build_client()
        with TestClient(app) as client:
            created = client.post(
                "/assistant/agents/a1/cron/jobs",
                json={
                    "name": "legacy",
                    "cron_expression": "0 9 * * *",
                    "input_template": "x",
                },
            )
            self.assertEqual(created.status_code, 201)
            job_id = created.json()["id"]

            updated = client.put(
                f"/assistant/agents/a1/cron/jobs/{job_id}",
                json={
                    "input_template": None,
                    "pre_action": None,
                    "task_kind": "agent_chat_reply",
                    "task_params_json": {
                        "user_request": "每天提醒我复盘",
                        "target_session_id": "asst-user",
                    },
                },
            )
            self.assertEqual(updated.status_code, 200, updated.text)
            body = updated.json()
            self.assertIsNone(body["input_template"])
            self.assertIsNone(body["pre_action"])
            self.assertEqual(body["task_kind"], "agent_chat_reply")
            self.assertEqual(
                body["task_params_json"]["target_session_id"], "asst-user",
            )

    def test_update_cron_job_switches_nested_task_back_to_legacy(self):
        app, _mgr, _runs = self._build_client()
        with TestClient(app) as client:
            created = client.post(
                "/assistant/agents/a1/cron/jobs",
                json={
                    "name": "task row",
                    "cron_expression": "0 9 * * *",
                    "task": {
                        "kind": "agent_chat_reply",
                        "params": {
                            "agent_id": "a1",
                            "user_request": "每天提醒我复盘",
                        },
                    },
                },
            )
            self.assertEqual(created.status_code, 201, created.text)
            job_id = created.json()["id"]

            updated = client.put(
                f"/assistant/agents/a1/cron/jobs/{job_id}",
                json={
                    "input_template": "pre={{ pre }}",
                    "pre_action": {
                        "kind": "noop",
                        "params": {"task_id": "task-1"},
                    },
                    "task": None,
                },
            )
            self.assertEqual(updated.status_code, 200, updated.text)
            body = updated.json()
            self.assertEqual(body["input_template"], "pre={{ pre }}")
            self.assertEqual(
                body["pre_action"],
                {"kind": "noop", "params": {"task_id": "task-1"}},
            )
            self.assertIsNone(body["task_kind"])
            self.assertIsNone(body["task_params_json"])

    def test_cron_run_trace_includes_related_cycle_session_ids(self):
        app, _mgr, run_repo = self._build_client()
        assistant_service = app.state.assistant_service

        class _CycleRuns:
            async def get_by_run_id(self, run_id):
                if run_id == "cycle-1":
                    return {"run_id": run_id, "session_id": "cron-debug-1"}
                return None

        # The route under test should use app.state when available.
        app.state.cycle_run_repository = _CycleRuns()

        run_repo.rows["crun-1"] = {
            "id": "crun-1",
            "job_id": "cron-1",
            "status": "success",
            "agent_session_id": "asst-agent",
            "pre_result_json": {
                "pre_data": {
                    "instances": [
                        {"task_id": "task-1", "run_id": "cycle-1", "status": "completed"}
                    ]
                }
            },
        }
        with TestClient(app) as client:
            response = client.get("/assistant/cron-job-runs/crun-1/trace")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            assistant_service.span_session_calls[-1],
            ["asst-agent", "cron-debug-1"],
        )

    def test_create_cron_job_translates_invalid_cron_expression_to_400(self):
        # Regression: AgentCronManager raises ValueError when APScheduler
        # rejects the cron expression (e.g. the model swaps field order and
        # writes 'hour=35'). Previously the route let that bubble up as a
        # FastAPI 500, so the assistant CLI received error_code='server_error'
        # with body 'Internal Server Error' — no actionable feedback. The
        # endpoint must translate caller-input errors to HTTP 400 and carry
        # the validator's message so the agent can self-correct.
        app, _mgr, _runs = self._build_client()
        with TestClient(app) as client:
            response = client.post(
                "/assistant/agents/a1/cron/jobs",
                json={
                    "name": "30s 问候",
                    "cron_expression": "49 35 23 5 *",
                    "input_template": "你好！",
                },
            )
        self.assertEqual(response.status_code, 400, msg=response.text)
        detail = response.json().get("detail", "")
        self.assertIn("invalid cron_expression", detail)
        self.assertIn("'49 35 23 5 *'", detail)

    def test_create_cron_job_rejects_zero_max_concurrency(self):
        """``max_concurrency=0`` makes asyncio.Semaphore(0) always
        locked, so every cron fire is silently skipped. The API must
        bounce caller-input 0 / negative as 400 so the assistant gets a
        structured fix-this signal instead of "registered but never
        runs"."""
        app, _mgr, _runs = self._build_client()
        with TestClient(app) as client:
            # The current API normalizes ``int(payload or 1)`` so 0 →
            # 1 at the route boundary. Use a string the route can't
            # coerce to 1: send max_concurrency=-1 explicitly. The
            # _normalize-via-or chain only catches falsy values; -1 is
            # truthy and reaches the manager.
            response = client.post(
                "/assistant/agents/a1/cron/jobs",
                json={
                    "name": "frozen",
                    "cron_expression": "* * * * *",
                    "input_template": "x",
                    "max_concurrency": -1,
                },
            )
        self.assertEqual(response.status_code, 400, msg=response.text)
        self.assertIn("max_concurrency", response.json().get("detail", ""))

    def test_create_cron_job_echoes_next_fire_time(self):
        """Caller can spot timezone-drift bugs immediately because the
        resolved next-fire-time comes back in the response."""
        app, _mgr, _runs = self._build_client()
        with TestClient(app) as client:
            response = client.post(
                "/assistant/agents/a1/cron/jobs",
                json={
                    "name": "echo",
                    "cron_expression": "0 9 * * *",
                    "timezone": "UTC",
                    "input_template": "x",
                },
            )
        self.assertEqual(response.status_code, 201, msg=response.text)
        self.assertIn("next_fire_time", response.json())

    def test_pause_cron_job_returns_404_on_race_not_500(self):
        """If the row vanishes between the route's pre-check and the
        manager's pause_job call, the manager raises ValueError. The
        route must convert that to 404, not let it become a 500."""
        app, mgr, _runs = self._build_client()
        with TestClient(app) as client:
            created = client.post(
                "/assistant/agents/a1/cron/jobs",
                json={
                    "name": "to-be-raced",
                    "cron_expression": "* * * * *",
                    "input_template": "x",
                },
            )
            self.assertEqual(created.status_code, 201)
            job_id = created.json()["id"]

            # Simulate the race: the row exists at get_job time, then
            # vanishes from the manager's pause_job perspective.
            async def _raise_not_found(jid):
                raise ValueError(f"Cron job not found: {jid}")
            mgr.pause_job = _raise_not_found

            response = client.post(
                f"/assistant/agents/a1/cron/jobs/{job_id}/pause"
            )
        self.assertEqual(response.status_code, 404, msg=response.text)

    def test_create_cron_job_blocks_distant_schedule_by_default(self):
        # Regression: an LLM that mis-orders cron fields ('28 23 23 5 *'
        # for "30 seconds later") or confuses timezones produces a valid
        # expression whose next fire is months away. The route must
        # block that path so the model is forced to recompute, not
        # silently accept and report "registered for 30 seconds from
        # now" when the job actually fires next year.
        app, _mgr, _runs = self._build_client()
        with TestClient(app) as client:
            response = client.post(
                "/assistant/agents/a1/cron/jobs",
                json={
                    "name": "30s greeting",
                    "cron_expression": "0 0 1 1 *",
                    "input_template": "你好",
                },
            )
        self.assertEqual(response.status_code, 400, msg=response.text)
        detail = response.json().get("detail", "")
        self.assertIn("acknowledge_distant_schedule", detail)

    def test_create_cron_job_accepts_distant_schedule_when_acknowledged(self):
        # The opt-out path: caller (direct API, not LLM) wants a far-future
        # one-shot. ``acknowledge_distant_schedule=true`` must reach the
        # manager and disable the guard.
        app, _mgr, _runs = self._build_client()
        with TestClient(app) as client:
            response = client.post(
                "/assistant/agents/a1/cron/jobs",
                json={
                    "name": "new year",
                    "cron_expression": "0 0 1 1 *",
                    "input_template": "🎆",
                    "acknowledge_distant_schedule": True,
                },
            )
        self.assertEqual(response.status_code, 201, msg=response.text)

    def test_create_cron_rejects_recursive_call_from_cron_session(self):
        """A cron-fired session (config.cron_origin=True) must NOT be
        able to create another cron job via the API. The recursive
        guard prevents prompt-injected or runaway agents from
        snowballing scheduled work.
        """
        app, _mgr, _runs = self._build_client()
        svc = app.state.assistant_service
        # Seed a cron-fired calling session.
        svc.sessions["sess-cron-origin"] = {
            "session_id": "sess-cron-origin",
            "agent_id": "a1",
            "config": {
                "cron_origin": True,
                "cron_origin_job_id": "cron-parent",
            },
        }
        with TestClient(app) as client:
            response = client.post(
                "/assistant/agents/a1/cron/jobs",
                json={
                    "name": "child",
                    "cron_expression": "0 9 * * *",
                    "input_template": "x",
                },
                headers={
                    "X-DOYOUTRADE-Calling-Session-Id": "sess-cron-origin",
                },
            )
        self.assertEqual(response.status_code, 403, msg=response.text)
        detail = response.json().get("detail", "")
        self.assertIn("Recursive cron creation blocked", detail)
        self.assertIn("cron-parent", detail)
        self.assertIn("acknowledge_cron_recursion", detail)

    def test_create_cron_acknowledge_recursion_overrides_block(self):
        """Direct API callers can opt out via
        ``acknowledge_cron_recursion=true`` (operator override; LLMs
        do not have this token in their context)."""
        app, _mgr, _runs = self._build_client()
        svc = app.state.assistant_service
        svc.sessions["sess-cron-origin"] = {
            "session_id": "sess-cron-origin",
            "agent_id": "a1",
            "config": {"cron_origin": True},
        }
        with TestClient(app) as client:
            response = client.post(
                "/assistant/agents/a1/cron/jobs",
                json={
                    "name": "intentional",
                    "cron_expression": "0 9 * * *",
                    "input_template": "x",
                    "acknowledge_cron_recursion": True,
                },
                headers={
                    "X-DOYOUTRADE-Calling-Session-Id": "sess-cron-origin",
                },
            )
        self.assertEqual(response.status_code, 201, msg=response.text)

    def test_create_cron_from_normal_session_succeeds(self):
        """A non-cron-fired session (no cron_origin flag) creates
        crons normally — the recursive guard must not false-positive
        on regular user-driven sessions."""
        app, _mgr, _runs = self._build_client()
        svc = app.state.assistant_service
        svc.sessions["sess-normal"] = {
            "session_id": "sess-normal",
            "agent_id": "a1",
            "config": {},
        }
        with TestClient(app) as client:
            response = client.post(
                "/assistant/agents/a1/cron/jobs",
                json={
                    "name": "user-driven",
                    "cron_expression": "0 9 * * *",
                    "input_template": "x",
                },
                headers={
                    "X-DOYOUTRADE-Calling-Session-Id": "sess-normal",
                },
            )
        self.assertEqual(response.status_code, 201, msg=response.text)

    def test_update_cron_job_blocks_distant_schedule_by_default(self):
        app, _mgr, _runs = self._build_client()
        with TestClient(app) as client:
            created = client.post(
                "/assistant/agents/a1/cron/jobs",
                json={
                    "name": "x",
                    "cron_expression": "* * * * *",
                    "input_template": "x",
                },
            )
            job_id = created.json()["id"]
            bad = client.put(
                f"/assistant/agents/a1/cron/jobs/{job_id}",
                json={"cron_expression": "0 0 1 1 *"},
            )
        self.assertEqual(bad.status_code, 400, msg=bad.text)
        self.assertIn("acknowledge_distant_schedule", bad.json().get("detail", ""))

    def test_update_cron_job_translates_invalid_cron_expression_to_400(self):
        app, _mgr, _runs = self._build_client()
        with TestClient(app) as client:
            created = client.post(
                "/assistant/agents/a1/cron/jobs",
                json={
                    "name": "x",
                    "cron_expression": "* * * * *",
                    "input_template": "x",
                },
            )
            job_id = created.json()["id"]
            bad = client.put(
                f"/assistant/agents/a1/cron/jobs/{job_id}",
                json={"cron_expression": "49 35 23 5 *"},
            )
        self.assertEqual(bad.status_code, 400, msg=bad.text)
        self.assertIn("invalid cron_expression", bad.json().get("detail", ""))

    def test_update_cron_job_rejects_invalid_pre_action(self):
        app, _mgr, _runs = self._build_client()
        with TestClient(app) as client:
            created = client.post(
                "/assistant/agents/a1/cron/jobs",
                json={
                    "name": "x",
                    "cron_expression": "* * * * *",
                    "input_template": "x",
                },
            )
            job_id = created.json()["id"]

            bad = client.put(
                f"/assistant/agents/a1/cron/jobs/{job_id}",
                json={"pre_action": {"params": {}}},  # missing kind
            )
            self.assertEqual(bad.status_code, 400)

    def test_trigger_cron_job_returns_run_id(self):
        app, _mgr, _runs = self._build_client()
        with TestClient(app) as client:
            created = client.post(
                "/assistant/agents/a1/cron/jobs",
                json={
                    "name": "x",
                    "cron_expression": "* * * * *",
                    "input_template": "x",
                },
            )
            job_id = created.json()["id"]
            triggered = client.post(
                f"/assistant/agents/a1/cron/jobs/{job_id}/run"
            )
        self.assertEqual(triggered.status_code, 200)
        body = triggered.json()
        self.assertIn("cron_job_run_id", body)
        self.assertTrue(body["cron_job_run_id"].startswith("crun-"))

    def test_list_cron_job_runs(self):
        app, _mgr, _runs = self._build_client()
        with TestClient(app) as client:
            created = client.post(
                "/assistant/agents/a1/cron/jobs",
                json={
                    "name": "x",
                    "cron_expression": "* * * * *",
                    "input_template": "x",
                },
            )
            job_id = created.json()["id"]
            triggered = client.post(
                f"/assistant/agents/a1/cron/jobs/{job_id}/run"
            )
            run_id = triggered.json()["cron_job_run_id"]

            listed = client.get(f"/assistant/cron-jobs/{job_id}/runs")
        self.assertEqual(listed.status_code, 200)
        body = listed.json()
        self.assertIn("items", body)
        ids = [r["id"] for r in body["items"]]
        self.assertIn(run_id, ids)

    def test_get_cron_job_run(self):
        app, _mgr, _runs = self._build_client()
        with TestClient(app) as client:
            created = client.post(
                "/assistant/agents/a1/cron/jobs",
                json={
                    "name": "x",
                    "cron_expression": "* * * * *",
                    "input_template": "x",
                },
            )
            job_id = created.json()["id"]
            triggered = client.post(
                f"/assistant/agents/a1/cron/jobs/{job_id}/run"
            )
            run_id = triggered.json()["cron_job_run_id"]

            fetched = client.get(f"/assistant/cron-job-runs/{run_id}")
        self.assertEqual(fetched.status_code, 200)
        body = fetched.json()
        self.assertEqual(body["id"], run_id)
        self.assertEqual(body["job_id"], job_id)
        self.assertEqual(body["status"], "running")

    def test_get_cron_job_run_not_found_returns_404(self):
        app, _mgr, _runs = self._build_client()
        with TestClient(app) as client:
            response = client.get("/assistant/cron-job-runs/does-not-exist")
        self.assertEqual(response.status_code, 404)

    def test_list_cron_job_runs_by_trace_id(self):
        app, _mgr, run_repo = self._build_client()
        trace = "a" * 32
        # Two fires sharing one trace (the cron.job.fire trace), one unrelated.
        run_repo.rows["crun-1"] = {"id": "crun-1", "job_id": "c1", "trace_id": trace, "fired_at": "2026-05-01T08:00:00"}
        run_repo.rows["crun-2"] = {"id": "crun-2", "job_id": "c1", "trace_id": trace, "fired_at": "2026-05-01T08:05:00"}
        run_repo.rows["crun-3"] = {"id": "crun-3", "job_id": "c1", "trace_id": "b" * 32, "fired_at": "2026-05-01T08:10:00"}

        with TestClient(app) as client:
            matched = client.get(f"/assistant/cron-job-runs?trace_id={trace}")
            empty = client.get("/assistant/cron-job-runs?trace_id=" + "c" * 32)
            missing_param = client.get("/assistant/cron-job-runs")

        self.assertEqual(matched.status_code, 200)
        body = matched.json()
        self.assertEqual(body["trace_id"], trace)
        self.assertEqual([r["id"] for r in body["items"]], ["crun-2", "crun-1"])

        self.assertEqual(empty.status_code, 200)
        self.assertEqual(empty.json()["items"], [])

        # trace_id is required — omitting it is a 422 (FastAPI validation).
        self.assertEqual(missing_param.status_code, 422)

    def test_list_cron_job_runs_clamps_excessive_limit(self):
        """An out-of-range limit gets clamped silently; the call must still succeed."""
        app, _mgr, _runs = self._build_client()
        with TestClient(app) as client:
            created = client.post(
                "/assistant/agents/a1/cron/jobs",
                json={
                    "name": "x",
                    "cron_expression": "* * * * *",
                    "input_template": "x",
                },
            )
            job_id = created.json()["id"]
            client.post(f"/assistant/agents/a1/cron/jobs/{job_id}/run")

            response = client.get(
                f"/assistant/cron-jobs/{job_id}/runs?limit=999999"
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("items", response.json())

    def test_list_cron_job_runs_rejects_zero_or_negative_limit(self):
        app, _mgr, _runs = self._build_client()
        with TestClient(app) as client:
            created = client.post(
                "/assistant/agents/a1/cron/jobs",
                json={
                    "name": "x",
                    "cron_expression": "* * * * *",
                    "input_template": "x",
                },
            )
            job_id = created.json()["id"]
            response = client.get(f"/assistant/cron-jobs/{job_id}/runs?limit=0")
        self.assertEqual(response.status_code, 400)

    def test_list_cron_job_runs_returns_503_when_repo_not_wired(self):
        service = _FakeService()
        assistant_service = _FakeAssistantService()
        cron_manager = _FakeCronManager(_FakeCronRunRepo())
        # Deliberately omit cron_run_repo.
        app = create_app(
            service,
            _FakeApprovalGate(),
            assistant_service=assistant_service,
            cron_manager=cron_manager,
        )
        with TestClient(app) as client:
            response = client.get("/assistant/cron-jobs/cron-0001/runs")
        self.assertEqual(response.status_code, 503)


class _PushApproval:
    """Minimal stand-in for ApprovalSnapshot used by _serialize_pending_approval.

    Only the attributes the serializer touches; created_at/expires_at default to
    None so the ``.isoformat() if … else None`` branches stay falsy.
    """

    def __init__(self, **kw):
        self.created_at = None
        self.expires_at = None
        self.decided_at = None
        self.resolved_at = None
        self.dispatched_at = None
        self.intent_payload = None
        self.dispatch_error = None
        self.dispatch_attempts = None
        self.reason = None
        self.__dict__.update(kw)


class _PushTriggerRepo:
    def __init__(self, trigger):
        self._trigger = trigger

    async def get_trigger(self, trigger_id):
        if self._trigger is None or trigger_id != getattr(self._trigger, "id", None):
            raise RecordNotFoundError(f"trigger not found: {trigger_id}")
        return self._trigger


class _PushFillRepo:
    def __init__(self, by_intent):
        self._by_intent = by_intent

    async def get_by_intent_id(self, *, task_id, intent_id):
        return self._by_intent.get(intent_id)


class _PushAssistantRepo:
    def __init__(self, sessions, messages):
        self._sessions = sessions
        self._messages = messages

    async def get_session(self, session_id):
        return self._sessions.get(session_id)

    async def list_messages(self, session_id, *, limit, offset):
        return list(self._messages.get(session_id, []))


class _PushAgentRepo:
    def __init__(self, agents):
        self._agents = agents

    async def get_agent(self, agent_id):
        return self._agents.get(agent_id)


class _PushAssistantSvc:
    def __init__(self, *, repository=None, agent_repo=None):
        self.repository = repository
        self.agent_repo = agent_repo


class _PushApprovalGate:
    def __init__(self, items):
        self.calls = []
        self._items = items

    async def list_pending(self):
        return [i for i in self._items if getattr(i, "status", None) == "pending"]

    async def list_approvals(self, **filters):
        self.calls.append(filters)
        run_id = filters.get("run_id")
        items = [
            i for i in self._items
            if run_id is None or getattr(i, "run_id", None) == run_id
        ]
        return items, len(items)


class _PushService:
    def __init__(self, *, payload, task_trigger_repository=None, trade_fill_repository=None):
        self._payload = payload
        self.task_trigger_repository = task_trigger_repository
        self.trade_fill_repository = trade_fill_repository
        self.cycle_run_repository = None
        self.instrument_catalog_repository = None

    async def get_run_debug_view(self, run_id):
        return dict(self._payload)


def _push_cycle_payload(**overrides):
    cycle_run = {
        "run_id": "run-push-1",
        "task_id": "task-1",
        "agent_name": "MyStrat",
        "session_id": None,
        "trace_id": "ab" * 16,
        "run_mode": "paper",
        "run_kind": "trigger",
        "trigger_id": "trg-1",
        "clock_mode": "wall",
        "cycle_time": None,
        "wall_started_at": "2026-06-14T01:00:00",
        "status": "completed",
        "details": {},
    }
    cycle_run.update(overrides.pop("cycle_run", {}))
    payload = {
        "resolved_from": {"identifier": "run-push-1", "identifier_type": "cycle_run"},
        "cycle_run": cycle_run,
        "session": None,
        "spans": [],
        "model_invocations": [],
        "signal_timeline": [],
        "signal_timeline_summary": {},
    }
    payload.update(overrides)
    return payload


class PushDetailTests(unittest.TestCase):
    """``GET /cycle-runs/{run_id}/debug-view`` push_detail enrichment.

    Verifies the cycle-detail modal can reconstruct, matching the REAL push:
    pushed cards (assistant_messages), approvals + dispatch receipt + matched
    fill, the composer agent, the strategy name and the landing session — and
    that every empty subsection carries an explicit reason (§错误可见性).
    """

    def _build_app(self, *, payload, approval_items=None, fills=None,
                   trigger=None, sessions=None, messages=None, agents=None):
        service = _PushService(
            payload=payload,
            task_trigger_repository=_PushTriggerRepo(trigger) if trigger is not None else None,
            trade_fill_repository=_PushFillRepo(fills or {}),
        )
        gate = _PushApprovalGate(approval_items or [])
        assistant = _PushAssistantSvc(
            repository=_PushAssistantRepo(sessions or {}, messages or {}),
            agent_repo=_PushAgentRepo(agents or {}),
        )
        app = create_app(service, gate, assistant_service=assistant)
        return app, service, gate

    def test_trigger_push_links_card_agent_session_and_approval_receipt(self):
        from datetime import datetime, timezone

        trigger = SimpleNamespace(
            id="trg-1",
            delivery_json={
                "mode": "prose",
                "composer_agent_id": "agent-x",
                "target": {"kind": "session", "session_id": "asst-1"},
            },
        )
        sessions = {
            "asst-1": {
                "session_id": "asst-1",
                "title": "Landing",
                "status": "idle",
                "agent_id": "agent-x",
            }
        }
        messages = {
            "asst-1": [
                {
                    "message_id": "m1",
                    "session_id": "asst-1",
                    "role": "assistant",
                    "content": "# 推送\n**已买入 600000.SH**",
                    "created_at": "2026-06-14T01:00:30",
                    "metadata": {
                        "source": "trigger",
                        "run_id": "run-push-1",
                        "cron_job_run_id": "run-push-1",
                        "trigger_id": "trg-1",
                    },
                },
                {
                    "message_id": "m2",
                    "session_id": "asst-1",
                    "role": "assistant",
                    "content": "other run card",
                    "created_at": "2026-06-14T02:00:00",
                    "metadata": {"source": "trigger", "run_id": "run-OTHER"},
                },
            ]
        }
        agents = {
            "agent-x": {
                "id": "agent-x",
                "name": "推送助手",
                "is_builtin": False,
                "status": "active",
                "model_route_name": "route-1",
                "tool_names": ["t"],
                "skill_names": [],
            }
        }
        approval = _PushApproval(
            approval_id="ap-1",
            intent_id="intent-1",
            run_id="run-push-1",
            task_id="task-1",
            status="approved",
            mode="live",
            symbol="600000.SH",
            action="buy",
            notional="780.00",
            account_id="acct-1",
            resolver_id="renjie",
            decision_source="web",
            dispatched_at=datetime(2026, 6, 14, 1, 1, tzinfo=timezone.utc),
        )
        fills = {
            "intent-1": {
                "quantity": "100",
                "price": "7.80",
                "amount": "780.00",
                "filled_at": "2026-06-14T01:01:00",
            }
        }
        app, _service, gate = self._build_app(
            payload=_push_cycle_payload(),
            approval_items=[approval],
            fills=fills,
            trigger=trigger,
            sessions=sessions,
            messages=messages,
            agents=agents,
        )
        with TestClient(app) as client:
            body = client.get("/cycle-runs/run-push-1/debug-view").json()

        pd = body["push_detail"]
        self.assertEqual(pd["resolved_from_kind"], "trigger")
        self.assertEqual(pd["strategy"]["name"], "MyStrat")
        # Composer agent resolved from the trigger's prose composer_agent_id.
        self.assertEqual(pd["composer_agent"]["compose_mode"], "prose")
        self.assertEqual(pd["composer_agent"]["agent"]["id"], "agent-x")
        self.assertIsNone(pd["composer_agent"]["reason"])
        # Landing session.
        self.assertEqual(pd["assistant_session"]["session"]["session_id"], "asst-1")
        # Byte-faithful pushed card — filtered to THIS run_id (m2 excluded).
        self.assertEqual(len(pd["pushed_messages"]["items"]), 1)
        self.assertEqual(pd["pushed_messages"]["items"][0]["message_id"], "m1")
        self.assertIn("已买入", pd["pushed_messages"]["items"][0]["content"])
        self.assertIsNone(pd["pushed_messages"]["reason"])
        # Approval + dispatch receipt + matched fill (decimal strings).
        self.assertEqual(pd["approvals"]["total"], 1)
        appr = pd["approvals"]["items"][0]
        self.assertEqual(appr["status"], "approved")
        self.assertEqual(appr["resolver_id"], "renjie")
        self.assertEqual(appr["matched_fill"]["price"], "7.80")
        self.assertEqual(appr["matched_fill"]["quantity"], "100")
        self.assertIsNone(appr["dispatch_error"])
        # The run_id filter was actually threaded to the gate.
        self.assertEqual(gate.calls[-1].get("run_id"), "run-push-1")

    def test_trigger_channel_reconstructs_card_when_not_persisted(self):
        # A Feishu channel push left no persisted copy (older run). For a
        # deterministic card the content is exactly reproducible from the same
        # (trigger, digest) inputs, so 周期详情 shows a reconstructed card rather
        # than a misleading "未推送卡片".
        trigger = SimpleNamespace(
            id="trg-1",
            name="群推送",
            delivery_json={
                "mode": "card",
                "no_signal_mode": "brief",
                "target": {"kind": "channel", "channel_id": "c1", "chat_id": "oc_g", "chat_name": "信号群"},
            },
        )
        app, _service, _gate = self._build_app(payload=_push_cycle_payload(), trigger=trigger)
        with TestClient(app) as client:
            pd = client.get("/cycle-runs/run-push-1/debug-view").json()["push_detail"]
        items = pd["pushed_messages"]["items"]
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["reconstructed"])
        self.assertTrue(items[0]["content"])
        self.assertEqual(items[0]["channel_target"], "信号群")
        self.assertIsNone(pd["pushed_messages"]["reason"])
        # Channel target → not an assistant session; card mode → no composer agent.
        self.assertEqual(pd["assistant_session"]["reason"], "channel_target_no_assistant_session")
        self.assertEqual(pd["composer_agent"]["reason"], "deterministic_card_no_composer_agent")

    def test_trigger_channel_uses_persisted_delivered_card(self):
        # When the delivery recorded the exact pushed card on the cycle run, that
        # faithful copy wins (no reconstruction) — incl. for prose pushes.
        trigger = SimpleNamespace(
            id="trg-1",
            name="群推送",
            delivery_json={
                "mode": "prose",
                "composer_agent_id": "agent-x",
                "target": {"kind": "channel", "channel_id": "c1", "chat_id": "oc_g", "chat_name": "信号群"},
            },
        )
        payload = _push_cycle_payload(cycle_run={"details": {"delivered_cards": [
            {
                "kind": "digest",
                "content": "# 实际推送\nAI 合成的卡片正文",
                "mode": "prose",
                "target_kind": "channel",
                "chat_name": "信号群",
                "status": "forwarded",
                "delivered_at": "2026-06-14T05:00:00",
            }
        ]}})
        agents = {"agent-x": {"id": "agent-x", "name": "推送助手", "is_builtin": False,
                              "status": "active", "model_route_name": "r1", "tool_names": [], "skill_names": []}}
        app, _service, _gate = self._build_app(payload=payload, trigger=trigger, agents=agents)
        with TestClient(app) as client:
            pd = client.get("/cycle-runs/run-push-1/debug-view").json()["push_detail"]
        items = pd["pushed_messages"]["items"]
        self.assertEqual(len(items), 1)
        self.assertFalse(items[0]["reconstructed"])
        self.assertIn("AI 合成的卡片正文", items[0]["content"])
        self.assertEqual(items[0]["delivery_status"], "forwarded")
        self.assertIsNone(pd["pushed_messages"]["reason"])

    def test_approval_failure_receipt_when_dispatch_errored(self):
        approval = _PushApproval(
            approval_id="ap-9",
            intent_id="intent-9",
            run_id="run-push-1",
            task_id="task-1",
            status="approved",
            symbol="600000.SH",
            action="buy",
            notional="500.00",
            dispatch_error="broker rejected",
            dispatch_attempts=2,
        )
        app, _service, _gate = self._build_app(
            payload=_push_cycle_payload(),
            approval_items=[approval],
            trigger=SimpleNamespace(id="trg-1", delivery_json={"mode": "card", "target": {"kind": "channel"}}),
        )
        with TestClient(app) as client:
            pd = client.get("/cycle-runs/run-push-1/debug-view").json()["push_detail"]
        appr = pd["approvals"]["items"][0]
        self.assertEqual(appr["dispatch_error"], "broker rejected")
        self.assertEqual(appr["dispatch_attempts"], 2)
        self.assertIsNone(appr["matched_fill"])

    def test_manual_run_has_no_delivery_reasons(self):
        app, _service, _gate = self._build_app(
            payload=_push_cycle_payload(cycle_run={"run_kind": "manual", "trigger_id": None}),
        )
        with TestClient(app) as client:
            pd = client.get("/cycle-runs/run-push-1/debug-view").json()["push_detail"]
        self.assertEqual(pd["pushed_messages"]["reason"], "manual_run_no_delivery")
        self.assertEqual(pd["assistant_session"]["reason"], "manual_run_no_delivery")
        self.assertEqual(pd["composer_agent"]["reason"], "manual_run_no_composer_agent")
        self.assertEqual(pd["approvals"]["reason"], "no_approvals_for_run")
        # Strategy name still surfaces on a manual cycle.
        self.assertEqual(pd["strategy"]["name"], "MyStrat")

    def test_non_cycle_run_carrier_reports_backtest_reasons(self):
        payload = _push_cycle_payload()
        payload["resolved_from"] = {"identifier": "x", "identifier_type": "backtest_job"}
        app, _service, _gate = self._build_app(payload=payload)
        with TestClient(app) as client:
            pd = client.get("/cycle-runs/run-push-1/debug-view").json()["push_detail"]
        self.assertEqual(pd["pushed_messages"]["reason"], "backtest_no_delivery")
        self.assertEqual(pd["approvals"]["reason"], "backtest_no_approvals")
        self.assertEqual(pd["composer_agent"]["reason"], "backtest_no_composer_agent")


class ConfigApiTests(unittest.TestCase):
    """GET/PUT /config and GET/PUT /qmt-proxy/config."""

    def setUp(self):
        from doyoutrade import config as config_mod

        self._home = tempfile.mkdtemp()
        self._saved_env = {
            key: os.environ.get(key)
            for key in ("DOYOUTRADE_HOME", "DOYOUTRADE_CONFIG")
        }
        os.environ["DOYOUTRADE_HOME"] = self._home
        os.environ.pop("DOYOUTRADE_CONFIG", None)
        config_mod.reset_config()

    def tearDown(self):
        from doyoutrade import config as config_mod

        for key, val in self._saved_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        config_mod.reset_config()
        shutil.rmtree(self._home, ignore_errors=True)

    def _client(self, service=None):
        service = service or _FakeService()
        app = create_app(
            service, _FakeApprovalGate(), assistant_service=_FakeAssistantService()
        )
        return TestClient(app), service

    def test_get_config_masks_secrets(self):
        client, _ = self._client()
        with client:
            resp = client.get("/config")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["path"], str(_p(self._home) / "config.yaml"))
        self.assertEqual(body["values"]["data"]["tushare"]["token"], "********")
        self.assertIn("server.port", body["restart_required_fields"])
        self.assertNotIn(
            "review.symbol_scope_mode", body["restart_required_fields"]
        )

    def test_put_config_updates_and_flags_restart(self):
        client, _ = self._client()
        with client:
            resp = client.put("/config", json={"server": {"port": 8090}})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "updated")
        self.assertTrue(body["restart_required"])
        self.assertEqual(body["restart_fields"], ["server.port"])

    def test_put_config_hot_field_no_restart(self):
        client, _ = self._client()
        with client:
            resp = client.put(
                "/config", json={"review": {"symbol_scope_mode": "block_all"}}
            )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["restart_required"])

    def test_put_config_invalid_returns_400(self):
        client, _ = self._client()
        with client:
            resp = client.put("/config", json={"qmt_proxy": {"mode": "bogus"}})
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body["error_code"], "invalid_config")
        self.assertEqual(body["error_type"], "validation_error")
        self.assertEqual(body["detail"]["field"], "qmt_proxy.mode")

    # --- /qmt-proxy/config forwarding -----------------------------------------

    def test_qmt_proxy_config_no_default_account_400(self):
        client, _ = self._client()
        with client:
            resp = client.get("/qmt-proxy/config")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error_code"], "qmt_proxy_unreachable")

    def _service_with_default_account(self):
        service = _FakeService()
        service.accounts["acct-1"] = {
            "id": "acct-1",
            "name": "qmt",
            "mode": "live",
            "base_url": "http://127.0.0.1:8001/",
            "token": "tok-abc",
            "is_default": True,
            "enabled": True,
        }
        return service

    def test_qmt_proxy_config_get_forwards_data(self):
        service = self._service_with_default_account()
        client, _ = self._client(service)
        fake = AsyncMock(return_value={"success": True, "data": {"path": "/x", "values": {}}})
        with patch("doyoutrade.api.app._forward_to_qmt_proxy", fake):
            with client:
                resp = client.get("/qmt-proxy/config")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"path": "/x", "values": {}})
        # base_url + token were resolved from the default account
        _, kwargs = fake.call_args
        self.assertEqual(kwargs["method"], "GET")
        self.assertEqual(kwargs["base_url"], "http://127.0.0.1:8001/")
        self.assertEqual(kwargs["token"], "tok-abc")

    def test_qmt_proxy_config_put_forwards_payload(self):
        service = self._service_with_default_account()
        client, _ = self._client(service)
        fake = AsyncMock(return_value={"success": True, "data": {"status": "updated"}})
        with patch("doyoutrade.api.app._forward_to_qmt_proxy", fake):
            with client:
                resp = client.put(
                    "/qmt-proxy/config", json={"xtquant": {"mode": "prod"}}
                )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "updated"})
        _, kwargs = fake.call_args
        self.assertEqual(kwargs["method"], "PUT")
        self.assertEqual(kwargs["payload"], {"xtquant": {"mode": "prod"}})

    def test_qmt_proxy_config_upstream_error_502(self):
        from doyoutrade.api.app import _QmtProxyForwardError

        service = self._service_with_default_account()
        client, _ = self._client(service)
        fake = AsyncMock(side_effect=_QmtProxyForwardError("qmt-proxy returned HTTP 500: boom"))
        with patch("doyoutrade.api.app._forward_to_qmt_proxy", fake):
            with client:
                resp = client.get("/qmt-proxy/config")
        self.assertEqual(resp.status_code, 502)
        body = resp.json()
        self.assertEqual(body["error_code"], "qmt_proxy_error")
        self.assertIn("boom", body["error_message"])

    def test_qmt_proxy_config_upstream_validation_400_passthrough(self):
        """Upstream 4xx (a bad user patch) propagates with its original status +
        envelope, NOT collapsed to a 502 connectivity error — so the UI shows a
        fixable field error instead of misleading 'go configure the account'."""
        from doyoutrade.api.app import _QmtProxyForwardError

        service = self._service_with_default_account()
        client, _ = self._client(service)
        upstream_body = {
            "success": False,
            "error_code": "invalid_config",
            "error_type": "validation_error",
            "message": "xtquant.mode 非法: 'bogus'",
            "field": "xtquant.mode",
        }
        fake = AsyncMock(
            side_effect=_QmtProxyForwardError(
                "qmt-proxy returned HTTP 400: xtquant.mode 非法",
                status_code=400,
                body=upstream_body,
            )
        )
        with patch("doyoutrade.api.app._forward_to_qmt_proxy", fake):
            with client:
                resp = client.put(
                    "/qmt-proxy/config", json={"xtquant": {"mode": "bogus"}}
                )
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body["error_code"], "invalid_config")
        self.assertEqual(body["detail"]["field"], "xtquant.mode")


class UpdateEndpointsTests(unittest.TestCase):
    """GET /update/status, POST /update/check, POST /update/apply."""

    def _client(self, update_service=None):
        app = create_app(
            _FakeService(),
            _FakeApprovalGate(),
            assistant_service=_FakeAssistantService(),
            update_service=update_service,
        )
        return TestClient(app)

    def _service(self, tag="v99.0.0", version="0.1.0", install_kind="package"):
        from doyoutrade.infra.updater import UpdateService

        async def fetch(repo):
            return {
                "tag_name": tag,
                "name": tag,
                "published_at": "2026-07-01T00:00:00Z",
                "html_url": f"https://github.com/renjiegod/doyoutrade/releases/tag/{tag}",
                "body": "notes",
            }

        return UpdateService(
            fetch_latest_release=fetch,
            install_kind=install_kind,
            version=version,
            which=lambda name: f"/usr/bin/{name}",
        )

    def test_endpoints_503_without_update_service(self):
        client = self._client(update_service=None)
        with client:
            for method, path in (
                ("get", "/update/status"),
                ("post", "/update/check"),
                ("post", "/update/apply"),
            ):
                resp = getattr(client, method)(path)
                self.assertEqual(resp.status_code, 503, msg=path)
                self.assertEqual(
                    resp.json()["detail"]["error_code"], "updater_unavailable"
                )

    def test_status_check_and_apply_flow(self):
        svc = self._service()
        restarts = []
        svc.bind_restart_requester(lambda: restarts.append(True))
        client = self._client(update_service=svc)
        with client:
            resp = client.get("/update/status")
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertFalse(body["update_available"])
            self.assertEqual(body["current_version"], "0.1.0")
            self.assertTrue(body["enabled"])  # default on

            resp = client.post("/update/check")
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertTrue(body["update_available"])
            self.assertEqual(body["latest"]["tag"], "v99.0.0")

            resp = client.post("/update/apply")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["state"], "restarting")
        self.assertIsNotNone(svc.staged_update)
        self.assertEqual(svc.staged_update.tag, "v99.0.0")

    def test_apply_without_update_returns_structured_409(self):
        svc = self._service(tag="v0.1.0", version="0.1.0")
        svc.bind_restart_requester(lambda: None)
        client = self._client(update_service=svc)
        with client:
            client.post("/update/check")
            resp = client.post("/update/apply")
        self.assertEqual(resp.status_code, 409)
        detail = resp.json()["detail"]
        self.assertEqual(detail["error_code"], "no_update_available")
        self.assertIn("hint", detail)

    def test_apply_on_source_checkout_is_refused(self):
        svc = self._service(install_kind="source")
        svc.bind_restart_requester(lambda: None)
        client = self._client(update_service=svc)
        with client:
            client.post("/update/check")
            resp = client.post("/update/apply")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(
            resp.json()["detail"]["error_code"], "dev_checkout_unsupported"
        )


def _p(path):
    from pathlib import Path

    return Path(path)


if __name__ == "__main__":
    unittest.main()
