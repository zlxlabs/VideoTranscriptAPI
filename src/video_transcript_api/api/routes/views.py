import os
import threading
from datetime import datetime, timezone
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

# robots.txt：允许首页和分享页面被收录，禁止 API 和静态资源
_ROBOTS_TXT_TEMPLATE = """\
User-agent: *
Allow: /
Disallow: /api/
Disallow: /static/
Sitemap: {base_url}/sitemap.xml
"""

# sitemap.xml：仅包含首页，引导搜索引擎只收录首页
_SITEMAP_XML_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>{base_url}/</loc>
  </url>
</urlset>
"""


@router.get("/robots.txt", include_in_schema=False)
async def robots_txt():
    """返回 robots.txt，允许首页被搜索引擎收录以建立域名信任."""
    base_url = get_base_url()
    content = _ROBOTS_TXT_TEMPLATE.format(base_url=base_url)
    return Response(content=content, media_type="text/plain")


@router.get("/sitemap.xml", include_in_schema=False)
async def sitemap_xml():
    """返回 sitemap.xml，仅包含首页以引导搜索引擎收录."""
    base_url = get_base_url()
    content = _SITEMAP_XML_TEMPLATE.format(base_url=base_url)
    return Response(content=content, media_type="application/xml")


# 首页 HTML：简洁的服务介绍页，供搜索引擎收录以建立域名信任
_HOME_HTML = """\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>VideoTranscriptAPI</title>
    <meta name="description" content="Multi-platform video transcription with AI-powered proofreading and summarization. Supports YouTube, Bilibili, Douyin, Xiaohongshu and more.">
    <meta name="theme-color" content="#667eea">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
            line-height: 1.6;
            color: #333;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            padding: 20px;
        }
        .card {
            background: #fff;
            border-radius: 16px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.15);
            padding: 48px;
            max-width: 560px;
            width: 100%;
            text-align: center;
        }
        .logo { font-size: 3rem; margin-bottom: 16px; }
        h1 { font-size: 1.8rem; margin-bottom: 8px; font-weight: 700; }
        .subtitle { color: #6b7280; margin-bottom: 32px; font-size: 0.95rem; }
        .features {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
            margin-bottom: 32px;
            text-align: left;
        }
        .feature {
            background: #f8f9fa;
            border-radius: 10px;
            padding: 14px 16px;
            font-size: 0.88rem;
            color: #374151;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .feature-icon { font-size: 1.2rem; flex-shrink: 0; }
        .cta {
            display: inline-block;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: #fff;
            text-decoration: none;
            padding: 12px 32px;
            border-radius: 8px;
            font-size: 0.95rem;
            font-weight: 500;
            transition: opacity 0.2s, transform 0.2s;
        }
        .cta:hover { opacity: 0.9; transform: translateY(-1px); }
        .footer {
            margin-top: 24px;
            font-size: 0.78rem;
            color: #9ca3af;
            opacity: 0.8;
        }
        .footer a { color: #9ca3af; text-decoration: none; }
        .footer a:hover { text-decoration: underline; }
        @media (max-width: 480px) {
            .card { padding: 32px 24px; }
            .features { grid-template-columns: 1fr; }
            h1 { font-size: 1.5rem; }
        }
    </style>
</head>
<body>
    <div class="card">
        <div class="logo">🎬</div>
        <h1>VideoTranscriptAPI</h1>
        <p class="subtitle">多平台视频转录与 AI 智能校对服务</p>
        <div class="features">
            <div class="feature"><span class="feature-icon">🌐</span>支持 YouTube、Bilibili、抖音、小红书、小宇宙播客及音视频直链</div>
            <div class="feature"><span class="feature-icon">🎙️</span>本地语音转文字 + LLM 智能校对</div>
            <div class="feature"><span class="feature-icon">📝</span>自动生成内容总结</div>
            <div class="feature"><span class="feature-icon">👀</span>网页版查看 + 企业微信推送</div>
            <div class="feature"><span class="feature-icon">📋</span>任务历史：搜索、过滤、已读追踪、摘要预览</div>
        </div>
        <div style="display: flex; gap: 12px; justify-content: center; flex-wrap: wrap;">
            <a class="cta" href="/add_task_by_web">提交任务</a>
            <a class="cta" href="/static/history.html" style="background: linear-gradient(135deg, #4f46e5 0%, #6366f1 100%);">任务历史</a>
        </div>
        <p style="margin-top: 16px; font-size: 0.85rem;">
            <a href="https://mp.weixin.qq.com/s/w8VnWJcUp5VkD5J-fYCUrg" target="_blank" rel="noopener" style="color: #667eea; text-decoration: none;">📖 开发契机和玩法分享</a>
        </p>
        <p class="footer">
            Powered by <a href="https://github.com/zj1123581321/VideoTranscriptAPI" target="_blank" rel="noopener">VideoTranscriptAPI</a>
            · Open Source ·
            <a href="https://github.com/zj1123581321/VideoTranscriptAPI" target="_blank" rel="noopener">☆ Star on GitHub</a>
        </p>
    </div>
</body>
</html>
"""


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def home():
    """首页：简洁的服务介绍，供搜索引擎收录以建立域名信任."""
    return HTMLResponse(content=_HOME_HTML)


def _build_text_metadata_header(view_data: Dict[str, Any], export_type: str) -> str:
    """生成纯文本导出的 YAML front matter 风格元数据头.

    Args:
        view_data: 页面数据字典
        export_type: 导出类型（calibrated/summary/transcript）

    Returns:
        包含元数据的字符串，以 '---' 分隔
    """
    type_map = {
        "calibrated": "校对文本",
        "summary": "总结文本",
        "transcript": "原始转录",
    }

    title = view_data.get("title", "未命名")
    platform = view_data.get("platform", "unknown")
    source_url = view_data.get("url", "")
    content_type_cn = type_map.get(export_type, export_type)
    from ...utils.timeutil.timezone_helper import get_configured_timezone
    export_date = datetime.now(get_configured_timezone()).strftime("%Y-%m-%d")

    lines = [
        "---",
        f"Title: {title}",
        f"Platform: {platform}",
        f"Type: {content_type_cn}",
    ]
    if source_url:
        lines.append(f"Source: {source_url}")
    lines.append(f"Export-Date: {export_date}")
    lines.append("---")
    lines.append("")  # 元数据与正文之间的空行

    return "\n".join(lines)


def _build_metadata_headers(view_data: Dict[str, Any], export_type: str) -> dict:
    """生成纯文本导出的 HTTP 自定义响应头.

    HTTP 响应头仅支持 Latin-1 编码，因此对包含非 ASCII 字符的值
    使用 RFC 5987 的 UTF-8'' 编码格式。

    Args:
        view_data: 页面数据字典
        export_type: 导出类型（calibrated/summary/transcript）

    Returns:
        包含自定义响应头的字典
    """
    from urllib.parse import quote

    type_map = {
        "calibrated": "calibrated",
        "summary": "summary",
        "transcript": "transcript",
    }

    title = view_data.get("title", "未命名")
    platform = view_data.get("platform", "unknown")
    source_url = view_data.get("url", "")
    content_type = type_map.get(export_type, export_type)

    def _safe_header_value(value: str) -> str:
        """将非 ASCII 值进行 URL 编码，确保 HTTP 头兼容性."""
        try:
            value.encode("latin-1")
            return value
        except UnicodeEncodeError:
            return quote(value, safe="")

    headers = {
        "X-Document-Title": _safe_header_value(title),
        "X-Platform": _safe_header_value(platform),
        "X-Content-Type": content_type,
    }
    if source_url:
        headers["X-Source-URL"] = _safe_header_value(source_url)

    return headers


def _build_page_html(
    view_data: Dict[str, Any], export_type: str, body_html: str
) -> str:
    """生成用于 ?page= 导出的极简 HTML 页面.

    页面包含完整的 meta 标签（Open Graph 等），适合爬虫抓取，
    同时提供干净的阅读体验。

    Args:
        view_data: 页面数据字典
        export_type: 导出类型（calibrated/summary/transcript）
        body_html: 已渲染的 HTML 正文内容

    Returns:
        完整的 HTML 字符串
    """
    import html as html_module

    type_map = {
        "calibrated": "校对文本",
        "summary": "内容总结",
        "transcript": "原始转录",
    }

    title = view_data.get("title", "未命名")
    platform = view_data.get("platform", "unknown")
    content_type_cn = type_map.get(export_type, export_type)
    source_url = view_data.get("url", "")

    # HTML 转义防止 XSS
    safe_title = html_module.escape(title)
    safe_platform = html_module.escape(platform)
    safe_content_type = html_module.escape(content_type_cn)
    safe_source_url = html_module.escape(source_url)

    page_title = f"{safe_title} - {safe_content_type}"
    og_desc = f"{safe_title} 的{safe_content_type}（{safe_platform}）"

    return f"""\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{page_title}</title>
    <meta name="description" content="{og_desc}">
    <meta name="robots" content="noindex">
    <meta name="theme-color" content="#667eea">
    <meta property="og:title" content="{page_title}">
    <meta property="og:description" content="{og_desc}">
    <meta property="og:type" content="article">
    <meta property="og:locale" content="zh_CN">
    <meta property="og:site_name" content="Video Transcript API">
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
                         "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
            line-height: 1.8;
            color: #333;
            max-width: 800px;
            margin: 0 auto;
            padding: 32px 20px;
            background: #fafafa;
        }}
        article {{
            background: #fff;
            border-radius: 8px;
            box-shadow: 0 1px 8px rgba(0,0,0,0.06);
            padding: 40px;
        }}
        h1 {{
            font-size: 1.5rem;
            margin: 0 0 8px 0;
            line-height: 1.4;
        }}
        .meta {{
            color: #6b7280;
            font-size: 0.875rem;
            margin-bottom: 24px;
            padding-bottom: 16px;
            border-bottom: 1px solid #e5e7eb;
        }}
        .meta a {{ color: #667eea; text-decoration: none; }}
        .meta a:hover {{ text-decoration: underline; }}
        .content {{ font-size: 1rem; }}
        .content h1 {{ font-size: 1.3rem; }}
        .content h2 {{ font-size: 1.15rem; }}
        .content h3 {{ font-size: 1.05rem; }}
        .content p {{ margin: 0.8em 0; }}
        .content blockquote {{
            border-left: 3px solid #667eea;
            margin: 1em 0;
            padding: 0.5em 1em;
            color: #555;
            background: #f8f9ff;
        }}
        .content pre {{
            background: #f3f4f6;
            padding: 12px 16px;
            border-radius: 6px;
            overflow-x: auto;
            font-size: 0.9rem;
        }}
        .content table {{
            border-collapse: collapse;
            width: 100%;
            margin: 1em 0;
        }}
        .content th, .content td {{
            border: 1px solid #e5e7eb;
            padding: 8px 12px;
            text-align: left;
        }}
        .content th {{ background: #f9fafb; }}
    </style>
</head>
<body>
    <article>
        <h1>{safe_title}</h1>
        <div class="meta">
            <span>{safe_content_type}</span>
            {f' · <span>{safe_platform}</span>' if platform != 'unknown' else ''}
            {f' · <a href="{safe_source_url}" rel="noopener">原始链接</a>' if source_url else ''}
        </div>
        <div class="content">
            {body_html}
        </div>
    </article>
</body>
</html>"""


def handle_page_export(view_data: Dict[str, Any], export_type: str) -> Response:
    """处理 ?page= 模式导出请求，返回完整 HTML 页面.

    与 ?raw= 返回纯文本不同，此模式返回包含完整 meta 标签的 HTML 页面，
    正文经过 Markdown 渲染，适合爬虫抓取和浏览器阅读。

    Args:
        view_data: 页面数据字典
        export_type: 导出类型（calibrated/summary/transcript）

    Returns:
        HTMLResponse 包含完整 HTML 页面
    """
    # 1. 检查任务状态（复用 raw export 的状态检查逻辑）
    status = view_data.get("status")

    if status in ["queued", "processing"]:
        return HTMLResponse(
            content="<html><body><p>校对文本正在生成中，请稍后再试...</p></body></html>",
            status_code=202,
        )
    if status == "file_cleaned":
        return HTMLResponse(
            content="<html><body><p>该文件已被清理</p></body></html>",
            status_code=410,
        )
    if status == "failed":
        return HTMLResponse(
            content="<html><body><p>任务处理失败</p></body></html>",
            status_code=500,
        )
    if status != "success":
        return HTMLResponse(
            content=f"<html><body><p>任务状态异常: {status}</p></body></html>",
            status_code=400,
        )

    # 2. 获取缓存目录
    cache_dir = view_data.get("cache_dir")
    if not cache_dir or not os.path.exists(cache_dir):
        return HTMLResponse(
            content="<html><body><p>缓存文件不存在</p></body></html>",
            status_code=404,
        )

    # 3. 根据导出类型确定文件路径
    if export_type == "calibrated":
        file_path = Path(cache_dir) / "llm_calibrated.txt"
    elif export_type == "summary":
        file_path = Path(cache_dir) / "llm_summary.txt"
    elif export_type == "transcript":
        funasr_file = Path(cache_dir) / "transcript_funasr.json"
        capswriter_file = Path(cache_dir) / "transcript_capswriter.txt"
        file_path = funasr_file if funasr_file.exists() else capswriter_file
    else:
        return HTMLResponse(
            content="<html><body><p>不支持的导出类型</p></body></html>",
            status_code=400,
        )

    # 4. 检查文件存在
    if not file_path or not file_path.exists():
        content_type_cn = {
            "calibrated": "校对文本",
            "summary": "总结文本",
            "transcript": "原始转录",
        }.get(export_type, export_type)
        return HTMLResponse(
            content=f"<html><body><p>{content_type_cn}文件不存在</p></body></html>",
            status_code=404,
        )

    # 5. 读取文件并渲染
    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.error("读取文件失败: %s, 错误: %s", file_path, exc)
        return HTMLResponse(
            content="<html><body><p>读取文件失败</p></body></html>",
            status_code=500,
        )

    # 6. 将内容渲染为 HTML（Markdown -> HTML）
    body_html = render_markdown_to_html(content)

    # 7. 构建完整 HTML 页面
    vt = view_data.get("view_token", "unknown")[:20]
    logger.info(f"Page export: type={export_type}, view_token={vt}")

    page_html = _build_page_html(view_data, export_type, body_html)

    # 构建 HTTP 自定义响应头
    custom_headers = _build_metadata_headers(view_data, export_type)

    return HTMLResponse(
        content=page_html,
        headers={
            "Cache-Control": "public, max-age=3600",
            "X-Robots-Tag": "noindex",
            **custom_headers,
        },
    )


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
            content="❌ 读取文件失败，请稍后重试",
            media_type="text/plain; charset=utf-8",
            status_code=500,
        )

    # 6. 返回纯文本响应，附带元数据头
    vt = view_data.get("view_token", "unknown")[:20]
    logger.info(f"Raw export: type={export_type}, view_token={vt}")

    # 在正文顶部添加 YAML front matter 元数据
    metadata_header = _build_text_metadata_header(view_data, export_type)
    content_with_metadata = metadata_header + content

    # 构建 HTTP 自定义响应头
    custom_headers = _build_metadata_headers(view_data, export_type)

    # 明确设置响应头，提高外部 AI 工具 (Gemini 等) URL fetcher 的兼容性
    content_bytes = content_with_metadata.encode("utf-8")
    return Response(
        content=content_bytes,
        media_type="text/plain",
        headers={
            "Content-Length": str(len(content_bytes)),
            "Cache-Control": "public, max-age=3600",
            "X-Content-Type-Options": "nosniff",
            "X-Robots-Tag": "noindex",
            **custom_headers,
        },
    )


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
        raise HTTPException(status_code=500, detail="访问页面失败，请稍后重试")


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
                content="❌ 读取文件失败，请稍后重试",
                media_type="text/plain; charset=utf-8",
                status_code=500,
            )

        title = view_data.get("title", "未命名")
        platform = view_data.get("platform", "unknown")
        filename = generate_download_filename(title, platform, export_type)
        from urllib.parse import quote

        encoded_filename = quote(filename)

        # 在正文顶部添加 YAML front matter 元数据
        metadata_header = _build_text_metadata_header(view_data, export_type)
        content_with_metadata = metadata_header + content

        # 构建 HTTP 自定义响应头
        custom_headers = _build_metadata_headers(view_data, export_type)

        headers = {
            "Content-Type": "text/plain; charset=utf-8",
            "Content-Disposition": f"inline; filename*=UTF-8''{encoded_filename}",
            "X-Content-Type-Options": "nosniff",
            **custom_headers,
        }

        logger.info(
            "导出文件: %s, 文件名: %s, view_token: %s",
            export_type,
            filename,
            view_data.get("view_token", "unknown")[:20],
        )

        return Response(
            content=content_with_metadata,
            media_type="text/plain; charset=utf-8",
            headers=headers,
        )

    except Exception as exc:
        logger.exception("导出文件异常: %s", exc)
        return Response(
            content="❌ 导出失败，请稍后重试",
            media_type="text/plain; charset=utf-8",
            status_code=500,
        )


@router.get("/view/{view_token}", response_class=HTMLResponse)
async def view_transcript(
    view_token: str,
    request: Request,
    raw: Optional[str] = None,
    page: Optional[str] = None,
):
    try:
        view_data = cache_manager.get_view_data_by_token(view_token)
        if not view_data:
            if raw:
                return Response(
                    content="❌ 页面不存在\n\nview_token 无效或已过期。",
                    media_type="text/plain; charset=utf-8",
                    status_code=404,
                )
            elif page:
                return HTMLResponse(
                    content="<html><body><p>页面不存在</p></body></html>",
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

        # 如果请求 HTML 页面导出（爬虫友好模式）
        if page:
            return handle_page_export(view_data, page)

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

            # 4. 读取校准质量统计
            processed_file = cache_dir_path / "llm_processed.json" if cache_dir else None
            if processed_file and processed_file.exists():
                try:
                    import json
                    with open(processed_file, "r", encoding="utf-8") as f:
                        processed_data = json.load(f)
                    cal_stats = processed_data.get("calibration_stats")
                    if cal_stats:
                        stats["calibration_stats"] = cal_stats
                except Exception as exc:
                    logger.error(f"读取校准统计失败: {exc}")

            fallback_text = view_data.get("transcript", "")
            transcript_path = Path(cache_dir) / "llm_calibrated.txt"
            if transcript_path.exists():
                fallback_text = transcript_path.read_text(encoding="utf-8")

            # 简化渲染逻辑：直接调用 render_with_cache_analysis
            view_data["calibrated_html"] = render_calibrated_content_smart(cache_dir)

        return templates.TemplateResponse(
            "transcript.html",
            {"request": request, **view_data, "view_token": view_token, "stats": stats},
        )

    except Exception as exc:
        logger.exception("查看转录页面异常: %s", exc)
        return templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "message": "查看页面失败，请稍后重试",
            },
        )
