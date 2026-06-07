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
        仅获取视频字幕（不下载音频）

        该方法是 create_and_wait() 的便捷封装，专门用于获取字幕文本。
        适用于不需要音频，只需要字幕的场景。

        Args:
            video_id: YouTube 视频 ID

        Returns:
            str: 字幕纯文本，如果无字幕则返回 None

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

        # 下载字幕内容并解析
        srt_content = self.download_content(result.transcript.url)
        plain_text = self.parse_srt_to_text(srt_content)

        logger.info(
            f"[youtube-api] Transcript fetched successfully: {video_id}, "
            f"length={len(plain_text)} chars, language={result.transcript.language}"
        )
        return plain_text

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

    @staticmethod
    def parse_srt_to_text(srt_content: str) -> str:
        """
        解析 SRT 字幕为纯文本

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
        lines = srt_content.strip().split("\n")
        text_parts = []

        # 时间轴正则
        timestamp_pattern = re.compile(r"\d{2}:\d{2}:\d{2}[,\.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,\.]\d{3}")

        i = 0
        while i < len(lines):
            line = lines[i].strip()

            # 跳过序号行（纯数字）
            if line.isdigit():
                i += 1
                continue

            # 跳过时间轴行
            if timestamp_pattern.match(line):
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

        return " ".join(text_parts)

    def close(self):
        """关闭客户端连接"""
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
