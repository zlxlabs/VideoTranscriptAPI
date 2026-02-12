import os
import re
import json
import time
import datetime
from typing import Any, Optional, Sequence, Tuple, Union

from .base import BaseDownloader
from .models import VideoMetadata, DownloadInfo
from ..utils.logging import setup_logger
from ..utils import create_debug_dir

# 创建日志记录器
logger = setup_logger("xiaohongshu_downloader")
# 创建调试目录
DEBUG_DIR = create_debug_dir()

# ---------------------------------------------------------------------------
# 端点配置 & 多路径提取常量
# ---------------------------------------------------------------------------

# 按优先级排列的 TikHub 端点配置
_ENDPOINT_CONFIGS: list[dict[str, Any]] = [
    {
        "name": "app_video_note",
        "path": "/api/v1/xiaohongshu/app/get_video_note_info",
        "params_builder": lambda url: {"share_text": url},
    },
    {
        "name": "web_v4",
        "path": "/api/v1/xiaohongshu/web/get_note_info_v4",
        "params_builder": lambda url: {"share_text": url},
    },
    {
        "name": "app_note",
        "path": "/api/v1/xiaohongshu/app/get_note_info",
        "params_builder": lambda url: {"share_text": url, "force_video_enabled": "true"},
    },
    {
        "name": "web_v2",
        "path": "/api/v1/xiaohongshu/web/get_note_info_v2",
        "params_builder": lambda url: {"share_text": url},
    },
]

# 路径类型: tuple 中元素可以是 str (dict key) 或 int (list index)
PathKey = Union[str, int]

# 视频 URL 候选提取路径（按优先级，作用于已 unwrap 的笔记数据）
# 多条路径返回不同 CDN 节点的 URL，下载时会逐个尝试以应对单节点故障
_VIDEO_URL_PATHS: list[Tuple[PathKey, ...]] = [
    # app_video_note: video_info_v2.media.stream.h264
    ("video_info_v2", "media", "stream", "h264", 0, "backup_urls", 0),
    ("video_info_v2", "media", "stream", "h264", 0, "master_url"),
    ("video_info_v2", "media", "stream", "h264", 0, "backup_urls", 1),
    # web V3/V4 flat: video.media.stream.h264
    ("video", "media", "stream", "h264", 0, "backup_urls", 0),
    ("video", "media", "stream", "h264", 0, "master_url"),
    ("video", "media", "stream", "h264", 0, "backup_urls", 1),
    # simple paths
    ("video", "url"),
    ("video_info", "url"),
    ("video_info", "media", "stream", "h264", 0, "backup_urls", 0),
    # fallback: audio URL from widgets_context (sufficient for transcription)
    ("_widgets_media_url",),
]

# 标题候选提取路径
_TITLE_PATHS: list[Tuple[PathKey, ...]] = [
    ("title",),
    ("note_info", "title"),
    ("note", "title"),
]

# 作者候选提取路径
_AUTHOR_PATHS: list[Tuple[PathKey, ...]] = [
    ("user", "nickname"),
    ("user", "nick_name"),
    ("user", "name"),
    ("note_user", "nickname"),
]

# 描述候选提取路径
_DESC_PATHS: list[Tuple[PathKey, ...]] = [
    ("desc",),
    ("description",),
    ("note_info", "desc"),
]


class XiaohongshuDownloader(BaseDownloader):
    """小红书视频下载器，支持多端点回退策略。"""

    def __init__(self) -> None:
        super().__init__()
        self._cached_video_info: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # 公共接口（保持不变）
    # ------------------------------------------------------------------

    def can_handle(self, url: str) -> bool:
        """判断是否可以处理该 URL。

        Args:
            url: 视频 URL

        Returns:
            是否可以处理
        """
        return "xiaohongshu.com" in url or "xhslink.com" in url

    def extract_note_id(self, url: str) -> str:
        """从 URL 中提取笔记 ID 的公共方法。

        Args:
            url: 视频 URL

        Returns:
            笔记 ID
        """
        return self._extract_note_id(url)

    def extract_video_id(self, url: str) -> str:
        """提取笔记 ID（兼容新接口）。

        Args:
            url: 视频 URL

        Returns:
            笔记 ID
        """
        return self._extract_note_id(url)

    # ------------------------------------------------------------------
    # 笔记 ID 提取（保持不变）
    # ------------------------------------------------------------------

    def _extract_note_id(self, url: str) -> str:
        """从 URL 中提取笔记 ID。

        Args:
            url: 视频 URL

        Returns:
            笔记 ID

        Raises:
            ValueError: 无法提取笔记 ID
        """
        # 解析短链接
        if "xhslink.com" in url:
            logger.info(f"Resolving xiaohongshu short link: {url}")
            url = self.resolve_short_url(url)
            logger.info(f"Resolved to full URL: {url}")

        # 尝试多种模式提取笔记ID
        patterns = [
            r'explore/(\w+)',
            r'discovery/item/(\w+)',
            r'items/(\w+)',
            r'/(\w{24})',
        ]

        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                note_id = match.group(1)
                logger.info(f"Extracted xiaohongshu note ID: {note_id}")
                return note_id

        # 如果用户直接提供了ID，尝试验证其格式
        if re.match(r'^\w{24}$', url):
            logger.info(f"User provided raw xiaohongshu note ID: {url}")
            return url

        logger.error(f"Failed to extract note ID from URL: {url}")
        raise ValueError(f"Failed to extract note ID from URL: {url}")

    # ------------------------------------------------------------------
    # 核心：多端点回退获取视频信息
    # ------------------------------------------------------------------

    def get_video_info(self, url: str) -> dict:
        """获取视频信息（多端点回退策略）。

        按优先级依次尝试多个 TikHub 端点，任一成功即返回。
        全部失败时抛出包含所有错误摘要的 ValueError。

        Args:
            url: 视频 URL

        Returns:
            包含 video_id, video_title, author, description,
            download_url, filename, platform 的字典

        Raises:
            ValueError: 所有端点均失败
        """
        note_id = self._extract_note_id(url)

        # 实例缓存命中
        if note_id in self._cached_video_info:
            logger.debug(f"[cache hit] Returning cached video info: {note_id}")
            return self._cached_video_info[note_id]

        errors: list[str] = []

        for config in _ENDPOINT_CONFIGS:
            name = config["name"]
            try:
                result = self._try_endpoint(url, note_id, config)
                # 成功 — 缓存并返回
                self._cached_video_info[note_id] = result
                logger.info(f"Endpoint '{name}' succeeded for note {note_id}")
                return result
            except Exception as exc:
                error_summary = f"[{name}] {exc}"
                errors.append(error_summary)
                logger.warning(f"Endpoint '{name}' failed: {exc}")

        # 所有端点均失败
        combined = "; ".join(errors)
        logger.error(f"All endpoints failed for {url}: {combined}")
        raise ValueError(
            f"All xiaohongshu API endpoints failed for {url}: {combined}"
        )

    # ------------------------------------------------------------------
    # 单端点调用 + 解析
    # ------------------------------------------------------------------

    def _try_endpoint(self, url: str, note_id: str, config: dict) -> dict:
        """尝试单个端点并解析响应。

        Args:
            url: 原始视频 URL
            note_id: 笔记 ID
            config: 端点配置字典

        Returns:
            标准化视频信息字典

        Raises:
            ValueError: 该端点调用或解析失败
        """
        name = config["name"]
        endpoint = config["path"]
        params = config["params_builder"](url)

        logger.info(f"Trying endpoint '{name}': {endpoint}")
        response = self.make_api_request(endpoint, params)

        # 基本响应校验
        self._validate_response(response, name)

        raw_data = response["data"]

        # 保存原始响应调试文件
        self._save_debug_response(raw_data, f"{name}_raw", note_id)

        # 解包嵌套结构到笔记级别
        note_data = self._unwrap_note_data(raw_data, name)

        # 从 widgets_context 等嵌入 JSON 中提取补充数据
        self._enrich_from_widgets_context(note_data, name)

        # 解析视频信息
        return self._parse_video_info(note_data, url, note_id, name)

    # ------------------------------------------------------------------
    # 响应数据 unwrap（解包嵌套结构）
    # ------------------------------------------------------------------

    @staticmethod
    def _unwrap_note_data(data: dict, name: str) -> dict:
        """从 TikHub 响应 data 中解包到笔记级别数据。

        不同端点的响应嵌套层级不同：
        - app_video_note: data.data[0] → 笔记数据（含 title, video_info_v2 等）
        - app_note:       data.data[0].note_list[0] → 笔记数据
        - web V3/V4:      data 直接就是笔记数据（含 title, video 等）

        本方法逐级检查并 unwrap 到包含视频字段的那一层。

        Args:
            data: API response["data"] 字段
            name: 端点名称（用于日志）

        Returns:
            解包后的笔记级别字典

        Raises:
            ValueError: 无法解包到有效的笔记数据
        """
        # 如果 data 直接包含视频相关字段，无需 unwrap
        if any(k in data for k in ("video", "video_info", "video_info_v2")):
            logger.debug(f"[{name}] data is already at note level")
            return data

        # 嵌套模式: data.data 是 list
        inner_data = data.get("data")
        if isinstance(inner_data, list) and len(inner_data) > 0:
            first_item = inner_data[0]
            if not isinstance(first_item, dict):
                raise ValueError(f"[{name}] data.data[0] is not a dict")

            # 检查 first_item 是否直接就是笔记（app_video_note 格式）
            if any(k in first_item for k in ("video_info_v2", "video", "video_info")):
                logger.debug(f"[{name}] unwrapped via data.data[0]")
                return first_item

            # 更深的嵌套: data.data[0].note_list[0]（app_note 格式）
            note_list = first_item.get("note_list")
            if isinstance(note_list, list) and len(note_list) > 0:
                note = note_list[0]
                if isinstance(note, dict):
                    logger.debug(f"[{name}] unwrapped via data.data[0].note_list[0]")
                    # 合并 user 信息（note_list 内的笔记可能缺少 user 字段）
                    if "user" not in note and "user" in first_item:
                        note["user"] = first_item["user"]
                    return note

            # first_item 有 title 但没有 video 字段 — 仍然返回让后续提取尝试
            if "title" in first_item or "desc" in first_item:
                logger.debug(f"[{name}] unwrapped via data.data[0] (no video key found)")
                return first_item

        raise ValueError(f"[{name}] Cannot unwrap response data to note level")

    # ------------------------------------------------------------------
    # widgets_context 解析与数据补充
    # ------------------------------------------------------------------

    @staticmethod
    def _enrich_from_widgets_context(note: dict, name: str) -> None:
        """解析 widgets_context JSON 字符串，将媒体 URL 注入到 note 中。

        某些端点（如 app_note）不直接返回 video_info_v2，但在
        widgets_context JSON 字符串中包含 note_sound_info.url（音频/视频流地址）。
        该音频足以用于语音转录。

        注入到 note["_widgets_media_url"] 以供路径提取。

        Args:
            note: 笔记级别字典（会被就地修改）
            name: 端点名称（用于日志）
        """
        wc_raw = note.get("widgets_context")
        if not isinstance(wc_raw, str) or not wc_raw.strip():
            return

        try:
            wc = json.loads(wc_raw)
        except (json.JSONDecodeError, TypeError):
            logger.debug(f"[{name}] Failed to parse widgets_context JSON")
            return

        # 提取 note_sound_info.url（音频/视频流 URL）
        sound_url = (
            wc.get("note_sound_info", {}).get("url")
            if isinstance(wc.get("note_sound_info"), dict)
            else None
        )
        if sound_url:
            note["_widgets_media_url"] = sound_url
            logger.debug(
                f"[{name}] Extracted media URL from widgets_context: "
                f"{sound_url[:80]}..."
            )

    # ------------------------------------------------------------------
    # 响应校验
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_response(response: Any, name: str) -> None:
        """校验 TikHub API 响应基本结构。

        Args:
            response: API 原始响应
            name: 端点名称（用于日志）

        Raises:
            ValueError: 响应格式不符合预期
        """
        if not isinstance(response, dict):
            raise ValueError(
                f"[{name}] API response is not a dict: {type(response)}"
            )

        code = response.get("code")
        if code != 200:
            msg = response.get("message", "unknown error")
            raise ValueError(f"[{name}] API returned code={code}: {msg}")

        if not isinstance(response.get("data"), dict):
            raise ValueError(f"[{name}] 'data' field missing or not a dict")

    # ------------------------------------------------------------------
    # 灵活的视频信息解析
    # ------------------------------------------------------------------

    def _parse_video_info(
        self, data: dict, url: str, note_id: str, endpoint_name: str
    ) -> dict:
        """从响应 data 中提取视频信息（兼容多种响应格式）。

        Args:
            data: API 响应中的 data 字段
            url: 原始视频 URL
            note_id: 笔记 ID
            endpoint_name: 端点名称

        Returns:
            标准化视频信息字典

        Raises:
            ValueError: 无法提取视频 URL
        """
        # 标题
        video_title = self._extract_first_match(data, _TITLE_PATHS) or ""
        if not video_title.strip():
            video_title = f"xiaohongshu_{note_id}"
            logger.warning(f"Title not found, using fallback: {video_title}")

        # 作者
        author = self._extract_first_match(data, _AUTHOR_PATHS) or "unknown"

        # 描述
        description = self._extract_first_match(data, _DESC_PATHS) or ""

        logger.info(
            f"Parsed metadata from '{endpoint_name}': "
            f"title='{video_title}', author='{author}', "
            f"desc_len={len(description)}"
        )

        # 视频 URL — 收集所有候选以便下载时逐个尝试
        all_video_urls = self._extract_all_matches(data, _VIDEO_URL_PATHS)
        if not all_video_urls:
            self._save_debug_response(data, f"no_video_url_{endpoint_name}", note_id)
            raise ValueError(
                f"[{endpoint_name}] Cannot extract video URL from response"
            )

        video_url = all_video_urls[0]
        logger.info(
            f"Found {len(all_video_urls)} video URL(s) via '{endpoint_name}': "
            f"{str(video_url)[:80]}..."
        )

        filename = f"xiaohongshu_{note_id}_{int(time.time())}.mp4"

        return {
            "video_id": note_id,
            "video_title": video_title,
            "author": author,
            "description": description,
            "download_url": video_url,
            "_candidate_urls": all_video_urls,
            "filename": filename,
            "platform": "xiaohongshu",
        }

    # ------------------------------------------------------------------
    # 深层路径安全提取工具
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_by_path(data: Any, path: Sequence[PathKey]) -> Any:
        """沿给定路径安全导航嵌套 dict/list。

        Args:
            data: 根数据对象
            path: 路径键序列，str 用于 dict，int 用于 list

        Returns:
            路径末端的值，路径中断时返回 None
        """
        current = data
        for key in path:
            if current is None:
                return None
            if isinstance(key, int):
                if isinstance(current, (list, tuple)) and 0 <= key < len(current):
                    current = current[key]
                else:
                    return None
            elif isinstance(key, str):
                if isinstance(current, dict) and key in current:
                    current = current[key]
                else:
                    return None
            else:
                return None
        return current

    @classmethod
    def _extract_first_match(
        cls, data: Any, paths: list[Tuple[PathKey, ...]]
    ) -> Any:
        """按优先级依次尝试多条路径，返回第一个非空值。

        Args:
            data: 根数据对象
            paths: 路径列表

        Returns:
            第一个非 None、非空字符串的值，全部失败返回 None
        """
        for path in paths:
            value = cls._extract_by_path(data, path)
            if value is not None and value != "":
                return value
        return None

    @classmethod
    def _extract_all_matches(
        cls, data: Any, paths: list[Tuple[PathKey, ...]]
    ) -> list[Any]:
        """提取所有路径的非空值（去重，保持优先级顺序）。

        Args:
            data: 根数据对象
            paths: 路径列表

        Returns:
            所有非 None、非空字符串的值列表（已去重）
        """
        seen: set = set()
        results: list[Any] = []
        for path in paths:
            value = cls._extract_by_path(data, path)
            if value is not None and value != "" and value not in seen:
                seen.add(value)
                results.append(value)
        return results

    # ------------------------------------------------------------------
    # 调试文件保存
    # ------------------------------------------------------------------

    @staticmethod
    def _save_debug_response(data: Any, label: str, note_id: str) -> None:
        """将响应数据保存到调试目录。

        Args:
            data: 要保存的数据
            label: 文件名标签
            note_id: 笔记 ID
        """
        try:
            ts = datetime.datetime.now().strftime("%y%m%d-%H%M%S")
            filename = f"{ts}_debug_xiaohongshu_{label}_{note_id}.json"
            filepath = os.path.join(DEBUG_DIR, filename)
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.debug(f"Debug response saved to: {filepath}")
        except Exception as exc:
            logger.warning(f"Failed to save debug response: {exc}")

    # ------------------------------------------------------------------
    # 字幕（小红书无字幕）
    # ------------------------------------------------------------------

    def get_subtitle(self, url: str) -> None:
        """获取字幕，小红书视频通常没有字幕，返回 None。

        Args:
            url: 视频 URL

        Returns:
            None
        """
        return None

    # ------------------------------------------------------------------
    # 标准化元数据 / 下载信息接口
    # ------------------------------------------------------------------

    def _fetch_metadata(self, url: str, video_id: str) -> VideoMetadata:
        """获取标准化视频元数据。"""
        info = self.get_video_info(url)
        return VideoMetadata(
            video_id=info.get("video_id", video_id),
            platform=info.get("platform", "xiaohongshu"),
            title=info.get("video_title", ""),
            author=info.get("author", ""),
            description=info.get("description", ""),
        )

    def _fetch_download_info(self, url: str, video_id: str) -> DownloadInfo:
        """获取标准化下载信息。"""
        info = self.get_video_info(url)
        filename = info.get("filename")
        file_ext = None
        if filename and "." in filename:
            file_ext = filename.rsplit(".", 1)[-1]
        return DownloadInfo(
            download_url=info.get("download_url"),
            file_ext=file_ext,
            filename=filename,
        )

    def download_file(self, url: str, filename: str) -> Optional[str]:
        """下载文件，支持多 CDN URL 回退。

        小红书不同 CDN 节点稳定性差异大（sns-v8/sns-v10 等），单个节点可能
        因网络环境不同而出现 ConnectionResetError。此处重写父类方法，从缓存
        中获取所有候选 URL 并逐个尝试，直到成功或全部失败。

        Args:
            url: 主下载 URL（由 _fetch_download_info 提供）。
            filename: 期望的本地文件名（含扩展名）。

        Returns:
            下载成功返回本地文件路径，失败返回 None。
        """
        # 收集所有候选 URL（主 URL + 缓存中的备选）
        candidate_urls = [url]
        for info in self._cached_video_info.values():
            for u in info.get("_candidate_urls", []):
                if u not in candidate_urls:
                    candidate_urls.append(u)

        if len(candidate_urls) > 1:
            logger.info(
                f"Xiaohongshu download: {len(candidate_urls)} candidate URLs available"
            )

        last_error: Optional[Exception] = None
        for idx, candidate_url in enumerate(candidate_urls):
            try:
                logger.info(
                    f"Trying URL [{idx + 1}/{len(candidate_urls)}]: "
                    f"{candidate_url[:100]}..."
                )
                result = super().download_file(candidate_url, filename)
                if result is not None:
                    return result
                # download_file 返回 None 表示文件无效，尝试下一个
                logger.warning(
                    f"URL [{idx + 1}] returned invalid file, trying next"
                )
            except Exception as e:
                last_error = e
                logger.warning(
                    f"URL [{idx + 1}] failed: {type(e).__name__}: "
                    f"{str(e)[:100]}"
                )

        logger.error(
            f"All {len(candidate_urls)} candidate URLs failed for "
            f"xiaohongshu download"
        )
        return None
