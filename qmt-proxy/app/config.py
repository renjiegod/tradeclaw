"""
应用配置管理
"""
import os
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from loguru import logger
from pydantic import BaseModel, Field


class XTQuantMode(str, Enum):
    """xtquant接口模式"""
    MOCK = "mock"  # 不连接xtquant，使用模拟数据
    DEV = "dev"    # 连接xtquant，获取真实数据，但不允许交易
    PROD = "prod"  # 连接xtquant，获取真实数据，允许真实交易


class AppConfig(BaseModel):
    """应用基础配置"""
    name: str = "qmt-proxy"
    version: str = "1.0.0"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 8000


class LoggingConfig(BaseModel):
    """日志配置"""
    level: str = "INFO"
    file: Optional[str] = "logs/app.log"
    error_file: Optional[str] = "logs/error.log"
    format: str = "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}"
    rotation: str = "10 MB"  # 日志文件轮转大小
    retention: str = "30 days"  # 日志保留时间
    compression: str = "zip"  # 压缩格式
    console_output: bool = True  # 是否同时输出到控制台
    backtrace: bool = True  # 是否显示完整堆栈跟踪
    diagnose: bool = False  # 是否显示诊断信息


class XTQuantDataConfig(BaseModel):
    """xtquant数据配置"""
    path: str = "./data"
    config_path: str = "./xtquant/config"
    qmt_userdata_path: Optional[str] = None  # QMT客户端的userdata_mini路径
    # 行情订阅配置
    max_queue_size: int = 1000  # 每个订阅队列最大长度
    max_subscriptions: int = 100  # 单实例最大订阅数
    heartbeat_timeout: int = 60  # WebSocket心跳超时（秒）
    whole_quote_enabled: bool = False  # 是否允许全推订阅


class XTQuantTradingConfig(BaseModel):
    """xtquant交易配置"""
    allow_real_trading: bool = False
    mock_account_id: str = "mock_account_001"
    mock_password: str = "mock_password"
    test_account_id: Optional[str] = None
    test_password: Optional[str] = None
    real_accounts: Optional[List[Dict[str, Any]]] = None


class QmtClientConfig(BaseModel):
    """单个 QMT 终端（客户端）配置。

    一个 client 对应一个独立运行的 QMT 极速交易终端，由其 ``userdata_mini``
    路径唯一标识。交易链路为每个 client 维护独立的 ``XtQuantTrader`` 实例；
    行情数据与券商无关，默认只用其中一个 client 作为数据源（见
    ``XTQuantConfig.resolve_data_source_client``）。

    ``mode`` / ``allow_real_trading`` 为可选的“按 client 覆盖”，留空时回退到
    全局 ``XTQuantConfig.mode`` / ``XTQuantConfig.trading.allow_real_trading``。
    经 ``XTQuantConfig.resolve_clients`` 解析后，这两个字段一定是具体值。
    """
    client_id: str
    name: Optional[str] = None
    qmt_userdata_path: Optional[str] = None
    mode: Optional[XTQuantMode] = None
    allow_real_trading: Optional[bool] = None
    is_data_source: bool = False


class XTQuantConfig(BaseModel):
    """xtquant配置"""
    mode: XTQuantMode = XTQuantMode.MOCK
    data: XTQuantDataConfig = Field(default_factory=XTQuantDataConfig)
    trading: XTQuantTradingConfig = Field(default_factory=XTQuantTradingConfig)
    # 多客户端（多 QMT 终端）配置；为空时由 data.qmt_userdata_path + 全局 mode
    # 合成单个 client（client_id="default"），保持单终端部署的向后兼容。
    clients: List[QmtClientConfig] = Field(default_factory=list)
    default_client_id: Optional[str] = None
    data_source_client_id: Optional[str] = None

    def resolve_clients(self) -> List[QmtClientConfig]:
        """返回已填充默认值的 client 列表（mode/allow_real_trading/path 均为具体值）。

        - 配置了 ``clients`` 时：逐个用全局值补全留空字段。
        - 未配置 ``clients`` 时：用 ``data.qmt_userdata_path`` + 全局 mode 合成
          一个 ``client_id="default"`` 的单 client（向后兼容旧单终端部署）。
        """
        if self.clients:
            # client_id 是路由主键，重复会让后一个静默覆盖前一个（少一个终端且无报错）。
            # 启动期直接拒绝，并明确指出重复的 id。
            seen: set[str] = set()
            duplicates: list[str] = []
            for client in self.clients:
                if client.client_id in seen:
                    duplicates.append(client.client_id)
                seen.add(client.client_id)
            if duplicates:
                raise ValueError(
                    "xtquant.clients 存在重复的 client_id: "
                    f"{sorted(set(duplicates))}；每个 QMT 终端必须用唯一的 client_id "
                    "（例如把重名的第二个终端改成 gj_real）"
                )
            resolved: List[QmtClientConfig] = []
            for client in self.clients:
                resolved.append(
                    QmtClientConfig(
                        client_id=client.client_id,
                        name=client.name or client.client_id,
                        qmt_userdata_path=client.qmt_userdata_path or self.data.qmt_userdata_path,
                        mode=client.mode if client.mode is not None else self.mode,
                        allow_real_trading=(
                            client.allow_real_trading
                            if client.allow_real_trading is not None
                            else self.trading.allow_real_trading
                        ),
                        is_data_source=client.is_data_source,
                    )
                )
            return resolved
        return [
            QmtClientConfig(
                client_id="default",
                name="default",
                qmt_userdata_path=self.data.qmt_userdata_path,
                mode=self.mode,
                allow_real_trading=self.trading.allow_real_trading,
                is_data_source=True,
            )
        ]

    def resolve_default_client_id(self) -> str:
        """解析默认 client_id：显式配置优先且必须存在，否则取第一个 client。"""
        clients = self.resolve_clients()
        if self.default_client_id and any(c.client_id == self.default_client_id for c in clients):
            return self.default_client_id
        return clients[0].client_id

    def get_client(self, client_id: str) -> Optional[QmtClientConfig]:
        """按 client_id 返回解析后的 client；不存在返回 None。"""
        for client in self.resolve_clients():
            if client.client_id == client_id:
                return client
        return None

    def resolve_client_id(self, requested: Optional[str]) -> Optional[str]:
        """把请求侧传入的 client_id 解析为有效 client_id。

        - 传入有效 client_id：原样返回。
        - 传入空：返回默认 client_id。
        - 传入未知 client_id：返回 None（由调用方决定如何报错），不静默回退，
          避免“路由到错误终端”这类不可见错误。
        """
        if not requested:
            return self.resolve_default_client_id()
        if self.get_client(requested) is not None:
            return requested
        return None

    def resolve_data_source_client(self) -> QmtClientConfig:
        """解析行情数据源 client：显式配置 > is_data_source 标记 > 默认 client。"""
        clients = self.resolve_clients()
        if self.data_source_client_id:
            for client in clients:
                if client.client_id == self.data_source_client_id:
                    return client
        for client in clients:
            if client.is_data_source:
                return client
        default_id = self.resolve_default_client_id()
        for client in clients:
            if client.client_id == default_id:
                return client
        return clients[0]


class SecurityConfig(BaseModel):
    """安全配置"""
    secret_key: str = "your-secret-key-change-in-production"
    api_key_header: str = "X-API-Key"
    api_keys: List[str] = Field(default_factory=list)


class CORSConfig(BaseModel):
    """CORS配置"""
    allow_origins: List[str] = Field(default_factory=lambda: ["*"])
    allow_credentials: bool = True
    allow_methods: List[str] = Field(default_factory=lambda: ["*"])
    allow_headers: List[str] = Field(default_factory=lambda: ["*"])

class UvicornConfig(BaseModel):
    """uvicorn配置"""
    timeout_keep_alive: int = 5


class Settings(BaseModel):
    """完整配置类"""
    app: AppConfig = Field(default_factory=AppConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    xtquant: XTQuantConfig = Field(default_factory=XTQuantConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    cors: CORSConfig = Field(default_factory=CORSConfig)
    uvicorn: UvicornConfig = Field(default_factory=UvicornConfig)
    
    # gRPC 配置（使用属性访问以保持向后兼容）
    grpc_enabled: bool = True
    grpc_host: str = "0.0.0.0"
    grpc_port: int = 50051
    grpc_max_workers: int = 10
    grpc_max_message_length: int = 50 * 1024 * 1024  # 50MB


def default_home_config_path() -> Path:
    """默认可写配置路径：``$DOYOUTRADE_HOME/qmt-proxy.yml`` 或 ``~/.doyoutrade/qmt-proxy.yml``。

    doyoutrade 内嵌 qmt-proxy 时会显式设置 ``QMT_PROXY_CONFIG`` 指向同一目录；
    独立部署时回退到用户主目录下的 ``.doyoutrade``，与 doyoutrade 全局配置同根。
    """
    home = os.environ.get("DOYOUTRADE_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".doyoutrade"
    return base / "qmt-proxy.yml"


def resolve_config_path() -> str:
    """解析配置文件路径（读写共用同一真源，避免 GET/PUT 与实际加载文件不一致）。

    优先级：
      1. 环境变量 ``QMT_PROXY_CONFIG``（显式路径，最高优先，无论是否存在）；
      2. ``~/.doyoutrade/qmt-proxy.yml``（新默认位置，存在时采用）；
      3. 兼容旧行为：当前工作目录下的 ``config.yml``（存在时采用）；
      4. 都不存在时回退到 (2) 作为默认写入目标（供 seeding 使用）。
    """
    env_path = os.environ.get("QMT_PROXY_CONFIG")
    if env_path:
        return str(Path(env_path).expanduser())

    home_path = default_home_config_path()
    if home_path.exists():
        return str(home_path)

    legacy_path = Path.cwd() / "config.yml"
    if legacy_path.exists():
        return str(legacy_path)

    return str(home_path)


def load_config(config_file: Optional[str] = None) -> Settings:
    """
    加载配置文件
    通过环境变量 APP_MODE 选择模式: mock, dev, prod
    默认使用 dev 模式
    """
    if config_file is None:
        config_file = resolve_config_path()

    if not os.path.exists(config_file):
        return Settings()

    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            config_data = yaml.safe_load(f)

        # 获取运行模式
        app_mode = os.getenv("APP_MODE", "dev").lower()
        
        if app_mode not in ["mock", "dev", "prod"]:
            app_mode = "dev"
        
        # 获取模式特定配置
        modes_config = config_data.get("modes", {})
        mode_config = modes_config.get(app_mode, {})
        
        if not mode_config:
            return Settings()
        
        # 构建完整配置
        final_config = {
            "app": {
                "name": config_data.get("app", {}).get("name", "qmt-proxy"),
                "version": config_data.get("app", {}).get("version", "1.0.0"),
                "debug": mode_config.get("debug", False),
                "host": mode_config.get("host", "0.0.0.0"),
                "port": mode_config.get("port", 8000)
            },
            "logging": {
                "level": mode_config.get("log_level", "INFO"),
                "file": config_data.get("logging", {}).get("file", "logs/app.log"),
                "error_file": config_data.get("logging", {}).get("error_file", "logs/error.log"),
                "format": config_data.get("logging", {}).get("format"),
                "rotation": config_data.get("logging", {}).get("rotation", "10 MB"),
                "retention": config_data.get("logging", {}).get("retention", "30 days"),
                "compression": config_data.get("logging", {}).get("compression", "zip"),
                # 允许模式特定配置覆盖全局配置
                "console_output": mode_config.get("logging", {}).get("console_output", config_data.get("logging", {}).get("console_output", True)),
                "backtrace": mode_config.get("logging", {}).get("backtrace", config_data.get("logging", {}).get("backtrace", True)),
                "diagnose": mode_config.get("logging", {}).get("diagnose", config_data.get("logging", {}).get("diagnose", False))
            },
            "xtquant": {
                "mode": mode_config.get("xtquant_mode", app_mode),
                "data": {
                    "path": config_data.get("xtquant", {}).get("data", {}).get("path", "./data"),
                    "config_path": config_data.get("xtquant", {}).get("data", {}).get("config_path", "./xtquant/config"),
                    "qmt_userdata_path": config_data.get("xtquant", {}).get("qmt_userdata_path")
                },
                "trading": {
                    "allow_real_trading": mode_config.get("allow_real_trading", False),
                    "mock_account_id": "mock_account_001",
                    "mock_password": "mock_password"
                },
                # 多客户端配置（可选）；留空时由 XTQuantConfig.resolve_clients 合成单 client。
                # clients 是 mode 无关的（每个 client 自带 qmt_userdata_path 与可选的
                # mode/allow_real_trading 覆盖），因此从 xtquant 顶层读取而非 modes 下。
                "clients": config_data.get("xtquant", {}).get("clients", []) or [],
                "default_client_id": config_data.get("xtquant", {}).get("default_client_id"),
                "data_source_client_id": config_data.get("xtquant", {}).get("data_source_client_id"),
            },
            "security": {
                "secret_key": config_data.get("security", {}).get("secret_key", "change-me"),
                "api_key_header": config_data.get("security", {}).get("api_key_header", "X-API-Key"),
                "api_keys": mode_config.get("api_keys", [])
            },
            "cors": mode_config.get("cors", {
                "allow_origins": ["*"],
                "allow_credentials": True,
                "allow_methods": ["*"],
                "allow_headers": ["*"]
            }),
            "uvicorn": {
                "timeout_keep_alive": config_data.get("uvicorn", {}).get("timeout_keep_alive", 5)
            },
            "grpc_enabled": config_data.get("grpc", {}).get("enabled", True),
            "grpc_host": config_data.get("grpc", {}).get("host", "0.0.0.0"),
            "grpc_port": config_data.get("grpc", {}).get("port", 50051),
            "grpc_max_workers": config_data.get("grpc", {}).get("max_workers", 10),
            "grpc_max_message_length": config_data.get("grpc", {}).get("max_message_length", 50 * 1024 * 1024),
        }
        
        return Settings(**final_config)

    except Exception as exc:
        # 错误可见性：解析失败不再静默吞（历史是 traceback.print_exc 后返回默认，
        # 排查时几乎不可见）。记 error 带异常类型 + 消息 + 出错文件路径；仍回退到
        # 默认 Settings 保证进程可启动，但故障必须在日志中可见。
        logger.error(
            "加载配置文件失败，回退到默认 Settings（可能屏蔽真实配置）: "
            f"{type(exc).__name__}: {exc} | config_file={config_file} | "
            f"app_mode={os.getenv('APP_MODE', 'dev')}"
        )
        return Settings()


_settings_instance: Optional[Settings] = None


def get_settings() -> Settings:
    """获取配置实例（单例模式）"""
    global _settings_instance
    if _settings_instance is None:
        _settings_instance = load_config()
    return _settings_instance


def reset_settings():
    """重置配置实例（用于测试）"""
    global _settings_instance
    _settings_instance = None


# 全局配置实例（延迟加载）
settings = None
