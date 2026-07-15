"""Tests for the xtdata diagnostics ring buffer + JSONL mirror."""
import json

import pytest

from app.utils import diagnostics


@pytest.fixture(autouse=True)
def _clean_buffer(tmp_path, monkeypatch):
    # Redirect the JSONL file into a temp dir so tests don't touch logs/.
    monkeypatch.setattr(diagnostics, "_JSONL_PATH", str(tmp_path / "xtdata_ops.jsonl"))
    diagnostics._reset_for_tests()
    yield
    diagnostics._reset_for_tests()


def test_record_and_recent_basic():
    diagnostics.record(
        operation="get_market_data",
        kwargs={"stock_list": ["000001.SZ"], "period": "1d"},
        duration_ms=12.3,
        ok=True,
    )
    records = diagnostics.recent(limit=10)
    assert len(records) == 1
    entry = records[0]
    assert entry["operation"] == "get_market_data"
    assert entry["ok"] is True
    assert entry["duration_ms"] == 12.3
    assert entry["kwargs_summary"]["stock_list"] == ["000001.SZ"]
    assert entry["kwargs_summary"]["period"] == "1d"
    assert "timestamp" in entry


def test_recent_is_newest_first():
    for i in range(3):
        diagnostics.record(
            operation="op", kwargs={"count": i}, duration_ms=float(i), ok=True
        )
    records = diagnostics.recent(limit=10)
    assert [r["kwargs_summary"]["count"] for r in records] == [2, 1, 0]


def test_recent_filters_only_errors():
    diagnostics.record(operation="a", kwargs={}, duration_ms=1.0, ok=True)
    diagnostics.record(
        operation="b", kwargs={}, duration_ms=2.0, ok=False, error="boom", exit_code=1
    )
    errors = diagnostics.recent(limit=10, only_errors=True)
    assert len(errors) == 1
    assert errors[0]["operation"] == "b"
    assert errors[0]["error"] == "boom"
    assert errors[0]["exit_code"] == 1


def test_recent_filters_min_duration_and_operation():
    diagnostics.record(operation="fast", kwargs={}, duration_ms=5.0, ok=True)
    diagnostics.record(operation="slow", kwargs={}, duration_ms=4000.0, ok=True)

    slow = diagnostics.recent(limit=10, min_duration_ms=1000)
    assert len(slow) == 1
    assert slow[0]["operation"] == "slow"

    only_fast = diagnostics.recent(limit=10, operation="fast")
    assert len(only_fast) == 1
    assert only_fast[0]["operation"] == "fast"


def test_recent_limit():
    for i in range(10):
        diagnostics.record(operation="op", kwargs={}, duration_ms=1.0, ok=True)
    assert len(diagnostics.recent(limit=3)) == 3


def test_summary_aggregates_by_operation():
    diagnostics.record(operation="get_market_data", kwargs={}, duration_ms=10.0, ok=True)
    diagnostics.record(operation="get_market_data", kwargs={}, duration_ms=30.0, ok=True)
    diagnostics.record(
        operation="get_market_data", kwargs={}, duration_ms=50.0, ok=False, error="x"
    )
    diagnostics.record(operation="get_local_data", kwargs={}, duration_ms=5.0, ok=True)

    summary = diagnostics.summary()
    assert summary["total"] == 4
    assert summary["error_count"] == 1

    gmd = summary["operations"]["get_market_data"]
    assert gmd["count"] == 3
    assert gmd["error_count"] == 1
    assert gmd["avg_duration_ms"] == 30.0  # (10+30+50)/3
    assert gmd["max_duration_ms"] == 50.0

    gld = summary["operations"]["get_local_data"]
    assert gld["count"] == 1
    assert gld["error_count"] == 0


def test_summarize_kwargs_handles_nested_download_and_get():
    summary = diagnostics.summarize_kwargs(
        "download_and_get_market_data",
        {
            "download": {"stock_code": "000001.SZ", "period": "1d"},
            "market": {"stock_list": ["000001.SZ"], "period": "1d", "count": -1},
        },
    )
    assert summary["download.stock_code"] == "000001.SZ"
    assert summary["market.stock_list"] == ["000001.SZ"]
    assert summary["market.count"] == -1


def test_summarize_kwargs_is_robust_to_non_dict():
    summary = diagnostics.summarize_kwargs("op", ["not", "a", "dict"])
    assert "_note" in summary


def test_record_writes_jsonl_line():
    diagnostics.record(
        operation="get_market_data",
        kwargs={"period": "1d"},
        duration_ms=7.0,
        ok=True,
    )
    with open(diagnostics._JSONL_PATH, encoding="utf-8") as fh:
        lines = fh.readlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["operation"] == "get_market_data"
    assert parsed["ok"] is True


def test_record_jsonl_failure_does_not_raise(monkeypatch):
    # Point the JSONL path at a directory that cannot be created (parent is a file).
    bad_parent = diagnostics._JSONL_PATH  # an existing-ish path; force a failure
    monkeypatch.setattr(diagnostics, "_JSONL_PATH", "/dev/null/cannot/exist.jsonl")
    warnings = []
    monkeypatch.setattr(
        diagnostics.logger, "warning", lambda msg, *a, **k: warnings.append(msg)
    )
    # Should not raise even though the write fails.
    diagnostics.record(operation="op", kwargs={}, duration_ms=1.0, ok=True)
    # Ring buffer still recorded.
    assert len(diagnostics.recent(limit=5)) == 1
    # And the failure was surfaced via logger.warning, not swallowed silently.
    assert warnings, "expected a logger.warning on JSONL write failure"
    _ = bad_parent
