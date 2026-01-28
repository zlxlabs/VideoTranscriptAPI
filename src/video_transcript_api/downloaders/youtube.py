import os
import re
import json
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
        使用 youtube-transcript-api 获取字幕
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
                    
                    # 拼接字幕文本
                    text_parts = []
                    for item in fetched_transcript:
                        # 使用属性访问而不是字典访问
                        text_parts.append(item.text.strip())
                    
                    result = ' '.join(text_parts)
                    if result.strip():
                        logger.info(f"成功获取视频 {video_id} 的 {lang} 字幕，长度: {len(result)} 字符")
                        return result
                        
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
                
                # 拼接字幕文本
                text_parts = []
                for item in fetched_transcript:
                    text_parts.append(item.text.strip())
                
                result = ' '.join(text_parts)
                if result.strip():
                    logger.info(f"成功获取视频 {video_id} 的 {first_lang} 字幕 (备选)，长度: {len(result)} 字符")
                    return result
                    
            except IpBlocked:
                logger.error(f"IP 被 YouTube 阻止")
                return "IP_BLOCKED"
            except Exception as e:
                logger.error(f"获取视频 {video_id} 的备选字幕失败: {e}")
        
        return None
    
    def _get_subtitle_with_tikhub_api(self, url):
        """
        使用原有的 TikHub API 获取字幕作为备用方案

        🆕 优化：复用实例缓存的 video_info，避免重复 TikHub API 请求
        （在同一任务内，get_video_info 通常已被调用）
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
            
            # 解析XML字幕
            return self._parse_youtube_subtitle_xml(xml_content)
        except Exception as e:
            logger.exception(f"TikHub API 获取字幕异常: {str(e)}")
            return None
    
    def _parse_youtube_subtitle_xml(self, xml_content):
        """
        解析YouTube字幕XML
        
        参数:
            xml_content: XML字幕内容
            
        返回:
            str: 解析后的字幕文本
        """
        try:
            root = ET.fromstring(xml_content)
            
            # 提取文本并按时间顺序排序
            texts = []
            for text_element in root.findall(".//text"):
                start = float(text_element.get("start", "0"))
                duration = float(text_element.get("dur", "0"))
                content = text_element.text or ""
                
                texts.append({
                    "start": start,
                    "duration": duration,
                    "content": content.strip()
                })
            
            # 按开始时间排序
            texts.sort(key=lambda x: x["start"])
            
            # 合并字幕文本
            merged_text = ""
            for text in texts:
                if text["content"]:
                    merged_text += text["content"] + " "
            
            logger.info(f"成功解析YouTube字幕，共{len(texts)}段")
            return merged_text.strip()
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
            # 创建临时目录
            temp_dir = tempfile.mkdtemp()
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
