from fastapi import APIRouter, Depends, HTTPException

from ..context import get_logger, get_user_manager
from ..services.transcription import TranscribeResponse, verify_token
from ..context import get_audit_logger

logger = get_logger()
audit_logger = get_audit_logger()
user_manager = get_user_manager()

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("/stats")
async def get_audit_stats(days: int = 30, user_info: dict = Depends(verify_token)):
    try:
        user_id = user_info.get("user_id")
        user_stats = audit_logger.get_user_stats(user_id, days)
        return TranscribeResponse(
            code=200,
            message="获取统计信息成功",
            data={
                "user_stats": user_stats,
                "is_multi_user_mode": user_manager.is_multi_user_mode(),
                "total_users": user_manager.get_user_count(),
            },
        )
    except Exception as exc:
        logger.exception("获取审计统计异常: %s", exc)
        raise HTTPException(status_code=500, detail=f"获取统计信息失败: {exc}")


@router.get("/calls")
async def get_audit_calls(
    limit: int = 100,
    user_info: dict = Depends(verify_token),
):
    try:
        user_id = user_info.get("user_id")
        recent_calls = audit_logger.get_recent_calls(user_id, limit)
        return TranscribeResponse(
            code=200,
            message="获取调用记录成功",
            data={"calls": recent_calls, "user_id": user_id, "limit": limit},
        )
    except Exception as exc:
        logger.exception("获取审计调用记录异常: %s", exc)
        raise HTTPException(status_code=500, detail=f"获取调用记录失败: {exc}")
