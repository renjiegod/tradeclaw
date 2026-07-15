"""REST client for qmt-proxy.

Parity with ``qmt-proxy/examples/ma_crossover_strategy.py`` (stage 3 ``connect_trading``):

- ``AsyncQmtProxyClient(base_url=..., api_key=token)``
- ``await trading.connect(account_id=...)`` → ``session_id``
- Subsequent calls use ``session_id`` (``get_account_info``, ``get_positions``, …).

DoYouTrade performs the connect lazily before the first account/positions read when
``data.qmt.account_id`` is set, and can persist the returned ``session_id`` to config.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time
from typing import Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

from opentelemetry import trace as otel_trace
from qmt_proxy_sdk import AsyncQmtProxyClient
from qmt_proxy_sdk.exceptions import ClientError, QmtProxyError
from qmt_proxy_sdk.models.data import TradingCalendarResponse

from doyoutrade.data.constants import DEFAULT_BAR_ADJUST

logger = logging.getLogger(__name__)

_A_SHARE_TZ = ZoneInfo("Asia/Shanghai")
_A_SHARE_MORNING_OPEN = time(9, 15)
_A_SHARE_MORNING_CLOSE = time(11, 30)
_A_SHARE_AFTERNOON_OPEN = time(13, 0)
_A_SHARE_AFTERNOON_CLOSE = time(15, 0)
_FULL_KLINE_UNSUPPORTED_MARKERS = (
    "function not realize",
    "commoncontrol",
    "当前客户端未支持此功能",
    "未支持此功能",
)

# Async callback used to persist a refreshed trading session id back onto the
# owning ``accounts`` row: ``(account_pk, session_id) -> Awaitable[None]``.
SessionPersist = Callable[[str, str], Awaitable[None]]

_QMT_PROXY_CONNECT_HINT = (
    "Check qmt-proxy: xtquant installed, qmt_userdata_path set, QMT running, and xttrader "
    "connects successfully; or set qmt-proxy xtquant mode to MOCK for simulated trading "
    "sessions without a live terminal."
)


class QmtRealtimeKlineUnsupportedError(RuntimeError):
    """Current QMT client cannot serve real-time intraday bars via ``get_full_kline``."""


def _token_for_connect_failure_log(value: Optional[str]) -> str:
    """Full token/account secret for troubleshooting failed connects (may be sensitive)."""
    if value is None or not str(value).strip():
        return "token=(not set)"
    return f"token={str(value).strip()!r}"


def doyoutrade_adjust_to_qmt(adjust: str) -> str:
    """Map DoYouTrade adjust tokens to qmt-proxy ``adjust_type`` values."""
    key = (adjust or DEFAULT_BAR_ADJUST).strip().lower()
    mapping = {"none": "none", "qfq": "front", "hfq": "back"}
    if key not in mapping:
        raise ValueError(
            f"unsupported adjust mode {adjust!r}; expected one of {sorted(mapping)}"
        )
    return mapping[key]


def _connect_debug_context(client: "QmtProxyRestClient") -> str:
    return (
        f"base_url={client.base_url!r} account_id={client.account_id!r} "
        f"{_token_for_connect_failure_log(client.token)} timeout_seconds={client.timeout_seconds} "
        f"account_pk={client.account_pk!r} session_id_before={client.session_id!r}"
    )


async def _emit_session_persist_failed(account_pk: str, exc: Exception) -> None:
    """Surface a session-id write-back failure as a structured debug event so
    operators see that the refreshed session was not persisted to the account
    row (the next cycle will re-connect rather than reuse it)."""
    try:
        from doyoutrade.debug import emit_debug_event

        await emit_debug_event(
            "account_session_persist_failed",
            {
                "account_id": account_pk,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "hint": "session_id refresh could not be written to the accounts row; "
                "next live read will re-connect",
            },
        )
    except Exception:  # noqa: BLE001 — observability must not break trading
        pass


def _market_payload_has_rows(payload) -> bool:
    """True when any returned ``MarketDataResponse`` carries at least one bar."""
    return any(getattr(item, "data", None) for item in (payload or []))


async def _emit_market_download_fallback(
    symbol: str, start_date: str, end_date: str, interval: str
) -> None:
    """Surface a fast-read miss → download-enabled retry as a structured debug
    event so operators can see which historical range was not yet downloaded in
    QMT and pre-download/backfill it to keep the fast path."""
    try:
        from doyoutrade.debug import emit_debug_event

        await emit_debug_event(
            "qmt_market_download_fallback",
            {
                "symbol": symbol,
                "start_date": start_date,
                "end_date": end_date,
                "interval": interval,
                "reason": "local_read_empty",
                "hint": "historical bars not yet downloaded in QMT for this range; "
                "fast disable_download read returned empty so a download-enabled "
                "fetch was retried (slower). Pre-download/backfill this range to "
                "keep the fast path.",
            },
        )
    except Exception:  # noqa: BLE001 — observability must not break data fetch
        pass


def _iter_error_strings(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        out: list[str] = []
        for key, item in value.items():
            out.append(str(key))
            out.extend(_iter_error_strings(item))
        return out
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for item in value:
            out.extend(_iter_error_strings(item))
        return out
    return [str(value)]


def _is_full_kline_unsupported(exc: Exception) -> bool:
    if not isinstance(exc, QmtProxyError):
        return False
    haystack = " ".join(
        part.strip().lower()
        for part in _iter_error_strings(
            [str(exc), getattr(exc, "message", None), getattr(exc, "payload", None)]
        )
        if part and str(part).strip()
    )
    if not haystack:
        return False
    return any(marker in haystack for marker in _FULL_KLINE_UNSUPPORTED_MARKERS)


def _now_market_tz() -> datetime:
    return datetime.now(_A_SHARE_TZ)


def _is_ashare_continuous_trading(instant: datetime) -> bool:
    local = instant.astimezone(_A_SHARE_TZ)
    if local.weekday() >= 5:
        return False
    clock = local.time().replace(second=0, microsecond=0)
    morning = _A_SHARE_MORNING_OPEN <= clock <= _A_SHARE_MORNING_CLOSE
    afternoon = _A_SHARE_AFTERNOON_OPEN <= clock <= _A_SHARE_AFTERNOON_CLOSE
    return morning or afternoon


def _resolve_intraday_fetch_mode(start_time: str, end_time: str) -> tuple[str, str]:
    """Choose realtime ``full_kline`` only for today's still-open A-share session."""
    if not end_time:
        return "history", "missing_end_time"
    try:
        end_date = date.fromisoformat(end_time[:10])
        start_date = date.fromisoformat(start_time[:10]) if start_time else None
    except ValueError:
        return "realtime", "unparseable_bound"

    now_local = _now_market_tz()
    today = now_local.date()
    includes_today = end_date == today or (start_date is not None and start_date == today)
    if not includes_today:
        return "history", "historical_window"
    if _is_ashare_continuous_trading(now_local):
        return "realtime", "live_session_today"
    return "history", "today_outside_live_session"


def _set_intraday_fetch_mode_span_attributes(mode: str, reason: str) -> None:
    span = otel_trace.get_current_span()
    if span is None or not span.is_recording():
        return
    span.set_attribute("qmt.intraday.fetch_mode", mode)
    span.set_attribute("qmt.intraday.fetch_reason", reason)


async def _emit_intraday_fetch_mode_selected(
    *,
    symbol: str,
    interval: str,
    start_time: str,
    end_time: str,
    mode: str,
    reason: str,
) -> None:
    try:
        from doyoutrade.debug import emit_debug_event

        await emit_debug_event(
            "qmt_intraday_fetch_mode_selected",
            {
                "symbol": symbol,
                "interval": interval,
                "start_time": start_time,
                "end_time": end_time,
                "mode": mode,
                "reason": reason,
                "hint": (
                    "realtime mode uses get_full_kline only during today's live A-share "
                    "session; otherwise historical get_market_data is used."
                ),
            },
        )
    except Exception:  # noqa: BLE001 — observability must not break data fetch
        pass


async def _emit_full_kline_unsupported(
    *,
    symbol: str,
    interval: str,
    start_time: str,
    end_time: str,
    exc: Exception,
) -> None:
    try:
        from doyoutrade.debug import emit_debug_event

        await emit_debug_event(
            "qmt_full_kline_unsupported",
            {
                "symbol": symbol,
                "interval": interval,
                "start_time": start_time,
                "end_time": end_time,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "hint": (
                    "current QMT client cannot serve real-time intraday bars via "
                    "get_full_kline; update the client / upgrade the research edition, "
                    "or rerun outside the live session so historical minute bars are used."
                ),
            },
        )
    except Exception:  # noqa: BLE001 — observability must not break data fetch
        pass


def _is_account_not_connected(exc: ClientError) -> bool:
    msg = (getattr(exc, "message", None) or str(exc) or "").strip()
    if "账户未连接" in msg:
        return True
    payload = getattr(exc, "payload", None)
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, dict) and "账户未连接" in str(detail.get("message", "")):
            return True
        if "账户未连接" in str(payload.get("message", "")):
            return True
    return False


class QmtProxyRestClient:
    """Async adapter over vendored qmt_proxy_sdk APIs."""

    def __init__(
        self,
        base_url: str,
        token: Optional[str] = None,
        timeout_seconds: float = 30.0,
        session_id: Optional[str] = None,
        account_id: Optional[str] = None,
        account_pk: Optional[str] = None,
        terminal_id: Optional[str] = None,
        session_persist: Optional[SessionPersist] = None,
        sdk_client: AsyncQmtProxyClient | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = float(timeout_seconds)
        self.session_id = session_id
        aid = None if account_id is None else str(account_id).strip()
        self.account_id = aid or None
        # Primary key of the owning ``accounts`` row (acct-...), used to write
        # a refreshed session_id back to the DB. None for ad-hoc clients.
        self.account_pk = account_pk or None
        # Which QMT terminal (client_id) on a multi-terminal qmt-proxy to route
        # to (sent as the ``X-QMT-Terminal`` header). None → proxy default.
        tid = None if terminal_id is None else str(terminal_id).strip()
        self.terminal_id = tid or None
        self._session_persist = session_persist
        self._owns_client = sdk_client is None
        self._client = sdk_client or AsyncQmtProxyClient(
            base_url=self.base_url,
            api_key=token,
            timeout=self.timeout_seconds,
            terminal_id=self.terminal_id,
        )
        self._trading_lock = asyncio.Lock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.aclose()

    async def aclose(self):
        if self._owns_client:
            await self._client.aclose()
            return
        close = getattr(self._client, "aclose", None)
        if close is not None:
            await close()

    async def check_health(self):
        return await self._client.system.check_health()

    async def fetch_bars(
        self,
        symbol: str,
        start_time: str,
        end_time: str,
        interval: str = "1m",
        *,
        adjust: str = DEFAULT_BAR_ADJUST,
    ):
        adjust_type = doyoutrade_adjust_to_qmt(adjust)
        if interval in _INTRADAY_INTERVALS:
            fetch_mode, fetch_reason = _resolve_intraday_fetch_mode(start_time, end_time)
            _set_intraday_fetch_mode_span_attributes(fetch_mode, fetch_reason)
            await _emit_intraday_fetch_mode_selected(
                symbol=symbol,
                interval=interval,
                start_time=start_time,
                end_time=end_time,
                mode=fetch_mode,
                reason=fetch_reason,
            )
            if fetch_mode == "realtime":
                # get_full_kline includes real-time/partial bars for the current session.
                try:
                    payload = await self._client.data.get_full_kline(
                        stock_codes=[symbol],
                        start_time=_datetime_str(start_time),
                        end_time=_datetime_str(end_time),
                        period=interval,
                        fields=["time", "open", "high", "low", "close", "volume", "amount"],
                        adjust_type=adjust_type,
                    )
                except Exception as exc:
                    if _is_full_kline_unsupported(exc):
                        logger.warning(
                            "qmt realtime intraday bars unsupported symbol=%s interval=%s "
                            "start=%s end=%s error_type=%s error=%s",
                            symbol,
                            interval,
                            start_time,
                            end_time,
                            type(exc).__name__,
                            exc,
                        )
                        await _emit_full_kline_unsupported(
                            symbol=symbol,
                            interval=interval,
                            start_time=start_time,
                            end_time=end_time,
                            exc=exc,
                        )
                        raise QmtRealtimeKlineUnsupportedError(
                            "qmt get_full_kline is unsupported by the current client for "
                            f"{interval} realtime bars on {symbol}; update the QMT client / "
                            "upgrade the research edition, or rerun outside the live session "
                            "so historical minute bars are used"
                        ) from exc
                    raise
            else:
                payload = await self._fetch_history_market_data(
                    symbol=symbol,
                    start_date=_compact_date(start_time),
                    end_date=_compact_date(end_time),
                    interval=interval,
                    adjust_type=adjust_type,
                )
        elif interval == "1d" and _range_includes_today(start_time, end_time):
            # Today's incomplete 1d bar: get_market_data returns nothing because the daily
            # bar is only stored after market close. Use get_full_tick to get the current
            # OHLCV snapshot (open/high/low/close/volume from today's trading so far).
            return await self._fetch_today_bar_from_tick(symbol, start_time)
        else:
            # Historical stored bars (only available after market close).
            payload = await self._fetch_history_market_data(
                symbol=symbol,
                start_date=_compact_date(start_time),
                end_date=_compact_date(end_time),
                interval=interval,
                adjust_type=adjust_type,
            )
        rows = []
        for item in payload:
            for row in item.data:
                amount_raw = row.get("amount")
                try:
                    amount = float(amount_raw) if amount_raw is not None else None
                except (TypeError, ValueError):
                    amount = None
                rows.append(
                    {
                        "symbol": item.stock_code,
                        "ts": row.get("time") or row.get("timestamp") or start_time,
                        "open": row.get("open"),
                        "high": row.get("high"),
                        "low": row.get("low"),
                        "close": row.get("close"),
                        "volume": row.get("volume", 0),
                        "amount": amount,
                    }
                )
        return rows

    async def _fetch_history_market_data(
        self,
        *,
        symbol: str,
        start_date: str,
        end_date: str,
        interval: str,
        adjust_type: str,
    ):
        """Fetch historical stored bars with a read-first / download-fallback strategy.

        The qmt-proxy ``/api/v1/data/market`` endpoint spawns a *fresh* isolated
        xtquant subprocess to download history on every call when
        ``disable_download`` is false (~4s baseline, independent of range size).
        Under a tight client timeout that reproducibly tripped
        ``httpx.ReadTimeout`` → ``TransportError`` → ``signal_generation_failed``.

        Fast path: ``disable_download=True`` reads only already-downloaded local
        bars (single subprocess, ~2s). Only when the local read comes back empty
        (the range was never downloaded) do we retry once with download enabled.
        The slow-path retry is made visible per CLAUDE.md §错误可见性 (structured
        debug event + ``logger.info``) so an operator can see why a fetch was
        slow and pre-download/backfill the range.
        """
        fields = ["time", "open", "high", "low", "close", "volume", "amount"]
        payload = await self._client.data.get_market_data(
            stock_codes=[symbol],
            start_date=start_date,
            end_date=end_date,
            period=interval,
            fields=fields,
            adjust_type=adjust_type,
            disable_download=True,
        )
        if _market_payload_has_rows(payload):
            return payload

        logger.info(
            "qmt market fast read empty; retrying with download "
            "symbol=%s start=%s end=%s interval=%s",
            symbol,
            start_date,
            end_date,
            interval,
        )
        await _emit_market_download_fallback(symbol, start_date, end_date, interval)
        return await self._client.data.get_market_data(
            stock_codes=[symbol],
            start_date=start_date,
            end_date=end_date,
            period=interval,
            fields=fields,
            adjust_type=adjust_type,
            disable_download=False,
        )

    async def get_trading_calendar(self, year: int) -> TradingCalendarResponse:
        return await self._client.data.get_trading_calendar(year)

    async def fetch_sectors(self) -> list[dict]:
        """List all sector / industry / concept boards via the proxy.

        Returns a list of ``{"sector_name", "stock_list", "sector_type"}``
        dicts (the qmt-proxy ``/api/v1/data/sectors`` shape). Used by
        :class:`doyoutrade.data.sector_qmt.QmtSectorProvider`.
        """
        responses = await self._client.data.get_sector_list()
        return [
            {
                "sector_name": r.sector_name,
                "stock_list": list(r.stock_list or []),
                "sector_type": r.sector_type or "",
            }
            for r in responses
        ]

    async def fetch_sector_members(
        self, sector_name: str, sector_type: str | None = None
    ) -> dict:
        """Fetch one board's constituents via ``/api/v1/data/sector``."""
        r = await self._client.data.get_stock_list_in_sector(sector_name, sector_type)
        return {
            "sector_name": r.sector_name,
            "stock_list": list(r.stock_list or []),
            "sector_type": r.sector_type or "",
        }

    async def fetch_instrument_info(self, stock_code: str) -> dict:
        """Fetch instrument detail (incl. float / total share volume).

        Returns ``{"FloatVolume", "TotalVolume"}`` (shares; ``None`` when the
        upstream omits them). Used by
        :class:`doyoutrade.data.fundamentals_qmt.QmtFundamentalsProvider` to
        derive float market cap as ``FloatVolume × latest_price``.
        """
        info = await self._client.data.get_instrument_info(stock_code)
        return {
            "FloatVolume": getattr(info, "FloatVolume", None),
            "TotalVolume": getattr(info, "TotalVolume", None),
        }

    async def fetch_account(self):
        async with self._trading_lock:
            await self._ensure_trading_session_locked()
            try:
                return await self._fetch_account_body()
            except ClientError as exc:
                if self.account_id and _is_account_not_connected(exc):
                    self.session_id = None
                    await self._connect_trading_and_persist()
                    return await self._fetch_account_body()
                raise

    async def _fetch_account_body(self):
        session_id = self._require_session_id()
        account = await self._client.trading.get_account_info(session_id)
        return {
            "account_id": account.account_id,
            "cash": float(account.available_balance),
            "equity": float(account.total_asset),
            "balance": float(account.balance),
            "market_value": float(account.market_value),
            "status": account.status,
        }

    async def fetch_positions(self):
        async with self._trading_lock:
            await self._ensure_trading_session_locked()
            try:
                return await self._fetch_positions_body()
            except ClientError as exc:
                if self.account_id and _is_account_not_connected(exc):
                    self.session_id = None
                    await self._connect_trading_and_persist()
                    return await self._fetch_positions_body()
                raise

    async def _fetch_positions_body(self):
        session_id = self._require_session_id()
        positions = await self._client.trading.get_positions(session_id)
        return [
            {
                "symbol": item.stock_code,
                "quantity": float(item.volume),
                "cost_price": float(item.cost_price),
                "market_price": float(item.market_price),
                "market_value": float(item.market_value),
                "profit_loss": float(item.profit_loss),
                "available": float(item.available_volume),
                "frozen": float(item.frozen_volume),
                "name": item.stock_name or None,
            }
            for item in positions
        ]

    async def fetch_asset(self):
        """Fetch the richer broker asset breakdown via ``get_asset``.

        Same session-management contract as :meth:`fetch_account` /
        :meth:`fetch_positions` (serialize under ``_trading_lock``, ensure a
        live session, transparently re-connect once on "账户未连接"). Returns a
        plain dict so callers never depend on the SDK pydantic model shape.
        Unlike :meth:`fetch_account` this keeps ``frozen_cash`` /
        ``available_cash`` / ``profit_loss`` which ``fetch_account`` drops.
        """
        async with self._trading_lock:
            await self._ensure_trading_session_locked()
            try:
                return await self._fetch_asset_body()
            except ClientError as exc:
                if self.account_id and _is_account_not_connected(exc):
                    self.session_id = None
                    await self._connect_trading_and_persist()
                    return await self._fetch_asset_body()
                raise

    async def _fetch_asset_body(self):
        session_id = self._require_session_id()
        asset = await self._client.trading.get_asset(session_id)
        return {
            "total_asset": float(asset.total_asset),
            "market_value": float(asset.market_value),
            "cash": float(asset.cash),
            "frozen_cash": float(asset.frozen_cash),
            "available_cash": float(asset.available_cash),
            "profit_loss": float(asset.profit_loss),
            "profit_loss_ratio": float(asset.profit_loss_ratio),
        }

    async def fetch_trades(self):
        """Fetch the session's executed trades (成交/交割单) via ``get_trades``.

        Same session-management contract as :meth:`fetch_positions`. The qmt-proxy
        ``get_trades`` takes only ``session_id`` (no date filter) and returns the
        live session's trades — typically the current trading day — so callers
        that want a single day's 交割单 filter by ``trade_time`` themselves
        (historical-day backfill is not reachable through a live session).
        Returns plain dicts (never SDK pydantic models).
        """
        async with self._trading_lock:
            await self._ensure_trading_session_locked()
            try:
                return await self._fetch_trades_body()
            except ClientError as exc:
                if self.account_id and _is_account_not_connected(exc):
                    self.session_id = None
                    await self._connect_trading_and_persist()
                    return await self._fetch_trades_body()
                raise

    async def _fetch_trades_body(self):
        session_id = self._require_session_id()
        trades = await self._client.trading.get_trades(session_id)
        out = []
        for item in trades:
            trade_time = item.trade_time
            trade_time_s = (
                trade_time.isoformat()
                if hasattr(trade_time, "isoformat")
                else str(trade_time)
            )
            out.append(
                {
                    "trade_id": str(item.trade_id),
                    "order_id": str(item.order_id),
                    "symbol": item.stock_code,
                    "side": item.side,
                    "quantity": int(item.volume),
                    "price": float(item.price),
                    "amount": float(item.amount),
                    "trade_time": trade_time_s,
                    "commission": float(item.commission),
                }
            )
        return out

    async def submit_order(
        self,
        *,
        stock_code: str,
        side: str,
        volume: int,
        price: Optional[float] = None,
        order_type: str = "LIMIT",
        strategy_name: Optional[str] = None,
    ) -> dict:
        """Submit a real broker order via qmt-proxy ``/api/v1/trading/order``.

        Same session-management contract as :meth:`fetch_account` /
        :meth:`fetch_positions`: serialize under ``_trading_lock``, ensure a live
        trading session, and transparently re-connect once on a "账户未连接"
        error (the session can lapse between cycles). Returns a plain dict so the
        execution adapter never depends on the SDK pydantic model shape.
        """
        async with self._trading_lock:
            await self._ensure_trading_session_locked()
            try:
                return await self._submit_order_body(
                    stock_code=stock_code,
                    side=side,
                    volume=volume,
                    price=price,
                    order_type=order_type,
                    strategy_name=strategy_name,
                )
            except ClientError as exc:
                if self.account_id and _is_account_not_connected(exc):
                    self.session_id = None
                    await self._connect_trading_and_persist()
                    return await self._submit_order_body(
                        stock_code=stock_code,
                        side=side,
                        volume=volume,
                        price=price,
                        order_type=order_type,
                        strategy_name=strategy_name,
                    )
                raise

    async def _submit_order_body(
        self,
        *,
        stock_code: str,
        side: str,
        volume: int,
        price: Optional[float],
        order_type: str,
        strategy_name: Optional[str],
    ) -> dict:
        session_id = self._require_session_id()
        resp = await self._client.trading.submit_order(
            session_id=session_id,
            stock_code=stock_code,
            side=side,
            volume=int(volume),
            price=price,
            order_type=order_type,
            strategy_name=strategy_name,
        )
        return {
            "order_id": resp.order_id,
            "stock_code": resp.stock_code,
            "side": resp.side,
            "order_type": resp.order_type,
            "volume": int(resp.volume),
            "price": resp.price,
            "status": resp.status,
            "filled_volume": int(resp.filled_volume),
            "filled_amount": float(resp.filled_amount),
            "average_price": resp.average_price,
        }

    async def cancel_order(self, *, order_id: str) -> dict:
        """Cancel a live broker order via qmt-proxy ``/api/v1/trading/cancel``.

        Same session-management contract as :meth:`submit_order` (serialize under
        ``_trading_lock``, ensure a live session, re-connect once on "账户未连接").
        Returns ``{"order_id", "success"}``.
        """
        async with self._trading_lock:
            await self._ensure_trading_session_locked()
            try:
                return await self._cancel_order_body(order_id)
            except ClientError as exc:
                if self.account_id and _is_account_not_connected(exc):
                    self.session_id = None
                    await self._connect_trading_and_persist()
                    return await self._cancel_order_body(order_id)
                raise

    async def _cancel_order_body(self, order_id: str) -> dict:
        session_id = self._require_session_id()
        result = await self._client.trading.cancel_order(
            session_id=session_id, order_id=str(order_id)
        )
        return {"order_id": str(order_id), "success": bool(result.success)}

    async def fetch_latest_quotes(self, symbols):
        response = await self._client.data.get_full_tick(stock_codes=list(symbols))
        quotes = []
        for symbol in symbols:
            ticks = response.ticks.get(symbol, [])
            if not ticks:
                continue
            tick = ticks[-1]
            price = float(tick.last_price)
            tick_payload = tick.model_dump(mode="json", exclude_none=True)
            quotes.append(
                {
                    "symbol": symbol,
                    "price": price,
                    "last": price,
                    "ts": tick.time,
                    "tick": tick_payload,
                }
            )
        return quotes

    def _require_session_id(self) -> str:
        if not self.session_id:
            raise RuntimeError("qmt session_id is required for trading account queries")
        return self.session_id

    async def _ensure_trading_session_locked(self) -> None:
        if self.account_id is None:
            self._require_session_id()
            return
        if not self.session_id:
            await self._connect_trading_and_persist()

    async def _connect_trading_and_persist(self) -> None:
        if not self.account_id:
            raise RuntimeError("qmt account_id is required to establish a trading session")
        ctx = _connect_debug_context(self)
        try:
            resp = await self._client.trading.connect(account_id=self.account_id)
        except Exception as exc:
            logger.error(
                "qmt trading connect request failed (%s); %s",
                exc,
                ctx,
                exc_info=True,
            )
            raise RuntimeError(
                f"qmt-proxy connect request failed: {exc}. {ctx}. {_QMT_PROXY_CONNECT_HINT}"
            ) from exc
        if not resp.success or not resp.session_id:
            detail = (resp.message or "qmt trading connect failed").strip()
            logger.error(
                "qmt trading connect rejected by proxy: success=%s session_id=%s message=%r; %s",
                resp.success,
                resp.session_id,
                detail,
                ctx,
            )
            raise RuntimeError(
                f"qmt-proxy connect failed ({detail}). {ctx}. {_QMT_PROXY_CONNECT_HINT}"
            )
        self.session_id = resp.session_id
        # Persist the refreshed session id back onto the account row. A
        # persistence failure must stay visible (per CLAUDE.md §错误可见性):
        # log with type+message+account_pk and emit a structured debug event,
        # but do not abort the trading flow — the in-memory session is valid.
        if self._session_persist is not None and self.account_pk:
            try:
                await self._session_persist(self.account_pk, self.session_id)
            except Exception as exc:  # noqa: BLE001 — visible, non-fatal
                logger.warning(
                    "account_session_persist_failed account_pk=%s (%s): %s",
                    self.account_pk,
                    type(exc).__name__,
                    exc,
                )
                await _emit_session_persist_failed(self.account_pk, exc)

    async def _fetch_today_bar_from_tick(self, symbol: str, start_time: str) -> list[dict]:
        """Fetch today's incomplete 1d bar using get_full_tick (current OHLCV snapshot)."""
        # Use the date portion of start_time for market hours bounds
        date_str = start_time[:10]  # e.g. "2026-04-28"
        session_start = f"{date_str} 09:30:00"
        session_end = f"{date_str} 15:00:00"
        response = await self._client.data.get_full_tick(
            stock_codes=[symbol],
            start_time=session_start,
            end_time=session_end,
        )
        ticks = response.ticks.get(symbol, [])
        if not ticks:
            return []
        tick = ticks[-1]
        return [
            {
                "symbol": symbol,
                "ts": date_str,
                "open": tick.open,
                "high": tick.high,
                "low": tick.low,
                "close": tick.last_price,
                "volume": tick.volume or 0,
                "amount": tick.amount,
            }
        ]


class QmtProxyWsClient:
    """Thin async WebSocket wrapper over the vendored SDK."""

    def __init__(
        self,
        base_url: str,
        token: Optional[str] = None,
        sdk_client: AsyncQmtProxyClient | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._owns_client = sdk_client is None
        self._client = sdk_client or AsyncQmtProxyClient(
            base_url=self.base_url,
            api_key=token,
        )

    def subscribe_quotes(self, symbols, period: str = "tick"):
        return self._client.data.subscribe_and_stream(symbols=list(symbols), period=period)

    async def aclose(self):
        if self._owns_client:
            await self._client.aclose()


_INTRADAY_INTERVALS = {"1m", "5m", "15m", "30m", "60m"}


def _compact_date(value: str) -> str:
    if not value:
        return ""
    date_part = value.split("T", 1)[0]
    return date_part.replace("-", "")


def _datetime_str(value: str) -> str:
    """Convert ISO datetime to 'YYYY-MM-DD HH:MM:SS' format for get_full_kline."""
    if not value:
        return ""
    return value.replace("T", " ")[:19]


def _range_includes_today(start_time: str, end_time: str) -> bool:
    """Return True only when the requested range is *only* today (no historical bars needed).

    get_full_tick is a real-time subscription API — it cannot backfill historical data.
    Using it when the range includes past dates (even if today is the end date) causes
    silent empty results. We only fall back to it when the caller explicitly asked for
    today's incomplete bar (start_date == end_date == today).
    """
    if not end_time:
        return False
    end_date_str = end_time[:10]
    start_date_str = start_time[:10] if start_time else ""
    try:
        end_date = date.fromisoformat(end_date_str)
        start_date = date.fromisoformat(start_date_str) if start_date_str else None
    except ValueError:
        return False
    today = date.today()
    return end_date == today and (start_date is None or start_date == today)
