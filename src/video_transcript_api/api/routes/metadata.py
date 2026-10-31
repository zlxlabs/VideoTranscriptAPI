"""POST /api/edit_metadata: post-hoc title/author editing (vta-261031).

Letting an operator fix a typo in an already-shared transcript page used to
require hand-editing SQLite; this route gives accounts holding the
edit_metadata permission a supported channel. Scope is deliberately narrow:

- only title/author; url/download_url/platform/media_id are not writable
  (the request model forbids extra fields, so a "url" key is a 422);
- three independent gates must all pass before any write: the edit_metadata
  permission, check_view_token_ownership (same judgment as recalibrate),
  and the exclusive-ownership/in-flight media constraints in
  services/metadata_editor.py;
- local-upload share tokens (upload_*) are rejected outright: their display
  metadata lives in the local_uploads table, which this route does not
  touch.

Deployment order (see docs/sessions/vta-metadata-edit-261031): deploy the
code first, then add "edit_metadata" to the target keys in config/users.json.
Before rolling the code back, remove the permission from users.json again --
_validate_users rejects the whole file on unknown permission names, so new
config + old code breaks every user's auth.
"""

import asyncio

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from ..context import (
    ApiError,
    get_audit_logger,
    get_cache_manager,
    get_logger,
    get_user_manager,
    lazy_resource,
)
from ..services.metadata_editor import (
    apply_metadata_update,
    check_media_constraints,
)
from ..services.transcription import verify_token
from ..services.view_token_resolver import ViewTokenResolver
from .audit import check_view_token_ownership

logger = lazy_resource(get_logger)
audit_logger = lazy_resource(get_audit_logger)
cache_manager = lazy_resource(get_cache_manager)
user_manager = lazy_resource(get_user_manager)

router = APIRouter(prefix="/api", tags=["metadata"])


class EditMetadataRequest(BaseModel):
    """title/author-only request body; extra keys (url, ...) are rejected."""

    model_config = ConfigDict(extra="forbid")

    view_token: str = Field(..., description="查看页面的 view_token")
    title: str | None = Field(None, max_length=200, description="新标题")
    author: str | None = Field(None, max_length=200, description="新作者")


def _clean_optional(value: str | None) -> str | None:
    """strip() a provided field; blank-after-strip means invalid, not absent.

    None (key omitted, or explicitly null) means "leave this field alone";
    a provided blank string is a client error and fails the whole request
    instead of being silently dropped (first review, F1).
    """
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        raise ApiError(400, "metadata_invalid")
    return stripped


@router.post("/edit_metadata")
async def edit_metadata(
    request_body: EditMetadataRequest,
    request: Request,
    user_info: dict = Depends(verify_token),
):
    """事后编辑分享页 title/author（需 edit_metadata 权限）。

    公开页与导出读的 video_cache（含同媒体全部 speaker 变体行）与
    task_status 展示副本在同一事务内更新；OG 分享卡 PNG 随写删除强制重绘。
    """
    view_token = request_body.view_token
    user_id = user_info.get("user_id")
    api_key = user_info.get("api_key")

    audit_logger.log_api_call(
        api_key=api_key,
        user_id=user_id,
        endpoint="/api/edit_metadata",
        user_agent=request.headers.get("User-Agent"),
        remote_ip=request.client.host if request.client else None,
    )

    # 权限检查：user_manager 与 verify_token 是同一个 runtime 优先的 DI 实例
    # （见 routes/tasks.py 顶部同名注释）。
    if not user_manager.check_permission(user_info, "edit_metadata"):
        logger.warning(f"用户 {user_id} 无 edit_metadata 权限")
        raise ApiError(403, "metadata_permission_denied")

    new_title = _clean_optional(request_body.title)
    new_author = _clean_optional(request_body.author)
    if new_title is None and new_author is None:
        raise ApiError(400, "metadata_invalid")

    # 本地上传分享（local_uploads 表）的元数据不在本卡数据模型内。
    if view_token.startswith("upload_"):
        logger.warning(f"edit_metadata 拒绝本地上传 token: {view_token}")
        raise ApiError(404, "transcript_not_found")

    cache_data = await asyncio.to_thread(
        ViewTokenResolver(cache_manager).get_cache_by_view_token, view_token
    )
    if not cache_data:
        logger.warning(f"view_token 对应的缓存不存在: {view_token}")
        raise ApiError(404, "transcript_not_found")

    task_info = cache_data.get("task_info", {})
    platform = cache_data.get("platform")
    media_id = cache_data.get("media_id")
    if not platform or not media_id or not task_info.get("task_id"):
        logger.error(
            f"edit_metadata 解析结果不完整: view_token={view_token} "
            f"platform={platform} media_id={media_id}"
        )
        raise ApiError(404, "transcript_not_found")

    # 归属校验：与 /api/recalibrate 完全同一套判定（含 legacy 审计行兜底），
    # fail-closed；to_thread 的理由同 recalibrate（同步 SQLite 查询不阻塞
    # 事件循环）。
    try:
        owned = await asyncio.to_thread(
            check_view_token_ownership,
            view_token,
            task_info["task_id"],
            user_id,
            cache_manager,
            audit_logger,
        )
    except Exception as e:
        logger.error(f"edit_metadata 归属校验暂时不可用(fail-closed 拒绝): {e}")
        raise ApiError(503, "ownership_check_unavailable")
    if not owned:
        logger.warning(
            f"用户 {user_id} 无权编辑 view_token={view_token}"
            f"（非该任务的权威提交者）"
        )
        raise ApiError(403, "metadata_ownership_denied")

    # 独占归属 + 在途检查（check_view_token_ownership 的"拥有其中一条即可"
    # 不足以保护共享媒体的整媒体写入，见 metadata_editor 模块注释）。
    await asyncio.to_thread(
        check_media_constraints,
        cache_manager,
        audit_logger,
        platform=platform,
        media_id=media_id,
        user_id=user_id,
        is_legacy=bool(user_info.get("is_legacy")),
    )

    previous = await asyncio.to_thread(
        apply_metadata_update,
        cache_manager,
        platform=platform,
        media_id=media_id,
        title=new_title,
        author=new_author,
    )

    final_title = new_title if new_title is not None else previous["old_title"]
    final_author = new_author if new_author is not None else previous["old_author"]
    logger.info(
        "metadata edited: view_token={} platform={} media_id={} "
        "title: {!r} -> {!r}; author: {!r} -> {!r}",
        view_token,
        platform,
        media_id,
        previous["old_title"],
        final_title,
        previous["old_author"],
        final_author,
    )

    return {
        "code": 200,
        "data": {
            "view_token": view_token,
            "title": final_title,
            "author": final_author,
        },
    }
