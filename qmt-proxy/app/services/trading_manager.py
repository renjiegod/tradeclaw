"""多 QMT 终端（客户端）交易管理器。

一个 qmt-proxy 进程可同时对接多个分属不同券商/终端的 QMT 客户端。每个终端由其
``userdata_mini`` 路径唯一标识（见 ``QmtClientConfig``），交易链路为每个终端维护
一个**独立的** ``TradingService``（内部各持有自己的 ``XtQuantTrader``）。

设计取舍：``TradingService`` 保持“单终端”语义完全不变（其既有单元测试不受影响），
本管理器通过**组合**在其之上做多实例注册 + 路由。每个 ``TradingService`` 拿到的是
一份按终端覆盖了 ``mode`` / ``qmt_userdata_path`` / ``allow_real_trading`` 的
``Settings`` 副本，因此服务内部无需感知“多终端”这件事。

线程模型：``XtQuantTrader.__init__`` 会调用 ``asyncio.set_event_loop``，因此其创建
必须发生在非主事件循环线程上。FastAPI 的同步 ``Depends`` 会在线程池中调用
``get_service``，从而满足该约束；``_lock`` 串行化懒加载，避免并发重复创建。
"""
from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from app.config import QmtClientConfig, Settings
from app.services.trading_service import TradingService
from app.utils.exceptions import TradingServiceException
from app.utils.logger import logger

# 未知终端的稳定错误码，路由层据此返回 400 + error_code（见 handle_xtquant_exception）。
UNKNOWN_TERMINAL_ERROR_CODE = "UNKNOWN_TERMINAL"


class TradingClientManager:
    """按 ``client_id`` 持有并路由多个 ``TradingService`` 的注册表。"""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._clients: Dict[str, QmtClientConfig] = {
            client.client_id: client for client in settings.xtquant.resolve_clients()
        }
        self._default_client_id = settings.xtquant.resolve_default_client_id()
        self._services: Dict[str, TradingService] = {}
        self._lock = threading.Lock()
        logger.info(
            f"TradingClientManager 初始化完成：终端={list(self._clients.keys())}，"
            f"默认={self._default_client_id}"
        )

    # ---- 元信息 -----------------------------------------------------------
    @property
    def default_client_id(self) -> str:
        return self._default_client_id

    def client_ids(self) -> List[str]:
        return list(self._clients.keys())

    def has_client(self, client_id: str) -> bool:
        return client_id in self._clients

    def resolve_client_id(self, requested: Optional[str]) -> str:
        """把请求侧 client_id 解析为有效值；未知则抛带 error_code 的异常（不静默回退）。"""
        resolved = self._settings.xtquant.resolve_client_id(requested)
        if resolved is None:
            raise TradingServiceException(
                f"未知的 QMT 终端 client_id: {requested!r}；可用终端: {self.client_ids()}",
                error_code=UNKNOWN_TERMINAL_ERROR_CODE,
            )
        return resolved

    # ---- 服务获取 ---------------------------------------------------------
    def _build_client_settings(self, client: QmtClientConfig) -> Settings:
        """构造按终端覆盖关键字段的 Settings 副本，供单终端 TradingService 使用。"""
        client_settings = self._settings.model_copy(deep=True)
        client_settings.xtquant.mode = client.mode
        client_settings.xtquant.data.qmt_userdata_path = client.qmt_userdata_path
        client_settings.xtquant.trading.allow_real_trading = bool(client.allow_real_trading)
        return client_settings

    def get_service(self, client_id: Optional[str] = None) -> TradingService:
        """获取指定终端的 TradingService（懒加载、线程安全）。"""
        resolved = self.resolve_client_id(client_id)
        with self._lock:
            service = self._services.get(resolved)
            if service is None:
                client = self._clients[resolved]
                mode_value = client.mode.value if client.mode else None
                logger.bind(client_id=resolved).info(
                    f"懒加载 TradingService：client_id={resolved} "
                    f"path={client.qmt_userdata_path} mode={mode_value}"
                )
                service = TradingService(self._build_client_settings(client), client_id=resolved)
                self._services[resolved] = service
            return service

    # ---- 状态 -------------------------------------------------------------
    def list_clients(self) -> List[Dict[str, Any]]:
        """列出所有已配置终端及其状态（不会强制初始化未加载的终端）。"""
        result: List[Dict[str, Any]] = []
        with self._lock:
            loaded = dict(self._services)
        for client_id, client in self._clients.items():
            service = loaded.get(client_id)
            result.append(
                {
                    "client_id": client_id,
                    "name": client.name,
                    "qmt_userdata_path": client.qmt_userdata_path,
                    "mode": client.mode.value if client.mode else None,
                    "allow_real_trading": bool(client.allow_real_trading),
                    "is_data_source": client.is_data_source,
                    "is_default": client_id == self._default_client_id,
                    "loaded": service is not None,
                    "initialized": bool(service and service._initialized),
                    "init_failure_reason": service._init_failure_reason if service else None,
                }
            )
        return result

    def shutdown(self) -> None:
        """关闭所有已加载终端的 TradingService。"""
        with self._lock:
            services = list(self._services.items())
            self._services.clear()
        for client_id, service in services:
            disconnect = getattr(service, "disconnect_all", None)
            if callable(disconnect):
                try:
                    disconnect()
                except Exception as exc:  # noqa: BLE001 - 关闭尽力而为，但要可见
                    logger.bind(client_id=client_id).warning(
                        f"关闭终端 TradingService 失败 client_id={client_id}: "
                        f"{type(exc).__name__}: {exc}"
                    )
