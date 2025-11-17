from fastapi import APIRouter, Depends, HTTPException

from ..context import get_logger, get_user_manager
from ..services.transcription import TranscribeResponse, verify_token

logger = get_logger()
user_manager = get_user_manager()

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("/profile")
async def get_user_profile(user_info: dict = Depends(verify_token)):
    try:
        safe_user_info = user_info.copy()
        if "api_key" in safe_user_info:
            safe_user_info["api_key"] = user_manager._mask_api_key(safe_user_info["api_key"])

        return TranscribeResponse(
            code=200,
            message="获取用户配置成功",
            data={
                "user_info": safe_user_info,
                "is_multi_user_mode": user_manager.is_multi_user_mode(),
            },
        )
    except Exception as exc:
        logger.exception("获取用户配置异常: %s", exc)
        raise HTTPException(status_code=500, detail=f"获取用户配置失败: {exc}")
