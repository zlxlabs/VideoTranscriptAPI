"""
YouTube Download API Server 客户端

该模块提供与 YouTube Download API Server 交互的客户端实现。
支持创建下载任务、轮询等待、下载文件等功能。
"""

import re
import time
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

from ..utils.logging import setup_logger
from .subtitle_types import SubtitleResult, sanitize_time_pair
from .youtube_api_errors import (
    YouTubeApiError,
    YouTubeApiTimeoutError,
    YouTubeApiNetworkError,
    ErrorCode,
)

logger = setup_logger("youtube_api_client")


@dataclass
class VideoInfo:
    """视频信息"""
    title: str
    author: str
    description: str
    duration: int
    channel_id: Optional[str] = None
    upload_date: Optional[str] = None
    view_count: Optional[int] = None
    thumbnail: Optional[str] = None


@dataclass
class FileInfo:
    """文件信息"""
    url: str
    size: Optional[int] = None
    format: Optional[str] = None
    language: Optional[str] = None


@dataclass
class TaskResult:
    """任务结果"""
    task_id: Optional[str]
    status: str
    video_id: str
    video_info: Optional[VideoInfo]
    audio: Optional[FileInfo]
    transcript: Optional[FileInfo]
    cache_hit: bool
    has_transcript: bool
    audio_fallback: bool
    error_code: Optional[str] = None
    error_message: Optional[str] = None


@dataclass
class VideoInfoResult:
    """视频元数据结果"""
    video_id: str
    video_info: VideoInfo
    cached: bool
    metadata_source: Optional[str] = None
    fetched_at: Optional[str] = None


class YouTubeApiClient:
    """
    YouTube Download API Server 客户端

    提供与 YouTube Download API Server 交互的完整功能：
    - 创建下载任务
    - 轮询等待任务完成
    - 下载音频和字幕文件
    - 解析 SRT 字幕为纯文本
    """

    def __init__(self, config: dict):
        """
        初始化客户端

        Args:
            config: 配置字典，包含以下字段：
                - base_url: API 服务器地址
                - api_key: 认证密钥
                - timeout: 单次请求超时（秒），默认 30
                - poll_interval: 轮询间隔（秒），默认 30
                - max_wait_time: 最大等待时间（秒），默认 3600
        """
        self.base_url = config["base_url"].rstrip("/")
        self.api_key = config["api_key"]
        self.timeout = config.get("timeout", 30)
        self.poll_interval = config.get("poll_interval", 30)
        self.max_wait_time = config.get("max_wait_time", 3600)

        # 创建带认证头的 session
        self.session = requests.Session()
        self.session.headers.update({
            "X-API-Key": self.api_key,
            "Content-Type": "application/json",
        })

        logger.info(f"[youtube-api] Client initialized: {self.base_url}")

    def fetch_transcript(self, video_id: str) -> Optional[str]:
        """
        仅获取视频字幕（不下载音频，向后兼容的纯文本入口）

        该方法是 create_and_wait() 的便捷封装，专门用于获取字幕文本。
        适用于不需要音频，只需要字幕的场景。内部委托给 fetch_transcript_result()，
        只取其中的纯文本部分，保持历史返回值不变。

        Args:
            video_id: YouTube 视频 ID

        Returns:
            str: 字幕纯文本，如果无字幕则返回 None

        Raises:
            YouTubeApiError: API 调用失败或视频无字幕
            YouTubeApiTimeoutError: 任务超时
            YouTubeApiNetworkError: 网络错误
        """
        result = self.fetch_transcript_result(video_id)
        return result.text if result else None

    def fetch_transcript_result(self, video_id: str) -> Optional[SubtitleResult]:
        """
        仅获取视频字幕（不下载音频），保留时间戳分段信息

        fetch_transcript() 的完整版：fetch_transcript() 只返回纯文本，这里额外
        携带 segments 时间戳分段，供后续需要时间轴的场景（get_subtitle_result
        等）使用。

        Args:
            video_id: YouTube 视频 ID

        Returns:
            SubtitleResult: 字幕文本 + 时间戳分段，如果无字幕则返回 None

        Raises:
            YouTubeApiError: API 调用失败或视频无字幕
            YouTubeApiTimeoutError: 任务超时
            YouTubeApiNetworkError: 网络错误
        """
        logger.info(f"[youtube-api] Fetching transcript only: {video_id}")

        # 调用统一接口，但只请求字幕
        result = self.create_and_wait(
            video_id=video_id,
            include_audio=False,
            include_transcript=True
        )

        # 检查是否有字幕
        if not result.has_transcript or not result.transcript:
            logger.warning(
                f"[youtube-api] No transcript available for video: {video_id}, "
                f"audio_fallback={result.audio_fallback}"
            )
            return None

        # 下载字幕内容并解析（含时间戳分段）
        srt_content = self.download_content(result.transcript.url)
        subtitle_result = self.parse_srt_to_subtitle_result(srt_content)

        logger.info(
            f"[youtube-api] Transcript fetched successfully: {video_id}, "
            f"length={len(subtitle_result.text)} chars, language={result.transcript.language}"
        )
        return subtitle_result

    def fetch_video_info(self, video_id: str) -> VideoInfoResult:
        """
        获取视频元数据（不下载文件）

        Args:
            video_id: YouTube 视频 ID

        Returns:
            VideoInfoResult: 元数据结果

        Raises:
            YouTubeApiError: API 调用失败或响应格式异常
            YouTubeApiNetworkError: 网络错误
        """
        logger.info(f"[youtube-api] Fetching video info: {video_id}")

        try:
            response = self.session.get(
                f"{self.base_url}/api/v1/videos/{video_id}/info",
                timeout=self.timeout,
            )

            if response.status_code == 401:
                raise YouTubeApiError(ErrorCode.UNEXPECTED, "Invalid API key")

            response.raise_for_status()
            data = response.json()

        except requests.exceptions.Timeout:
            raise YouTubeApiNetworkError("Request timeout during video info fetch")
        except requests.exceptions.RequestException as e:
            raise YouTubeApiNetworkError(f"Network error: {e}", original_error=e)

        if not isinstance(data, dict):
            raise YouTubeApiError(ErrorCode.UNEXPECTED, "Invalid response format")

        video_info = data.get("video_info") or {}
        if not isinstance(video_info, dict) or not video_info:
            raise YouTubeApiError(ErrorCode.UNEXPECTED, "Missing video_info in response")

        parsed_info = VideoInfo(
            title=video_info.get("title", ""),
            author=video_info.get("author", ""),
            description=video_info.get("description", ""),
            duration=video_info.get("duration", 0),
            channel_id=video_info.get("channel_id"),
            upload_date=video_info.get("upload_date"),
            view_count=video_info.get("view_count"),
            thumbnail=video_info.get("thumbnail"),
        )

        return VideoInfoResult(
            video_id=data.get("video_id", video_id),
            video_info=parsed_info,
            cached=bool(data.get("cached")),
            metadata_source=data.get("metadata_source"),
            fetched_at=data.get("fetched_at"),
        )

    def create_and_wait(
        self,
        video_id: str,
        include_audio: bool = False,
        include_transcript: bool = True,
    ) -> TaskResult:
        """
        创建任务并等待完成

        Args:
            video_id: YouTube 视频 ID
            include_audio: 是否请求音频
            include_transcript: 是否请求字幕

        Returns:
            TaskResult: 任务结果

        Raises:
            YouTubeApiError: API 调用失败
            YouTubeApiTimeoutError: 任务超时
            YouTubeApiNetworkError: 网络错误
        """
        video_url = f"https://www.youtube.com/watch?v={video_id}"

        logger.info(
            f"[youtube-api] Creating task: video_id={video_id}, "
            f"audio={include_audio}, transcript={include_transcript}"
        )

        # 创建任务
        result = self._create_task(video_url, include_audio, include_transcript)

        # 如果缓存命中，直接返回
        if result.cache_hit:
            logger.info(f"[youtube-api] Cache hit for video: {video_id}")
            return result

        # 如果任务已完成（不太可能，但处理一下）
        if result.status in ("completed", "failed", "cancelled"):
            return result

        # 轮询等待
        if result.task_id:
            return self._poll_until_complete(result.task_id, video_id)

        # 不应该到这里
        raise YouTubeApiError(
            ErrorCode.UNEXPECTED,
            "Task created but no task_id returned and not cache hit"
        )

    def _create_task(
        self,
        video_url: str,
        include_audio: bool,
        include_transcript: bool,
    ) -> TaskResult:
        """
        创建下载任务

        Args:
            video_url: 完整的 YouTube 视频 URL
            include_audio: 是否请求音频
            include_transcript: 是否请求字幕

        Returns:
            TaskResult: 创建结果（可能是缓存命中直接完成）

        Raises:
            YouTubeApiError: API 调用失败
            YouTubeApiNetworkError: 网络错误
        """
        payload = {
            "video_url": video_url,
            "include_audio": include_audio,
            "include_transcript": include_transcript,
            "priority": "urgent",  # 设置为紧急任务，让服务器优先处理
        }

        try:
            response = self.session.post(
                f"{self.base_url}/api/v1/tasks",
                json=payload,
                timeout=self.timeout,
            )

            if response.status_code == 401:
                raise YouTubeApiError(ErrorCode.UNEXPECTED, "Invalid API key")

            if response.status_code == 400:
                detail = response.json().get("detail", "Bad request")
                raise YouTubeApiError(ErrorCode.UNEXPECTED, detail)

            response.raise_for_status()
            return self._parse_response(response.json())

        except requests.exceptions.Timeout:
            raise YouTubeApiNetworkError("Request timeout during task creation")
        except requests.exceptions.RequestException as e:
            raise YouTubeApiNetworkError(f"Network error: {e}", original_error=e)

    def _poll_until_complete(self, task_id: str, video_id: str) -> TaskResult:
        """
        轮询等待任务完成

        Args:
            task_id: 任务 ID
            video_id: 视频 ID（用于日志）

        Returns:
            TaskResult: 完成的任务结果

        Raises:
            YouTubeApiError: API 调用失败
            YouTubeApiTimeoutError: 任务超时
            YouTubeApiNetworkError: 网络错误
        """
        start_time = time.time()
        poll_count = 0

        logger.info(
            f"[youtube-api] Polling task {task_id}, "
            f"interval={self.poll_interval}s, max_wait={self.max_wait_time}s"
        )

        while True:
            poll_count += 1
            elapsed = time.time() - start_time

            # 检查超时
            if elapsed > self.max_wait_time:
                logger.error(
                    f"[youtube-api] Task {task_id} timeout after {elapsed:.1f}s"
                )
                raise YouTubeApiTimeoutError(video_id, self.max_wait_time)

            # 查询任务状态
            try:
                response = self.session.get(
                    f"{self.base_url}/api/v1/tasks/{task_id}",
                    timeout=self.timeout,
                )
                response.raise_for_status()
                result = self._parse_response(response.json())

            except requests.exceptions.Timeout:
                logger.warning(
                    f"[youtube-api] Poll request timeout, will retry. "
                    f"Poll #{poll_count}, elapsed={elapsed:.1f}s"
                )
                time.sleep(self.poll_interval)
                continue
            except requests.exceptions.RequestException as e:
                logger.warning(
                    f"[youtube-api] Poll request failed: {e}, will retry. "
                    f"Poll #{poll_count}, elapsed={elapsed:.1f}s"
                )
                time.sleep(self.poll_interval)
                continue

            # 检查任务状态
            if result.status == "completed":
                logger.info(
                    f"[youtube-api] Task {task_id} completed. "
                    f"Poll #{poll_count}, elapsed={elapsed:.1f}s"
                )
                return result

            if result.status == "failed":
                logger.error(
                    f"[youtube-api] Task {task_id} failed: "
                    f"{result.error_code} - {result.error_message}"
                )
                raise YouTubeApiError.from_api_response({
                    "code": result.error_code or ErrorCode.DOWNLOAD_FAILED,
                    "message": result.error_message,
                })

            if result.status == "cancelled":
                raise YouTubeApiError(ErrorCode.UNEXPECTED, "Task was cancelled")

            # 任务仍在进行中
            logger.debug(
                f"[youtube-api] Task {task_id} status={result.status}. "
                f"Poll #{poll_count}, elapsed={elapsed:.1f}s, "
                f"next poll in {self.poll_interval}s"
            )
            time.sleep(self.poll_interval)

    def _parse_response(self, data: dict) -> TaskResult:
        """
        解析 API 响应数据

        Args:
            data: API 响应 JSON

        Returns:
            TaskResult: 解析后的结果
        """
        # 解析视频信息
        video_info = None
        if vi := data.get("video_info"):
            video_info = VideoInfo(
                title=vi.get("title", ""),
                author=vi.get("author", ""),
                description=vi.get("description", ""),
                duration=vi.get("duration", 0),
                channel_id=vi.get("channel_id"),
                upload_date=vi.get("upload_date"),
                view_count=vi.get("view_count"),
                thumbnail=vi.get("thumbnail"),
            )

        # 解析文件信息
        audio = None
        transcript = None
        if files := data.get("files"):
            if audio_data := files.get("audio"):
                audio = FileInfo(
                    url=audio_data["url"],
                    size=audio_data.get("size"),
                    format=audio_data.get("format"),
                )
            if transcript_data := files.get("transcript"):
                transcript = FileInfo(
                    url=transcript_data["url"],
                    size=transcript_data.get("size"),
                    format=transcript_data.get("format"),
                    language=transcript_data.get("language"),
                )

        # 解析错误信息
        error_code = None
        error_message = None
        if error := data.get("error"):
            error_code = error.get("code")
            error_message = error.get("message")

        # 解析结果标志（注意：result 可能是 null，需要用 or {} 处理）
        result_data = data.get("result") or {}
        has_transcript = result_data.get("has_transcript", False)
        audio_fallback = result_data.get("audio_fallback", False)

        return TaskResult(
            task_id=data.get("task_id"),
            status=data.get("status", "unknown"),
            video_id=data.get("video_id", ""),
            video_info=video_info,
            audio=audio,
            transcript=transcript,
            cache_hit=data.get("cache_hit", False),
            has_transcript=has_transcript,
            audio_fallback=audio_fallback,
            error_code=error_code,
            error_message=error_message,
        )

    def download_content(self, file_url: str) -> str:
        """
        下载文件内容并返回文本

        Args:
            file_url: 相对文件 URL（如 /api/v1/files/xxx.srt）

        Returns:
            str: 文件文本内容

        Raises:
            YouTubeApiNetworkError: 下载失败
        """
        full_url = f"{self.base_url}{file_url}"
        logger.debug(f"[youtube-api] Downloading content: {file_url}")

        try:
            # 文件下载不需要 API Key
            response = requests.get(full_url, timeout=60)
            response.raise_for_status()
            return response.text

        except requests.exceptions.RequestException as e:
            raise YouTubeApiNetworkError(
                f"Failed to download content: {e}",
                original_error=e
            )

    def download_to_local(self, file_url: str, target_dir: str | None = None) -> str:
        """
        下载文件到本地

        Args:
            file_url: 相对文件 URL（如 /api/v1/files/xxx.m4a）
            target_dir: 目标目录，如果为 None 则使用临时目录

        Returns:
            str: 本地文件路径

        Raises:
            YouTubeApiNetworkError: 下载失败
        """
        full_url = f"{self.base_url}{file_url}"

        # 从 URL 提取文件名
        filename = Path(file_url).name
        if not filename:
            filename = "audio.m4a"

        # 确定保存路径
        if target_dir:
            local_path = Path(target_dir) / filename
        else:
            # 默认落在当前任务目录下，随任务结束一并清理，不再泄漏到系统 /tmp
            from ..utils.tempfile_manager import get_shared_temp_manager

            task_dir = get_shared_temp_manager().get_current_task_dir()
            temp_dir = tempfile.mkdtemp(dir=str(task_dir))
            local_path = Path(temp_dir) / filename

        logger.info(f"[youtube-api] Downloading file: {file_url} -> {local_path}")

        try:
            # 文件下载不需要 API Key，使用流式下载
            response = requests.get(full_url, timeout=300, stream=True)
            response.raise_for_status()

            with open(local_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)

            file_size = local_path.stat().st_size
            logger.info(
                f"[youtube-api] File downloaded: {local_path} ({file_size} bytes)"
            )
            return str(local_path)

        except requests.exceptions.RequestException as e:
            raise YouTubeApiNetworkError(
                f"Failed to download file: {e}",
                original_error=e
            )

    # SRT 时间轴行正则（如 "00:00:01,000 --> 00:00:04,000"）。这是 legacy
    # 文本提取路径（parse_srt_to_subtitle_result 主循环，即 parse_srt_to_text
    # 的实现）判断"这一行是不是时间轴声明"的唯一依据：`.match()` 只锚定
    # 行首、不要求整行都被这段正则耗尽——命中即整行被当作时间轴丢弃（不进入
    # text），未命中则整行原样当作正文保留（进入 text）。
    #
    # gate-r28 P2 根治：segments 提取路径（`_extract_srt_segments`）此前在
    # 这条判定之外，额外独立维护了一套"看起来像时间轴"启发式
    # （`_looks_like_timeline_attempt`，要求行紧跟纯数字索引行、且 "-->"
    # 两侧都含时间样式片段），目的是"抢救"格式已损坏、这条严格正则判定不出
    # 的时间轴行，避免其文本被下一条 cue 的收集起点吞掉。但这层"抢救"本身
    # 制造了新的分歧：一条 cue 缺失时间轴行、其正文首行恰好两侧都含时间样式
    # 片段时（如 "Meet at 12:30 --> leave at 13:00"），会被误判为损坏时间轴
    # 而整行被吞掉——legacy 明明会把这行原样保留为正文，segments 里却彻底
    # 消失，text 与 segments 背离。这是"启发式判定与 legacy 不一致"这类问题
    # 第 N 次露头（此前 gate-r14/r16/r27 已经各修过一次同源分歧）。
    #
    # 根治方式：不再为 segments 路径单独维护第二套启发式判定，两条路径统一
    # 复用同一个判定函数 `_is_srt_timeline_declaration_line`（就是对这条
    # 正则 `.match()` 的直接包装）——当且仅当 legacy 会把某一行整行当作
    # 时间轴声明消费掉时，segments 路径才把它当作 cue 边界；legacy 留作
    # 正文的行（不论是不是"看起来像"损坏的时间轴），segments 路径一律按
    # 正文/孤儿路径处理，文本保留、时间置 None。两条路径对"这一行算不算
    # 时间轴"的答案因此逐行完全一致，构造上不可能再出现背离，不需要靠新增
    # 测试用例逐个堵漏洞。
    #
    # 代价（也是唯一自洽的选择）：一份 SRT 如果通篇没有任何一行能满足这条
    # 严格判定（例如所有 "-->" 行都因为位数缺失/多余、分隔符错误而匹配不
    # 上），segments 会退化为 None——这与 legacy"完全找不到任何可识别内容"
    # 时的历史行为一致：既然 legacy 判定"这不是一条可信的时间轴声明"，
    # segments 就不能装作找到了一条只是时间为 None 的 cue。
    _SRT_TIMESTAMP_LINE_PATTERN = re.compile(
        r"\d{2}:\d{2}:\d{2}[,\.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,\.]\d{3}"
    )

    # 严格版时间戳正则，仅用于 segments 提取路径解析具体时间值（gate-r24
    # P2）。`_SRT_TIMESTAMP_LINE_PATTERN` 配合 `.match()` 只锚定开头、不要求
    # 整行都被消耗——"00:00:01,000 --> 00:00:04,0000"（结尾多出一位毫秒数字）
    # 或行尾带任意垃圾字符的行，仍能匹配出一段"合法"的时间前缀。这条正则与
    # `.fullmatch()` 搭配使用，专门校验 "-->" 某一侧是否"恰好"就是一段合法
    # 的时间声明：
    # - 允许首尾空白（`\s*`）：调用处传入的一侧文本已经过 `.strip()`，这里的
    #   `\s*` 只是防御性冗余，不依赖调用方一定已经 strip 过；
    # - 毫秒固定 3 位数字（`\d{3}`，gate-r25 P2）：SRT 标准毫秒字段就是恰好
    #   3 位零填充写法（如 "000"）。1-2 位毫秒是非标准写法，和"结尾多出一位
    #   毫秒数字"或"行尾带垃圾字符"本质上是同一类问题：格式不是恰好合法的
    #   样子，因此必须同等对待——fullmatch 失败，落入下面的"该侧时间不可信"
    #   分支（文本保留、该侧时间置 None），不能被静默当成一个合法时间收录。
    #
    # 这条正则只负责"某一侧数值是否可信"这一步（cue 已经确定是一条时间轴
    # 之后），不参与 cue 边界判定（那是 `_is_srt_timeline_declaration_line`
    # 的职责），因此收紧这条正则不影响哪一行被当作时间轴、也不影响
    # `parse_srt_to_text` 的输出。
    #
    # gate-r26 P2：起止两侧独立校验。之前用一条覆盖整行的严格正则配合
    # `.fullmatch()`——只要有一侧格式损坏（如结束分钟越界，或结束毫秒不是
    # 恰好 3 位），整行 fullmatch 失败，起止两侧会被一起判定为"损坏时间轴"，
    # start_time 也被连带置 None。但合法的 start 恰恰是章节锚定最需要的
    # 信息——不能因为 end 一侧的损坏而陪葬。因此改为对 "-->" 两侧分别单独
    # 套用同一份"单侧时间戳"正则（下方）配合 `.fullmatch()`：哪一侧不能
    # 恰好匹配，就把哪一侧置 None；只有两侧都无法匹配时才等同于旧版"整条
    # 时间轴损坏"的效果（均为 None）。分钟/秒时钟分量是否 < 60 由
    # `has_valid_clock_components` 在 fullmatch 成功之后再单独校验（该正则
    # 本身只按数字位数匹配，不校验数值范围）。
    #
    # 调用处对已经命中的时间轴行做 `str.partition("-->")` 并对两侧各自
    # `.strip()`，天然吸收了首尾空白容忍，因此单侧正则本身不需要再写 `\s*`。
    _SRT_TIMESTAMP_SIDE_STRICT_PATTERN = re.compile(
        r"(\d{2}):(\d{2}):(\d{2})[,\.](\d{3})"
    )

    # UTF-8 BOM（U+FEFF）字符。部分编辑器/导出工具会在文件最开头写入这个不可见
    # 字符——它不是 Python str.strip() 会移除的空白字符，一旦出现在 SRT 首个
    # 索引行开头（如 "﻿1"），会让该行的 isdigit() 判断失真，见
    # `_strip_bom` 与 `_extract_srt_segments` 的说明。
    _UTF8_BOM = "﻿"

    @staticmethod
    def _strip_bom(line: str) -> str:
        """去掉行首可能存在的 UTF-8 BOM 字符，仅供 segments 提取路径在做
        "是不是纯数字索引行" 判断前使用（见 `_extract_srt_segments`）。

        背景：str.strip() 不会移除 U+FEFF（它按 Unicode 定义不是空白字符），
        所以文件开头若带 BOM，第一个索引行 "1" 实际会是 "﻿1"——
        "﻿1".isdigit() 为 False，导致 segments 提取路径把这一行误判成
        普通正文（孤儿文本），产出一条"内容是序号、时间为空"的伪 segment，
        真正的第一条 cue 反而被顶到 segments 第二位。

        这个函数只服务于 segments 提取路径，绝不能应用到 `parse_srt_to_text`/
        `parse_srt_to_subtitle_result` 里计算 `text` 字段的那个主循环——那个
        主循环对 BOM 文件的历史行为就是把 "﻿1" 当成普通正文保留（BOM
        字符本身也会原样嵌入输出文本），这是需要逐字节保持的向后兼容契约，
        不是待修的 bug。

        Args:
            line: 已经 strip() 过的单行文本

        Returns:
            str: 去掉行首 BOM 后的文本；不含 BOM 时原样返回
        """
        return line.lstrip(YouTubeApiClient._UTF8_BOM)

    @staticmethod
    def _is_srt_timeline_declaration_line(line: str) -> bool:
        """判断某一行是否会被 legacy 文本提取路径当作一条时间轴声明整行吞掉

        这是 legacy 文本提取（`parse_srt_to_subtitle_result` 主循环，即
        `parse_srt_to_text` 的实现）与 `_extract_srt_segments` 共用的唯一
        判定入口：直接包装 `_SRT_TIMESTAMP_LINE_PATTERN.match()`，不做任何
        额外的"看起来像"式宽松判断。两条路径都调用这同一个函数，因此对
        "这一行算不算时间轴"的答案逐行保证一致，不存在第二套独立维护、可能
        与 legacy 走偏的启发式（gate-r28 P2 根治，详见类定义上方
        `_SRT_TIMESTAMP_LINE_PATTERN` 的说明）。

        Args:
            line: 已经 strip 过的单行文本

        Returns:
            bool: legacy 文本路径是否会把这一整行当作时间轴声明消费掉（即
                该行不会出现在 parse_srt_to_text 的输出里）
        """
        return bool(YouTubeApiClient._SRT_TIMESTAMP_LINE_PATTERN.match(line))

    @staticmethod
    def parse_srt_to_text(srt_content: str) -> str:
        """
        解析 SRT 字幕为纯文本（向后兼容入口，保持历史行为逐字节一致）

        SRT 格式示例:
            1
            00:00:01,000 --> 00:00:04,000
            Hello world

            2
            00:00:05,000 --> 00:00:08,000
            This is a test

        Args:
            srt_content: SRT 格式的字幕内容

        Returns:
            str: 纯文本（各句之间用空格连接）
        """
        return YouTubeApiClient.parse_srt_to_subtitle_result(srt_content).text

    @staticmethod
    def parse_srt_to_subtitle_result(srt_content: str) -> SubtitleResult:
        """
        解析 SRT 字幕，同时返回纯文本与时间戳分段信息

        文本提取算法与历史版本的 parse_srt_to_text 完全一致（逐字节兼容，未识别
        为时间轴的行会和以前一样被当作普通文本保留）；时间戳分段提取是独立的
        容错步骤，绝不会影响 text 字段（容错铁律）。segments 一旦非 None，
        所有 cue 的文本必须都在其中——单条 cue 的时间轴格式损坏时，只会让该
        条的 start_time/end_time 置为 None，不会把整条 cue 从 segments 里丢
        掉；只有整段 SRT 完全没有任何可识别的 cue 时，segments 才会是 None。

        Args:
            srt_content: SRT 格式的字幕内容

        Returns:
            SubtitleResult: text（纯文本，逐字节兼容旧版）+ segments（可能为 None）
        """
        lines = srt_content.strip().split("\n")
        text_parts = []

        i = 0
        while i < len(lines):
            line = lines[i].strip()

            # 跳过序号行（纯数字）
            if line.isdigit():
                i += 1
                continue

            # 跳过时间轴行（判定逻辑与 segments 提取路径共用同一个函数，
            # 见 _is_srt_timeline_declaration_line）
            if YouTubeApiClient._is_srt_timeline_declaration_line(line):
                i += 1
                continue

            # 跳过空行
            if not line:
                i += 1
                continue

            # 移除 HTML 标签（如 <i>、</i> 等）
            line = re.sub(r"<[^>]+>", "", line)

            # 收集字幕文本
            if line:
                text_parts.append(line)

            i += 1

        text = " ".join(text_parts)

        # 独立提取时间戳分段：任何异常都不能影响上面已经算好的 text
        segments = None
        try:
            segments = YouTubeApiClient._extract_srt_segments(lines) or None
        except Exception as e:
            logger.warning(f"[youtube-api] SRT 时间戳解析失败，segments 置空: {e}")
            segments = None

        return SubtitleResult(text=text, segments=segments)

    @staticmethod
    def _extract_srt_segments(lines: list) -> list:
        """
        从 SRT 文本行中提取时间戳分段信息

        核心不变式（文本永不丢失）：只要一条 cue 有非空文本，就必须出现在
        返回的 segments 里——哪怕它的时间轴行格式已经损坏无法解析具体数值，
        甚至整行缺失（索引行后面直接是正文，完全没有任何被识别为时间轴的
        行）。前一种情况下该条目的 start_time / end_time 会被置为 None，但
        text 依旧完整保留；后一种"孤儿文本"（无法归属到任何 cue 的正文块）
        同样会被落成一条独立 segment（start_time / end_time 均为 None），
        保证不会出现"进了 result.text 却不进 segments"的静默丢字幕。只有当
        整段 SRT 里完全没有任何一行满足 `_is_srt_timeline_declaration_line`
        （即 legacy 也完全找不到任何时间轴声明）时，才会返回空列表（由调用
        方转换为 None）——此时孤儿文本也一并丢弃，维持历史行为不变。

        cue 边界判定（哪一行算时间轴）与 legacy 文本提取路径完全共用同一个
        判定函数 `_is_srt_timeline_declaration_line`（gate-r28 P2 根治，见
        该函数与 `_SRT_TIMESTAMP_LINE_PATTERN` 的说明）——不再为这条路径
        单独维护一套"看起来像时间轴"的启发式。legacy 判定为正文的行（哪怕
        它包含 "-->" 或数字+冒号这样"像"时间的片段），这里也一律按正文/
        孤儿路径处理：文本保留，时间置 None。

        时间值提取仍然是独立的严格步骤：cue 边界一旦确定，"-->" 两侧各自
        再套用 `_SRT_TIMESTAMP_SIDE_STRICT_PATTERN.fullmatch()` 校验数值是
        否可信（位数、时钟范围），该侧不可信则该侧置 None，不影响另一侧。

        任何异常都不会向上抛出影响文本提取（由调用方 try/except 兜底）。

        Args:
            lines: parse_srt_to_subtitle_result 已经按行 split 好的原始行列表

        Returns:
            list[dict]: 每项形如
                {"start_time": float|None, "end_time": float|None, "text": str}
        """

        def to_seconds(hours: str, minutes: str, seconds: str, millis: str) -> float:
            return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000.0

        def has_valid_clock_components(minutes: str, seconds: str) -> bool:
            """校验分钟、秒分量是否落在合法时钟范围内（均须 < 60，小时不限）。

            与 `transcriber.segments.parse_time_to_seconds` 对 "HH:MM:SS"
            的校验口径保持一致（同一条"损坏时间不得伪装真实时间"策略的两处
            落地）：`_SRT_TIMESTAMP_LINE_PATTERN` 只按数字位数匹配
            （`\\d{2}`），不校验数值范围，因此像 "00:00:99,000" 这样每段都
            恰好两位数字、但秒分量越界的字符串同样会被判定为一条时间轴。若
            不在这里补一道数值校验，会被 `to_seconds` 静默换算成 99 秒这个
            虚假时间。
            """
            return int(minutes) < 60 and int(seconds) < 60

        segments = []
        i = 0
        # 孤儿文本缓冲区：收集尚未归属到任何 cue 的正文行——典型场景是某条
        # cue 的时间轴行整行缺失（索引行后面直接接正文，没有任何时间轴声明
        # 行），导致这段正文既不在任何 cue 的时间轴之后，也没有自己的时间轴
        # 可以开启一条新 cue；也包括一行"看起来像"时间轴但不满足严格判定的
        # 行（legacy 判定为正文，这里同样按正文处理，gate-r28 P2）。收集
        # 规则与主文本提取（parse_srt_to_text）保持一致：纯数字行当索引行
        # 无条件跳过，空行是块边界，其余非空行收集为文本。
        orphan_text_parts: list = []
        # 整份 SRT 是否曾出现过至少一行满足 `_is_srt_timeline_declaration_line`
        # 的时间轴声明。只有出现过，孤儿文本才会被落地为独立 segment；如果
        # 整份文件压根没有任何一行满足这个判定（与 legacy 完全找不到时间轴
        # 声明的判断口径一致），维持历史行为返回空列表（由调用方转换为
        # None），孤儿文本全部丢弃不落地——避免把"根本不是字幕格式的纯文本"
        # 伪装成一条时间为 None 的 segment。
        found_any_cue = False

        def flush_orphan_text() -> None:
            """把当前孤儿文本缓冲区落地为一条独立 segment（时间均为 None）"""
            nonlocal orphan_text_parts
            text = " ".join(t for t in orphan_text_parts if t).strip()
            if text:
                segments.append({"start_time": None, "end_time": None, "text": text})
            orphan_text_parts = []

        while i < len(lines):
            line = lines[i].strip()
            is_timeline = YouTubeApiClient._is_srt_timeline_declaration_line(line)

            if not is_timeline:
                if not line:
                    # 空行：块边界，把已缓冲的孤儿文本落地成一条 segment
                    flush_orphan_text()
                elif not YouTubeApiClient._strip_bom(line).isdigit():
                    # 非空、非纯数字：与主文本提取的规则一致，当作正文内容
                    # 缓冲起来（纯数字行按索引行无条件跳过，不计入文本；这
                    # 里不做 R4 式 lookahead——那只在已确认的 cue 内部文本
                    # 收集里生效，孤儿区没有"当前 cue"可言）。BOM 容忍：
                    # 剥掉行首 UTF-8 BOM 后再判断是否纯数字，否则文件开头
                    # 带 BOM 时第一条索引行 "﻿1" 会被误判成孤儿正文，产出
                    # 一条"内容是序号、时间为空"的伪 segment（见 `_strip_bom`
                    # 文档）。这里只是跳过判断，不修改 line 本身——真落进
                    # orphan_text_parts 的分支仍然用原始（含 BOM）的 line，
                    # 不影响任何真实正文内容的保留。
                    orphan_text_parts.append(re.sub(r"<[^>]+>", "", line))
                i += 1
                continue

            # 命中一条 cue 的时间轴声明：先把此前尚未归属的孤儿文本落地，
            # 再按原逻辑处理这条 cue
            flush_orphan_text()
            found_any_cue = True

            # 时间值解析用严格锚定匹配（gate-r24 P2），并对起止两侧独立校验
            # （gate-r26 P2）：cue 边界判定用上面共用的
            # `_is_srt_timeline_declaration_line`（不能改，见该函数与
            # `_SRT_TIMESTAMP_LINE_PATTERN` 的文档），但具体时间数值必须每
            # 一侧各自严格匹配才可信——宽松匹配成功但某一侧严格匹配失败
            # （如该侧毫秒位数超标、时钟分量越界、行尾带垃圾字符），只把那
            # 一侧置 None，不连累另一侧本来合法的时间（如 start）；只有两侧
            # 都无法解析时，效果才等同旧版"整条时间轴损坏"（均为 None）。
            start_part, _, end_part = line.partition("-->")
            start_match = YouTubeApiClient._SRT_TIMESTAMP_SIDE_STRICT_PATTERN.fullmatch(
                start_part.strip()
            )
            end_match = YouTubeApiClient._SRT_TIMESTAMP_SIDE_STRICT_PATTERN.fullmatch(
                end_part.strip()
            )

            start_time = None
            if start_match:
                h1, m1, s1, ms1 = start_match.groups()
                # 数字位数齐全（正则能匹配），但分钟/秒分量超出合法时钟范围
                # （如 "00:99:00,000" 的分钟分量 99）——与格式彻底损坏的时间
                # 同等对待：该侧置 None，不静默换算出一个虚假秒数（见
                # has_valid_clock_components 文档）。
                if has_valid_clock_components(m1, s1):
                    start_time = to_seconds(h1, m1, s1, ms1)

            end_time = None
            if end_match:
                h2, m2, s2, ms2 = end_match.groups()
                if has_valid_clock_components(m2, s2):
                    end_time = to_seconds(h2, m2, s2, ms2)

            # 区间倒挂校验（如时间轴顺序写反）一律诚实降级为 None，绝不影响
            # 文本保留（详见 sanitize_time_pair 文档）。两侧各自独立解析后
            # 仍需经过这一步统一收口：即便两侧各自"合法"，仍可能出现
            # end < start 的倒挂区间；而当某一侧本就是 None 时，该函数的
            # "仅当两侧均非 None 才判定倒挂"规则保证另一侧的合法值不会被
            # 无谓牵连（如 start=None、end=4.0 时原样保留 end）。
            start_time, end_time = sanitize_time_pair(start_time, end_time)

            # 收集该时间轴对应的文本行，直到遇到空行/下一条 cue 的索引行/
            # 下一个时间轴声明行（同样用共用的判定函数，避免误吞下一条
            # cue）。
            text_lines = []
            j = i + 1
            while j < len(lines):
                candidate = lines[j].strip()

                if not candidate:
                    break

                if YouTubeApiClient._strip_bom(candidate).isdigit():
                    # 纯数字行本身可能是真正的"下一条 cue 的索引行"，也可能只是
                    # 正文里恰好出现的一个数字（如歌词/台词就是个数字 "42"）。
                    # 只有它的下一行满足时间轴声明判定时，才当作索引行、结束
                    # 当前 cue 的文本收集；否则按普通正文继续收集，避免把该
                    # 行之后的正文从这条 cue 的 segments 里丢掉。BOM 容忍：
                    # 同上，剥掉行首 BOM 后再判断是否纯数字（对称覆盖 BOM
                    # 理论上出现在非首行的边界情况，虽然实践中 BOM 只会出现
                    # 在文件最开头）。
                    next_line = lines[j + 1].strip() if j + 1 < len(lines) else ""
                    next_is_timeline = YouTubeApiClient._is_srt_timeline_declaration_line(
                        next_line
                    )
                    if next_is_timeline:
                        break
                    text_lines.append(re.sub(r"<[^>]+>", "", candidate))
                    j += 1
                    continue

                if YouTubeApiClient._is_srt_timeline_declaration_line(candidate):
                    break
                text_lines.append(re.sub(r"<[^>]+>", "", candidate))
                j += 1

            segment_text = " ".join(t for t in text_lines if t).strip()
            if segment_text:
                segments.append({
                    "start_time": start_time,
                    "end_time": end_time,
                    "text": segment_text,
                })

            i = j

        # 文件结尾仍有残留孤儿文本（文件未以空行收尾）：一并落地
        flush_orphan_text()

        if not found_any_cue:
            # 整份 SRT 没有任何一行满足时间轴声明判定（与 legacy 完全找不到
            # 时间轴声明的判断口径一致）：维持历史行为返回空列表（调用方会
            # 转换成 None），孤儿文本一律不落地
            return []

        return segments

    def close(self):
        """关闭客户端连接"""
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
