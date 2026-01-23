import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from ..context import (
    get_cache_manager,
    get_config,
    get_logger,
    get_static_dir,
    get_templates,
)
from ...utils.llm import EnhancedLLMProcessor
from ...utils.rendering import (
    get_base_url,
    render_calibrated_content_smart,
    render_markdown_to_html,
    render_transcript_content,
)
from ...utils.timeutil import format_datetime_for_display

logger = get_logger()
cache_manager = get_cache_manager()
templates = get_templates()
static_dir = get_static_dir()


router = APIRouter()


def sanitize_filename(filename: str) -> str:
    """
    清理文件名中的非法字符

    Args:
        filename: 原始文件名

    Returns:
        str: 清理后的安全文件名
    """
    # 移除或替换 Windows 和 Linux 中的非法字符
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        filename = filename.replace(char, "_")

    # 移除控制字符
    filename = "".join(char for char in filename if ord(char) >= 32)

    # 移除首尾空格和点
    filename = filename.strip(". ")

    # 如果文件名为空，返回默认值
    if not filename:
        filename = "未命名"

    return filename


def generate_download_filename(title: str, platform: str, content_type: str) -> str:
    """
    生成下载文件名：视频标题-校对文本-平台.txt

    Args:
        title: 视频标题
        platform: 平台名称（youtube/bilibili/douyin等）
        content_type: 内容类型（calibrated/summary/transcript）

    Returns:
        str: 格式化的文件名
    """
    # 清理标题中的非法字符
    safe_title = sanitize_filename(title)

    # 内容类型映射
    type_map = {
        "calibrated": "校对文本",
        "summary": "总结文本",
        "transcript": "原始转录",
    }

    # 平台名称映射
    platform_map = {
        "youtube": "YouTube",
        "bilibili": "哔哩哔哩",
        "douyin": "抖音",
        "xiaohongshu": "小红书",
        "xiaoyuzhou": "小宇宙",
        "generic": "自定义",
    }

    content_name = type_map.get(content_type, content_type)
    platform_name = platform_map.get(platform, platform)

    # 限制标题长度，避免文件名过长
    max_title_length = 50
    if len(safe_title) > max_title_length:
        safe_title = safe_title[:max_title_length] + "..."

    return f"{safe_title}-{content_name}-{platform_name}.txt"


def handle_raw_export(view_data: Dict[str, Any], export_type: str) -> Response:
    """
    处理 Raw 模式导出请求（GitHub Raw 模式）

    Args:
        view_data: 页面数据
        export_type: 导出类型（calibrated/summary/transcript）

    Returns:
        Response: 纯文本响应
    """
    # 1. 检查任务状态
    status = view_data.get("status")

    if status in ["queued", "processing"]:
        return Response(
            content="⏳ 校对文本正在生成中，请稍后再试...\n\n请刷新页面或稍后访问此链接。",
            media_type="text/plain; charset=utf-8",
            status_code=202,
        )

    if status == "file_cleaned":
        return Response(
            content="❌ 该文件已被清理\n\n如需重新获取，请重新提交转录任务。",
            media_type="text/plain; charset=utf-8",
            status_code=410,
        )

    if status == "failed":
        return Response(
            content="❌ 任务处理失败\n\n请重新提交转录任务。",
            media_type="text/plain; charset=utf-8",
            status_code=500,
        )

    if status != "success":
        return Response(
            content=f"❌ 任务状态异常: {status}",
            media_type="text/plain; charset=utf-8",
            status_code=400,
        )

    # 2. 获取缓存目录
    cache_dir = view_data.get("cache_dir")
    if not cache_dir or not os.path.exists(cache_dir):
        return Response(
            content="❌ 缓存文件不存在\n\n该文件可能已被清理。",
            media_type="text/plain; charset=utf-8",
            status_code=404,
        )

    # 3. 根据导出类型确定文件路径
    file_path = None

    if export_type == "calibrated":
        file_path = Path(cache_dir) / "llm_calibrated.txt"
    elif export_type == "summary":
        file_path = Path(cache_dir) / "llm_summary.txt"
    elif export_type == "transcript":
        # 优先返回 FunASR JSON，降级到 CapsWriter TXT
        funasr_file = Path(cache_dir) / "transcript_funasr.json"
        capswriter_file = Path(cache_dir) / "transcript_capswriter.txt"
        file_path = funasr_file if funasr_file.exists() else capswriter_file
    else:
        return Response(
            content=f"❌ 不支持的导出类型: {export_type}\n\n支持的类型: calibrated, summary, transcript",
            media_type="text/plain; charset=utf-8",
            status_code=400,
        )

    # 4. 检查文件是否存在
    if not file_path or not file_path.exists():
        content_type_cn = {
            "calibrated": "校对文本",
            "summary": "总结文本",
            "transcript": "原始转录",
        }.get(export_type, export_type)

        return Response(
            content=f"❌ {content_type_cn}文件不存在\n\n该任务可能未启用相关功能。",
            media_type="text/plain; charset=utf-8",
            status_code=404,
        )

    # 5. 读取文件内容
    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.error("读取文件失败: %s, 错误: %s", file_path, exc)
        return Response(
            content=f"❌ 读取文件失败: {exc}",
            media_type="text/plain; charset=utf-8",
            status_code=500,
        )

    # 6. 返回纯文本响应（Raw 模式不需要文件名头）
    logger.info(
        "Raw 模式导出: %s, view_token: %s",
        export_type,
        view_data.get("view_token", "unknown")[:20],
    )

    return Response(content=content, media_type="text/plain; charset=utf-8")


@router.get("/add_task_by_web", response_class=HTMLResponse)
async def add_task_by_web(request: Request):
    """Web任务添加页面"""
    try:
        index_file = static_dir / "index.html"
        if index_file.exists():
            content = index_file.read_text(encoding="utf-8")
            return HTMLResponse(content=content)
        else:
            logger.error("Web任务添加页面文件不存在: %s", index_file)
            return HTMLResponse(
                content="<h1>页面未找到</h1><p>请确保 index.html 文件存在于 static 目录中。</p>",
                status_code=404,
            )
    except Exception as exc:
        logger.exception("访问Web任务添加页面异常: %s", exc)
        raise HTTPException(status_code=500, detail=f"访问页面失败: {exc}")


@router.get("/export/{view_token}/{export_type}")
async def export_content(view_token: str, export_type: str, request: Request):
    """
    导出文件内容

    Args:
        view_token: 查看token
        export_type: 导出类型 (calibrated/summary/transcript)

    Returns:
        FileResponse: 文件响应
    """
    try:
        # 获取查看页面数据
        view_data = cache_manager.get_view_data_by_token(view_token)
        if not view_data:
            return Response(
                content="❌ 页面不存在\n\nview_token 无效或已过期。",
                media_type="text/plain; charset=utf-8",
                status_code=404,
            )

        cache_dir = view_data.get("cache_dir")
        if not cache_dir or not os.path.exists(cache_dir):
            return Response(
                content="❌ 缓存文件不存在\n\n该文件可能已被清理。",
                media_type="text/plain; charset=utf-8",
                status_code=404,
            )

        if export_type == "calibrated":
            file_path = Path(cache_dir) / "llm_calibrated.txt"
        elif export_type == "summary":
            file_path = Path(cache_dir) / "llm_summary.txt"
        elif export_type == "transcript":
            funasr_file = Path(cache_dir) / "transcript_funasr.json"
            capswriter_file = Path(cache_dir) / "transcript_capswriter.txt"
            file_path = funasr_file if funasr_file.exists() else capswriter_file
        else:
            return Response(
                content=f"❌ 不支持的导出类型: {export_type}\n\n支持的类型: calibrated, summary, transcript",
                media_type="text/plain; charset=utf-8",
                status_code=400,
            )

        if not file_path or not file_path.exists():
            content_type_cn = {
                "calibrated": "校对文本",
                "summary": "总结文本",
                "transcript": "原始转录",
            }.get(export_type, export_type)
            return Response(
                content=f"❌ {content_type_cn}文件不存在\n\n该任务可能未启用相关功能。",
                media_type="text/plain; charset=utf-8",
                status_code=404,
            )

        try:
            content = file_path.read_text(encoding="utf-8")
        except Exception as exc:
            logger.error("读取文件失败: %s, 错误: %s", file_path, exc)
            return Response(
                content=f"❌ 读取文件失败: {exc}",
                media_type="text/plain; charset=utf-8",
                status_code=500,
            )

        title = view_data.get("title", "未命名")
        platform = view_data.get("platform", "unknown")
        filename = generate_download_filename(title, platform, export_type)
        from urllib.parse import quote

        encoded_filename = quote(filename)
        headers = {
            "Content-Type": "text/plain; charset=utf-8",
            "Content-Disposition": f"inline; filename*=UTF-8''{encoded_filename}",
            "X-Content-Type-Options": "nosniff",
        }

        logger.info(
            "导出文件: %s, 文件名: %s, view_token: %s",
            export_type,
            filename,
            view_data.get("view_token", "unknown")[:20],
        )

        return Response(
            content=content, media_type="text/plain; charset=utf-8", headers=headers
        )

    except Exception as exc:
        logger.exception("导出文件异常: %s", exc)
        return Response(
            content=f"❌ 导出失败: {exc}",
            media_type="text/plain; charset=utf-8",
            status_code=500,
        )


@router.get("/view/{view_token}", response_class=HTMLResponse)
async def view_transcript(view_token: str, request: Request, raw: Optional[str] = None):
    try:
        view_data = cache_manager.get_view_data_by_token(view_token)
        if not view_data:
            if raw:
                return Response(
                    content="❌ 页面不存在\n\nview_token 无效或已过期。",
                    media_type="text/plain; charset=utf-8",
                    status_code=404,
                )
            else:
                return templates.TemplateResponse(
                    "error.html",
                    {
                        "request": request,
                        "message": "view_token 无效或已过期",
                    },
                )

        # 如果请求导出原始文件（GitHub Raw 模式）
        if raw:
            return handle_raw_export(view_data, raw)

        if view_data.get("created_at"):
            view_data["created_at_display"] = format_datetime_for_display(
                view_data["created_at"]
            )

        if view_data["status"] == "processing":
            return templates.TemplateResponse(
                "processing.html",
                {
                    "request": request,
                    **view_data,
                    "page_title": f"正在处理 - {view_data.get('title', '转录任务')}",
                },
            )
        if view_data["status"] == "failed":
            return templates.TemplateResponse(
                "error.html",
                {
                    "request": request,
                    "message": view_data.get("error_message", "任务处理失败"),
                    **view_data,
                },
            )
        if view_data["status"] == "file_cleaned":
            return templates.TemplateResponse(
                "cleaned.html",
                {"request": request, **view_data},
            )
        if view_data["status"] == "success":
            if view_data.get("summary"):
                view_data["summary_html"] = render_markdown_to_html(
                    view_data["summary"]
                )

            cache_dir = view_data.get("cache_dir")

            # 计算字数统计
            stats = {"original_length": 0, "calibrated_length": 0, "summary_length": 0}

            if cache_dir and os.path.exists(cache_dir):
                cache_dir_path = Path(cache_dir)

                # 1. 计算原始转录字数
                funasr_file = cache_dir_path / "transcript_funasr.json"
                capswriter_file = cache_dir_path / "transcript_capswriter.txt"

                if funasr_file.exists():
                    # FunASR JSON 格式：提取 text 字段
                    try:
                        import json

                        with open(funasr_file, "r", encoding="utf-8") as f:
                            funasr_data = json.load(f)
                        # 复用现有的格式化方法
                        from ...transcriber import FunASRSpeakerClient

                        funasr_client = FunASRSpeakerClient()
                        transcript_text = funasr_client.format_transcript_with_speakers(
                            funasr_data
                        )
                        stats["original_length"] = len(transcript_text)
                        logger.debug(
                            f"原始转录字数(FunASR): {stats['original_length']}"
                        )
                    except Exception as exc:
                        logger.error(f"计算FunASR转录字数失败: {exc}")
                elif capswriter_file.exists():
                    # CapsWriter 纯文本格式
                    try:
                        with open(capswriter_file, "r", encoding="utf-8") as f:
                            stats["original_length"] = len(f.read())
                        logger.debug(
                            f"原始转录字数(CapsWriter): {stats['original_length']}"
                        )
                    except Exception as exc:
                        logger.error(f"计算CapsWriter转录字数失败: {exc}")

                # 2. 计算校对文本字数
                calibrated_file = cache_dir_path / "llm_calibrated.txt"
                if calibrated_file.exists():
                    try:
                        with open(calibrated_file, "r", encoding="utf-8") as f:
                            stats["calibrated_length"] = len(f.read())
                        logger.debug(f"校对文本字数: {stats['calibrated_length']}")
                    except Exception as exc:
                        logger.error(f"计算校对文本字数失败: {exc}")

                # 3. 计算总结文本字数
                summary_file = cache_dir_path / "llm_summary.txt"
                if summary_file.exists():
                    try:
                        with open(summary_file, "r", encoding="utf-8") as f:
                            stats["summary_length"] = len(f.read())
                        logger.debug(f"总结文本字数: {stats['summary_length']}")
                    except Exception as exc:
                        logger.error(f"计算总结文本字数失败: {exc}")

            fallback_text = view_data.get("transcript", "")
            transcript_path = Path(cache_dir) / "llm_calibrated.txt"
            if transcript_path.exists():
                fallback_text = transcript_path.read_text(encoding="utf-8")

            # 简化渲染逻辑：直接调用 render_with_cache_analysis
            view_data["calibrated_html"] = render_calibrated_content_smart(cache_dir)

        return templates.TemplateResponse(
            "transcript.html",
            {"request": request, **view_data, "stats": stats},
        )

    except Exception as exc:
        logger.exception("查看转录页面异常: %s", exc)
        return templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "message": f"查看页面失败: {exc}",
            },
        )
