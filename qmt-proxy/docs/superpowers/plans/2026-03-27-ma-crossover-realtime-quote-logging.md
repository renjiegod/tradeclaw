# MA Crossover Realtime Quote Logging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the realtime tick log in `examples/ma_crossover_strategy.py` to print realtime price, change percentage, turnover amount, volume, moving averages, and position state without changing trading behavior.

**Architecture:** Keep the strategy control flow intact and extract only the tick-log formatting into a small helper that can be unit-tested directly. This helper is introduced only for testability; the realtime strategy logic still changes in place at the current log site. Compute change percentage from `pre_close` when valid, print `N/A` for missing optional quote fields, and continue skipping ticks with invalid `last_price` exactly as today.

**Tech Stack:** Python 3.12, pytest, `qmt_proxy_sdk` quote models, standard library logging

---

### Task 1: Add a failing test for tick log formatting

**Files:**
- Modify: `examples/ma_crossover_strategy.py`
- Create: `tests/examples/test_ma_crossover_strategy.py`

- [ ] **Step 1: Write the failing test**

```python
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LIBS_ROOT = PROJECT_ROOT / "libs"

if str(LIBS_ROOT) not in sys.path:
    sys.path.insert(0, str(LIBS_ROOT))

from qmt_proxy_sdk.models.data import QuoteData

from examples.ma_crossover_strategy import format_tick_log_line


def test_format_tick_log_line_includes_price_change_amount_and_volume():
    quote = QuoteData(
        stock_code="600519.SH",
        last_price=1234.56,
        pre_close=1220.0,
        amount=4567890.0,
        volume=1200,
    )

    line = format_tick_log_line(
        tick_count=1,
        quote=quote,
        short_ma=1230.12,
        long_ma=1218.34,
        position_str="无",
    )

    assert "[TICK #0001] 600519.SH" in line
    assert "价格=1234.56" in line
    assert "涨跌幅=1.19%" in line
    assert "成交额=4567890.00" in line
    assert "量=1200" in line
    assert "MA5=1230.12" in line
    assert "MA20=1218.34" in line
    assert "持仓=无" in line
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/examples/test_ma_crossover_strategy.py::test_format_tick_log_line_includes_price_change_amount_and_volume -v`
Expected: FAIL because `format_tick_log_line` does not exist yet.

- [ ] **Step 3: Write minimal implementation**

```python
def format_tick_log_line(...):
    ...
```

Add a helper in `examples/ma_crossover_strategy.py` that:

- formats `last_price`, `amount`, `volume`, `short_ma`, and `long_ma`
- computes change percentage from `pre_close`
- uses `N/A` for missing optional values
- treats zero `amount` and zero `volume` as valid values
- treats missing, zero, or negative `pre_close` as invalid for percentage calculation
- uses `SHORT_MA_PERIOD` and `LONG_MA_PERIOD` for MA labels
- formats price, amount, percentage, and MAs to two decimal places

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/examples/test_ma_crossover_strategy.py::test_format_tick_log_line_includes_price_change_amount_and_volume -v`
Expected: PASS

### Task 2: Cover missing and zero-value quote fields

**Files:**
- Modify: `tests/examples/test_ma_crossover_strategy.py`
- Modify: `examples/ma_crossover_strategy.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_format_tick_log_line_uses_na_for_missing_optional_fields():
    quote = QuoteData(
        stock_code="600519.SH",
        last_price=1234.56,
    )

    line = format_tick_log_line(
        tick_count=2,
        quote=quote,
        short_ma=None,
        long_ma=None,
        position_str="无",
    )

    assert "涨跌幅=N/A" in line
    assert "成交额=N/A" in line
    assert "量=N/A" in line
    assert "MA5=N/A" in line
    assert "MA20=N/A" in line


def test_format_tick_log_line_keeps_zero_amount_and_volume():
    quote = QuoteData(
        stock_code="600519.SH",
        last_price=1234.56,
        pre_close=0,
        amount=0,
        volume=0,
    )

    line = format_tick_log_line(
        tick_count=3,
        quote=quote,
        short_ma=1230.0,
        long_ma=1218.0,
        position_str="100股",
    )

    assert "涨跌幅=N/A" in line
    assert "成交额=0.00" in line
    assert "量=0" in line
    assert "持仓=100股" in line
```

The first test should verify:

- missing `pre_close` prints `涨跌幅=N/A`
- missing `amount` prints `成交额=N/A`
- missing `volume` prints `量=N/A`

The second test should verify:

- `amount=0` prints `成交额=0.00`
- `volume=0` prints `量=0`
- `pre_close=0` prints `涨跌幅=N/A`
- `pre_close<0` also prints `涨跌幅=N/A` if covered in implementation as the invalid case

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/examples/test_ma_crossover_strategy.py -v`
Expected: FAIL with mismatched formatting output.

- [ ] **Step 3: Write minimal implementation**

Update the helper so optional-field formatting distinguishes:

- missing from zero
- valid numeric values from invalid values

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/examples/test_ma_crossover_strategy.py -v`
Expected: PASS

### Task 3: Wire the helper into realtime strategy logging

**Files:**
- Modify: `examples/ma_crossover_strategy.py`
- Test: `tests/examples/test_ma_crossover_strategy.py`

- [ ] **Step 1: Replace inline tick log formatting with helper usage**

Keep the existing realtime strategy flow unchanged and replace only the current `log.info(...)` tick message with:

```python
log.info(
    format_tick_log_line(
        tick_count=tick_count,
        quote=quote,
        short_ma=short_ma,
        long_ma=long_ma,
        position_str=position_str,
    )
)
```

- [ ] **Step 2: Run focused tests**

Run: `pytest tests/examples/test_ma_crossover_strategy.py -v`
Expected: PASS

- [ ] **Step 3: Run related regression tests**

Run: `pytest tests/unit/test_run.py tests/sdk/test_ws.py -v`
Expected: PASS

- [ ] **Step 4: Run lints/diagnostics check for edited files**

Check diagnostics for:

- `examples/ma_crossover_strategy.py`
- `tests/examples/test_ma_crossover_strategy.py`

- [ ] **Step 5: Verify no unintended behavior changes**

Confirm:

- invalid or non-positive `last_price` still skips the tick before logging
- MA calculation and signal detection logic are unchanged
- order execution paths are unchanged
