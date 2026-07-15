"""Swarm DAG 纯算法测试：环检测、拓扑分层、依赖解析。"""

from __future__ import annotations

import unittest

from doyoutrade.swarm.dag import resolve_dependencies, topological_layers, validate_dag
from doyoutrade.swarm.models import SwarmTask, TaskStatus


def _task(tid: str, depends_on: list[str] | None = None) -> SwarmTask:
    deps = depends_on or []
    return SwarmTask(
        id=tid,
        agent_id=f"agent-{tid}",
        prompt_template="",
        depends_on=deps,
        blocked_by=list(deps),
        status=TaskStatus.blocked if deps else TaskStatus.pending,
    )


class ValidateDagTests(unittest.TestCase):
    def test_accepts_acyclic(self) -> None:
        tasks = [_task("a"), _task("b", ["a"]), _task("c", ["a", "b"])]
        validate_dag(tasks)  # 不抛即通过

    def test_rejects_cycle(self) -> None:
        tasks = [_task("a", ["c"]), _task("b", ["a"]), _task("c", ["b"])]
        with self.assertRaises(ValueError) as ctx:
            validate_dag(tasks)
        self.assertIn("Cycle", str(ctx.exception))

    def test_rejects_unknown_dependency(self) -> None:
        tasks = [_task("a", ["ghost"])]
        with self.assertRaises(ValueError) as ctx:
            validate_dag(tasks)
        self.assertIn("unknown", str(ctx.exception))


class TopologicalLayersTests(unittest.TestCase):
    def test_layers_group_parallelizable_tasks(self) -> None:
        # a,b 并行 → c 依赖二者
        tasks = [_task("a"), _task("b"), _task("c", ["a", "b"])]
        layers = topological_layers(tasks)
        self.assertEqual(set(layers[0]), {"a", "b"})
        self.assertEqual(layers[1], ["c"])

    def test_diamond(self) -> None:
        tasks = [
            _task("a"),
            _task("b", ["a"]),
            _task("c", ["a"]),
            _task("d", ["b", "c"]),
        ]
        layers = topological_layers(tasks)
        self.assertEqual(layers[0], ["a"])
        self.assertEqual(set(layers[1]), {"b", "c"})
        self.assertEqual(layers[2], ["d"])


class ResolveDependenciesTests(unittest.TestCase):
    def test_unblocks_downstream_when_last_upstream_done(self) -> None:
        tasks = [_task("a"), _task("b"), _task("c", ["a", "b"])]
        c = tasks[2]
        # 先完成 a：c 仍被 b 阻塞
        newly = resolve_dependencies(tasks, "a")
        self.assertEqual(newly, [])
        self.assertEqual(c.status, TaskStatus.blocked)
        self.assertEqual(c.blocked_by, ["b"])
        # 再完成 b：c 解锁为 pending
        newly = resolve_dependencies(tasks, "b")
        self.assertEqual(newly, ["c"])
        self.assertEqual(c.status, TaskStatus.pending)
        self.assertEqual(c.blocked_by, [])


if __name__ == "__main__":
    unittest.main()
