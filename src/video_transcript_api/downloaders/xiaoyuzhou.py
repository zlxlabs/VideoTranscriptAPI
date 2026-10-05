import os
import re
import json
import time
import requests
from bs4 import BeautifulSoup
from .base import BaseDownloader
from .models import VideoMetadata, DownloadInfo
from ..utils.logging import setup_logger

# 创建日志记录器
logger = setup_logger("xiaoyuzhou_downloader")

class XiaoyuzhouDownloader(BaseDownloader):
    """
    小宇宙播客下载器
    """
    def __init__(self):
        super().__init__()
        self._cached_video_info: dict[str, dict] = {}

    def can_handle(self, url):
        """
        判断是否可以处理该URL
        
        参数:
            url: 播客URL
            
        返回:
            bool: 是否可以处理
        """
        return "xiaoyuzhoufm.com" in url
    
    def _extract_episode_id(self, url):
        """
        从URL中提取播客剧集ID
        
        参数:
            url: 播客URL
            
        返回:
            str: 剧集ID
        """
        # 匹配 /episode/ 后面的ID
        pattern = r'/episode/([a-f0-9]+)'
        match = re.search(pattern, url)
        if match:
            episode_id = match.group(1)
            logger.info(f"从URL中提取到小宇宙播客剧集ID: {episode_id}")
            return episode_id
        
        logger.error(f"无法从URL中提取小宇宙播客剧集ID: {url}")
        raise ValueError(f"无法从URL中提取小宇宙播客剧集ID: {url}")
    
    def extract_video_id(self, url):
        """
        从URL中提取视频ID的公共方法（与其他下载器保持一致）
        
        参数:
            url: 播客URL
            
        返回:
            str: 剧集ID
        """
        return self._extract_episode_id(url)
    
    def get_video_info(self, url):
        """
        获取播客信息
        
        参数:
            url: 播客URL
            
        返回:
            dict: 包含播客信息的字典
        """
        try:
            # 提取剧集ID
            episode_id = self._extract_episode_id(url)

            if episode_id in self._cached_video_info:
                logger.debug(f"[实例缓存命中] 使用缓存的视频信息: {episode_id}")
                return self._cached_video_info[episode_id]
            
            # 请求网页内容
            logger.info(f"访问小宇宙播客页面: {url}")
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
            
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            response.encoding = 'utf-8'
            
            # 解析HTML
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 提取标题、作者和描述
            video_title = None
            author = "未知作者"
            description = ""
            
            title_tag = soup.find('title')
            if title_tag:
                full_title = title_tag.get_text().strip()
                logger.info(f"完整标题: {full_title}")
                
                # 解析格式：标题 - 作者 | 小宇宙 - 听播客，上小宇宙
                # 找到最后一个 "| 小宇宙" 的位置，然后向前找 "-"
                xiaoyuzhou_index = full_title.rfind('| 小宇宙')
                if xiaoyuzhou_index != -1:
                    # 获取 "| 小宇宙" 之前的部分
                    before_xiaoyuzhou = full_title[:xiaoyuzhou_index].strip()
                    
                    # 找到最后一个 " - " 的位置
                    last_dash_index = before_xiaoyuzhou.rfind(' - ')
                    if last_dash_index != -1:
                        video_title = before_xiaoyuzhou[:last_dash_index].strip()
                        author = before_xiaoyuzhou[last_dash_index + 3:].strip()
                        logger.info(f"从title提取到播客标题: {video_title}")
                        logger.info(f"从title提取到播客作者: {author}")
                    else:
                        # 如果没有找到 " - "，整个内容作为标题
                        video_title = before_xiaoyuzhou
                        logger.info(f"从title提取到播客标题（无作者）: {video_title}")
                else:
                    # 如果无法解析，尝试使用og:title
                    og_title_meta = soup.find('meta', {'property': 'og:title'})
                    if og_title_meta:
                        video_title = og_title_meta.get('content', '').strip()
                        logger.info(f"从og:title提取到播客标题: {video_title}")
                    
                    if not video_title:
                        # 移除小宇宙的网站后缀作为备用方案
                        video_title = re.sub(r'\s*-\s*.*小宇宙.*$', '', full_title).strip()
                        logger.info(f"使用备用方案提取标题: {video_title}")
            
            if not video_title:
                video_title = f"xiaoyuzhou_{episode_id}"
                logger.warning(f"未找到播客标题，使用ID作为标题: {video_title}")
            
            # 提取描述信息
            # 优先从 schema:podcast-show script 标签获取，因为它包含更详细的内容
            script_tag = soup.find('script', {'name': 'schema:podcast-show', 'type': 'application/ld+json'})
            if script_tag:
                try:
                    schema_data = json.loads(script_tag.string)
                    description = schema_data.get('description', '').strip()
                    if description:
                        logger.info(f"从schema:podcast-show提取到播客描述: {description[:100]}...")
                except Exception as e:
                    logger.warning(f"解析schema:podcast-show失败: {str(e)}")
            
            # 如果没有从 schema 中获取到，再尝试从 meta 标签获取
            if not description:
                desc_meta = soup.find('meta', {'name': 'description'})
                if not desc_meta:
                    desc_meta = soup.find('meta', {'property': 'og:description'})
                
                if desc_meta:
                    description = desc_meta.get('content', '').strip()
                    logger.info(f"从meta标签提取到播客描述: {description[:100]}...")
            
            # 提取音频下载地址
            audio_url = None
            og_audio_meta = soup.find('meta', {'property': 'og:audio'})
            if og_audio_meta:
                audio_url = og_audio_meta.get('content')
                logger.info(f"提取到音频下载地址: {audio_url[:50]}...")
            
            if not audio_url:
                logger.error(f"无法从页面中提取音频下载地址: {url}")
                raise ValueError(f"无法从页面中提取音频下载地址")
            
            # 从音频URL提取实际扩展名（小宇宙音频可能是.mp3或.m4a等）
            audio_ext = ".m4a"  # 默认扩展名
            audio_path = audio_url.split("?")[0]  # 去掉查询参数
            if "." in audio_path.split("/")[-1]:
                detected_ext = os.path.splitext(audio_path.split("/")[-1])[1].lower()
                if detected_ext in (".mp3", ".m4a", ".wav", ".ogg", ".aac"):
                    audio_ext = detected_ext
                    logger.info(f"从音频URL检测到扩展名: {audio_ext}")

            safe_title = re.sub(r'[\\/*?:"<>|]', "_", video_title)
            filename = f"xiaoyuzhou_{episode_id}_{int(time.time())}{audio_ext}"
            
            result = {
                "video_id": episode_id,
                "video_title": video_title,
                "author": author,
                "description": description,
                "download_url": audio_url,
                "filename": filename,
                "platform": "xiaoyuzhou"
            }

            self._cached_video_info[episode_id] = result
            logger.info(f"成功获取小宇宙播客信息: ID={episode_id}")
            return result
            
        except Exception as e:
            logger.exception(f"获取小宇宙播客信息异常: {str(e)}")
            raise
    
    def get_subtitle(self, url):
        """
        获取字幕，播客通常没有字幕，返回None
        
        参数:
            url: 播客URL
            
        返回:
            str: 字幕文本，播客通常返回None
        """
        # 播客通常没有字幕，直接返回None
        return None

    def _fetch_metadata(self, url: str, video_id: str) -> VideoMetadata:
        info = self.get_video_info(url)
        return VideoMetadata(
            video_id=info.get("video_id", video_id),
            platform=info.get("platform", "xiaoyuzhou"),
            title=info.get("video_title", ""),
            author=info.get("author", ""),
            description=info.get("description", ""),
        )

    def _fetch_download_info(self, url: str, video_id: str) -> DownloadInfo:
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
