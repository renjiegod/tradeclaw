"""诊断路由

暴露 xtdata 子进程调用的内存环形缓冲记录与聚合统计，便于在出现慢调用 /
失败时直接通过接口排查，而无需复现。
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status

from app.dependencies import verify_api_key
from app.utils import diagnostics
from app.utils.helpers import format_response
from app.utils.logger import logger

router = APIRouter(prefix="/api/v1/diagnostics", tags=["诊断"])


@router.get("/xtdata-ops")
async def get_xtdata_ops(
    limit: int = 50,
    only_errors: bool = False,
    min_duration_ms: Optional[float] = None,
    operation: Optional[str] = None,
    client_id: Optional[str] = None,
    api_key: str = Depends(verify_api_key),
):
    """获取最近的 xtdata 子进程调用记录（最新在前），可按 client_id 过滤终端"""
    try:
        records = diagnostics.recent(
            limit=limit,
            only_errors=only_errors,
            min_duration_ms=min_duration_ms,
            operation=operation,
            client_id=client_id,
        )
        return format_response(
            data={"records": records, "count": len(records)},
            message="获取 xtdata 调用记录成功",
        )
    except Exception as e:
        logger.error(f"获取 xtdata 调用记录失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"message": f"获取 xtdata 调用记录失败: {str(e)}"},
        )


@router.get("/summary")
async def get_summary(
    client_id: Optional[str] = None,
    api_key: str = Depends(verify_api_key),
):
    """获取 xtdata 子进程调用的聚合统计（不传 client_id 时附带分终端拆分）"""
    try:
        return format_response(
            data=diagnostics.summary(client_id=client_id),
            message="获取 xtdata 调用统计成功",
        )
    except Exception as e:
        logger.error(f"获取 xtdata 调用统计失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"message": f"获取 xtdata 调用统计失败: {str(e)}"},
        )
