"""Tests covering idempotent behavior of the 20260422 rename migrations.

Context: the 20260422 rename chain was added in a live development repo where
some databases had already applied an earlier (now-reverted) version of the
Task/Run rename. That earlier work physically renamed ``instances`` -> ``tasks``
and ``backtest_jobs`` -> ``runs`` (plus added ``mode`` on ``runs``), but left
``cycle_runs`` / ``debug_sessions`` / ``model_invocations`` with ``instance_id``
columns and left the alembic version stamped at ``20260422_01``.

Running ``alembic upgrade head`` against that half-migrated state must succeed
without clobbering data — migrations 01/02/03 need to be idempotent: detect
already-renamed objects and skip them, and create any missing indexes/columns.

These tests simulate the three realistic starting states against a sqlite DB:

1. Fresh DB (already covered by ``test_persistence.test_alembic_upgrade_creates_runtime_schema``).
2. Fully rename-applied DB stamped at ``20260422_01`` — mimics the user's
   Postgres box after the reverted Task/Run refactor commits.
3. Half-applied DB stamped at ``20260422_02`` — mimics a box where only 02
   completed (not expected in practice but guards against partial re-runs).
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _alembic(args: list[str], db_url: str) -> subprocess.CompletedProcess[str]:
    """Run ``alembic`` with the given args against ``db_url``."""

    return subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"db_url={db_url}", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )


def _tables(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }


def _columns(db_path: Path, table: str) -> set[str]:
    with sqlite3.connect(db_path) as connection:
        return {row[1] for row in connection.execute(f"PRAGMA table_info('{table}')")}


def _indexes(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }


def _alembic_version(db_path: Path) -> str | None:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    return row[0] if row else None


class IdempotentRenameMigrationTests(unittest.TestCase):
    """``alembic upgrade head`` must converge from already-renamed states."""

    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.db_path = Path(self._tempdir.name) / "half-migrated.db"
        self.db_url = f"sqlite:///{self.db_path}"

    def _upgrade_to(self, revision: str) -> None:
        result = _alembic(["upgrade", revision], self.db_url)
        self.assertEqual(
            result.returncode,
            0,
            msg=f"upgrade to {revision} failed:\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}",
        )

    def _stamp(self, revision: str) -> None:
        result = _alembic(["stamp", revision], self.db_url)
        self.assertEqual(
            result.returncode,
            0,
            msg=f"stamp {revision} failed:\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}",
        )

    def _simulate_already_renamed_tables(self) -> None:
        """Mimic a DB where instances/backtest_jobs were already renamed."""

        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "ALTER TABLE instances RENAME COLUMN instance_id TO task_id"
            )
            connection.execute("ALTER TABLE instances RENAME TO tasks")
            connection.execute("DROP INDEX ix_backtest_jobs_instance_created")
            connection.execute(
                "ALTER TABLE backtest_jobs RENAME COLUMN instance_id TO task_id"
            )
            connection.execute(
                "ALTER TABLE backtest_jobs RENAME COLUMN backtest_job_id TO run_id"
            )
            connection.execute("ALTER TABLE backtest_jobs RENAME TO runs")
            connection.execute(
                "ALTER TABLE runs ADD COLUMN \"mode\" VARCHAR(16) NOT NULL DEFAULT 'backtest'"
            )
            connection.commit()

    def test_upgrade_is_noop_when_tables_and_columns_already_renamed(self) -> None:
        """Reproduces the user's Postgres half-migration: tables renamed, stamped at _01."""

        self._upgrade_to("20260411_01")
        self._simulate_already_renamed_tables()
        self._stamp("20260422_01")

        self._upgrade_to("head")

        tables = _tables(self.db_path)
        self.assertIn("tasks", tables)
        self.assertIn("runs", tables)
        self.assertNotIn("instances", tables)
        self.assertNotIn("backtest_jobs", tables)

        self.assertIn("task_id", _columns(self.db_path, "tasks"))
        runs_columns = _columns(self.db_path, "runs")
        self.assertIn("task_id", runs_columns)
        self.assertIn("run_id", runs_columns)
        self.assertIn("mode", runs_columns)

        for table in ("cycle_runs", "debug_sessions", "model_invocations"):
            cols = _columns(self.db_path, table)
            self.assertIn("task_id", cols, msg=f"{table} should have task_id")
            self.assertNotIn("instance_id", cols, msg=f"{table} should not have instance_id")

        indexes = _indexes(self.db_path)
        self.assertIn("ix_runs_task_created", indexes)
        self.assertIn("ix_cycle_runs_task_started", indexes)
        self.assertIn("ix_debug_sessions_task_created_at", indexes)
        self.assertNotIn("ix_cycle_runs_instance_started", indexes)
        self.assertNotIn("ix_debug_sessions_instance_created_at", indexes)

        # alembic_version should advance to latest head.
        self.assertNotEqual(_alembic_version(self.db_path), "20260422_01")

    def test_upgrade_succeeds_from_fully_renamed_state_stamped_at_02(self) -> None:
        """If 02 already applied its column renames but 03 never ran, 03 must no-op safely."""

        self._upgrade_to("20260411_01")
        # Apply 02-equivalent column/index state manually on top of squashed schema.
        with sqlite3.connect(self.db_path) as connection:
            connection.execute("DROP INDEX ix_cycle_runs_instance_started")
            connection.execute("DROP INDEX ix_debug_sessions_instance_created_at")
            connection.execute(
                "ALTER TABLE cycle_runs RENAME COLUMN instance_id TO task_id"
            )
            connection.execute(
                "ALTER TABLE debug_sessions RENAME COLUMN instance_id TO task_id"
            )
            connection.execute(
                "ALTER TABLE model_invocations RENAME COLUMN instance_id TO task_id"
            )
            connection.execute(
                "CREATE INDEX ix_cycle_runs_task_started "
                "ON cycle_runs (task_id, wall_started_at)"
            )
            connection.execute(
                "CREATE INDEX ix_debug_sessions_task_created_at "
                "ON debug_sessions (task_id, created_at)"
            )
            connection.commit()
        self._simulate_already_renamed_tables()
        self._stamp("20260422_02")

        self._upgrade_to("head")

        tables = _tables(self.db_path)
        self.assertIn("tasks", tables)
        self.assertIn("runs", tables)

    def test_upgrade_head_remains_working_for_fresh_sqlite_db(self) -> None:
        """Regression guard: a pristine sqlite DB still upgrades cleanly."""

        self._upgrade_to("head")

        tables = _tables(self.db_path)
        self.assertIn("tasks", tables)
        self.assertIn("runs", tables)
        self.assertNotIn("instances", tables)
        self.assertNotIn("backtest_jobs", tables)
        self.assertIn("mode", _columns(self.db_path, "runs"))
        self.assertIn("context_compaction_json", _columns(self.db_path, "agents"))
        self.assertIn("tool_configs_json", _columns(self.db_path, "agents"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
