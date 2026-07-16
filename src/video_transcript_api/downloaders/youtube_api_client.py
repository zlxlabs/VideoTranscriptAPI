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
from .subtitle_types import SubtitleResult
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

    # SRT 时间轴行正则（如 "00:00:01,000 --> 00:00:04,000"），文本提取和时间戳
    # 提取共用同一份正则，保证两者对"什么算时间轴行"的判断完全一致。
    _SRT_TIMESTAMP_LINE_PATTERN = re.compile(
        r"\d{2}:\d{2}:\d{2}[,\.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,\.]\d{3}"
    )
    # 同上，但带分组，用于把各段数字提取出来换算成秒
    _SRT_TIMESTAMP_RANGE_PATTERN = re.compile(
        r"(\d{2}):(\d{2}):(\d{2})[,\.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,\.](\d{3})"
    )

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

            # 跳过时间轴行
            if YouTubeApiClient._SRT_TIMESTAMP_LINE_PATTERN.match(line):
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
    def _looks_like_timeline_attempt(line: str, prev_line: str) -> bool:
        """
        判断某一行是否"像"一条 SRT 时间轴声明行，即使其数字格式已经损坏

        用箭头符号 "-->" 作为宽松判定依据：正常 SRT 里只有时间轴行会包含这个
        符号，字幕正文几乎不会出现。这样即便一条 cue 的时间轴因为格式错误
        (缺位数字、分隔符错误等) 匹配不上 `_SRT_TIMESTAMP_RANGE_PATTERN`，也
        能被识别为"这里本应是一条时间轴"，从而正确切出 cue 边界，避免把下一条
        cue 的内容错误地拼接为当前 cue 的文本（反之亦然）。

        但这个宽松匹配不能无条件生效：字幕正文本身偶尔会出现 "-->"（如操作
        指引 "Settings --> Privacy"），若不加约束会被误判成下一条 cue 的时间
        轴，导致该文本行从当前 cue 的文本里静默丢失。SRT 的标准结构是
        "索引行(纯数字) -> 时间轴行 -> 文本行... -> 空行分隔"，因此只有当
        "-->" 行紧跟在一个纯数字索引行之后（即处于"预期时间轴位置"）时，才
        当作（哪怕格式已损坏的）时间轴声明；否则一律当作普通正文。

        Args:
            line: 已经 strip 过的单行文本
            prev_line: 已经 strip 过的上一行文本，用于判断当前行是否紧跟在
                纯数字索引行之后

        Returns:
            bool: 是否应被当作一条时间轴声明（不论格式是否合法）
        """
        return "-->" in line and prev_line.isdigit()

    @staticmethod
    def _extract_srt_segments(lines: list) -> list:
        """
        从 SRT 文本行中提取时间戳分段信息

        核心不变式（文本永不丢失）：只要一条 cue 有非空文本，就必须出现在
        返回的 segments 里——哪怕它的时间轴行格式已经损坏无法解析。此时该
        条目的 start_time / end_time 会被置为 None，但 text 依旧完整保留，
        下游按 segments 消费不会静默丢字。只有当整段 SRT 里完全没有任何可
        识别的 cue（既没有合法时间轴，也没有看起来像时间轴的行）时，才会
        返回空列表（由调用方转换为 None）。

        任何异常都不会向上抛出影响文本提取（由调用方 try/except 兜底）。

        Args:
            lines: parse_srt_to_subtitle_result 已经按行 split 好的原始行列表

        Returns:
            list[dict]: 每项形如
                {"start_time": float|None, "end_time": float|None, "text": str}
        """

        def to_seconds(hours: str, minutes: str, seconds: str, millis: str) -> float:
            return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000.0

        segments = []
        i = 0
        # 上一行（已 strip），用于判断当前行是否紧跟纯数字索引行——只有处于
        # "预期时间轴位置" 的 "-->" 行才可能是（损坏的）时间轴，正文中偶然
        # 出现的 "-->" 一律当普通文本，见 _looks_like_timeline_attempt。
        prev_line = ""
        while i < len(lines):
            line = lines[i].strip()
            match = YouTubeApiClient._SRT_TIMESTAMP_RANGE_PATTERN.match(line)
            is_timeline_attempt = bool(match) or YouTubeApiClient._looks_like_timeline_attempt(
                line, prev_line
            )

            if not is_timeline_attempt:
                prev_line = line
                i += 1
                continue

            if match:
                h1, m1, s1, ms1, h2, m2, s2, ms2 = match.groups()
                start_time = to_seconds(h1, m1, s1, ms1)
                end_time = to_seconds(h2, m2, s2, ms2)
            else:
                # 时间轴行格式损坏（如缺位数字、分隔符错误），无法解析出具体
                # 秒数，但仍需保留该 cue 的文本，时间字段置 None
                start_time = None
                end_time = None

            # 收集该时间轴对应的文本行，直到遇到空行/下一条 cue 的索引行/下一个
            # 时间轴行（下一个时间轴行同样按"宽松判定"处理，避免误吞下一条
            # cue；判定时同样要求紧跟纯数字索引行，正文里的 "-->" 不会被当作
            # 边界）。
            text_lines = []
            j = i + 1
            prev_candidate = line  # 时间轴行本身作为文本收集循环的"上一行"
            while j < len(lines):
                candidate = lines[j].strip()

                if not candidate:
                    break

                if candidate.isdigit():
                    # 纯数字行本身可能是真正的"下一条 cue 的索引行"，也可能只是
                    # 正文里恰好出现的一个数字（如歌词/台词就是个数字 "42"）。
                    # 只有它的下一行"像"一条时间轴（含损坏时间轴的宽松尝试）时，
                    # 才当作索引行、结束当前 cue 的文本收集；否则按普通正文继续
                    # 收集，避免把该行之后的正文从这条 cue 的 segments 里丢掉。
                    next_line = lines[j + 1].strip() if j + 1 < len(lines) else ""
                    next_is_timeline = bool(
                        YouTubeApiClient._SRT_TIMESTAMP_RANGE_PATTERN.match(next_line)
                    ) or YouTubeApiClient._looks_like_timeline_attempt(next_line, candidate)
                    if next_is_timeline:
                        break
                    text_lines.append(re.sub(r"<[^>]+>", "", candidate))
                    prev_candidate = candidate
                    j += 1
                    continue

                candidate_match = YouTubeApiClient._SRT_TIMESTAMP_RANGE_PATTERN.match(candidate)
                candidate_is_timeline_attempt = bool(
                    candidate_match
                ) or YouTubeApiClient._looks_like_timeline_attempt(candidate, prev_candidate)
                if candidate_is_timeline_attempt:
                    break
                text_lines.append(re.sub(r"<[^>]+>", "", candidate))
                prev_candidate = candidate
                j += 1

            segment_text = " ".join(t for t in text_lines if t).strip()
            if segment_text:
                segments.append({
                    "start_time": start_time,
                    "end_time": end_time,
                    "text": segment_text,
                })

            prev_line = lines[j - 1].strip()
            i = j

        return segments

    def close(self):
        """关闭客户端连接"""
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
