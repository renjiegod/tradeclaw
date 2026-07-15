"""Swarm YAML preset 加载器。

从本模块旁的 ``presets/`` 目录读取 YAML preset，解析为 SwarmRun /
SwarmAgentSpec / SwarmTask。preset 引用的 tools / skills 必须是 doyoutrade 真实
注册的工具名与 ``doyoutrade/skills`` 下真实存在的 skill。

移植自 Vibe-Trading ``agent/src/swarm/presets.py``。
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from string import Formatter

import yaml

from doyoutrade.swarm.dag import topological_layers, validate_dag
from doyoutrade.swarm.models import (
    RunStatus,
    SwarmAgentSpec,
    SwarmRun,
    SwarmTask,
    TaskStatus,
)

PRESETS_DIR = Path(__file__).resolve().parent / "presets"
_INTERNAL_TEMPLATE_VARS = {"upstream_context"}


def load_preset(name: str) -> dict:
    """按名加载 YAML preset。

    Raises:
        FileNotFoundError: preset 文件不存在。
    """
    path = PRESETS_DIR / f"{name}.yaml"
    if not path.exists():
        available = (
            [p.stem for p in PRESETS_DIR.glob("*.yaml")] if PRESETS_DIR.exists() else []
        )
        raise FileNotFoundError(f"Preset {name!r} not found. Available: {available}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def list_presets() -> list[dict]:
    """返回全部可用 preset 的摘要信息。

    Returns:
        每项含 name / title / description / agent_count / variables。
    """
    if not PRESETS_DIR.exists():
        return []
    results: list[dict] = []
    for path in sorted(PRESETS_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        results.append(
            {
                "name": data.get("name", path.stem),
                "title": data.get("title", ""),
                "description": data.get("description", ""),
                "agent_count": len(data.get("agents", [])),
                "variables": data.get("variables", []),
            }
        )
    return results


def _declared_variable_names(raw_variables: list) -> set[str]:
    names: set[str] = set()
    for item in raw_variables:
        name = item.get("name") if isinstance(item, dict) else str(item)
        if name:
            names.add(str(name))
    return names


def _template_variables(template: str) -> set[str]:
    """返回提示词模板引用的 Python format 字段。"""
    variables: set[str] = set()
    for _, field_name, _, _ in Formatter().parse(template or ""):
        if not field_name:
            continue
        root = field_name.split(".", 1)[0].split("[", 1)[0]
        if root and root not in _INTERNAL_TEMPLATE_VARS:
            variables.add(root)
    return variables


def build_run_from_preset(preset_name: str, user_vars: dict[str, str]) -> SwarmRun:
    """从 preset + 用户变量构造一个 SwarmRun（status=pending）。

    Raises:
        FileNotFoundError: preset 不存在。
        KeyError / ValueError: preset YAML 缺字段或非法。
    """
    data = load_preset(preset_name)

    agents: list[SwarmAgentSpec] = []
    for agent_data in data.get("agents", []):
        agents.append(
            SwarmAgentSpec(
                id=agent_data["id"],
                role=agent_data.get("role", ""),
                system_prompt=agent_data.get("system_prompt", ""),
                tools=agent_data.get("tools", []),
                skills=agent_data.get("skills", []),
                max_iterations=agent_data.get("max_iterations", 25),
                timeout_seconds=agent_data.get("timeout_seconds", 600),
                model_route_name=agent_data.get("model_route_name", ""),
                max_retries=agent_data.get("max_retries", 1),
            )
        )

    tasks: list[SwarmTask] = []
    for task_data in data.get("tasks", []):
        depends_on = task_data.get("depends_on", [])
        status = TaskStatus.blocked if depends_on else TaskStatus.pending
        tasks.append(
            SwarmTask(
                id=task_data["id"],
                agent_id=task_data["agent_id"],
                prompt_template=task_data.get("prompt_template", ""),
                depends_on=depends_on,
                blocked_by=list(depends_on),
                input_from=task_data.get("input_from", {}),
                status=status,
            )
        )

    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%d-%H%M%S")
    short_uuid = uuid.uuid4().hex[:8]
    run_id = f"swarm-{ts}-{short_uuid}"

    return SwarmRun(
        id=run_id,
        preset_name=preset_name,
        status=RunStatus.pending,
        user_vars=user_vars,
        agents=agents,
        tasks=tasks,
        created_at=now.isoformat(),
    )


def inspect_preset(name: str) -> dict:
    """校验 preset 并返回 dry-run 执行计划（不启动 worker、不调 LLM）。

    捕获常见 YAML / DAG 错误，并暴露运行时使用的拓扑层级。
    """
    data = load_preset(name)
    run = build_run_from_preset(name, {})

    errors: list[str] = []
    warnings: list[str] = []

    agent_ids = [agent.id for agent in run.agents]
    task_ids = [task.id for task in run.tasks]
    agent_id_set = set(agent_ids)
    task_id_set = set(task_ids)

    for dup in sorted(i for i, c in Counter(agent_ids).items() if c > 1):
        errors.append(f"Duplicate agent id: {dup}")
    for dup in sorted(i for i, c in Counter(task_ids).items() if c > 1):
        errors.append(f"Duplicate task id: {dup}")

    for task in run.tasks:
        if task.agent_id not in agent_id_set:
            errors.append(
                f"Task '{task.id}' references unknown agent '{task.agent_id}'"
            )
        for _, upstream_task_id in task.input_from.items():
            if upstream_task_id not in task_id_set:
                errors.append(
                    f"Task '{task.id}' input_from references unknown task "
                    f"'{upstream_task_id}'"
                )

    layers: list[list[str]] = []
    try:
        validate_dag(run.tasks)
        layers = topological_layers(run.tasks)
    except ValueError as exc:
        errors.append(str(exc))

    dependents: dict[str, list[str]] = defaultdict(list)
    for task in run.tasks:
        for dep in task.depends_on:
            dependents[dep].append(task.id)

    def is_upstream(candidate: str, task_id: str) -> bool:
        seen: set[str] = set()
        stack = [candidate]
        while stack:
            current = stack.pop()
            if current == task_id:
                return True
            if current in seen:
                continue
            seen.add(current)
            stack.extend(dependents.get(current, []))
        return False

    for task in run.tasks:
        for key, upstream_task_id in task.input_from.items():
            if upstream_task_id in task_id_set and not is_upstream(
                upstream_task_id, task.id
            ):
                warnings.append(
                    f"Task '{task.id}' input_from '{key}' references "
                    f"'{upstream_task_id}', which is not upstream in the DAG"
                )

    declared_variables = _declared_variable_names(data.get("variables", []))
    used_variables: set[str] = set()
    for task in data.get("tasks", []):
        try:
            used_variables.update(_template_variables(task.get("prompt_template", "")))
        except ValueError as exc:
            errors.append(
                f"Task '{task.get('id', '?')}' has invalid prompt template: {exc}"
            )

    missing = sorted(used_variables - declared_variables)
    unused = sorted(declared_variables - used_variables)
    if missing:
        warnings.append("Prompt templates use undeclared variables: " + ", ".join(missing))
    if unused:
        warnings.append(
            "Declared variables are not used by task prompt templates: "
            + ", ".join(unused)
        )

    task_agent = {task.id: task.agent_id for task in run.tasks}
    return {
        "name": data.get("name", name),
        "title": data.get("title", ""),
        "description": data.get("description", ""),
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "variables": sorted(declared_variables),
        "used_variables": sorted(used_variables),
        "agents": [
            {"id": a.id, "role": a.role, "tools": a.tools, "skills": a.skills}
            for a in run.agents
        ],
        "tasks": [
            {
                "id": t.id,
                "agent_id": t.agent_id,
                "depends_on": t.depends_on,
                "input_from": t.input_from,
            }
            for t in run.tasks
        ],
        "layers": [
            [{"task_id": tid, "agent_id": task_agent.get(tid, "")} for tid in layer]
            for layer in layers
        ],
    }
