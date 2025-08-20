"""
YouTube采集模块
"""
import re
import asyncio
import json
import xml.etree.ElementTree as ET
from typing import Dict, Any, List, Optional
from datetime import datetime
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import TranscriptsDisabled, VideoUnavailable, IpBlocked, NoTranscriptFound
import yt_dlp
import httpx
from loguru import logger
import feedparser
from pathlib import Path
import tempfile
import aiofiles

from .base import BaseAcquisition
from ..utils.text_processor import TextProcessor


class YouTubeAcquisition(BaseAcquisition):
    """YouTube采集器"""
    
    def __init__(self, settings: 'Settings'):
        super().__init__(settings)
        self.timeout = settings.config.get("polling_config", {}).get("request_timeout", 30)
        
        # 初始化 YouTube Transcript API
        self.ytt_api = YouTubeTranscriptApi()
        
        # TikHub API 配置
        self.tikhub_api_key = settings.tikhub_api_key
        self.tikhub_base_url = "https://api.tikhub.io"
        
        # yt-dlp配置
        self.ydl_opts = {
            'format': 'bestaudio/best',
            'quiet': True,
            'no_warnings': True,
            'extract_flat': True,
            'no_check_certificate': True,
            'ignoreerrors': True
        }
        
        # 初始化文本处理器
        self.text_processor = TextProcessor(settings)
        
    async def fetch(self, subscription: Dict[str, Any]) -> List[Dict[str, Any]]:
        """获取YouTube内容"""
        url = subscription.get("url", "")
        
        try:
            # 提取channel ID
            channel_id = self._extract_channel_id(url)
            if not channel_id:
                logger.error(f"无法提取channel ID: {url}")
                return []
                
            # 构建RSS URL
            rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
            
            # 获取RSS内容
            logger.debug(f"开始获取RSS: {rss_url}")
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(rss_url)
                response.raise_for_status()
                logger.debug(f"RSS响应状态: {response.status_code}, 内容长度: {len(response.text)}")
                
            # 解析RSS
            logger.debug("开始解析RSS内容")
            feed = feedparser.parse(response.text)
            logger.debug(f"RSS解析完成，条目数: {len(feed.entries)}")
            
            # 处理视频条目
            contents = []
            max_items = self.settings.config.get("polling_config", {}).get("max_items_per_fetch", 10)
            for entry in feed.entries[:max_items]:  # 处理最新的指定数量视频
                try:
                    content_item = await self.parse_content(entry)
                    content_item["subscription_id"] = subscription.get("id")
                    
                    # 获取视频文本内容
                    video_text = await self._get_video_transcript(
                        content_item["video_id"],
                        video_title=content_item.get("title")
                    )
                    
                    # 只有成功获取到文本内容才添加到结果中
                    if video_text and video_text.strip():
                        # 处理文本以提升可读性
                        # 从订阅信息获取语言代码
                        language_code = subscription.get("language", "auto")
                        # 转换语言代码：cn -> zh
                        if language_code == "cn":
                            language_code = "zh"
                        elif language_code not in ["zh", "en"]:
                            language_code = "auto"
                            
                        processed_text = await self.text_processor.process_text(
                            video_text,
                            video_title=content_item.get("title"),
                            mode='subtitle',  # 字幕模式
                            force_local=False,  # 优先使用LLM处理
                            language=language_code  # 传递语言信息
                        )
                        
                        content_item["content"] = processed_text
                        content_item["raw_content"] = video_text  # 保留原始文本
                        content_item["content_type"] = "text"  # 将视频字幕/转录作为文本内容处理
                        
                        # 添加 YouTube 特有的元数据
                        content_item["metadata"] = {
                            "source_type": "youtube",
                            "video_id": content_item["video_id"],
                            "transcript_length": len(video_text),
                            "channel_name": subscription.get("name", ""),
                            "video_url": content_item["url"]
                        }
                        
                        contents.append(content_item)
                        logger.debug(f"YouTube视频 {content_item['video_id']} 处理完成，文本长度: {len(video_text)}")
                    else:
                        logger.warning(f"YouTube视频 {content_item['video_id']} 未能获取到文本内容，跳过处理")
                    
                except Exception as e:
                    error_msg = str(e) if e else "Unknown error"
                    error_type = type(e).__name__
                    logger.error(f"处理YouTube视频失败: {error_type}: {error_msg}")
                    # 记录更多调试信息
                    logger.debug(f"视频处理异常详情: {repr(e)}")
                    continue
                    
            logger.info(f"从 {subscription.get('name')} 获取了 {len(contents)} 个视频")
            return contents
            
        except Exception as e:
            await self.handle_error(subscription, e)
            return []
            
    async def parse_content(self, entry: Any) -> Dict[str, Any]:
        """解析YouTube RSS条目"""
        # 提取视频ID
        video_id = entry.get("yt_videoid", "")
        if not video_id:
            # 从URL提取
            link = entry.get("link", "")
            match = re.search(r"v=([a-zA-Z0-9_-]+)", link)
            if match:
                video_id = match.group(1)
                
        # 解析发布时间
        published_date = None
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            published_date = datetime(*entry.published_parsed[:6])
            
        title = entry.get("title", "")
        url = entry.get("link", "")
        
        return {
            "title": title,
            "url": url,
            "original_title": title,  # 调度器期望的字段名
            "original_url": url,      # 调度器期望的字段名
            "video_id": video_id,
            "author": entry.get("author", ""),
            "published_date": published_date,
            "description": entry.get("summary", ""),
            "raw_entry": entry
        }
        
    def _extract_channel_id(self, url: str) -> Optional[str]:
        """从YouTube URL提取channel ID"""
        # 处理不同格式的YouTube频道URL
        patterns = [
            r"youtube\.com/channel/([a-zA-Z0-9_-]+)",
            r"youtube\.com/c/([a-zA-Z0-9_-]+)",
            r"youtube\.com/user/([a-zA-Z0-9_-]+)",
            r"youtube\.com/@([a-zA-Z0-9_-]+)"
        ]
        
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                channel_identifier = match.group(1)
                
                # 如果是channel ID格式，直接返回
                if pattern.startswith(r"youtube\.com/channel/"):
                    return channel_identifier
                    
                # 否则需要获取实际的channel ID
                # 这里简化处理，实际项目中可能需要调用YouTube API
                logger.warning(f"需要将 {channel_identifier} 转换为channel ID")
                return None
                
        return None
        
    async def _get_video_transcript(self, video_id: str, video_title: Optional[str] = None) -> str:
        """获取视频字幕或转录文本"""
        if not video_id:
            logger.warning("视频ID为空，无法获取字幕")
            return ""
            
        logger.debug(f"开始获取视频 {video_id} 的文本内容")
            
        try:
            # 首先尝试获取字幕
            logger.debug(f"尝试获取视频 {video_id} 的字幕")
            transcript = await self._fetch_youtube_transcript(video_id)
            if transcript and transcript.strip():
                # 检查是否是IP被阻止的标记
                if transcript == "IP_BLOCKED":
                    logger.info(f"检测到IP被阻止，尝试使用TikHub API获取视频 {video_id} 的字幕")
                    tikhub_subtitle = await self._get_subtitle_with_tikhub_api(video_id)
                    if tikhub_subtitle and tikhub_subtitle.strip():
                        logger.info(f"TikHub API字幕获取成功，长度: {len(tikhub_subtitle)} 字符")
                        return tikhub_subtitle
                elif transcript == "TRANSCRIPTS_DISABLED":
                    # 字幕被禁用，直接进入下载流程，无需再尝试TikHub API
                    logger.info(f"视频 {video_id} 字幕已被禁用，直接进入音频下载流程")
                else:
                    logger.info(f"视频 {video_id} 字幕获取成功，长度: {len(transcript)} 字符")
                    return transcript
            else:
                logger.info(f"视频 {video_id} 字幕获取失败或内容为空")
                
                # 如果直接获取字幕失败，尝试使用TikHub API
                logger.info(f"尝试使用TikHub API获取视频 {video_id} 的字幕")
                tikhub_subtitle = await self._get_subtitle_with_tikhub_api(video_id)
                if tikhub_subtitle and tikhub_subtitle.strip():
                    logger.info(f"TikHub API字幕获取成功，长度: {len(tikhub_subtitle)} 字符")
                    return tikhub_subtitle
                else:
                    logger.info(f"TikHub API也无法获取字幕")
                
            # 如果没有字幕，下载音频并转录
            logger.info(f"视频 {video_id} 没有字幕，尝试下载音频转录")
            audio_path = await self._download_audio(video_id)
            
            if audio_path:
                logger.info(f"视频 {video_id} 音频下载成功: {audio_path}")
                # 调用ASR服务转录
                transcript = await self._transcribe_audio(audio_path, video_title)
                
                # 清理临时文件
                Path(audio_path).unlink(missing_ok=True)
                logger.debug(f"清理临时音频文件: {audio_path}")
                
                if transcript and transcript.strip():
                    logger.info(f"视频 {video_id} 音频转录成功，长度: {len(transcript)} 字符")
                    return transcript
                else:
                    logger.warning(f"视频 {video_id} 音频转录失败或内容为空")
            else:
                logger.error(f"视频 {video_id} 音频下载失败")
                
        except Exception as e:
            logger.error(f"获取视频文本失败 {video_id}: {e}")
            
        logger.warning(f"视频 {video_id} 无法获取任何文本内容")
        return ""
        
    async def _fetch_youtube_transcript(self, video_id: str) -> Optional[str]:
        """获取YouTube字幕（使用新版本API）"""
        try:
            # 在异步环境中运行同步代码
            loop = asyncio.get_event_loop()
            
            def get_transcript():
                try:
                    # 使用新版本API检查可用字幕列表
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
                    # 标记需要使用TikHub API
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
                            else:
                                logger.warning(f"视频 {video_id} 的 {lang} 字幕内容为空")
                                
                        except IpBlocked:
                            logger.error(f"IP 被 YouTube 阻止，无法获取视频 {video_id} 的 {lang} 字幕")
                            # 标记需要使用TikHub API
                            return "IP_BLOCKED"
                        except NoTranscriptFound:
                            logger.debug(f"视频 {video_id} 没有 {lang} 字幕")
                            continue
                        except Exception as e:
                            error_str = str(e)
                            if "no element found" in error_str or "line 1, column 0" in error_str:
                                logger.warning(f"视频 {video_id} 的 {lang} 字幕数据损坏或为空: {e}")
                            else:
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
                            # 使用属性访问而不是字典访问
                            text_parts.append(item.text.strip())
                        
                        result = ' '.join(text_parts)
                        if result.strip():
                            logger.info(f"成功获取视频 {video_id} 的 {first_lang} 字幕 (备选)，长度: {len(result)} 字符")
                            return result
                        else:
                            logger.warning(f"视频 {video_id} 的 {first_lang} 字幕 (备选) 内容为空")
                            
                    except IpBlocked:
                        logger.error(f"IP 被 YouTube 阻止，无法获取视频 {video_id} 的备选字幕")
                        # 标记需要使用TikHub API
                        return "IP_BLOCKED"
                    except Exception as e:
                        error_str = str(e)
                        if "no element found" in error_str or "line 1, column 0" in error_str:
                            logger.error(f"视频 {video_id} 的备选字幕数据损坏或为空: {e}")
                        else:
                            logger.error(f"获取视频 {video_id} 的备选字幕失败: {e}")
                
                logger.info(f"视频 {video_id} 虽然有字幕列表但无法获取有效内容，可能是 YouTube Shorts 或数据损坏")
                return None
                
            transcript = await loop.run_in_executor(None, get_transcript)
            return transcript
            
        except Exception as e:
            logger.error(f"获取字幕失败 {video_id}: {e}")
            return None
            
    async def _download_audio(self, video_id: str) -> Optional[str]:
        """下载视频音频"""
        try:
            # 创建临时目录
            temp_dir = tempfile.mkdtemp()
            output_path = str(Path(temp_dir) / f"{video_id}.mp3")
            
            # 配置yt-dlp，使用与客户端类似的配置
            ydl_opts = {
                # 使用与客户端相同的格式选择策略
                'format': 'bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio/best',
                'outtmpl': output_path.replace('.mp3', '.%(ext)s'),
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
                # 启用更详细的日志以便调试
                'quiet': False,
                'no_warnings': False,
                'verbose': True,
                # 使用更新的User-Agent
                'http_headers': {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Accept-Language': 'en-us,en;q=0.5',
                    'Accept-Encoding': 'gzip, deflate',
                    'Accept-Charset': 'ISO-8859-1,utf-8;q=0.7,*;q=0.7'
                },
                # 启用extractor参数，模拟客户端行为
                'extractor_args': {
                    'youtube': {
                        'player_client': ['tv', 'android', 'ios', 'web'],
                        'player_skip': [],
                        'skip': []
                    }
                },
                # 重试配置
                'retries': 10,
                'fragment_retries': 10,
                'skip_unavailable_fragments': False,
                # 网络配置
                'socket_timeout': 30,
                # 不检查SSL证书（某些环境下需要）
                'nocheckcertificate': True,
                # 使用原生下载器处理HLS流
                'hls_prefer_native': True,
                # 启用进度条
                'progress': True,
                'noprogress': False,
            }
            
            # 下载音频
            loop = asyncio.get_event_loop()
            
            def download():
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        logger.info(f"开始使用yt-dlp下载: https://www.youtube.com/watch?v={video_id}")
                        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
                        logger.info(f"yt-dlp提取信息成功: {info.get('title', 'Unknown')}")
                        return True
                except Exception as e:
                    logger.error(f"yt-dlp下载异常: {e}")
                    raise
                    
            success = await loop.run_in_executor(None, download)
            
            if not success:
                return None
            
            # 检查文件是否存在
            if Path(output_path).exists():
                logger.info(f"音频文件已生成: {output_path}")
                return output_path
            else:
                # 查找其他格式
                for file in Path(temp_dir).glob(f"{video_id}.*"):
                    if file.suffix in ['.mp3', '.m4a', '.wav', '.webm', '.opus']:
                        logger.info(f"找到音频文件: {file}")
                        return str(file)
                        
            logger.error(f"未找到下载的音频文件: {video_id}")
            return None
            
        except Exception as e:
            error_str = str(e)
            if "403" in error_str or "Forbidden" in error_str:
                logger.error(f"yt-dlp下载被禁止(403)，尝试使用TikHub API下载音频 {video_id}")
                # 尝试使用TikHub API下载音频
                tikhub_audio = await self._download_audio_with_tikhub_api(video_id)
                if tikhub_audio:
                    return tikhub_audio
            
            logger.error(f"下载音频失败 {video_id}: {e}")
            return None
            
    async def _transcribe_audio(self, audio_path: str, video_title: Optional[str] = None) -> str:
        """转录音频（调用ASR服务）"""
        try:
            from ..utils.asr_client import ASRClient
            
            asr_client = ASRClient(self.settings)
            # 调用ASR时传递视频标题，并启用文本处理
            transcript = await asr_client.transcribe_file(
                audio_path, 
                process_text=True,
                video_title=video_title
            )
            
            if transcript:
                logger.info(f"音频转录成功，长度: {len(transcript)} 字符")
                return transcript
            else:
                logger.warning(f"音频转录失败: {audio_path}")
                return ""
                
        except Exception as e:
            logger.error(f"调用ASR服务失败: {e}")
            return ""
    
    def _extract_video_id(self, url: str) -> Optional[str]:
        """从YouTube URL提取视频ID"""
        # 解析短链接
        if "youtu.be" in url:
            match = re.search(r'youtu\.be/([^?&]+)', url)
            if match:
                return match.group(1)
        
        # 从URL中提取视频ID
        if "youtube.com/watch" in url:
            # 形如 https://www.youtube.com/watch?v=VIDEO_ID
            match = re.search(r'v=([^&]+)', url)
            if match:
                return match.group(1)
        elif "youtu.be/" in url:
            # 形如 https://youtu.be/VIDEO_ID
            match = re.search(r'youtu\.be/([^?&]+)', url)
            if match:
                return match.group(1)
        
        logger.error(f"无法从URL中提取Youtube视频ID: {url}")
        return None
    
    async def _get_subtitle_with_tikhub_api(self, video_id: str) -> Optional[str]:
        """使用TikHub API获取字幕"""
        if not self.tikhub_api_key or self.tikhub_api_key == "tokenxxx":
            logger.info("TikHub API密钥未配置或为默认值，跳过第三方API方法")
            return None
            
        try:
            # 调用TikHub API获取视频信息
            endpoint = f"{self.tikhub_base_url}/api/v1/youtube/web/get_video_info"
            params = {"video_id": video_id}
            headers = {
                "accept": "application/json",
                "Authorization": f"Bearer {self.tikhub_api_key}"
            }
            
            logger.info(f"调用TikHub API获取YouTube视频信息: video_id={video_id}")
            
            # 配置支持重定向的客户端
            async with httpx.AsyncClient(
                timeout=self.timeout, 
                follow_redirects=True
            ) as client:
                response = await client.get(endpoint, params=params, headers=headers)
                response.raise_for_status()
                
                api_response = response.json()
                
                # 检查API响应
                if api_response.get("code") != 200:
                    error_msg = api_response.get("message", "未知错误")
                    logger.error(f"TikHub API返回错误: {error_msg}")
                    return None
                
                data = api_response.get("data", {})
                if not data:
                    logger.error("TikHub API响应中缺少data字段")
                    return None
                
                # 获取字幕信息
                subtitles = data.get("subtitles", {})
                if not subtitles or not subtitles.get("items"):
                    logger.info(f"TikHub API未找到视频 {video_id} 的字幕")
                    return None
                
                subtitle_items = subtitles.get("items", [])
                
                # 优先选择中文字幕，其次是英文字幕
                zh_subtitle = next((item for item in subtitle_items if item.get("code") == "zh"), None)
                en_subtitle = next((item for item in subtitle_items if item.get("code") == "en"), None)
                
                subtitle_info = zh_subtitle or en_subtitle
                
                if not subtitle_info or not subtitle_info.get("url"):
                    logger.info(f"TikHub API未找到可用字幕: {video_id}")
                    return None
                
                # 下载字幕XML（使用已配置重定向的客户端）
                subtitle_url = subtitle_info["url"]
                logger.info(f"从TikHub API下载字幕: {subtitle_url[:50]}...")
                
                try:
                    subtitle_response = await client.get(subtitle_url, timeout=30)
                    subtitle_response.raise_for_status()
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 429:
                        logger.warning(f"TikHub API字幕下载遇到429限流，稍后重试")
                        # 可以在这里添加重试逻辑或直接跳过
                        return None
                    else:
                        raise
                
                xml_content = subtitle_response.text
                
                # 检查是否为空内容
                if not xml_content or xml_content.strip() == "":
                    logger.warning(f"TikHub API返回的字幕内容为空")
                    return None
                
                # 解析XML字幕
                parsed_subtitle = self._parse_youtube_subtitle_xml(xml_content)
                if parsed_subtitle and parsed_subtitle.strip():
                    return parsed_subtitle
                else:
                    logger.warning(f"TikHub API字幕解析后内容为空")
                    return None
                
        except Exception as e:
            logger.exception(f"TikHub API获取字幕失败 {video_id}: {str(e)}")
            return None
    
    def _parse_youtube_subtitle_xml(self, xml_content: str) -> Optional[str]:
        """解析YouTube字幕XML"""
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
    
    async def _download_audio_with_tikhub_api(self, video_id: str) -> Optional[str]:
        """使用TikHub API获取音频下载地址并下载"""
        if not self.tikhub_api_key or self.tikhub_api_key == "tokenxxx":
            logger.info("TikHub API密钥未配置或为默认值，跳过第三方API下载方法")
            return None
            
        try:
            # 调用TikHub API获取视频信息
            endpoint = f"{self.tikhub_base_url}/api/v1/youtube/web/get_video_info"
            params = {"video_id": video_id}
            headers = {
                "accept": "application/json",
                "Authorization": f"Bearer {self.tikhub_api_key}"
            }
            
            logger.info(f"调用TikHub API获取YouTube音频下载地址: video_id={video_id}")
            
            # 配置支持重定向的客户端
            async with httpx.AsyncClient(
                timeout=60, 
                follow_redirects=True
            ) as client:
                # 获取视频信息
                response = await client.get(endpoint, params=params, headers=headers)
                response.raise_for_status()
                
                api_response = response.json()
                
                # 检查API响应
                if api_response.get("code") != 200:
                    error_msg = api_response.get("message", "未知错误")
                    logger.error(f"TikHub API返回错误: {error_msg}")
                    return None
                
                data = api_response.get("data", {})
                if not data:
                    logger.error("TikHub API响应中缺少data字段")
                    return None
                
                # 获取音频下载地址
                audio_items = data.get("audios", {}).get("items", [])
                
                if not audio_items:
                    logger.error(f"TikHub API未找到视频 {video_id} 的音频下载地址")
                    return None
                
                download_url = audio_items[0].get("url")
                if not download_url:
                    logger.error(f"TikHub API音频项目中没有下载URL")
                    return None
                
                # 创建临时文件
                temp_dir = tempfile.mkdtemp()
                output_path = str(Path(temp_dir) / f"{video_id}.m4a")
                
                # 下载音频文件（使用已配置重定向的客户端）
                logger.info(f"从TikHub API下载音频: {download_url[:50]}...")
                
                download_response = await client.get(download_url)
                download_response.raise_for_status()
                
                # 保存文件
                async with aiofiles.open(output_path, 'wb') as f:
                    await f.write(download_response.content)
                
                # 检查文件是否存在
                if Path(output_path).exists():
                    logger.info(f"TikHub API音频下载成功: {output_path}")
                    return output_path
                else:
                    logger.error(f"TikHub API音频下载失败，文件不存在: {output_path}")
                    return None
                
        except Exception as e:
            logger.exception(f"TikHub API下载音频失败 {video_id}: {str(e)}")
            return None