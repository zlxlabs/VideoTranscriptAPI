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
from ...utils.cache import should_upgrade_cache, analyze_cache_capabilities, CacheCapabilities
from ...utils.llm import EnhancedLLMProcessor
from ...utils.rendering import (
    get_base_url,
    render_calibrated_content_smart,
    render_markdown_to_html,
    render_transcript_content,
    render_transcript_content_smart,
)
from ...utils.timeutil import format_datetime_for_display

logger = get_logger()
cache_manager = get_cache_manager()
templates = get_templates()
static_dir = get_static_dir()

router = APIRouter()


@router.get("/add_task_by_web", response_class=HTMLResponse)
async def add_task_by_web():
    try:
        index_file = static_dir / "index.html"
        if index_file.exists():
            return FileResponse(index_file)
        logger.error("Web任务添加页面文件不存在: %s", index_file)
        raise HTTPException(status_code=404, detail="Web任务添加页面文件未找到")
    except Exception as exc:
        logger.exception("访问Web任务添加页面异常: %s", exc)
        raise HTTPException(status_code=500, detail=f"访问Web任务添加页面失败: {exc}")


def sanitize_filename(filename: str) -> str:
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        filename = filename.replace(char, "_")
    filename = "".join(char for char in filename if ord(char) >= 32)
    filename = filename.strip(". ")
    return filename or "未命名"


def generate_download_filename(title: str, platform: str, content_type: str) -> str:
    safe_title = sanitize_filename(title)
    type_map = {"calibrated": "校对文本", "summary": "总结文本", "transcript": "原始转录"}
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
    max_title_length = 50
    if len(safe_title) > max_title_length:
        safe_title = safe_title[:max_title_length] + "..."
    return f"{safe_title}-{content_name}-{platform_name}.txt"


def handle_file_export(view_data: Dict[str, Any], export_type: str) -> Response:
    if view_data["status"] in ["queued", "processing"]:
        return Response(
            content="⏳ 校对文本正在生成中，请稍后再试...\n\n请刷新页面或稍后访问此链接。",
            media_type="text/plain; charset=utf-8",
            status_code=202,
        )

    if view_data["status"] == "file_cleaned":
        return Response(
            content="❌ 该文件已被清理\n\n如需重新获取，请重新提交转录任务。",
            media_type="text/plain; charset=utf-8",
            status_code=410,
        )

    if view_data["status"] == "failed":
        return Response(
            content="❌ 任务处理失败\n\n请重新提交转录任务。",
            media_type="text/plain; charset=utf-8",
            status_code=500,
        )

    if view_data["status"] != "success":
        return Response(
            content=f"❌ 任务状态异常: {view_data['status']}",
            media_type="text/plain; charset=utf-8",
            status_code=400,
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

    return Response(content=content, media_type="text/plain; charset=utf-8", headers=headers)


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
            return templates.TemplateResponse(
                "error.html",
                {"request": request, "message": "页面不存在", "title": "404 - 页面未找到"},
            )

        if raw:
            return handle_file_export(view_data, raw)

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
                view_data["summary_html"] = render_markdown_to_html(view_data["summary"])

            cache_dir = view_data.get("cache_dir")

            # 计算字数统计
            stats = {
                'original_length': 0,
                'calibrated_length': 0,
                'summary_length': 0
            }

            if cache_dir and os.path.exists(cache_dir):
                # 统一进行一次缓存能力分析，避免重复
                logger.debug("开始统一缓存能力分析: %s", cache_dir)
                cache_capabilities = analyze_cache_capabilities(cache_dir)
                logger.debug("缓存能力分析完成，复用于渲染和升级检查")

                cache_dir_path = Path(cache_dir)

                # 1. 计算原始转录字数
                funasr_file = cache_dir_path / "transcript_funasr.json"
                capswriter_file = cache_dir_path / "transcript_capswriter.txt"

                if funasr_file.exists():
                    # FunASR JSON 格式：提取 text 字段
                    try:
                        import json
                        with open(funasr_file, 'r', encoding='utf-8') as f:
                            funasr_data = json.load(f)
                        # 复用现有的格式化方法
                        from ...transcriber import FunASRSpeakerClient
                        funasr_client = FunASRSpeakerClient()
                        transcript_text = funasr_client.format_transcript_with_speakers(funasr_data)
                        stats['original_length'] = len(transcript_text)
                        logger.debug(f"原始转录字数(FunASR): {stats['original_length']}")
                    except Exception as exc:
                        logger.error(f"计算FunASR转录字数失败: {exc}")
                elif capswriter_file.exists():
                    # CapsWriter 纯文本格式
                    try:
                        with open(capswriter_file, 'r', encoding='utf-8') as f:
                            stats['original_length'] = len(f.read())
                        logger.debug(f"原始转录字数(CapsWriter): {stats['original_length']}")
                    except Exception as exc:
                        logger.error(f"计算CapsWriter转录字数失败: {exc}")

                # 2. 计算校对文本字数
                calibrated_file = cache_dir_path / "llm_calibrated.txt"
                if calibrated_file.exists():
                    try:
                        with open(calibrated_file, 'r', encoding='utf-8') as f:
                            stats['calibrated_length'] = len(f.read())
                        logger.debug(f"校对文本字数: {stats['calibrated_length']}")
                    except Exception as exc:
                        logger.error(f"计算校对文本字数失败: {exc}")

                # 3. 计算总结文本字数
                summary_file = cache_dir_path / "llm_summary.txt"
                if summary_file.exists():
                    try:
                        with open(summary_file, 'r', encoding='utf-8') as f:
                            stats['summary_length'] = len(f.read())
                        logger.debug(f"总结文本字数: {stats['summary_length']}")
                    except Exception as exc:
                        logger.error(f"计算总结文本字数失败: {exc}")

                fallback_text = view_data.get("transcript", "")
                transcript_path = Path(cache_dir) / "llm_calibrated.txt"
                if transcript_path.exists():
                    fallback_text = transcript_path.read_text(encoding="utf-8")

                try:
                    view_data["transcript_html"] = render_transcript_content_smart(
                        cache_dir, fallback_text
                    )
                except Exception as exc:
                    logger.exception("智能渲染转录内容失败: %s", exc)
                    view_data["transcript_html"] = render_transcript_content(fallback_text)

                if Path(cache_dir, "llm_calibrated.txt").exists():
                    # 传递缓存能力信息，避免重复分析
                    view_data["calibrated_html"] = render_calibrated_content_smart(
                        cache_dir, capabilities=cache_capabilities
                    )

                # 传递缓存能力信息，避免重复分析
                _trigger_cache_upgrade_if_needed(cache_dir, view_data, capabilities=cache_capabilities)
            else:
                fallback_text = view_data.get("transcript", "")
                view_data["transcript_html"] = render_transcript_content(fallback_text)

            base_url = get_base_url()
            view_data["download_links"] = {
                "calibrated": f"{base_url}/view/{view_token}?raw=calibrated",
                "summary": f"{base_url}/view/{view_token}?raw=summary",
                "transcript": f"{base_url}/view/{view_token}?raw=transcript",
            }

            # 传递字数统计数据给模板
            view_data["stats"] = stats

            return templates.TemplateResponse(
                "transcript.html",
                {"request": request, **view_data},
            )

        return templates.TemplateResponse(
            "error.html",
            {"request": request, "message": "未知状态", **view_data},
        )
    except Exception as exc:
        logger.exception("查看页面异常: %s, 错误: %s", view_token, exc)
        return templates.TemplateResponse(
            "error.html",
            {"request": request, "message": "服务异常", "title": "服务异常"},
        )


def _trigger_cache_upgrade_if_needed(cache_dir: str, view_data: dict, capabilities: Optional[CacheCapabilities] = None):
    """
    检查并触发缓存升级（如果需要）

    Args:
        cache_dir: 缓存目录路径
        view_data: 视图数据
        capabilities: 可选的缓存能力信息，如果提供则直接使用，避免重复分析
    """
    try:
        logger.info("进入缓存升级检查: %s", cache_dir)

        # 如果未提供缓存能力信息，则进行分析（保持向后兼容）
        if capabilities is None:
            logger.debug("未提供缓存能力信息，开始分析")
            should_upgrade = should_upgrade_cache(cache_dir)
        else:
            logger.debug("复用已有的缓存能力分析结果")
            # 使用分析器判断是否应该升级
            from ...utils.cache.cache_analyzer import CacheCapabilityAnalyzer
            analyzer = CacheCapabilityAnalyzer()
            should_upgrade = analyzer.should_upgrade_cache(capabilities)

        logger.info("缓存升级检查结果: %s -> %s", cache_dir, should_upgrade)

        if not should_upgrade:
            logger.info("缓存无需升级: %s", cache_dir)
            return

        logger.info("检测到高价值缓存，触发后台升级: %s", cache_dir)

        def background_upgrade():
            try:
                funasr_file = os.path.join(cache_dir, "transcript_funasr.json")
                if not os.path.exists(funasr_file):
                    return

                import json

                with open(funasr_file, "r", encoding="utf-8") as file:
                    funasr_data = json.load(file)

                video_metadata = {
                    "video_title": view_data.get("title", "未知标题"),
                    "author": view_data.get("author", "未知作者"),
                    "description": view_data.get("description", ""),
                }

                config = get_config()
                llm_processor = EnhancedLLMProcessor(config)
                if llm_processor.should_use_structured_processing(cache_dir):
                    logger.info("开始结构化升级: %s", cache_dir)
                    llm_processor.process_llm_task_with_structure(
                        cache_dir, funasr_data, video_metadata
                    )
                    logger.info("缓存升级完成: %s", cache_dir)
                else:
                    logger.debug("缓存无需升级: %s", cache_dir)
            except Exception as exc:
                logger.error("后台缓存升级失败: %s, %s", cache_dir, exc)

        threading.Thread(target=background_upgrade, daemon=True).start()
    except Exception as exc:
        logger.error("触发缓存升级失败: %s", exc)
