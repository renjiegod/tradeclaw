# Real Trading Query And Prod Gated Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `TradingService` use real QMT trading queries for connect/account/positions/asset/orders/trades in `dev` and `prod`, while guaranteeing that only `prod` with `allow_real_trading=true` may execute real order and cancel calls.

**Architecture:** Keep `TradingService` as the single behavior source for both REST and gRPC. Add focused service-level helpers for mode checks, real-session context lookup, defensive QMT field mapping, and broker-truth query paths. Preserve mock behavior in `mock` mode, but make `dev` and `prod` fail fast on missing real trading connectivity instead of returning hard-coded sample data.

**Tech Stack:** Python 3, pytest, FastAPI dependency-driven service layer, xtquant/xttrader integration, Pydantic models

---

### Task 1: Add failing tests for mode-gated query behavior

**Files:**
- Create: `tests/unit/test_trading_service.py`
- Modify: `app/services/trading_service.py`

- [ ] **Step 1: Write the failing tests**

```python
from types import SimpleNamespace

import pytest

from app.config import Settings, XTQuantMode
from app.models.trading_models import CancelOrderRequest, OrderRequest, OrderSide, OrderType
from app.services.trading_service import TradingService
import app.services.trading_service as trading_service_module


def make_settings(mode: XTQuantMode, allow_real_trading: bool = False) -> Settings:
    settings = Settings()
    settings.xtquant.mode = mode
    settings.xtquant.trading.allow_real_trading = allow_real_trading
    return settings


def register_real_session(service: TradingService, session_id: str = "real-session") -> str:
    service._connected_accounts[session_id] = {
        "account_id": "acct-001",
        "account_type": "SECURITY",
        "account": object(),
        "connected_time": object(),
    }
    return session_id


def test_dev_positions_raises_instead_of_returning_mock_when_real_backend_unavailable(monkeypatch):
    monkeypatch.setattr(trading_service_module, "XTQUANT_AVAILABLE", True)
    service = TradingService(make_settings(XTQuantMode.DEV))
    service._initialized = False
    session_id = register_real_session(service)

    with pytest.raises(trading_service_module.TradingServiceException, match="xttrader|初始化|backend|连接"):
        service.get_positions(session_id)


def test_dev_asset_raises_instead_of_returning_mock_when_real_backend_unavailable(monkeypatch):
    monkeypatch.setattr(trading_service_module, "XTQUANT_AVAILABLE", True)
    service = TradingService(make_settings(XTQuantMode.DEV))
    service._initialized = False
    session_id = register_real_session(service)

    with pytest.raises(trading_service_module.TradingServiceException, match="xttrader|初始化|backend|连接"):
        service.get_asset_info(session_id)


def test_dev_positions_raise_for_unknown_session(monkeypatch):
    monkeypatch.setattr(trading_service_module, "XTQUANT_AVAILABLE", True)
    service = TradingService(make_settings(XTQuantMode.DEV))
    service._initialized = True

    with pytest.raises(trading_service_module.TradingServiceException, match="账户未连接|session"):
        service.get_positions("missing-session")


def test_mock_mode_positions_still_return_simulated_data():
    service = TradingService(make_settings(XTQuantMode.MOCK))
    response = service.connect_account(SimpleNamespace(account_id="acct-001", password="pw", client_id=1))

    positions = service.get_positions(response.session_id)

    assert response.success is True
    assert isinstance(positions, list)


def test_non_prod_submit_order_does_not_call_real_xttrader(monkeypatch):
    monkeypatch.setattr(trading_service_module, "XTQUANT_AVAILABLE", True)
    service = TradingService(make_settings(XTQuantMode.DEV, allow_real_trading=False))
    session_id = register_real_session(service)

    called = {"order_stock": 0}

    def fake_order_stock(*args, **kwargs):
        called["order_stock"] += 1
        return "should-not-happen"

    monkeypatch.setattr(trading_service_module.xttrader, "order_stock", fake_order_stock)

    response = service.submit_order(
        session_id,
        OrderRequest(
            stock_code="000001.SZ",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            volume=100,
            price=10.0,
        ),
    )

    assert response.order_id.startswith("mock_order_")
    assert called["order_stock"] == 0


def test_non_prod_cancel_does_not_call_real_xttrader(monkeypatch):
    monkeypatch.setattr(trading_service_module, "XTQUANT_AVAILABLE", True)
    service = TradingService(make_settings(XTQuantMode.DEV, allow_real_trading=False))
    session_id = register_real_session(service)

    called = {"cancel_order_stock": 0}

    def fake_cancel_order_stock(*args, **kwargs):
        called["cancel_order_stock"] += 1
        return False

    monkeypatch.setattr(trading_service_module.xttrader, "cancel_order_stock", fake_cancel_order_stock)

    success = service.cancel_order(session_id, CancelOrderRequest(order_id="broker-order-001"))

    assert success is True
    assert called["cancel_order_stock"] == 0
```

These tests should assert:

- `dev` query paths fail fast with `TradingServiceException` instead of returning hard-coded sample positions/assets
- unknown real-mode sessions still fail cleanly
- `mock` mode keeps the simulated workflow alive
- non-`prod` write paths never call real xttrader functions

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_trading_service.py -v`
Expected: FAIL because `get_positions()` and `get_asset_info()` still return mock data in `dev`, and the real-session semantics are not implemented yet.

- [ ] **Step 3: Write minimal implementation**

In `app/services/trading_service.py`, add the smallest changes needed so:

- real query modes (`dev` and `prod`) reject uninitialized/missing trading backend access
- the existing prod-only write gate remains authoritative
- non-`prod` submit/cancel continue returning simulated responses without touching broker APIs

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_trading_service.py -v`
Expected: PASS

### Task 2: Add failing tests for real-mode connect and session context

**Files:**
- Modify: `tests/unit/test_trading_service.py`
- Modify: `app/services/trading_service.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_dev_connect_returns_unsuccessful_response_when_real_backend_not_ready(monkeypatch):
    monkeypatch.setattr(trading_service_module, "XTQUANT_AVAILABLE", True)
    service = TradingService(make_settings(XTQuantMode.DEV))
    service._initialized = False

    response = service.connect_account(SimpleNamespace(account_id="acct-001", password="pw", client_id=1))

    assert response.success is False
    assert response.session_id is None
    assert "xttrader" in response.message or "初始化" in response.message


def test_dev_connect_stores_real_account_context(monkeypatch):
    monkeypatch.setattr(trading_service_module, "XTQUANT_AVAILABLE", True)
    service = TradingService(make_settings(XTQuantMode.DEV))
    service._initialized = True

    fake_account = SimpleNamespace(account_id="acct-001", account_type="SECURITY")

    monkeypatch.setattr(service, "_connect_real_account", lambda request: fake_account)
    monkeypatch.setattr(service, "_build_account_info_from_real_account", lambda account: SimpleNamespace(
        account_id="acct-001",
        account_type="SECURITY",
        account_name="acct-001",
        status="CONNECTED",
        balance=1.0,
        available_balance=1.0,
        frozen_balance=0.0,
        market_value=0.0,
        total_asset=1.0,
    ))

    response = service.connect_account(SimpleNamespace(account_id="acct-001", password="pw", client_id=1))

    assert response.success is True
    assert response.session_id in service._connected_accounts
    assert service._connected_accounts[response.session_id]["account"] is fake_account
```

These tests should pin:

- real-mode connect failures return `ConnectResponse(success=False, ...)` rather than fake success
- successful real-mode connect stores QMT account context under proxy `session_id`

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_trading_service.py::test_dev_connect_returns_unsuccessful_response_when_real_backend_not_ready tests/unit/test_trading_service.py::test_dev_connect_stores_real_account_context -v`
Expected: FAIL because `connect_account()` still fabricates success and does not store real account context.

- [ ] **Step 3: Write minimal implementation**

Add small internal helpers such as:

```python
def _require_real_trading_backend(self) -> None:
    ...


def _connect_real_account(self, request):
    ...


def _store_real_session(self, request, account, account_info) -> str:
    ...
```

Implementation notes:

- `mock` mode keeps the current simulated connect behavior
- `dev` and `prod` call the real connection helper
- expected real connect failures return `ConnectResponse(success=False, message=...)`
- successful connects store the real account object or equivalent context for later broker queries

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_trading_service.py::test_dev_connect_returns_unsuccessful_response_when_real_backend_not_ready tests/unit/test_trading_service.py::test_dev_connect_stores_real_account_context -v`
Expected: PASS

### Task 3: Add failing tests for positions and asset mapping

**Files:**
- Modify: `tests/unit/test_trading_service.py`
- Modify: `app/services/trading_service.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_dev_positions_map_real_qmt_objects(monkeypatch):
    monkeypatch.setattr(trading_service_module, "XTQUANT_AVAILABLE", True)
    service = TradingService(make_settings(XTQuantMode.DEV))
    service._initialized = True
    session_id = register_real_session(service)

    fake_position = SimpleNamespace(
        stock_code="000001.SZ",
        stock_name="平安银行",
        volume=100,
        can_use_volume=80,
        open_price=10.0,
        market_value=1050.0,
        last_price=10.5,
    )

    monkeypatch.setattr(service, "_query_real_positions", lambda session: [fake_position])

    positions = service.get_positions(session_id)

    assert len(positions) == 1
    assert positions[0].stock_code == "000001.SZ"
    assert positions[0].available_volume == 80
    assert positions[0].cost_price == 10.0
    assert positions[0].market_price == 10.5


def test_dev_asset_maps_real_qmt_object(monkeypatch):
    monkeypatch.setattr(trading_service_module, "XTQUANT_AVAILABLE", True)
    service = TradingService(make_settings(XTQuantMode.DEV))
    service._initialized = True
    session_id = register_real_session(service)

    fake_asset = SimpleNamespace(
        total_asset=100000.0,
        market_value=25000.0,
        cash=70000.0,
        frozen_cash=5000.0,
        available_cash=65000.0,
    )

    monkeypatch.setattr(service, "_query_real_asset", lambda session: fake_asset)

    asset = service.get_asset_info(session_id)

    assert asset.total_asset == 100000.0
    assert asset.market_value == 25000.0
    assert asset.cash == 70000.0
    assert asset.available_cash == 65000.0
```

Also add one empty-result test:

```python
def test_dev_positions_returns_empty_list_for_empty_real_result(monkeypatch):
    ...
    monkeypatch.setattr(service, "_query_real_positions", lambda session: [])
    assert service.get_positions(session_id) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_trading_service.py -k "positions or asset" -v`
Expected: FAIL because real-mode mapping helpers do not exist yet.

- [ ] **Step 3: Write minimal implementation**

Add focused helpers in `app/services/trading_service.py`:

```python
def _query_real_positions(self, session_id: str):
    ...


def _map_position(self, raw_position) -> PositionInfo:
    ...


def _query_real_asset(self, session_id: str):
    ...


def _map_asset(self, raw_asset) -> AssetInfo:
    ...
```

Implementation requirements:

- require real backend and stored real session context in `dev` and `prod`
- map known QMT aliases such as `can_use_volume` -> `available_volume` and `open_price` -> `cost_price`
- compute derived values conservatively when safe
- return `[]` for empty real position results
- raise `TradingServiceException` for unmappable critical fields

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_trading_service.py -k "positions or asset" -v`
Expected: PASS

### Task 4: Add failing tests for orders and trades broker-truth queries

**Files:**
- Modify: `tests/unit/test_trading_service.py`
- Modify: `app/services/trading_service.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_dev_orders_map_real_qmt_results(monkeypatch):
    monkeypatch.setattr(trading_service_module, "XTQUANT_AVAILABLE", True)
    service = TradingService(make_settings(XTQuantMode.DEV))
    service._initialized = True
    session_id = register_real_session(service)

    fake_order = SimpleNamespace(
        order_id="broker-order-001",
        stock_code="000001.SZ",
        order_type="LIMIT",
        order_volume=100,
        price=10.0,
        order_status="SUBMITTED",
        traded_volume=20,
        traded_amount=200.0,
        traded_price=10.0,
    )

    monkeypatch.setattr(service, "_query_real_orders", lambda session: [fake_order])

    orders = service.get_orders(session_id)

    assert len(orders) == 1
    assert orders[0].order_id == "broker-order-001"
    assert orders[0].filled_volume == 20


def test_dev_trades_map_real_qmt_results(monkeypatch):
    monkeypatch.setattr(trading_service_module, "XTQUANT_AVAILABLE", True)
    service = TradingService(make_settings(XTQuantMode.DEV))
    service._initialized = True
    session_id = register_real_session(service)

    fake_trade = SimpleNamespace(
        traded_id="trade-001",
        order_id="broker-order-001",
        stock_code="000001.SZ",
        side="BUY",
        traded_volume=100,
        traded_price=10.5,
        traded_amount=1050.0,
        traded_time="20260327103000",
        commission=1.2,
    )

    monkeypatch.setattr(service, "_query_real_trades", lambda session: [fake_trade])

    trades = service.get_trades(session_id)

    assert len(trades) == 1
    assert trades[0].trade_id == "trade-001"
    assert trades[0].amount == 1050.0


def test_dev_orders_use_broker_truth_not_local_cache(monkeypatch):
    monkeypatch.setattr(trading_service_module, "XTQUANT_AVAILABLE", True)
    service = TradingService(make_settings(XTQuantMode.DEV))
    service._initialized = True
    session_id = register_real_session(service)
    service._orders["mock_order_1"] = SimpleNamespace(order_id="mock_order_1")

    monkeypatch.setattr(service, "_query_real_orders", lambda session: [])

    assert service.get_orders(session_id) == []
```

Also add the analogous empty-result test for trades.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_trading_service.py -k "orders or trades" -v`
Expected: FAIL because `dev` still serves local/mock query results.

- [ ] **Step 3: Write minimal implementation**

Add and wire helpers:

```python
def _query_real_orders(self, session_id: str):
    ...


def _map_order(self, raw_order) -> OrderResponse:
    ...


def _query_real_trades(self, session_id: str):
    ...


def _map_trade(self, raw_trade) -> TradeInfo:
    ...
```

Implementation requirements:

- `dev` and `prod` must query broker/QMT truth instead of `self._orders` and `self._trades`
- empty real results return `[]`
- field alias handling should cover common QMT variants for ids, filled metrics, and timestamps

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_trading_service.py -k "orders or trades" -v`
Expected: PASS

### Task 5: Add failing tests for prod-only real order and cancel execution

**Files:**
- Modify: `tests/unit/test_trading_service.py`
- Modify: `app/services/trading_service.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_prod_submit_order_calls_real_xttrader_when_explicitly_allowed(monkeypatch):
    monkeypatch.setattr(trading_service_module, "XTQUANT_AVAILABLE", True)
    service = TradingService(make_settings(XTQuantMode.PROD, allow_real_trading=True))
    service._initialized = True
    session_id = register_real_session(service)

    called = {"order_stock": 0}

    def fake_order_stock(account, stock_code, side, volume, price, order_type):
        called["order_stock"] += 1
        assert account is service._connected_accounts[session_id]["account"]
        return "broker-order-001"

    monkeypatch.setattr(trading_service_module.xttrader, "order_stock", fake_order_stock)

    response = service.submit_order(
        session_id,
        OrderRequest(
            stock_code="000001.SZ",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            volume=100,
            price=10.0,
        ),
    )

    assert response.order_id == "broker-order-001"
    assert called["order_stock"] == 1


def test_prod_cancel_calls_real_xttrader_when_explicitly_allowed(monkeypatch):
    monkeypatch.setattr(trading_service_module, "XTQUANT_AVAILABLE", True)
    service = TradingService(make_settings(XTQuantMode.PROD, allow_real_trading=True))
    service._initialized = True
    session_id = register_real_session(service)

    called = {"cancel_order_stock": 0}

    def fake_cancel_order_stock(account, order_id):
        called["cancel_order_stock"] += 1
        assert account is service._connected_accounts[session_id]["account"]
        assert order_id == "broker-order-001"
        return True

    monkeypatch.setattr(trading_service_module.xttrader, "cancel_order_stock", fake_cancel_order_stock)

    assert service.cancel_order(session_id, CancelOrderRequest(order_id="broker-order-001")) is True
    assert called["cancel_order_stock"] == 1


def test_prod_without_allow_real_trading_still_does_not_call_real_xttrader(monkeypatch):
    monkeypatch.setattr(trading_service_module, "XTQUANT_AVAILABLE", True)
    service = TradingService(make_settings(XTQuantMode.PROD, allow_real_trading=False))
    service._initialized = True
    session_id = register_real_session(service)

    called = {"order_stock": 0, "cancel_order_stock": 0}

    def fake_order_stock(*args, **kwargs):
        called["order_stock"] += 1
        return "should-not-happen"

    def fake_cancel_order_stock(*args, **kwargs):
        called["cancel_order_stock"] += 1
        return False

    monkeypatch.setattr(trading_service_module.xttrader, "order_stock", fake_order_stock)
    monkeypatch.setattr(trading_service_module.xttrader, "cancel_order_stock", fake_cancel_order_stock)

    order_response = service.submit_order(
        session_id,
        OrderRequest(
            stock_code="000001.SZ",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            volume=100,
            price=10.0,
        ),
    )
    cancel_success = service.cancel_order(session_id, CancelOrderRequest(order_id="broker-order-001"))

    assert order_response.order_id.startswith("mock_order_")
    assert cancel_success is True
    assert called["order_stock"] == 0
    assert called["cancel_order_stock"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_trading_service.py -k "prod_submit_order or prod_cancel" -v`
Expected: FAIL because the current implementation passes proxy `session_id` into xttrader and still ties cancel to the local order cache.

- [ ] **Step 3: Write minimal implementation**

Update write-side helpers so:

- real writes resolve the stored real account context from `_connected_accounts`
- real cancel uses the broker order id directly
- the prod-only execution gate remains centralized in `_should_use_real_trading()`
- `prod` with `allow_real_trading=False` stays on the simulated path

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_trading_service.py -k "prod_submit_order or prod_cancel" -v`
Expected: PASS

### Task 6: Add failing tests for real account-info reads

**Files:**
- Modify: `tests/unit/test_trading_service.py`
- Modify: `app/services/trading_service.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_dev_account_info_maps_real_qmt_account(monkeypatch):
    monkeypatch.setattr(trading_service_module, "XTQUANT_AVAILABLE", True)
    service = TradingService(make_settings(XTQuantMode.DEV))
    service._initialized = True
    session_id = register_real_session(service)

    fake_account_info = SimpleNamespace(
        account_id="acct-001",
        account_type="SECURITY",
        account_name="主账户",
        balance=100000.0,
        available_balance=90000.0,
        frozen_balance=10000.0,
        market_value=30000.0,
        total_asset=130000.0,
        status="CONNECTED",
    )

    monkeypatch.setattr(service, "_query_real_account_info", lambda session: fake_account_info)

    account = service.get_account_info(session_id)

    assert account.account_id == "acct-001"
    assert account.total_asset == 130000.0
    assert account.available_balance == 90000.0


def test_dev_account_info_raises_when_real_query_fails(monkeypatch):
    monkeypatch.setattr(trading_service_module, "XTQUANT_AVAILABLE", True)
    service = TradingService(make_settings(XTQuantMode.DEV))
    service._initialized = True
    session_id = register_real_session(service)

    def raise_query_error(session):
        raise RuntimeError("backend down")

    monkeypatch.setattr(service, "_query_real_account_info", raise_query_error)

    with pytest.raises(trading_service_module.TradingServiceException, match="账户|QMT|backend"):
        service.get_account_info(session_id)
```

These tests should pin that `GET account` in `dev` and `prod` is real-query-backed rather than a stale connect-time cache.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_trading_service.py -k "account_info" -v`
Expected: FAIL because `get_account_info()` still only returns the cached connect-time object.

- [ ] **Step 3: Write minimal implementation**

Add and wire helpers:

```python
def _query_real_account_info(self, session_id: str):
    ...


def _map_account_info(self, raw_account) -> AccountInfo:
    ...
```

Implementation requirements:

- `mock` mode may still return the stored simulated account snapshot
- `dev` and `prod` should query broker-backed account data on read
- connect-time cached account info may remain as fallback metadata for the session record, but not as broker truth for real-mode `get_account_info()`

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_trading_service.py -k "account_info" -v`
Expected: PASS

### Task 7: Run full focused verification and diagnostics

**Files:**
- Modify: `app/services/trading_service.py`
- Create: `tests/unit/test_trading_service.py`

- [ ] **Step 1: Run the full focused unit suite**

Run: `pytest tests/unit/test_trading_service.py -v`
Expected: PASS

- [ ] **Step 2: Run related API regression coverage**

Run: `pytest tests/rest/test_trading_api.py -v`
Expected: PASS, or existing integration-only failures unrelated to this change are documented clearly.

- [ ] **Step 3: Run gRPC parity verification**

Run: `pytest tests/grpc/test_trading_grpc_service.py -k "account or positions or asset or orders or trades" -v`
Expected: PASS where tests are implemented, or clear confirmation that the gRPC layer delegates directly to `TradingService` and has no duplicated business logic.

- [ ] **Step 4: Run diagnostics for edited files**

Check diagnostics for:

- `app/services/trading_service.py`
- `tests/unit/test_trading_service.py`

Expected: no newly introduced lint or type errors that can be fixed quickly.

- [ ] **Step 5: Manually verify safety invariants**

Confirm in code and test results:

- `mock` mode still uses simulated query behavior
- `dev` and `prod` query paths never fall back to hard-coded sample positions/assets/orders/trades
- only `prod + allow_real_trading=true` may call broker write APIs
- non-`prod` cancel remains observably simulated rather than broker-confirmed
