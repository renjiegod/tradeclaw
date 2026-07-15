"""qmt-proxy 配置读写（Web UI 可改的静态/低频配置）。

契约见共享规格「契约 B」。关键点：

- 文件按 ``modes.<APP_MODE>`` 分段（见 ``app/config.py::load_config``）。写回时必须
  把「与运行模式相关」的字段写进 ``modes.<当前 APP_MODE>`` 段，把「与模式无关」的
  字段写到顶层——写错位置会静默丢配置。
- YAML 用 ``ruamel.yaml`` round-trip 读写，保留注释（PyYAML 会丢注释，禁止用于写回）。
- secret（``security.api_keys`` / ``security.secret_key``）GET 时脱敏为 ``********``；
  PUT 时若值等于掩码或缺省 → 保持原值不变。
- 错误可见性（CLAUDE.md §错误可见性）：坏值直接结构化报错（``ConfigStoreError``，带
  ``error_code`` / ``field``），禁止静默 coercion / except pass；每次写盘 ``logger.info``
  记录改了哪些字段 + restart_required。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from app.config import (
    QmtClientConfig,
    XTQuantConfig,
    XTQuantMode,
    get_settings,
    reset_settings,
    resolve_config_path,
)
from app.utils.logger import logger

# 脱敏掩码：GET 返回 secret 时替换成它；PUT 时若收到它 → 视为“未改”，保留原值。
MASK = "********"

# 一个哨兵，用于区分「patch 未提供该字段」与「显式传 null」。
_MISSING = object()

# 契约 B：qmt-proxy 侧所有可写字段改完都需重启 proxy（service 单例启动时深拷贝快照
# xtquant）。因此 restart_required_fields = 全部可写字段。顺序与契约保持一致。
RESTART_REQUIRED_FIELDS: List[str] = [
    "xtquant.mode",
    "xtquant.data.qmt_userdata_path",
    "xtquant.clients",
    "xtquant.default_client_id",
    "xtquant.data_source_client_id",
    "xtquant.trading.allow_real_trading",
    "security.api_keys",
    "logging.level",
    "grpc.enabled",
    "grpc.host",
    "grpc.port",
    "app.host",
    "app.port",
]

_RESTART_FIELD_SET = set(RESTART_REQUIRED_FIELDS)

# 首启缺文件时的最小合法结构（含 modes.{mock,dev,prod}）。用 ruamel 解析成
# CommentedMap 以带上注释，seeding 后仍是 round-trip 友好的文档。
_SEED_TEMPLATE = """\
# qmt-proxy 服务端配置（由 doyoutrade 配置管理自动生成）
# 位置：~/.doyoutrade/qmt-proxy.yml（可用环境变量 QMT_PROXY_CONFIG 覆盖）
# 运行模式由环境变量 APP_MODE 选择：mock / dev / prod（缺省 dev）

app:
  name: "qmt-proxy"
  version: "1.0.0"

# gRPC 配置（顶层：与运行模式无关）
grpc:
  enabled: true
  host: "0.0.0.0"
  port: 50051
  max_workers: 10

# 日志配置（顶层）；每个模式可用 modes.<mode>.log_level 覆盖级别
logging:
  format: "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}"
  file: "logs/app.log"
  error_file: "logs/error.log"
  rotation: "10 MB"
  retention: "30 days"
  compression: "zip"

# 安全配置（顶层）
security:
  secret_key: "change-this-to-secure-key-in-production"
  api_key_header: "X-API-Key"

# xtquant 配置（顶层：与运行模式无关）
xtquant:
  qmt_userdata_path:
  clients: []
  default_client_id:
  data_source_client_id:

# 运行模式配置（写回时 mode 相关字段落在 modes.<APP_MODE> 段）
modes:
  mock:
    xtquant_mode: "mock"
    allow_real_trading: false
    host: "0.0.0.0"
    port: 8000
    log_level: "INFO"
    api_keys: []
  dev:
    xtquant_mode: "dev"
    allow_real_trading: false
    host: "0.0.0.0"
    port: 8000
    log_level: "INFO"
    api_keys: []
  prod:
    xtquant_mode: "prod"
    allow_real_trading: true
    host: "0.0.0.0"
    port: 8000
    log_level: "INFO"
    api_keys: []
"""


class ConfigStoreError(Exception):
    """配置读写/校验失败的结构化异常。

    ``error_code`` 是发布给前端的稳定 token（契约沿用 ``invalid_config``）；``field``
    指出出错的点分路径，``error_type`` 区分校验错误与其它错误。
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "invalid_config",
        field: Optional[str] = None,
        error_type: str = "validation_error",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.field = field
        self.error_type = error_type


# --------------------------------------------------------------------------- #
# YAML round-trip helpers
# --------------------------------------------------------------------------- #
def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _load_document(path: str) -> CommentedMap:
    """读取现有 YAML 为 round-trip 文档；缺文件时播种最小合法结构。"""
    if not os.path.exists(path):
        return _seed_document()
    y = _yaml()
    with open(path, "r", encoding="utf-8") as f:
        doc = y.load(f)
    if doc is None:
        # 空文件：等价于缺配置，播种一份可写的结构而不是静默沿用空 map。
        logger.info(f"qmt-proxy 配置文件为空，按最小合法结构播种: path={path}")
        return _seed_document()
    if not isinstance(doc, dict):
        raise ConfigStoreError(
            f"配置文件顶层必须是映射, 实际是 {type(doc).__name__}: {doc!r}",
            field=None,
        )
    return doc


def _seed_document() -> CommentedMap:
    y = _yaml()
    return y.load(_SEED_TEMPLATE)


def _dump_document(path: str, doc: CommentedMap) -> None:
    """原子写回：先写临时文件再 rename，避免写一半损坏原配置。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    y = _yaml()
    with open(tmp, "w", encoding="utf-8") as f:
        y.dump(doc, f)
    os.replace(tmp, target)


def seed_config_if_missing(path: Optional[str] = None) -> str:
    """若配置文件缺失则播种最小合法结构，返回文件路径。

    供 doyoutrade 内嵌启动时复用（避免各自复制 example）。已存在则原样返回。
    """
    resolved = path or resolve_config_path()
    if not os.path.exists(resolved):
        _dump_document(resolved, _seed_document())
        logger.info(f"qmt-proxy 配置文件缺失，已播种最小合法结构: path={resolved}")
    return resolved


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _current_app_mode() -> str:
    mode = os.getenv("APP_MODE", "dev").lower()
    if mode not in ("mock", "dev", "prod"):
        mode = "dev"
    return mode


def _dig(patch: Dict[str, Any], *keys: str) -> Any:
    """按路径取值；中间层存在但不是对象 → 结构化报错；缺失 → 返回 ``_MISSING``。"""
    cur: Any = patch
    for i, key in enumerate(keys):
        if not isinstance(cur, dict):
            container = ".".join(keys[:i])
            raise ConfigStoreError(
                f"配置片段 {container} 必须是对象, 实际是 {type(cur).__name__}",
                field=container or None,
            )
        if key not in cur:
            return _MISSING
        cur = cur[key]
    return cur


def _ensure_map(parent: CommentedMap, key: str, dotted: str) -> CommentedMap:
    """确保 ``parent[key]`` 是映射：缺失则建空 map；存在但非 map → 结构化报错（不静默替换）。"""
    if key not in parent or parent[key] is None:
        parent[key] = CommentedMap()
    node = parent[key]
    if not isinstance(node, dict):
        raise ConfigStoreError(
            f"配置节 {dotted} 必须是映射, 实际是 {type(node).__name__}: {node!r}",
            field=dotted,
        )
    return node


def _require_str(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ConfigStoreError(
            f"{field} 必须是字符串, 实际是 {type(value).__name__}: {value!r}", field=field
        )
    return value


def _require_str_or_none(value: Any, field: str) -> Optional[str]:
    if value is None or isinstance(value, str):
        return value
    raise ConfigStoreError(
        f"{field} 必须是字符串或 null, 实际是 {type(value).__name__}: {value!r}", field=field
    )


def _require_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigStoreError(
            f"{field} 必须是布尔值, 实际是 {type(value).__name__}: {value!r}", field=field
        )
    return value


def _require_port(value: Any, field: str) -> int:
    # bool 是 int 子类，必须先排除，避免 True 被当成端口 1。
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigStoreError(
            f"{field} 必须是整数端口, 实际是 {type(value).__name__}: {value!r}", field=field
        )
    if not (1 <= value <= 65535):
        raise ConfigStoreError(f"{field} 必须在 1..65535 之间, 实际是 {value}", field=field)
    return value


def _validate_clients(clients: Any) -> List[Dict[str, Any]]:
    """复用 ``QmtClientConfig`` / ``XTQuantConfig.resolve_clients`` 校验 clients 列表。

    - schema 违反（如缺 client_id）→ pydantic ValidationError；
    - 重复 client_id → resolve_clients 抛 ValueError。
    两者都转成结构化 ``ConfigStoreError``。返回原样的 dict 列表用于写回（保留调用方字段）。
    """
    if not isinstance(clients, list):
        raise ConfigStoreError(
            f"xtquant.clients 必须是数组, 实际是 {type(clients).__name__}", field="xtquant.clients"
        )
    for idx, item in enumerate(clients):
        if not isinstance(item, dict):
            raise ConfigStoreError(
                f"xtquant.clients[{idx}] 必须是对象, 实际是 {type(item).__name__}: {item!r}",
                field="xtquant.clients",
            )
    try:
        models = [QmtClientConfig(**dict(item)) for item in clients]
        XTQuantConfig(clients=models).resolve_clients()
    except ValidationError as exc:
        raise ConfigStoreError(
            f"xtquant.clients 校验失败: {exc.errors()}", field="xtquant.clients"
        ) from exc
    except ValueError as exc:  # resolve_clients 的重复 client_id
        raise ConfigStoreError(str(exc), field="xtquant.clients") from exc
    # 返回普通 dict 列表（调用方原始内容）供写回。
    return [dict(item) for item in clients]


def _is_masked_secret_list(value: Any) -> bool:
    """判断 api_keys 是否为「脱敏占位」——空列表或每个元素都是掩码 → 视为未改。"""
    if not isinstance(value, list):
        return False
    if len(value) == 0:
        return True
    return all(item == MASK for item in value)


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def read_config_masked() -> Dict[str, Any]:
    """GET /api/v1/config 的 data：当前有效配置（脱敏）+ resolved_clients + 重启字段清单。"""
    settings = get_settings()
    xt = settings.xtquant
    api_keys = list(settings.security.api_keys or [])

    try:
        resolved_clients = [c.model_dump(mode="json") for c in xt.resolve_clients()]
    except ValueError as exc:
        # 现有文件里 clients 有重复 id：暴露给调用方修复，不要静默吞。
        raise ConfigStoreError(str(exc), field="xtquant.clients") from exc

    values = {
        "xtquant": {
            "mode": xt.mode.value,
            "data": {"qmt_userdata_path": xt.data.qmt_userdata_path},
            "trading": {"allow_real_trading": xt.trading.allow_real_trading},
            "clients": [c.model_dump(mode="json") for c in xt.clients],
            "default_client_id": xt.default_client_id,
            "data_source_client_id": xt.data_source_client_id,
        },
        "security": {
            "api_keys": [MASK for _ in api_keys],
            "api_keys_set": len(api_keys) > 0,
            "api_keys_count": len(api_keys),
        },
        "logging": {"level": settings.logging.level},
        "grpc": {
            "enabled": settings.grpc_enabled,
            "host": settings.grpc_host,
            "port": settings.grpc_port,
        },
        "app": {"host": settings.app.host, "port": settings.app.port},
    }

    return {
        "path": resolve_config_path(),
        "app_mode": _current_app_mode(),
        "values": values,
        "resolved_clients": resolved_clients,
        "restart_required_fields": list(RESTART_REQUIRED_FIELDS),
    }


def write_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    """PUT /api/v1/config：把部分 patch mode-aware 写回，reset_settings，返回重启信息。"""
    if not isinstance(patch, dict):
        raise ConfigStoreError(
            f"请求体必须是对象, 实际是 {type(patch).__name__}", field=None
        )

    path = resolve_config_path()
    doc = _load_document(path)
    mode = _current_app_mode()

    # 先读出/校验现有 secret，供脱敏保留逻辑用。
    existing_settings = get_settings()
    existing_api_keys = list(existing_settings.security.api_keys or [])

    changed_fields: List[str] = []

    def _mark(dotted: str) -> None:
        changed_fields.append(dotted)

    # ---- mode 相关字段 → modes.<mode>.* ---------------------------------- #
    modes_map = _ensure_map(doc, "modes", "modes")
    mode_map = _ensure_map(modes_map, mode, f"modes.{mode}")

    v = _dig(patch, "xtquant", "mode")
    if v is not _MISSING:
        _require_str(v, "xtquant.mode")
        try:
            XTQuantMode(v)
        except ValueError as exc:
            raise ConfigStoreError(
                f"xtquant.mode 非法: {v!r}，可选 mock/dev/prod", field="xtquant.mode"
            ) from exc
        mode_map["xtquant_mode"] = v
        _mark("xtquant.mode")

    v = _dig(patch, "xtquant", "trading", "allow_real_trading")
    if v is not _MISSING:
        _require_bool(v, "xtquant.trading.allow_real_trading")
        mode_map["allow_real_trading"] = v
        _mark("xtquant.trading.allow_real_trading")

    v = _dig(patch, "security", "api_keys")
    if v is not _MISSING:
        # 脱敏保留：空列表或全为掩码 → 用户没改，保留原值（不写、不计重启）。
        if _is_masked_secret_list(v):
            logger.info(
                "security.api_keys 收到脱敏占位，保留原值不变 "
                f"(count={len(existing_api_keys)})"
            )
        else:
            if not isinstance(v, list) or not all(isinstance(k, str) for k in v):
                raise ConfigStoreError(
                    "security.api_keys 必须是字符串数组", field="security.api_keys"
                )
            mode_map["api_keys"] = list(v)
            _mark("security.api_keys")

    v = _dig(patch, "app", "host")
    if v is not _MISSING:
        _require_str(v, "app.host")
        mode_map["host"] = v
        _mark("app.host")

    v = _dig(patch, "app", "port")
    if v is not _MISSING:
        _require_port(v, "app.port")
        mode_map["port"] = v
        _mark("app.port")

    v = _dig(patch, "logging", "level")
    if v is not _MISSING:
        _require_str(v, "logging.level")
        mode_map["log_level"] = v
        _mark("logging.level")

    # ---- 与模式无关字段 → 顶层 ------------------------------------------ #
    v = _dig(patch, "xtquant", "data", "qmt_userdata_path")
    if v is not _MISSING:
        v = _require_str_or_none(v, "xtquant.data.qmt_userdata_path")
        # 空串 = 用户清空该字段 = 未设；归一为 null，避免写入 "" 后被当成一个
        # 空路径（与 None/未设的回退语义不同，会让 xtdata.data_dir 指向空串）。
        if isinstance(v, str) and not v.strip():
            v = None
        xt_map = _ensure_map(doc, "xtquant", "xtquant")
        xt_map["qmt_userdata_path"] = v
        _mark("xtquant.data.qmt_userdata_path")

    v = _dig(patch, "xtquant", "clients")
    if v is not _MISSING:
        clients = _validate_clients(v)
        xt_map = _ensure_map(doc, "xtquant", "xtquant")
        xt_map["clients"] = clients
        _mark("xtquant.clients")

    v = _dig(patch, "xtquant", "default_client_id")
    if v is not _MISSING:
        _require_str_or_none(v, "xtquant.default_client_id")
        xt_map = _ensure_map(doc, "xtquant", "xtquant")
        xt_map["default_client_id"] = v
        _mark("xtquant.default_client_id")

    v = _dig(patch, "xtquant", "data_source_client_id")
    if v is not _MISSING:
        _require_str_or_none(v, "xtquant.data_source_client_id")
        xt_map = _ensure_map(doc, "xtquant", "xtquant")
        xt_map["data_source_client_id"] = v
        _mark("xtquant.data_source_client_id")

    v = _dig(patch, "grpc", "enabled")
    if v is not _MISSING:
        _require_bool(v, "grpc.enabled")
        grpc_map = _ensure_map(doc, "grpc", "grpc")
        grpc_map["enabled"] = v
        _mark("grpc.enabled")

    v = _dig(patch, "grpc", "host")
    if v is not _MISSING:
        _require_str(v, "grpc.host")
        grpc_map = _ensure_map(doc, "grpc", "grpc")
        grpc_map["host"] = v
        _mark("grpc.host")

    v = _dig(patch, "grpc", "port")
    if v is not _MISSING:
        _require_port(v, "grpc.port")
        grpc_map = _ensure_map(doc, "grpc", "grpc")
        grpc_map["port"] = v
        _mark("grpc.port")

    # secret_key / api_key_header（顶层 security.*）；secret_key 走脱敏保留。
    v = _dig(patch, "security", "secret_key")
    if v is not _MISSING:
        if v == MASK or v is None:
            logger.info("security.secret_key 收到脱敏占位/空，保留原值不变")
        else:
            _require_str(v, "security.secret_key")
            sec_map = _ensure_map(doc, "security", "security")
            sec_map["secret_key"] = v
            _mark("security.secret_key")

    v = _dig(patch, "security", "api_key_header")
    if v is not _MISSING:
        _require_str(v, "security.api_key_header")
        sec_map = _ensure_map(doc, "security", "security")
        sec_map["api_key_header"] = v
        _mark("security.api_key_header")

    # ---- 写盘 + reset ---------------------------------------------------- #
    _dump_document(path, doc)

    restart_fields = [f for f in changed_fields if f in _RESTART_FIELD_SET]
    restart_required = len(restart_fields) > 0

    # 错误可见性配套：每次写盘记录改了哪些字段 + restart。
    logger.info(
        "qmt-proxy 配置已写回 "
        f"path={path} app_mode={mode} changed={changed_fields} "
        f"restart_required={restart_required} restart_fields={restart_fields}"
    )

    # 让下次 get_settings() 重新加载新配置。
    reset_settings()

    return {
        "status": "updated",
        "restart_required": restart_required,
        "restart_fields": restart_fields,
        "path": path,
    }
