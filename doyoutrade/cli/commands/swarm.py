"""`doyoutrade-cli swarm ...` 命令：多智能体 Swarm 团队的薄 HTTP 适配器。

与 ``assistant`` 命令一致——API server 拥有 swarm 运行时状态，本模块只是把
``/swarm/*`` 端点包成 envelope，供编程 agent 在不开浏览器的情况下启动/查看
swarm 运行。
"""

from __future__ import annotations

import json
from typing import Any

import click

from doyoutrade.cli._api import invoke_api
from doyoutrade.cli._envelope import error_envelope
from doyoutrade.cli._invoke import read_session_meta
from doyoutrade.cli.main import run_async_command


@click.group()
def swarm() -> None:
    """多智能体 Swarm 团队命令。"""


@swarm.command("list-presets")
def swarm_list_presets() -> None:
    """列出全部可用的 swarm preset 团队。"""

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api("GET", "/swarm/presets", meta=read_session_meta())

    click.get_current_context().exit(run_async_command(_run))


@swarm.command("inspect")
@click.argument("preset")
def swarm_inspect(preset: str) -> None:
    """对一个 preset 做 dry-run 校验，输出 DAG 层级（不启动 worker）。"""

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "GET",
            f"/swarm/presets/{preset}",
            meta=read_session_meta(),
            not_found_error_code="preset_not_found",
        )

    click.get_current_context().exit(run_async_command(_run))


@swarm.command("run")
@click.argument("preset")
@click.argument("vars_json", required=False, default="{}")
def swarm_run(preset: str, vars_json: str) -> None:
    """启动一个 swarm run。VARS_JSON 是模板变量的 JSON，如 '{"target":"AAPL","market":"US"}'。

    立即返回 run 句柄；用 `swarm show <run_id>` 查看进度，或订阅
    `/swarm/runs/<id>/events/stream` 看实时状态。
    """

    try:
        user_vars = json.loads(vars_json) if vars_json else {}
        if not isinstance(user_vars, dict):
            raise ValueError("vars_json 必须是 JSON 对象")
    except (ValueError, TypeError) as exc:
        envelope = error_envelope(
            error_code="invalid_vars_json",
            error_type=type(exc).__name__,
            message=f"无法解析 vars_json：{exc}",
            meta=read_session_meta(),
        )
        click.echo(json.dumps(envelope, ensure_ascii=False, indent=2))
        click.get_current_context().exit(2)
        return

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "POST",
            "/swarm/runs",
            json={"preset_name": preset, "user_vars": user_vars},
            meta=read_session_meta(),
            not_found_error_code="preset_not_found",
        )

    click.get_current_context().exit(run_async_command(_run))


@swarm.command("list")
@click.option("--limit", type=int, default=50, show_default=True)
def swarm_list(limit: int) -> None:
    """列出 swarm 运行历史。"""

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "GET", "/swarm/runs", params={"limit": limit}, meta=read_session_meta()
        )

    click.get_current_context().exit(run_async_command(_run))


@swarm.command("show")
@click.argument("run_id")
def swarm_show(run_id: str) -> None:
    """查看一个 swarm run 的详情（含各任务状态与最终报告）。"""

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "GET",
            f"/swarm/runs/{run_id}",
            meta=read_session_meta(),
            not_found_error_code="swarm_run_not_found",
        )

    click.get_current_context().exit(run_async_command(_run))


@swarm.command("cancel")
@click.argument("run_id")
def swarm_cancel(run_id: str) -> None:
    """取消一个运行中的 swarm。"""

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "POST",
            f"/swarm/runs/{run_id}/cancel",
            meta=read_session_meta(),
            not_found_error_code="swarm_run_not_found",
        )

    click.get_current_context().exit(run_async_command(_run))
