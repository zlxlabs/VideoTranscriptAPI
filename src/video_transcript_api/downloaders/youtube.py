import os
import re
import json
import math
import time
import datetime
import xml.etree.ElementTree as ET
import requests
import yt_dlp
import tempfile
from pathlib import Path
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import TranscriptsDisabled, VideoUnavailable, IpBlocked, NoTranscriptFound
from .base import BaseDownloader
from .models import VideoMetadata, DownloadInfo
from .subtitle_types import SubtitleResult, sanitize_time_pair
from ..utils.logging import setup_logger
from ..utils import create_debug_dir
from ..utils.ytdlp import YtdlpConfigBuilder

# 创建日志记录器
logger = setup_logger("youtube_downloader")
# 创建调试目录
DEBUG_DIR = create_debug_dir()

class YoutubeDownloader(BaseDownloader):
    """
    Youtube视频下载器
    优先使用 youtube-transcript-api 获取字幕，失败后才使用 yt-dlp 下载视频
    """
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 初始化 YouTube Transcript API
        self.ytt_api = YouTubeTranscriptApi()
        # 延迟初始化 yt-dlp 配置构建器
        self._ytdlp_builder: YtdlpConfigBuilder | None = None
        # 延迟初始化 YouTube API Server 客户端
        self._youtube_api_client = None
        self._init_youtube_api_client()

        # 🆕 实例级缓存（生命周期 = 任务生命周期）
        # 用途：避免同一任务内的重复 TikHub API 请求
        # 场景：get_video_info() 和 _get_subtitle_with_tikhub_api() 复用同一次 API 响应
        self._cached_video_info: dict[str, dict] = {}

    @property
    def ytdlp_builder(self) -> YtdlpConfigBuilder:
        """延迟初始化并返回 yt-dlp 配置构建器"""
        if self._ytdlp_builder is None:
            self._ytdlp_builder = YtdlpConfigBuilder(self.config)
        return self._ytdlp_builder

    def _init_youtube_api_client(self):
        """
        初始化 YouTube API Server 客户端（如果配置启用）
        """
        api_config = self.config.get("youtube_api_server", {})
        if api_config.get("enabled"):
            try:
                from .youtube_api_client import YouTubeApiClient
                self._youtube_api_client = YouTubeApiClient(api_config)
                logger.info("[youtube] API Server client initialized")
            except Exception as e:
                logger.error(f"[youtube] Failed to initialize API Server client: {e}")
                self._youtube_api_client = None

    @property
    def use_api_server(self) -> bool:
        """是否使用 YouTube API Server"""
        return self._youtube_api_client is not None

    def fetch_for_transcription(self, url: str, use_speaker_recognition: bool = False) -> dict:
        """
        一次性获取 YouTube 视频的所有转录所需资源（仅当启用 API Server 时使用）

        该方法通过一次 API 请求获取视频信息、字幕或音频，避免多次请求。
        根据 use_speaker_recognition 参数决定请求策略：
        - True: 必须下载音频（用于说话人识别转录）
        - False: 优先获取字幕，无字幕则自动 fallback 到音频

        Args:
            url: 视频 URL
            use_speaker_recognition: 是否需要说话人识别

        Returns:
            dict: 包含以下字段：
                - video_id: str
                - video_title: str
                - author: str
                - description: str
                - platform: "youtube"
                - transcript: str | None (字幕文本，已解析为纯文本)
                - audio_path: str | None (本地音频文件路径)
                - need_transcription: bool (是否需要调用转录服务)

        Raises:
            RuntimeError: 未启用 API Server
            YouTubeApiError: API 调用失败（不会降级到其他方式）
        """
        if not self.use_api_server:
            raise RuntimeError("YouTube API Server not enabled")

        from .youtube_api_client import YouTubeApiClient

        video_id = self._extract_video_id(url)
        logger.info(
            f"[youtube] fetch_for_transcription: video_id={video_id}, "
            f"use_speaker_recognition={use_speaker_recognition}"
        )

        # 根据 use_speaker_recognition 决定请求参数
        # - 需要说话人识别: 必须下载音频
        # - 不需要说话人识别: 优先字幕，无则 fallback 到音频
        include_audio = use_speaker_recognition
        include_transcript = not use_speaker_recognition

        # 调用 API（一次请求获取所有信息）
        result = self._youtube_api_client.create_and_wait(
            video_id,
            include_audio=include_audio,
            include_transcript=include_transcript
        )

        # 提取视频信息
        video_info = result.video_info
        video_title = video_info.title if video_info else f"youtube_{video_id}"
        author = video_info.author if video_info else ""
        description = video_info.description if video_info else ""

        transcript = None
        audio_path = None
        need_transcription = False

        if use_speaker_recognition:
            # 必须下载音频用于说话人识别
            if result.audio and result.audio.url:
                audio_path = self._youtube_api_client.download_to_local(result.audio.url)
                need_transcription = True
                logger.info(f"[youtube] Audio downloaded for speaker recognition: {audio_path}")
            else:
                from .youtube_api_errors import YouTubeApiError, ErrorCode
                raise YouTubeApiError(
                    ErrorCode.UNEXPECTED,
                    "Audio requested but not returned by API"
                )
        else:
            # 优先使用字幕
            if result.has_transcript and not result.audio_fallback and result.transcript:
                # 有字幕，下载并解析
                srt_content = self._youtube_api_client.download_content(result.transcript.url)
                transcript = YouTubeApiClient.parse_srt_to_text(srt_content)
                need_transcription = False
                logger.info(
                    f"[youtube] Transcript downloaded and parsed, "
                    f"length={len(transcript)} chars"
                )
            elif result.audio and result.audio.url:
                # 无字幕，下载 fallback 的音频
                audio_path = self._youtube_api_client.download_to_local(result.audio.url)
                need_transcription = True
                logger.info(f"[youtube] No transcript, audio fallback: {audio_path}")
            else:
                from .youtube_api_errors import YouTubeApiError, ErrorCode
                raise YouTubeApiError(
                    ErrorCode.UNEXPECTED,
                    "Neither transcript nor audio returned by API"
                )

        return {
            "video_id": video_id,
            "video_title": video_title,
            "author": author,
            "description": description,
            "platform": "youtube",
            "transcript": transcript,
            "audio_path": audio_path,
            "need_transcription": need_transcription,
        }

    def can_handle(self, url):
        """
        判断是否可以处理该URL
        
        参数:
            url: 视频URL
            
        返回:
            bool: 是否可以处理
        """
        return "youtube.com" in url or "youtu.be" in url
    
    def _extract_video_id(self, url):
        """
        从URL中提取视频ID

        参数:
            url: 视频URL

        返回:
            str: 视频ID
        """
        # 解析短链接
        if "youtu.be" in url:
            url = self.resolve_short_url(url)

        # 从URL中提取视频ID
        if "youtube.com/watch" in url:
            # 形如 https://www.youtube.com/watch?v=VIDEO_ID
            match = re.search(r'v=([^&]+)', url)
            if match:
                return match.group(1)
        elif "youtube.com/shorts/" in url:
            # 形如 https://www.youtube.com/shorts/VIDEO_ID
            match = re.search(r'/shorts/([^?&/]+)', url)
            if match:
                video_id = match.group(1)
                logger.info(f"从YouTube Shorts URL中提取到视频ID: {video_id}")
                return video_id
        elif "youtube.com/live/" in url:
            # 形如 https://www.youtube.com/live/VIDEO_ID
            match = re.search(r'/live/([^?&/]+)', url)
            if match:
                video_id = match.group(1)
                logger.info(f"从YouTube Live URL中提取到视频ID: {video_id}")
                return video_id
        elif "youtu.be/" in url:
            # 形如 https://youtu.be/VIDEO_ID
            match = re.search(r'youtu\.be/([^?&]+)', url)
            if match:
                return match.group(1)

        logger.error(f"无法从URL中提取Youtube视频ID: {url}")
        raise ValueError(f"无法从URL中提取Youtube视频ID: {url}")
    
    def extract_video_id(self, url):
        """
        从URL中提取视频ID的公共方法
        
        参数:
            url: 视频URL
        返回:
            str: 视频ID
        """
        return self._extract_video_id(url)
    
    def get_video_info(self, url):
        """
        获取视频信息，直接使用 TikHub API（避免 yt-dlp 触发机器人风控）

        使用实例级缓存避免重复 API 请求（任务内有效）

        参数:
            url: 视频URL

        返回:
            dict: 包含视频信息的字典
        """
        try:
            # 提取视频ID
            video_id = self._extract_video_id(url)

            # 🆕 检查实例缓存（避免同一任务内的重复请求）
            if video_id in self._cached_video_info:
                logger.debug(f"[实例缓存命中] 使用缓存的视频信息: {video_id}")
                return self._cached_video_info[video_id]

            # 实例缓存未命中，调用 TikHub API
            logger.info(f"[API请求] 调用 TikHub API 获取 YouTube 视频信息: {video_id}")
            endpoint = f"/api/v1/youtube/web/get_video_info"
            params = {"video_id": video_id}
            
            logger.info(f"调用TikHub API获取YouTube视频信息: video_id={video_id}")
            response = self.make_api_request(endpoint, params)
            
            # 生成时间戳前缀
            timestamp_prefix = datetime.datetime.now().strftime("%y%m%d-%H%M%S")
            
            # 记录API响应摘要，帮助调试
            if isinstance(response, dict):
                response_code = response.get("code")
                response_msg = response.get("message", "无消息")
                logger.info(f"API响应状态: {response_code}, 消息: {response_msg}")
                
                # 保存完整响应到文件，用于调试
                debug_file = os.path.join(DEBUG_DIR, f"{timestamp_prefix}_debug_youtube_{video_id}.json")
                with open(debug_file, 'w', encoding='utf-8') as f:
                    json.dump(response, f, ensure_ascii=False, indent=2)
                logger.debug(f"API完整响应已保存到: {debug_file}")
            
            # 检查响应格式并提供详细错误信息
            if not isinstance(response, dict):
                logger.error(f"API返回格式错误，预期字典，实际: {type(response)}")
                raise ValueError("API返回格式错误，无法解析响应")
            
            # TikHub API成功响应时返回code=200
            if response.get("code") != 200:
                error_msg = response.get("message", "未知错误")
                logger.error(f"API返回错误代码: {response.get('code')}, 错误信息: {error_msg}")
                
                # 保存错误响应到文件
                error_file = os.path.join(DEBUG_DIR, f"{timestamp_prefix}_error_youtube_{video_id}.json")
                with open(error_file, 'w', encoding='utf-8') as f:
                    json.dump(response, f, ensure_ascii=False, indent=2)
                logger.debug(f"错误响应已保存到: {error_file}")
                
                raise ValueError(f"获取YouTube视频信息失败: {error_msg}")
            
            # 检查data字段
            if not response.get("data"):
                logger.error("API响应中缺少data字段或格式不正确")
                
                # 保存错误响应到文件
                error_file = os.path.join(DEBUG_DIR, f"{timestamp_prefix}_error_data_youtube_{video_id}.json")
                with open(error_file, 'w', encoding='utf-8') as f:
                    json.dump(response, f, ensure_ascii=False, indent=2)
                
                raise ValueError("API响应数据格式错误，缺少必要字段")
            
            # 提取必要信息
            data = response.get("data", {})
            
            # 视频标题
            video_title = data.get("title", "")
            if not video_title or video_title.strip() == "":
                video_title = f"youtube_{video_id}"
                logger.warning(f"未找到视频标题，使用ID作为标题: {video_title}")
            
            # 视频作者
            author = data.get("channel", {}).get("name", "未知作者")
            
            # 视频描述
            description = data.get("description", "")
            
            logger.info(f"获取到视频信息: 标题='{video_title}', 作者='{author}', 描述长度={len(description)}")
            
            # 尝试获取音频下载地址
            download_url = None
            file_ext = "mp4"  # 默认扩展名
            
            audio_items = data.get("audios", {}).get("items", [])
            
            if audio_items and len(audio_items) > 0:
                download_url = audio_items[0].get("url")
                file_ext = "m4a"  # YouTube音频通常为m4a格式
                logger.info(f"找到音频下载URL: {download_url[:50]}...")
            
            if not download_url:
                logger.error("无法获取YouTube视频音频下载地址")
                
                # 保存错误数据到文件
                error_file = os.path.join(DEBUG_DIR, f"{timestamp_prefix}_error_no_audio_youtube_{video_id}.json")
                with open(error_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                
                raise ValueError(f"无法获取Youtube视频音频下载地址: {url}")
            
            # 清理文件名中的非法字符
            safe_title = re.sub(r'[\\/*?:"<>|]', "_", video_title)
            filename = f"youtube_{video_id}_{int(time.time())}.{file_ext}"
            
            # 获取字幕信息
            subtitles = data.get("subtitles", {})
            subtitle_info = None
            
            # 检查字幕数据
            if subtitles and subtitles.get("items"):
                subtitle_items = subtitles.get("items", [])
                
                # 优先选择中文字幕，其次是英文字幕
                zh_subtitle = next((item for item in subtitle_items if item.get("code") == "zh"), None)
                en_subtitle = next((item for item in subtitle_items if item.get("code") == "en"), None)
                
                subtitle_info = zh_subtitle or en_subtitle
                
                if subtitle_info:
                    logger.info(f"找到字幕: 语言={subtitle_info.get('code', '未知')}")
            
            result = {
                "video_id": video_id,
                "video_title": video_title,
                "author": author,
                "description": description,
                "download_url": download_url,
                "filename": filename,
                "platform": "youtube",
                "subtitle_info": subtitle_info,
                "download_method": "tikhub"
            }

            # 🆕 缓存到实例变量（仅在当前任务内有效，任务结束自动释放）
            self._cached_video_info[video_id] = result
            logger.info(f"[缓存保存] 视频信息已缓存到实例: {video_id}")
            logger.info(f"成功获取YouTube视频信息: ID={video_id}, 文件类型={file_ext}")
            return result
                
        except Exception as e:
            logger.exception(f"获取YouTube视频信息异常: {str(e)}")
            raise
    
    def get_subtitle(self, url):
        """
        获取字幕，按优先级策略执行

        优先级策略：
        - 如果启用 youtube_api_server：
          1. youtube_api_server（绕过本地 IP 封禁）
          2. TikHub API（直接跳转，跳过本地方案）
        - 如果未启用 youtube_api_server：
          1. 本地 youtube-transcript-api
          2. TikHub API（备用）

        参数:
            url: 视频URL

        返回:
            str: 字幕文本，如果有的话
        """
        try:
            video_id = self._extract_video_id(url)
            if not video_id:
                logger.warning("[字幕获取] 无法提取视频ID")
                return None

            # ============================================================
            # 分支 A：启用了 youtube_api_server
            # ============================================================
            if self.use_api_server:
                logger.info(
                    f"[字幕获取] 使用 youtube_api_server 优先策略: video_id={video_id}"
                )

                # 尝试通过 API Server 获取字幕
                try:
                    transcript = self._youtube_api_client.fetch_transcript(video_id)
                    if transcript and transcript.strip():
                        logger.info(
                            f"[字幕获取] youtube_api_server 成功: "
                            f"length={len(transcript)} chars"
                        )
                        return transcript
                    else:
                        # 返回 None 或空字符串 = 视频没有字幕，不需要重试
                        logger.info(
                            f"[字幕获取] 视频没有可用字幕（已由 API Server 确认）: {video_id}"
                        )
                        return None
                except Exception as api_error:
                    # 只有失败（异常）时才回退到 TikHub
                    logger.warning(
                        f"[字幕获取] youtube_api_server 失败: {api_error}, "
                        f"回退到 TikHub API（跳过本地方案）"
                    )
                    return self._get_subtitle_with_tikhub_api(url)

            # ============================================================
            # 分支 B：未启用 youtube_api_server，使用本地方案
            # ============================================================
            else:
                logger.info(
                    f"[字幕获取] 使用本地方案: video_id={video_id}"
                )

                # 首先尝试使用 youtube-transcript-api
                transcript = self._fetch_youtube_transcript(video_id)

                if transcript and transcript.strip():
                    # 检查是否是IP被阻止的标记
                    if transcript == "IP_BLOCKED":
                        logger.warning(
                            f"[字幕获取] 本地方案 IP 被封，回退到 TikHub API: {video_id}"
                        )
                        return self._get_subtitle_with_tikhub_api(url)
                    elif transcript == "TRANSCRIPTS_DISABLED":
                        logger.info(f"[字幕获取] 视频字幕已被禁用: {video_id}")
                        return None
                    else:
                        logger.info(
                            f"[字幕获取] 本地方案成功: length={len(transcript)} chars"
                        )
                        return transcript

                # 如果 youtube-transcript-api 失败，尝试使用 TikHub API
                logger.info(
                    f"[字幕获取] 本地方案失败，回退到 TikHub API: {video_id}"
                )
                return self._get_subtitle_with_tikhub_api(url)

        except Exception as e:
            logger.exception(f"[字幕获取] 异常: {str(e)}")
            return None

    def get_subtitle_result(self, url):
        """
        获取字幕，保留时间戳分段信息（get_subtitle 的完整版，供后续接线使用）

        行为策略与 get_subtitle 完全一致（同样的优先级、同样的回退顺序），
        唯一区别是成功时返回 SubtitleResult（text + 可选 segments）而不是纯
        文本。get_subtitle 出于向后兼容需要保持字符串返回值不变，因此这里
        单独实现一份平行的分支逻辑，而不是让 get_subtitle 反过来委托本方法
        （否则 mock 了 get_subtitle 内部旧方法名的既有测试会失效）。

        参数:
            url: 视频URL

        返回:
            SubtitleResult | None: 字幕文本 + 时间戳分段，如果没有字幕则返回 None
        """
        try:
            video_id = self._extract_video_id(url)
            if not video_id:
                logger.warning("[字幕获取] 无法提取视频ID")
                return None

            # ============================================================
            # 分支 A：启用了 youtube_api_server
            # ============================================================
            if self.use_api_server:
                logger.info(
                    f"[字幕获取] 使用 youtube_api_server 优先策略: video_id={video_id}"
                )

                try:
                    subtitle_result = self._youtube_api_client.fetch_transcript_result(video_id)
                    # 有效性判定：text 非空 或 segments 非空 即视为"有字幕"。
                    # 旧的逐字节兼容文本提取（parse_srt_to_text）会把纯数字
                    # 正文行误判为 SRT 序号行而跳过，导致 text 为空；而新的
                    # segments 提取用 lookahead 正确识别并保留了这些内容。
                    # 只看 text 会把这种"文本丢了但时间轴还在"的结果连同
                    # segments 一起当成"没有字幕"整体丢弃
                    if subtitle_result and (subtitle_result.text.strip() or subtitle_result.segments):
                        logger.info(
                            f"[字幕获取] youtube_api_server 成功: "
                            f"length={len(subtitle_result.text)} chars"
                        )
                        return subtitle_result
                    else:
                        # 返回 None 或空文本 = 视频没有字幕，不需要重试
                        logger.info(
                            f"[字幕获取] 视频没有可用字幕（已由 API Server 确认）: {video_id}"
                        )
                        return None
                except Exception as api_error:
                    # 只有失败（异常）时才回退到 TikHub
                    logger.warning(
                        f"[字幕获取] youtube_api_server 失败: {api_error}, "
                        f"回退到 TikHub API（跳过本地方案）"
                    )
                    return self._get_subtitle_result_with_tikhub_api(url)

            # ============================================================
            # 分支 B：未启用 youtube_api_server，使用本地方案
            # ============================================================
            else:
                logger.info(
                    f"[字幕获取] 使用本地方案: video_id={video_id}"
                )

                transcript_result = self._fetch_youtube_transcript_result(video_id)

                if transcript_result == "IP_BLOCKED":
                    logger.warning(
                        f"[字幕获取] 本地方案 IP 被封，回退到 TikHub API: {video_id}"
                    )
                    return self._get_subtitle_result_with_tikhub_api(url)
                elif transcript_result == "TRANSCRIPTS_DISABLED":
                    logger.info(f"[字幕获取] 视频字幕已被禁用: {video_id}")
                    return None
                elif isinstance(transcript_result, SubtitleResult) and (
                    transcript_result.text.strip() or transcript_result.segments
                ):
                    logger.info(
                        f"[字幕获取] 本地方案成功: length={len(transcript_result.text)} chars"
                    )
                    return transcript_result

                # 如果 youtube-transcript-api 失败，尝试使用 TikHub API
                logger.info(
                    f"[字幕获取] 本地方案失败，回退到 TikHub API: {video_id}"
                )
                return self._get_subtitle_result_with_tikhub_api(url)

        except Exception as e:
            logger.exception(f"[字幕获取] 异常: {str(e)}")
            return None

    def _fetch_metadata(self, url: str, video_id: str) -> VideoMetadata:
        if self.use_api_server:
            try:
                result = self._youtube_api_client.fetch_video_info(video_id)
                info = result.video_info
                title = info.title or f"youtube_{video_id}"
                author = info.author or ""
                description = info.description or ""

                extra = {
                    "duration": info.duration,
                    "channel_id": info.channel_id,
                    "upload_date": info.upload_date,
                    "view_count": info.view_count,
                    "thumbnail": info.thumbnail,
                    "cached": result.cached,
                    "metadata_source": result.metadata_source,
                    "fetched_at": result.fetched_at,
                }

                logger.info(
                    f"[youtube-api] Metadata fetched: video_id={result.video_id}, "
                    f"title={title[:50]}"
                )
                return VideoMetadata(
                    video_id=result.video_id or video_id,
                    platform="youtube",
                    title=title,
                    author=author,
                    description=description,
                    extra=extra,
                )
            except Exception as e:
                logger.warning(
                    f"[youtube-api] Metadata fetch failed, fallback to TikHub: {e}"
                )

        info = self.get_video_info(url)
        extra = {}
        if "subtitle_info" in info:
            extra["subtitle_info"] = info.get("subtitle_info")
        return VideoMetadata(
            video_id=info.get("video_id", video_id),
            platform=info.get("platform", "youtube"),
            title=info.get("video_title", ""),
            author=info.get("author", ""),
            description=info.get("description", ""),
            extra=extra,
        )

    def _fetch_download_info(self, url: str, video_id: str) -> DownloadInfo:
        info = self.get_video_info(url)
        filename = info.get("filename")
        file_ext = None
        if filename and "." in filename:
            file_ext = filename.rsplit(".", 1)[-1]
        extra = {}
        if info.get("download_method"):
            extra["download_method"] = info.get("download_method")
        return DownloadInfo(
            download_url=info.get("download_url"),
            file_ext=file_ext,
            filename=filename,
            subtitle_url=None,
            extra=extra,
        )
    
    def _fetch_youtube_transcript(self, video_id):
        """
        使用 youtube-transcript-api 获取字幕（向后兼容的纯文本入口）

        内部委托给 _fetch_youtube_transcript_result()：成功时只取其中的纯
        文本部分，控制信号哨兵字符串（"IP_BLOCKED" / "TRANSCRIPTS_DISABLED"）
        和 None 原样透传，保持历史返回值不变。
        """
        result = self._fetch_youtube_transcript_result(video_id)
        if isinstance(result, SubtitleResult):
            return result.text
        return result

    def _fetch_youtube_transcript_result(self, video_id):
        """
        使用 youtube-transcript-api 获取字幕，保留时间戳分段信息

        _fetch_youtube_transcript() 的完整版：那个方法只返回纯文本，这里
        成功时额外返回 SubtitleResult 中的 segments，供 get_subtitle_result()
        使用。控制信号（字符串哨兵 "IP_BLOCKED" / "TRANSCRIPTS_DISABLED"，
        以及 None）与历史行为完全一致。

        返回:
            SubtitleResult | str | None:
                - SubtitleResult: 成功获取字幕（text 逐字节兼容旧版，segments
                  可能为 None）
                - "IP_BLOCKED" / "TRANSCRIPTS_DISABLED": 控制信号哨兵
                - None: 未获取到字幕
        """
        try:
            # 列出可用字幕
            transcript_list = self.ytt_api.list(video_id)
            available_languages = []

            for transcript in transcript_list:
                available_languages.append(transcript.language_code)
                logger.debug(f"视频 {video_id} 可用字幕: {transcript.language_code} (自动生成: {transcript.is_generated})")

            if not available_languages:
                logger.info(f"视频 {video_id} 没有可用字幕")
                return None

        except TranscriptsDisabled:
            logger.info(f"视频 {video_id} 字幕已被禁用")
            return "TRANSCRIPTS_DISABLED"
        except VideoUnavailable:
            logger.warning(f"视频 {video_id} 不可用")
            return None
        except IpBlocked:
            logger.error(f"IP 被 YouTube 阻止，无法获取视频 {video_id} 的字幕")
            return "IP_BLOCKED"
        except Exception as e:
            logger.warning(f"无法获取视频 {video_id} 的字幕列表: {e}")
            return None

        # 尝试按优先级获取字幕
        priority_languages = ['zh-CN', 'zh-TW', 'zh', 'en']

        for lang in priority_languages:
            if lang in available_languages:
                try:
                    logger.debug(f"尝试获取 {video_id} 的 {lang} 字幕")
                    fetched_transcript = self.ytt_api.fetch(video_id, languages=[lang])

                    subtitle_result = self._build_subtitle_result_from_snippets(fetched_transcript)
                    if subtitle_result.text.strip():
                        logger.info(f"成功获取视频 {video_id} 的 {lang} 字幕，长度: {len(subtitle_result.text)} 字符")
                        return subtitle_result

                except IpBlocked:
                    logger.error(f"IP 被 YouTube 阻止")
                    return "IP_BLOCKED"
                except NoTranscriptFound:
                    logger.debug(f"视频 {video_id} 没有 {lang} 字幕")
                    continue
                except Exception as e:
                    logger.warning(f"获取视频 {video_id} 的 {lang} 字幕失败: {e}")
                    continue

        # 如果优先语言都失败，尝试获取第一个可用字幕
        if available_languages:
            try:
                first_lang = available_languages[0]
                logger.debug(f"尝试获取 {video_id} 的 {first_lang} 字幕 (备选)")
                fetched_transcript = self.ytt_api.fetch(video_id, languages=[first_lang])

                subtitle_result = self._build_subtitle_result_from_snippets(fetched_transcript)
                if subtitle_result.text.strip():
                    logger.info(f"成功获取视频 {video_id} 的 {first_lang} 字幕 (备选)，长度: {len(subtitle_result.text)} 字符")
                    return subtitle_result

            except IpBlocked:
                logger.error(f"IP 被 YouTube 阻止")
                return "IP_BLOCKED"
            except Exception as e:
                logger.error(f"获取视频 {video_id} 的备选字幕失败: {e}")

        return None

    def _build_subtitle_result_from_snippets(self, fetched_transcript) -> SubtitleResult:
        """
        将 youtube-transcript-api 返回的字幕条目转换为 SubtitleResult

        文本拼接逻辑（item.text.strip() 后用空格 join）与历史版本逐字节一致；
        时间戳（item.start / item.duration）提取是独立的容错步骤，按条目
        (per-item) 容错：单条 snippet 的 .start / .duration 缺失或数值非法，
        只会让该条的 start_time / end_time 置为 None，不会连累其余条目、也
        不会让整个 segments 降级为 None（文本永不丢失的不变式：segments 一旦
        非 None，所有条目的文本必须都在其中）。只有全部条目都没有文本时，
        segments 才会是 None。

        参数:
            fetched_transcript: 可迭代的字幕条目（每项需有 .text 属性，
                时间戳分段额外需要 .start / .duration 属性）

        返回:
            SubtitleResult
        """
        # 只迭代一次原始数据（部分实现可能是一次性迭代器），文本与时间戳
        # 分别从同一份缓存列表中提取，避免二次迭代导致数据丢失
        raw_items = list(fetched_transcript)

        text_parts = [item.text.strip() for item in raw_items]
        text = ' '.join(text_parts)

        candidate_segments = []
        for item in raw_items:
            # start_time 单独解析：缺失属性 / 非数字 / 非有限值（inf、nan）/
            # 超出 float 可表示范围的天文数字（如反序列化后的 10**400，
            # float() 转换会抛 OverflowError 而不是静默变成 inf——因为
            # int->float 与 str->float 走不同的转换路径，只有前者会溢出
            # 抛异常）都视为该条时间不可用，置 None，但绝不影响 text 的保留。
            #
            # bool 必须在 float() 之前显式排除（gate-r21 P3）：bool 是 int
            # 的子类，float(True) == 1.0、float(False) == 0.0 会被 float()
            # 静默当成合法的小数值时间戳接受，但上游（无论是库本身的缺陷
            # 数据，还是反序列化混入的脏值）传来的 bool 从不代表真实时间，
            # 与统一适配器 transcriber.segments.parse_time_to_seconds
            # "bool 一律拒绝"的口径保持一致，这里显式拒绝。判断必须留在
            # try 内部（借用 TypeError 走进已有的 except 分支）：`.start`
            # 属性本身也可能缺失，判断必须和属性访问共享同一层异常保护，
            # 不能在 try 之外单独访问 item.start，否则会把"属性缺失"这种
            # 本该被容忍的场景变成未捕获异常。
            try:
                if isinstance(item.start, bool):
                    raise TypeError("start is bool, not a genuine time value")
                start_time = float(item.start)
                if not math.isfinite(start_time):
                    start_time = None
            except (AttributeError, TypeError, ValueError, OverflowError):
                start_time = None

            # 负 start 在派生 end_time 之前立刻清洗（不能等到最后统一调用
            # sanitize_time_pair 才清洗）：end_time = start_time + duration，
            # 若在 start_time 还带着非法负值时就用它计算 end，一个负 start
            # （如 -5）配上足够大的正 duration（如 10）会算出看似合法的正数
            # end（5），而这个 end 本身毫无意义——它是从一个已知非法的起点
            # 派生出来的，不能因为数值本身非负就蒙混过关。提前清洗保证
            # end_time 只可能从"已验证合法的 start_time"派生。
            if start_time is not None and start_time < 0:
                start_time = None

            # end_time 依赖 start_time + duration，两者任一不可用则整体置 None；
            # duration 同样可能是天文数字，同上需要捕获 OverflowError。同样
            # 要先排除 bool（理由同 start_time 的处理，见上方注释）——
            # float(True)/float(False) 若不拦截会被当成合法的 1 秒/0 秒时长。
            # 判断同样留在 try 内部：`.duration` 属性本身可能缺失（见
            # FakeSnippetMissingDuration 场景），不能在 try 之外单独访问。
            end_time = None
            if start_time is not None:
                try:
                    if isinstance(item.duration, bool):
                        raise TypeError("duration is bool, not a genuine time value")
                    duration = float(item.duration)
                    if math.isfinite(duration):
                        # start_time 与 duration 各自有限，不代表二者之和有限：
                        # float 加法不像 int->float 转换那样会抛 OverflowError，
                        # 而是静默饱和为 inf（如二者均为 1e308）。相加后必须
                        # 再校验一次，非有限值同样置 None，维持“时间字段要么
                        # None 要么有限非负”的不变式
                        candidate_end_time = start_time + duration
                        if math.isfinite(candidate_end_time):
                            end_time = candidate_end_time
                except (AttributeError, TypeError, ValueError, OverflowError):
                    end_time = None

            # 负值与区间倒挂校验：负 start / 负 duration（体现为负 end 或
            # end < start）一律诚实降级为 None，绝不影响文本保留（详见
            # sanitize_time_pair 文档）
            start_time, end_time = sanitize_time_pair(start_time, end_time)

            candidate_segments.append({
                "start_time": start_time,
                "end_time": end_time,
                "text": item.text.strip(),
            })

        segments = candidate_segments or None

        return SubtitleResult(text=text, segments=segments)

    def _get_subtitle_with_tikhub_api(self, url):
        """
        使用原有的 TikHub API 获取字幕作为备用方案（向后兼容的纯文本入口）

        内部委托给 _get_subtitle_result_with_tikhub_api()，只取其中的纯文本
        部分，保持历史返回值（str | None）不变。
        """
        result = self._get_subtitle_result_with_tikhub_api(url)
        return result.text if result else None

    def _get_subtitle_result_with_tikhub_api(self, url):
        """
        使用原有的 TikHub API 获取字幕作为备用方案，保留时间戳分段信息

        _get_subtitle_with_tikhub_api() 的完整版：那个方法只返回纯文本，这里
        额外携带 SubtitleResult 中的 segments，供 get_subtitle_result() 使用。

        🆕 优化：复用实例缓存的 video_info，避免重复 TikHub API 请求
        （在同一任务内，get_video_info 通常已被调用）

        返回:
            SubtitleResult | None
        """
        try:
            video_id = self._extract_video_id(url)

            # 🆕 优先复用实例缓存（避免重复 API 请求）
            if video_id in self._cached_video_info:
                logger.debug(f"[实例缓存命中] 复用 video_info，避免重复 API 请求: {video_id}")
                video_info = self._cached_video_info[video_id]
            else:
                # 如果缓存不存在，首次调用 get_video_info（会自动缓存）
                logger.info(f"[实例缓存未命中] 调用 get_video_info: {video_id}")
                video_info = self.get_video_info(url)

            subtitle_info = video_info.get("subtitle_info")

            if not subtitle_info or not subtitle_info.get("url"):
                logger.info(f"TikHub API 未找到字幕信息")
                return None

            # 下载字幕XML
            subtitle_url = subtitle_info["url"]

            logger.info(f"从 TikHub API 下载字幕: {subtitle_url[:50]}...")
            response = requests.get(subtitle_url, timeout=30)
            response.raise_for_status()

            xml_content = response.text

            # 生成时间戳前缀
            timestamp_prefix = datetime.datetime.now().strftime("%y%m%d-%H%M%S")

            # 保存字幕XML到文件，用于调试
            video_id = video_info.get("video_id")
            subtitle_file = os.path.join(DEBUG_DIR, f"{timestamp_prefix}_subtitle_youtube_{video_id}.xml")
            with open(subtitle_file, 'w', encoding='utf-8') as f:
                f.write(xml_content)
            logger.debug(f"字幕内容已保存到: {subtitle_file}")

            # 解析XML字幕（含时间戳分段）
            return self._parse_youtube_subtitle_xml(xml_content)
        except Exception as e:
            logger.exception(f"TikHub API 获取字幕异常: {str(e)}")
            return None
    
    def _parse_youtube_subtitle_xml(self, xml_content):
        """
        解析YouTube字幕XML

        文本提取算法与历史版本逐字节兼容（按 start 排序后拼接）；时间戳分段
        提取是独立的容错步骤，按条目 (per-item) 容错："start"/"dur" 属性缺失
        或格式错误只会让该条的 start_time / end_time 置为 None，不会连累其余
        条目、也不会让整个 segments 降级为 None（容错铁律：时间解析失败绝不
        能影响文本提取；文本永不丢失：segments 一旦非 None，所有有文本的条目
        必须都在其中）。

        注（gate-r21 P3 排查结论）：本路径天然不受 bool 时间值污染——下方
        `start_raw`/`dur_raw` 全部来自 `Element.get("start"/"dur")`，
        ElementTree 对 XML 属性的取值恒为 `str`（属性缺失时才是 `None`），
        不存在 `youtube-transcript-api` 那个 snippet 对象路径里"库返回的
        属性可能是任意 Python 类型（含 bool）"的问题，因此无需、也不会加
        `isinstance(..., bool)` 判断——`float("True")` 本身就会因
        `ValueError` 走进下方已有的 except 分支，按现有"格式错误->None"的
        容错逻辑处理，不会被误判成合法时间。

        参数:
            xml_content: XML字幕内容

        返回:
            SubtitleResult | None: 解析成功返回 SubtitleResult（text 逐字节兼容
            旧版，segments 可能为 None）；XML 本身无法解析时返回 None（与历史
            行为一致）
        """
        try:
            root = ET.fromstring(xml_content)

            # 提取文本内容与原始时间属性字符串。start/dur 在此处不做数值转换，
            # 也不做默认值填充——`.get()` 不传 default，属性缺失时得到 None，
            # 而不是伪造成字符串 "0"。伪造成 "0" 会在下方解析出一个虚假的
            # "起点为 0" 或"零时长"时间戳，违反"坏/缺时间 -> None"的不变式。
            # 属性缺失与格式错误统一交给下方独立的时间戳分段提取步骤处理
            # （两者都会走 except 分支，最终落到 None，不影响文本提取）。
            raw_items = []
            for text_element in root.findall(".//text"):
                raw_items.append({
                    "start_raw": text_element.get("start"),
                    "dur_raw": text_element.get("dur"),
                    "content": (text_element.text or "").strip(),
                })

            # 按开始时间排序；无法解析为数字的 start（含属性缺失的 None）视为
            # 0，仅用于决定排序位置，不影响下方 start_time 字段本身的 None 判定。
            # 非有限值（nan/inf）同样视为 0：float("nan") 不会抛异常，若不校验
            # isfinite 会把字面量 nan 当作排序 key 直接喂给 sort()——NaN 与任何
            # 数比较恒为 False，会破坏其它合法条目之间原本正确的相对顺序（不只
            # 是这条脏数据自己排错位置），而不仅仅是不可解析字符串那种能被
            # except 捕获的情况。
            def _safe_start(item):
                try:
                    value = float(item["start_raw"])
                except (TypeError, ValueError):
                    return 0.0
                return value if math.isfinite(value) else 0.0

            raw_items.sort(key=_safe_start)

            # 合并字幕文本（与历史行为保持逐字节一致）
            merged_text = ""
            for item in raw_items:
                if item["content"]:
                    merged_text += item["content"] + " "
            text = merged_text.strip()

            # 独立提取时间戳分段：按条目容错，单条 start/dur 属性非法只影响
            # 该条的时间字段，不影响上面已经算好的 text，也不连累其余条目；
            # 外层 try/except 是最后一道防线，兜底任何未预料的异常，同样只让
            # segments 降级为 None，绝不影响 text
            segments = None
            try:
                candidate_segments = []
                for item in raw_items:
                    if not item["content"]:
                        continue

                    # start_time 单独解析：非数字 / 非有限值（inf、nan）都视为
                    # 该条时间不可用，置 None，但绝不影响文本的保留
                    try:
                        start = float(item["start_raw"])
                        if not math.isfinite(start):
                            start = None
                    except (TypeError, ValueError):
                        start = None

                    # 负 start 在派生 end 之前立刻清洗（同 snippet 路径的处理，见
                    # _build_subtitle_result_from_snippets 的同名注释）：负 start
                    # 配正 duration 可能算出一个看似合法的正 end，但那是从已知
                    # 非法的起点派生出来的，不能放行——必须先清洗 start，end
                    # 只从已验证合法的 start 派生
                    if start is not None and start < 0:
                        start = None

                    # end_time 依赖 start + duration，两者任一不可用则整体置 None
                    end = None
                    if start is not None:
                        try:
                            duration = float(item["dur_raw"])
                            if math.isfinite(duration):
                                # start 与 duration 各自有限，不代表二者之和有限：
                                # float 加法不像 int->float 转换那样会抛
                                # OverflowError，而是静默饱和为 inf（如二者均
                                # 为 1e308）。相加后必须再校验一次，非有限值
                                # 同样置 None，维持“时间字段要么 None 要么有限
                                # 非负”的不变式
                                candidate_end = start + duration
                                if math.isfinite(candidate_end):
                                    end = candidate_end
                        except (TypeError, ValueError):
                            end = None

                    # 负值与区间倒挂校验：负 start / 负 duration（体现为负
                    # end 或 end < start）一律诚实降级为 None，绝不影响文本
                    # 保留（详见 sanitize_time_pair 文档）
                    start, end = sanitize_time_pair(start, end)

                    candidate_segments.append({
                        "start_time": start,
                        "end_time": end,
                        "text": item["content"],
                    })
                segments = candidate_segments or None
            except Exception as e:
                logger.warning(f"YouTube字幕XML时间戳解析失败，segments 置空: {e}")
                segments = None

            logger.info(f"成功解析YouTube字幕，共{len(raw_items)}段")
            return SubtitleResult(text=text, segments=segments)
        except Exception as e:
            logger.exception(f"解析Youtube字幕XML异常: {str(e)}")
            return None
    
    def _get_video_info_with_ytdlp(self, url, use_cookie: bool = True):
        """
        使用 yt-dlp 获取视频信息（不下载）

        参数:
            url: 视频URL
            use_cookie: 是否使用 cookie（如果可用）

        返回:
            dict: 包含视频信息的字典
        """
        try:
            video_id = self._extract_video_id(url)

            # 使用配置构建器获取 yt-dlp 选项
            ydl_opts = self.ytdlp_builder.build_info_opts(use_cookie=use_cookie)
            ydl_opts['ignoreerrors'] = False

            cookie_status = "with cookie" if (use_cookie and self.ytdlp_builder.is_cookie_available()) else "without cookie"
            logger.info(f"[youtube] Getting video info ({cookie_status}): {video_id}")

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)

                # 提取必要信息
                video_title = info.get('title', f'youtube_{video_id}')
                author = info.get('channel', info.get('uploader', '未知作者'))
                description = info.get('description', '')

                # 查找音频格式
                formats = info.get('formats', [])
                audio_url = None
                file_ext = "m4a"

                # 优先选择 m4a 音频
                for fmt in formats:
                    if fmt.get('acodec') != 'none' and fmt.get('vcodec') == 'none':
                        if fmt.get('ext') == 'm4a':
                            audio_url = fmt.get('url')
                            break

                # 如果没有找到 m4a，选择任意音频格式
                if not audio_url:
                    for fmt in formats:
                        if fmt.get('acodec') != 'none' and fmt.get('vcodec') == 'none':
                            audio_url = fmt.get('url')
                            file_ext = fmt.get('ext', 'm4a')
                            break

                if audio_url:
                    logger.info(f"[youtube] Found audio download URL (yt-dlp)")

                # 清理文件名中的非法字符
                safe_title = re.sub(r'[\\/*?:"<>|]', "_", video_title)
                filename = f"youtube_{video_id}_{int(time.time())}.{file_ext}"

                # 检查是否有字幕
                subtitles = info.get('subtitles', {})
                automatic_captions = info.get('automatic_captions', {})
                all_subtitles = {**subtitles, **automatic_captions}

                subtitle_info = None
                if all_subtitles:
                    # 优先选择中文字幕，其次是英文字幕
                    for lang_code in ['zh-CN', 'zh-TW', 'zh', 'en']:
                        if lang_code in all_subtitles:
                            subtitle_entries = all_subtitles[lang_code]
                            if subtitle_entries:
                                subtitle_info = {
                                    "code": lang_code,
                                    "url": subtitle_entries[0].get('url')
                                }
                                logger.info(f"[youtube] Found subtitle: lang={lang_code} (yt-dlp)")
                                break

                result = {
                    "video_id": video_id,
                    "video_title": video_title,
                    "author": author,
                    "description": description,
                    "download_url": audio_url,
                    "filename": filename,
                    "platform": "youtube",
                    "subtitle_info": subtitle_info
                }

                logger.info(f"[youtube] Successfully got video info via yt-dlp: ID={video_id}")
                return result

        except Exception as e:
            logger.error(f"[youtube] yt-dlp get video info error: {e}")
            raise
    
    def download_video_with_priority(self, url, video_info=None):
        """
        按优先级下载视频：优先使用yt-dlp，备用TikHub API
        
        参数:
            url: 视频URL
            video_info: 视频信息（可选，如果提供则使用其中的下载方法信息）
            
        返回:
            str: 本地文件路径，失败返回None
        """
        logger.info(f"开始按优先级下载YouTube视频: {url}")
        
        # 首先尝试yt-dlp下载
        try:
            logger.info("优先使用yt-dlp下载...")
            audio_result = self.download_audio_for_transcription(url)
            if audio_result and audio_result.get("audio_path"):
                logger.info(f"yt-dlp下载成功: {audio_result['audio_path']}")
                return audio_result["audio_path"]
            else:
                logger.warning("yt-dlp下载失败或未返回有效路径")
        except Exception as e:
            logger.warning(f"yt-dlp下载异常: {e}")
        
        # 如果yt-dlp失败，尝试使用TikHub API
        if video_info:
            download_url = video_info.get("download_url")
            filename = video_info.get("filename")

            if download_url and filename:
                logger.info("降级使用TikHub API下载...")
                try:
                    local_file = self.download_file(download_url, filename)
                    if local_file:
                        logger.info(f"TikHub API下载成功: {local_file}")
                        return local_file
                    else:
                        logger.error("TikHub API下载失败")
                except Exception as e:
                    logger.error(f"TikHub API下载异常: {e}")
            else:
                logger.warning("video_info 中没有 TikHub API 下载信息，无法使用备用下载方式")
        else:
            logger.warning("没有提供video_info，无法使用TikHub API备用下载")
        
        logger.error(f"所有下载方式均失败: {url}")
        return None
    
    def download_audio_for_transcription(self, url):
        """
        使用 yt-dlp 下载音频用于转录

        下载策略:
        1. 如果 cookie 可用，先尝试带 cookie 下载
        2. 带 cookie 失败且允许 fallback，降级为无 cookie 下载
        3. 返回结果或 None

        参数:
            url: 视频URL

        返回:
            dict: 包含音频文件路径和视频信息的字典，失败返回 None
        """
        video_id = self._extract_video_id(url)
        if not video_id:
            logger.error("[youtube] Failed to extract video ID")
            return None

        # 策略1: 尝试带 cookie 下载
        if self.ytdlp_builder.is_cookie_available():
            logger.info(f"[youtube] Starting download (with cookie): {video_id}")
            result = self._download_with_ytdlp(video_id, use_cookie=True)
            if result:
                return result

            logger.warning(f"[youtube] Download with cookie failed: {video_id}")

            # 检查是否允许降级
            if not self.ytdlp_builder.should_fallback():
                logger.error(f"[youtube] Fallback disabled, download aborted: {video_id}")
                return None

            logger.info(f"[youtube] Falling back to cookie-less download: {video_id}")

        # 策略2: 无 cookie 下载
        logger.info(f"[youtube] Starting download (without cookie): {video_id}")
        return self._download_with_ytdlp(video_id, use_cookie=False)

    def _download_with_ytdlp(self, video_id: str, use_cookie: bool = False) -> dict | None:
        """
        执行 yt-dlp 下载

        参数:
            video_id: YouTube 视频 ID
            use_cookie: 是否使用 cookie

        返回:
            dict: 包含音频文件路径和视频信息的字典，失败返回 None
        """
        temp_dir = None
        try:
            # 创建临时目录（落在当前任务目录下，随任务结束一并清理，不再泄漏到系统 /tmp）
            temp_dir = tempfile.mkdtemp(dir=str(self.temp_manager.get_current_task_dir()))
            output_template = str(Path(temp_dir) / f"{video_id}.%(ext)s")
            output_path = str(Path(temp_dir) / f"{video_id}.mp3")

            # 使用配置构建器获取 yt-dlp 选项
            ydl_opts = self.ytdlp_builder.build_download_opts(
                output_template=output_template,
                use_cookie=use_cookie,
                audio_only=True
            )

            cookie_status = "with cookie" if (use_cookie and self.ytdlp_builder.is_cookie_available()) else "without cookie"
            video_url = f"https://www.youtube.com/watch?v={video_id}"
            logger.info(f"[youtube] yt-dlp downloading ({cookie_status}): {video_url}")

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(video_url, download=True)
                logger.info(f"[youtube] yt-dlp extract info success: {info.get('title', 'Unknown')}")

            # 查找下载的音频文件
            if Path(output_path).exists():
                logger.info(f"[youtube] Audio file generated: {output_path}")
                return {
                    "audio_path": output_path,
                    "video_title": info.get('title', f'youtube_{video_id}'),
                    "author": info.get('channel', '未知作者'),
                    "description": info.get('description', '')
                }

            # 查找其他格式
            for file in Path(temp_dir).glob(f"{video_id}.*"):
                if file.suffix in ['.mp3', '.m4a', '.wav', '.webm', '.opus']:
                    logger.info(f"[youtube] Found audio file: {file}")
                    return {
                        "audio_path": str(file),
                        "video_title": info.get('title', f'youtube_{video_id}'),
                        "author": info.get('channel', '未知作者'),
                        "description": info.get('description', '')
                    }

            logger.error(f"[youtube] Audio file not found after download: {video_id}")
            return None

        except Exception as e:
            error_str = str(e)
            if "403" in error_str or "Forbidden" in error_str:
                logger.error(f"[youtube] yt-dlp download forbidden (403)")
            elif "Sign in" in error_str or "age" in error_str.lower():
                logger.error(f"[youtube] Video requires authentication or age verification")
            else:
                logger.error(f"[youtube] yt-dlp download error: {e}")
            return None
