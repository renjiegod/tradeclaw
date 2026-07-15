"""Swarm DAG 纯算法：环检测、拓扑分层、依赖解析。

移植自 Vibe-Trading ``agent/src/swarm/task_store.py`` 的纯算法部分，去掉了文件
IO —— 这里全部在内存 task 列表上运算，持久化交给 ``store.py``。
"""

from __future__ import annotations

from collections import defaultdict, deque

from doyoutrade.swarm.models import SwarmTask, TaskStatus


def validate_dag(tasks: list[SwarmTask]) -> None:
    """DFS 环检测，确保任务 DAG 无环且依赖都存在。

    Args:
        tasks: SwarmTask 列表。

    Raises:
        ValueError: 检测到环（消息含环路径）或依赖了未知任务。
    """
    graph: dict[str, list[str]] = {t.id: list(t.depends_on) for t in tasks}
    all_ids = {t.id for t in tasks}

    for task in tasks:
        for dep in task.depends_on:
            if dep not in all_ids:
                raise ValueError(f"Task '{task.id}' depends on unknown task '{dep}'")

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {tid: WHITE for tid in all_ids}
    path: list[str] = []

    def dfs(node: str) -> None:
        color[node] = GRAY
        path.append(node)
        for neighbor in graph.get(node, []):
            if color[neighbor] == GRAY:
                cycle_start = path.index(neighbor)
                cycle = path[cycle_start:] + [neighbor]
                raise ValueError(f"Cycle detected in task DAG: {' -> '.join(cycle)}")
            if color[neighbor] == WHITE:
                dfs(neighbor)
        path.pop()
        color[node] = BLACK

    for tid in all_ids:
        if color[tid] == WHITE:
            dfs(tid)


def topological_layers(tasks: list[SwarmTask]) -> list[list[str]]:
    """Kahn 算法拓扑分层；同层任务可并行执行。

    Args:
        tasks: SwarmTask 列表（须为合法无环 DAG）。

    Returns:
        按执行顺序排列的层列表，每层是可并行的任务 id 列表。

    Raises:
        ValueError: DAG 含环（拓扑排序无法完成）。
    """
    in_degree: dict[str, int] = {t.id: 0 for t in tasks}
    dependents: dict[str, list[str]] = defaultdict(list)

    for task in tasks:
        in_degree[task.id] = len(task.depends_on)
        for dep in task.depends_on:
            dependents[dep].append(task.id)

    queue: deque[str] = deque(tid for tid, deg in in_degree.items() if deg == 0)
    layers: list[list[str]] = []
    processed = 0

    while queue:
        layer: list[str] = list(queue)
        queue.clear()
        layers.append(layer)
        processed += len(layer)
        for tid in layer:
            for downstream in dependents[tid]:
                in_degree[downstream] -= 1
                if in_degree[downstream] == 0:
                    queue.append(downstream)

    if processed != len(tasks):
        raise ValueError(
            f"DAG contains a cycle: processed {processed}/{len(tasks)} tasks"
        )

    return layers


def resolve_dependencies(
    tasks: list[SwarmTask], completed_task_id: str
) -> list[str]:
    """在内存 task 列表上，把已完成任务从所有下游的 blocked_by 中移除。

    若某任务 blocked_by 变空且状态为 blocked，则标记为新解锁（pending）。
    原地修改传入的 task 对象。

    Args:
        tasks: 全部 SwarmTask（会被原地修改）。
        completed_task_id: 刚完成的任务 id。

    Returns:
        新解锁任务 id 列表（blocked_by 从非空变空）。
    """
    newly_unblocked: list[str] = []
    for task in tasks:
        if completed_task_id not in task.blocked_by:
            continue
        task.blocked_by = [tid for tid in task.blocked_by if tid != completed_task_id]
        if not task.blocked_by and task.status == TaskStatus.blocked:
            task.status = TaskStatus.pending
            newly_unblocked.append(task.id)
    return newly_unblocked
