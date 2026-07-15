"""诊断记录按 client_id 打标 / 过滤 / 分终端统计测试。"""
from app.utils import diagnostics


def _record(client_id: str, *, ok: bool = True, operation: str = "get_market_data"):
    diagnostics.record(
        operation=operation,
        kwargs={"stock_code": "000001.SZ"},
        duration_ms=12.5,
        ok=ok,
        client_id=client_id,
    )


def test_record_labels_client_id_and_recent_filters():
    diagnostics._reset_for_tests()
    _record("dgzq")
    _record("gj", ok=False)
    _record("dgzq")

    all_recent = diagnostics.recent()
    assert {e["client_id"] for e in all_recent} == {"dgzq", "gj"}

    only_dgzq = diagnostics.recent(client_id="dgzq")
    assert len(only_dgzq) == 2
    assert all(e["client_id"] == "dgzq" for e in only_dgzq)

    only_gj = diagnostics.recent(client_id="gj")
    assert len(only_gj) == 1
    assert only_gj[0]["ok"] is False


def test_summary_breaks_down_by_client():
    diagnostics._reset_for_tests()
    _record("dgzq")
    _record("dgzq")
    _record("gj", ok=False)

    summary = diagnostics.summary()
    assert summary["total"] == 3
    assert summary["error_count"] == 1
    assert summary["clients"]["dgzq"] == {"count": 2, "error_count": 0}
    assert summary["clients"]["gj"] == {"count": 1, "error_count": 1}

    scoped = diagnostics.summary(client_id="gj")
    assert scoped["client_id"] == "gj"
    assert scoped["total"] == 1
    assert scoped["error_count"] == 1
    assert "clients" not in scoped


def test_record_defaults_client_id_to_default():
    diagnostics._reset_for_tests()
    diagnostics.record(operation="get_market_data", kwargs={}, duration_ms=1.0, ok=True)
    assert diagnostics.recent()[0]["client_id"] == "default"
