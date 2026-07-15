"""配置管理路由（契约 B：/api/v1/config）。

- ``GET /api/v1/config``：返回脱敏后的当前有效配置 + resolved_clients + 重启字段清单。
- ``PUT /api/v1/config``：部分 patch，mode-aware 写回 ``~/.doyoutrade/qmt-proxy.yml``
  （ruamel 保注释）后 ``reset_settings()``；返回 restart 信息。

统一走 ``format_response(data=...)``；鉴权 ``Depends(verify_api_key)``。校验失败返回
HTTP 400 + 结构化 ``error_code`` / ``error_type`` / ``field``（不静默吞坏值）。
"""
from typing import Any, Dict

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse

from app.config_store import ConfigStoreError, read_config_masked, write_config
from app.dependencies import verify_api_key
from app.utils.helpers import format_response
from app.utils.logger import logger

router = APIRouter(prefix="/api/v1/config", tags=["配置"])


def _error_response(exc: ConfigStoreError) -> JSONResponse:
    """把 ConfigStoreError 映射为 400 + 结构化错误信封（复用 format_response）。"""
    content = format_response(data=None, message=exc.message, success=False, code=400)
    content["error_code"] = exc.error_code
    content["error_type"] = exc.error_type
    if exc.field is not None:
        content["field"] = exc.field
    return JSONResponse(status_code=400, content=content)


@router.get("")
async def get_config(api_key: str = Depends(verify_api_key)):
    """获取当前有效配置（脱敏）。"""
    try:
        data = read_config_masked()
    except ConfigStoreError as exc:
        logger.warning(
            f"读取 qmt-proxy 配置失败: {exc.message} | "
            f"error_code={exc.error_code} field={exc.field}"
        )
        return _error_response(exc)
    return format_response(data=data, message="获取配置成功")


@router.put("")
async def update_config(
    payload: Dict[str, Any] = Body(default_factory=dict),
    api_key: str = Depends(verify_api_key),
):
    """部分更新配置并写回（写后需重启 proxy 生效）。"""
    try:
        data = write_config(payload)
    except ConfigStoreError as exc:
        logger.warning(
            f"写入 qmt-proxy 配置失败: {exc.message} | "
            f"error_code={exc.error_code} field={exc.field}"
        )
        return _error_response(exc)
    return format_response(data=data, message="更新配置成功")
