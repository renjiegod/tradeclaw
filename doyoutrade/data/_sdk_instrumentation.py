"""Instrumented wrappers for qmt_proxy_sdk API classes.

Replaces DataApi / TradingApi / SystemApi instances on an AsyncQmtProxyClient
with wrappers that emit an OpenTelemetry span and debug event for every
public API call, capturing the method name, parameters, and response summary.

Usage::

    from doyoutrade.data._sdk_instrumentation import instrument_sdk

    client = AsyncQmtProxyClient(...)
    instrument_sdk(client)   # patches client.data / client.trading / client.system in-place

All public async methods of DataApi, TradingApi, and SystemApi are covered.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import time
from typing import Any

from opentelemetry.trace import Status, StatusCode

from doyoutrade.debug import emit_debug_event
from doyoutrade.observability import get_tracer
from doyoutrade.observability.worker_span_context import mark_worker_ancestor_spans_error


def instrument_sdk(client: Any) -> None:
    """Monkey-patch all API sub-objects on an AsyncQmtProxyClient with instrumented wrappers.

    After calling this, every public async method on client.data / client.trading /
    client.system will create a span and emit a debug event.
    """
    tracer = get_tracer("doyoutrade.data")

    if hasattr(client, "data") and hasattr(client.data, "_transport"):
        client.data = _InstrumentedDataApi(client.data, tracer)
    if hasattr(client, "trading") and hasattr(client.trading, "_transport"):
        client.trading = _InstrumentedTradingApi(client.trading, tracer)
    if hasattr(client, "system") and hasattr(client.system, "_transport"):
        client.system = _InstrumentedSystemApi(client.system, tracer)


# ---------------------------------------------------------------------------
# Helpers shared by all instrumented API wrappers
# ---------------------------------------------------------------------------

_SDK_METHODS: set[str] = set()


def _instrument_method(method_name: str, tracer, prefix: str):
    async def wrapper(self, *args: Any, **kwargs: Any) -> Any:
        span_name = f"qmt_sdk.{prefix}.{method_name}"
        event_name = f"qmt_sdk.{prefix}.{method_name}"
        start = time.perf_counter()

        sig = inspect.signature(getattr(type(self).__bases__[0], method_name))
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        call_args = dict(bound.arguments)

        with tracer.start_as_current_span(span_name) as span:
            span.set_attribute("qmt_sdk.api", prefix)
            span.set_attribute("qmt_sdk.method", method_name)
            for k, v in _truncate_args(call_args).items():
                span.set_attribute(f"qmt_sdk.arg.{k}", str(v)[:256])
            try:
                result = await method_name.__get__(self, type(self))(  # type: ignore[union-attr]
                    *args, **kwargs
                )
            except Exception as exc:
                span.set_attribute("qmt_sdk.error", str(exc)[:256])
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                mark_worker_ancestor_spans_error(str(exc))
                raise
            finally:
                duration_ms = (time.perf_counter() - start) * 1000
                span.set_attribute("qmt_sdk.duration_ms", duration_ms)
                _fire_event(
                    event_name,
                    {
                        "method": method_name,
                        "duration_ms": round(duration_ms, 2),
                        "args": _truncate_args(call_args),
                        "ok": True,
                    },
                )
            return result

    return wrapper


def _truncate_args(args: dict[str, Any]) -> dict[str, Any]:
    """Truncate large values for safe logging."""
    result = {}
    for k, v in args.items():
        if isinstance(v, list) and len(v) > 10:
            result[k] = f"<{len(v)} items>"
        elif isinstance(v, dict):
            result[k] = {kk: _truncate_val(vv) for kk, vv in list(v.items())[:10]}
        elif isinstance(v, str) and len(v) > 128:
            result[k] = v[:128] + "..."
        else:
            result[k] = _truncate_val(v)
    return result


def _truncate_val(v: Any) -> Any:
    if isinstance(v, (int, float, bool, type(None))):
        return v
    return str(v)[:256]


def _fire_event(event_name: str, payload: dict[str, Any]) -> None:
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(emit_debug_event(event_name, payload))
    except RuntimeError:
        pass


# ---------------------------------------------------------------------------
# Instrumented DataApi
# ---------------------------------------------------------------------------

_QMT_DATA_METHODS = {
    "get_market_data",
    "get_financial_data",
    "get_sector_list",
    "get_stock_list_in_sector",
    "get_index_weight",
    "get_trading_calendar",
    "get_instrument_info",
    "get_etf_info",
    "get_instrument_type",
    "get_holidays",
    "get_convertible_bonds",
    "get_ipo_info",
    "get_period_list",
    "get_data_dir",
    "get_local_data",
    "get_full_tick",
    "get_divid_factors",
    "get_full_kline",
    "download_history_data",
    "download_history_data_batch",
    "download_financial_data",
    "download_financial_data_batch",
    "download_sector_data",
    "download_index_weight",
    "download_cb_data",
    "download_etf_info",
    "download_holiday_data",
    "download_history_contracts",
    "create_sector_folder",
    "create_sector",
    "add_sector_stocks",
    "remove_sector_stocks",
    "remove_sector",
    "reset_sector",
    "get_l2_quote",
    "get_l2_order",
    "get_l2_transaction",
    "create_subscription",
    "delete_subscription",
    "get_subscription",
    "list_subscriptions",
}


class _InstrumentedDataApi:
    """DataApi wrapper that adds span + debug event to every public async method."""

    def __init__(self, inner, tracer):
        self._inner = inner
        self._tracer = tracer
        self._transport = inner._transport

    def __getattr__(self, name: str) -> Any:
        # passthrough non-data methods
        attr = getattr(self._inner, name)
        if name in _QMT_DATA_METHODS and callable(attr):
            return self._wrap(name, attr)
        return attr

    def _wrap(self, method_name: str, method):
        @functools.wraps(method)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            span_name = f"qmt_sdk.data.{method_name}"
            event_name = f"qmt_sdk.data.{method_name}"
            start = time.perf_counter()

            # build sanitized args dict for event
            try:
                sig = inspect.signature(method)
                bound = sig.bind(*args, **kwargs)
                bound.apply_defaults()
                call_args = _truncate_args(dict(bound.arguments))
            except Exception:
                call_args = {"_error": "could not inspect signature"}

            with self._tracer.start_as_current_span(span_name) as span:
                span.set_attribute("qmt_sdk.api", "data")
                span.set_attribute("qmt_sdk.method", method_name)
                for k, v in call_args.items():
                    span.set_attribute(f"qmt_sdk.arg.{k}", str(v)[:256])
                try:
                    result = await method(*args, **kwargs)
                except Exception as exc:
                    span.set_attribute("qmt_sdk.error", str(exc)[:256])
                    span.set_status(Status(StatusCode.ERROR, str(exc)))
                    mark_worker_ancestor_spans_error(str(exc))
                    raise
                finally:
                    duration_ms = (time.perf_counter() - start) * 1000
                    span.set_attribute("qmt_sdk.duration_ms", duration_ms)
                    _fire_event(
                        event_name,
                        {
                            "method": method_name,
                            "duration_ms": round(duration_ms, 2),
                            "args": call_args,
                            "ok": True,
                        },
                    )
                return result

        return wrapper


# ---------------------------------------------------------------------------
# Instrumented TradingApi
# ---------------------------------------------------------------------------

class _InstrumentedTradingApi:
    """TradingApi wrapper that adds span + debug event to every public async method."""

    _METHODS = {
        "connect",
        "disconnect",
        "get_account_info",
        "get_positions",
        "get_asset",
        "get_risk",
        "get_strategies",
        "get_orders",
        "get_trades",
        "submit_order",
        "cancel_order",
        "get_connection_status",
    }

    def __init__(self, inner, tracer):
        self._inner = inner
        self._tracer = tracer
        self._transport = inner._transport

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if name in self._METHODS and callable(attr):
            return self._wrap(name, attr)
        return attr

    def _wrap(self, method_name: str, method):
        @functools.wraps(method)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            span_name = f"qmt_sdk.trading.{method_name}"
            event_name = f"qmt_sdk.trading.{method_name}"
            start = time.perf_counter()

            try:
                sig = inspect.signature(method)
                bound = sig.bind(*args, **kwargs)
                bound.apply_defaults()
                call_args = _truncate_args(dict(bound.arguments))
            except Exception:
                call_args = {"_error": "could not inspect signature"}

            with self._tracer.start_as_current_span(span_name) as span:
                span.set_attribute("qmt_sdk.api", "trading")
                span.set_attribute("qmt_sdk.method", method_name)
                for k, v in call_args.items():
                    span.set_attribute(f"qmt_sdk.arg.{k}", str(v)[:256])
                try:
                    result = await method(*args, **kwargs)
                except Exception as exc:
                    span.set_attribute("qmt_sdk.error", str(exc)[:256])
                    span.set_status(Status(StatusCode.ERROR, str(exc)))
                    mark_worker_ancestor_spans_error(str(exc))
                    raise
                finally:
                    duration_ms = (time.perf_counter() - start) * 1000
                    span.set_attribute("qmt_sdk.duration_ms", duration_ms)
                    _fire_event(
                        event_name,
                        {
                            "method": method_name,
                            "duration_ms": round(duration_ms, 2),
                            "args": call_args,
                            "ok": True,
                        },
                    )
                return result

        return wrapper


# ---------------------------------------------------------------------------
# Instrumented SystemApi
# ---------------------------------------------------------------------------

class _InstrumentedSystemApi:
    """SystemApi wrapper that adds span + debug event to every public async method."""

    _METHODS = {"get_root", "get_info", "check_health", "check_ready", "check_live"}

    def __init__(self, inner, tracer):
        self._inner = inner
        self._tracer = tracer
        self._transport = inner._transport

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if name in self._METHODS and callable(attr):
            return self._wrap(name, attr)
        return attr

    def _wrap(self, method_name: str, method):
        @functools.wraps(method)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            span_name = f"qmt_sdk.system.{method_name}"
            event_name = f"qmt_sdk.system.{method_name}"
            start = time.perf_counter()

            try:
                sig = inspect.signature(method)
                bound = sig.bind(*args, **kwargs)
                bound.apply_defaults()
                call_args = _truncate_args(dict(bound.arguments))
            except Exception:
                call_args = {"_error": "could not inspect signature"}

            with self._tracer.start_as_current_span(span_name) as span:
                span.set_attribute("qmt_sdk.api", "system")
                span.set_attribute("qmt_sdk.method", method_name)
                for k, v in call_args.items():
                    span.set_attribute(f"qmt_sdk.arg.{k}", str(v)[:256])
                try:
                    result = await method(*args, **kwargs)
                except Exception as exc:
                    span.set_attribute("qmt_sdk.error", str(exc)[:256])
                    span.set_status(Status(StatusCode.ERROR, str(exc)))
                    mark_worker_ancestor_spans_error(str(exc))
                    raise
                finally:
                    duration_ms = (time.perf_counter() - start) * 1000
                    span.set_attribute("qmt_sdk.duration_ms", duration_ms)
                    _fire_event(
                        event_name,
                        {
                            "method": method_name,
                            "duration_ms": round(duration_ms, 2),
                            "args": call_args,
                            "ok": True,
                        },
                    )
                return result

        return wrapper
