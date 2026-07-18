"""Migration coverage for knowledge-graph source ownership and active edges."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class KnowledgeGraphProjectionIntegrityMigrationTests(unittest.TestCase):
    """The P0 migration must preserve history while enforcing one active edge."""

    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.db_path = Path(self._tempdir.name) / "kg-migration.db"
        self.db_url = f"sqlite:///{self.db_path}"

    def _upgrade(self, revision: str) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "alembic",
                "-x",
                f"db_url={self.db_url}",
                "upgrade",
                revision,
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"upgrade failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

    def test_upgrade_backfills_source_and_expires_duplicate_active_edge(self) -> None:
        self._upgrade("20260717_01")
        with sqlite3.connect(self.db_path) as connection:
            connection.executemany(
                """
                INSERT INTO kg_nodes (
                    id, node_type, name, display_name, attrs, created_at, updated_at
                ) VALUES (?, ?, ?, NULL, NULL, ?, ?)
                """,
                [
                    (
                        "kgn-signal",
                        "signal",
                        "dsig-1",
                        "2026-07-17 10:00:00",
                        "2026-07-17 10:00:00",
                    ),
                    (
                        "kgn-symbol",
                        "symbol",
                        "300059",
                        "2026-07-17 10:00:00",
                        "2026-07-17 10:00:00",
                    ),
                ],
            )
            connection.executemany(
                """
                INSERT INTO kg_edges (
                    id, src_id, dst_id, relation, fact, attrs, dedupe_key,
                    state_key, provenance, confidence, source_ref, valid_at,
                    invalid_at, created_at, expired_at
                ) VALUES (?, 'kgn-signal', 'kgn-symbol', 'signals', ?, NULL,
                          'signal|dsig-1', NULL, 'deterministic', NULL,
                          'db:decision_signals/dsig-1', NULL, NULL, ?, NULL)
                """,
                [
                    ("kge-old", "旧事实", "2026-07-17 10:00:00"),
                    ("kge-new", "新事实", "2026-07-17 11:00:00"),
                ],
            )
            connection.commit()

        self._upgrade("head")

        with sqlite3.connect(self.db_path) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertTrue(
                {
                    "kg_graph_state",
                    "kg_change_sets",
                    "kg_change_operations",
                    "kg_revisions",
                    "kg_approval_decisions",
                    "kg_schema_items",
                }.issubset(tables)
            )
            revision_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info('kg_revisions')"
                )
            }
            self.assertIn("reverts_revision", revision_columns)
            self.assertIn("replays_revision", revision_columns)
            self.assertEqual(
                connection.execute(
                    "SELECT head_revision FROM kg_graph_state "
                    "WHERE state_key = 'default'"
                ).fetchone(),
                (0,),
            )
            rows = connection.execute(
                """
                SELECT id, source_key, expired_at
                FROM kg_edges
                ORDER BY id
                """
            ).fetchall()
            self.assertEqual(
                {row[1] for row in rows},
                {"db:decision_signals"},
            )
            active = [row for row in rows if row[2] is None]
            self.assertEqual([row[0] for row in active], ["kge-new"])
            indexes = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )
            }
            self.assertIn("uq_kg_edges_active_dedupe", indexes)
            self.assertIn("ix_kg_edges_source_active", indexes)

            connection.execute(
                """
                INSERT INTO kg_edges (
                    id, src_id, dst_id, relation, fact, attrs, dedupe_key,
                    state_key, provenance, confidence, source_key, source_ref,
                    valid_at, invalid_at, created_at, expired_at
                ) VALUES (
                    'kge-manual', 'kgn-signal', 'kgn-symbol', 'signals',
                    '人工事实', NULL, 'manual|1', NULL, 'manual', 1.0,
                    'manual:test', 'manual:test', NULL, NULL,
                    '2026-07-17 11:30:00', NULL
                )
                """
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO kg_edges (
                        id, src_id, dst_id, relation, fact, attrs, dedupe_key,
                        state_key, provenance, confidence, source_key, source_ref,
                        valid_at, invalid_at, created_at, expired_at
                    ) VALUES (
                        'kge-duplicate', 'kgn-signal', 'kgn-symbol', 'signals',
                        '重复事实', NULL, 'signal|dsig-1', NULL, 'deterministic',
                        NULL, 'db:decision_signals',
                        'db:decision_signals/dsig-1', NULL, NULL,
                        '2026-07-17 12:00:00', NULL
                    )
                    """
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
