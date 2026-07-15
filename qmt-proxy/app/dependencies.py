"""
依赖注入模块
"""
import os
import sys
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# 添加xtquant包到Python路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.config import Settings, get_settings
from app.utils.exceptions import AuthenticationException, TradingServiceException
from app.utils.logger import logger

# 请求侧选择 QMT 终端（client）的 HTTP 头；缺省走默认终端。
CLIENT_ID_HEADER = "X-QMT-Terminal"

# 安全方案
security = HTTPBearer(auto_error=False)


def _mask_credential(value: str) -> str:
    """日志中脱敏凭证，保留首尾各 2 字符与长度便于对照。"""
    if len(value) <= 4:
        return "****"
    return f"{value[:2]}...{value[-2:]}(len={len(value)})"


def _auth_failure_hints(request: Request, settings: Settings) -> str:
    """汇总常见误用方式，便于从日志一眼看出调用方传参问题。"""
    hints: list[str] = []
    if request.query_params.get("api_key"):
        hints.append(
            "query 参数 api_key 已忽略（请改用 Authorization: Bearer <key>）"
        )
    legacy_header = settings.security.api_key_header
    if legacy_header and request.headers.get(legacy_header):
        hints.append(
            f"请求头 {legacy_header} 已忽略（请改用 Authorization: Bearer <key>）"
        )
    auth_header = request.headers.get("Authorization")
    if auth_header and not auth_header.lower().startswith("bearer "):
        hints.append(f"Authorization 格式应为 Bearer <key>，当前: {_mask_credential(auth_header)}")
    elif not auth_header:
        hints.append("缺少 Authorization: Bearer <api_key> 请求头")
    return "; ".join(hints) if hints else "无额外提示"


def _request_log_context(request: Request) -> str:
    client = request.client.host if request.client else "?"
    terminal = request.headers.get(CLIENT_ID_HEADER) or "(default)"
    return f"{request.method} {request.url.path} client={client} terminal={terminal}"


# 全局服务实例（单例模式）
_data_service_instance = None
_trading_manager_instance = None
_subscription_manager_instance = None


def get_data_service(settings: Settings = Depends(get_settings)):
    """获取DataService单例实例"""
    global _data_service_instance

    if _data_service_instance is None:
        from app.services.data_service import DataService
        logger.info("初始化 DataService...")
        _data_service_instance = DataService(settings)

    return _data_service_instance


def get_trading_manager(settings: Settings = Depends(get_settings)):
    """获取 TradingClientManager 单例（多 QMT 终端注册表）。"""
    global _trading_manager_instance

    if _trading_manager_instance is None:
        from app.services.trading_manager import TradingClientManager
        logger.info("初始化 TradingClientManager...")
        _trading_manager_instance = TradingClientManager(settings)

    return _trading_manager_instance


def get_trading_service(settings: Settings = Depends(get_settings)):
    """获取默认终端的 TradingService（向后兼容旧调用方 / gRPC 默认路由）。"""
    return get_trading_manager(settings).get_service(None)


def get_client_id(
    x_qmt_terminal: Optional[str] = Header(default=None, alias=CLIENT_ID_HEADER),
) -> Optional[str]:
    """从 X-QMT-Terminal 头读取请求侧选择的终端 client_id（可空 → 默认终端）。"""
    return x_qmt_terminal


def get_request_trading_service(
    client_id: Optional[str] = Depends(get_client_id),
    manager=Depends(get_trading_manager),
):
    """按 X-QMT-Terminal 头路由到对应终端的 TradingService。

    未知 client_id 直接 400 + error_code（不静默回退到默认终端，避免下错单到错误终端）。
    """
    try:
        return manager.get_service(client_id)
    except TradingServiceException as exc:
        logger.warning(
            f"交易终端路由失败: {exc.message} | "
            f"requested={client_id!r} available={manager.client_ids()}"
        )
        raise HTTPException(
            status_code=400,
            detail={"message": exc.message, "error_code": exc.error_code or "UNKNOWN_TERMINAL"},
        )


def get_subscription_manager(settings: Settings = Depends(get_settings)):
    """获取SubscriptionManager单例实例"""
    global _subscription_manager_instance
    
    if _subscription_manager_instance is None:
        from app.services.subscription_manager import SubscriptionManager
        logger.info("初始化 SubscriptionManager...")
        _subscription_manager_instance = SubscriptionManager(settings)
    
    return _subscription_manager_instance


async def get_api_key(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    settings: Settings = Depends(get_settings)
) -> Optional[str]:
    """获取API密钥"""
    if not credentials:
        return None
    
    # 这里可以添加API密钥验证逻辑
    # 目前简单返回token
    return credentials.credentials


async def verify_api_key(
    request: Request,
    api_key: Optional[str] = Depends(get_api_key),
    settings: Settings = Depends(get_settings),
) -> str:
    """验证API密钥

    失败一律抛 AuthenticationException（带 error_code 区分失败模式），由
    app.main 的专用 handler 统一映射为 HTTP 401 + WWW-Authenticate: Bearer：

    - API_KEY_MISSING: 缺少 Authorization 头（或 Bearer 后为空）
    - AUTHORIZATION_MALFORMED: 有 Authorization 头但不是 Bearer scheme
    - API_KEY_INVALID: key 不在 modes.<APP_MODE>.api_keys 白名单
    """
    ctx = _request_log_context(request)
    if not api_key:
        auth_header = request.headers.get("Authorization")
        if auth_header and not auth_header.lower().startswith("bearer "):
            reason, error_code = "Authorization 头格式错误，应为 Bearer <api_key>", "AUTHORIZATION_MALFORMED"
        else:
            reason, error_code = "API密钥缺失", "API_KEY_MISSING"
        logger.warning(
            f"API 认证失败: {reason} | error_code={error_code} | {ctx} | "
            f"{_auth_failure_hints(request, settings)}"
        )
        raise AuthenticationException(reason, error_code=error_code)

    # 验证API密钥是否在允许列表中
    if settings.security.api_keys and api_key not in settings.security.api_keys:
        logger.warning(
            f"API 认证失败: 无效的API密钥 | error_code=API_KEY_INVALID | {ctx} | "
            f"provided={_mask_credential(api_key)} | "
            f"hint=配置 modes.<APP_MODE>.api_keys 白名单，当前 APP_MODE={os.getenv('APP_MODE', 'dev')}"
        )
        raise AuthenticationException("无效的API密钥", error_code="API_KEY_INVALID")

    return api_key


def get_xtquant_data_path(settings: Settings = Depends(get_settings)) -> str:
    """获取xtquant数据路径"""
    return settings.xtquant.data.path


def get_xtquant_config_path(settings: Settings = Depends(get_settings)) -> str:
    """获取xtquant配置路径"""
    return settings.xtquant.data.config_path


def get_xtquant_mode(settings: Settings = Depends(get_settings)) -> str:
    """获取xtquant接口模式"""
    return settings.xtquant.mode.value


def is_real_trading_allowed(settings: Settings = Depends(get_settings)) -> bool:
    """检查是否允许真实交易"""
    return (
        settings.xtquant.mode.value == "real" and 
        settings.xtquant.trading.allow_real_trading
    )
