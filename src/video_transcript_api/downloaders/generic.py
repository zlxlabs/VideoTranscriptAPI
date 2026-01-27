import os
import mimetypes
import hashlib
import requests
from urllib.parse import urlparse, unquote
from .base import BaseDownloader
from .models import VideoMetadata, DownloadInfo
from ..utils.logging import setup_logger
import datetime

# 创建日志记录器
logger = setup_logger("generic_downloader")

class GenericDownloader(BaseDownloader):
    """
    通用下载器，用于处理直接的音视频下载链接
    """
    
    def __init__(self):
        """
        初始化通用下载器
        """
        super().__init__()
        self._cached_video_info: dict[str, dict] = {}
        # 支持的音视频扩展名
        self.supported_audio_extensions = {'.mp3', '.wav', '.m4a', '.aac', '.ogg', '.flac', '.wma'}
        self.supported_video_extensions = {'.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm', '.m4v'}
        self.supported_extensions = self.supported_audio_extensions | self.supported_video_extensions

        # 初始化临时文件目录
        temp_dir_config = self.config.get("storage", {}).get("temp_dir", "./data/temp")
        self.temp_dir = os.path.abspath(temp_dir_config)
        # 确保临时目录存在
        os.makedirs(self.temp_dir, exist_ok=True)
        
    def can_handle(self, url):
        """
        判断是否可以处理该URL
        通用下载器作为兜底，可以处理任何URL
        
        参数:
            url: 视频URL
            
        返回:
            bool: 总是返回True作为兜底处理器
        """
        return True
    
    def _is_media_url(self, url):
        """
        检查URL是否直接指向媒体文件
        
        参数:
            url: 文件URL
            
        返回:
            bool: 是否是媒体文件URL
        """
        try:
            parsed_url = urlparse(url)
            path = unquote(parsed_url.path.lower())
            
            # 检查URL路径中的文件扩展名
            _, ext = os.path.splitext(path)
            if ext in self.supported_extensions:
                return True
            
            # 尝试HEAD请求获取Content-Type
            try:
                response = requests.head(url, allow_redirects=True, timeout=10)
                content_type = response.headers.get('Content-Type', '').lower()
                
                # 检查Content-Type是否是音视频类型
                if any(media_type in content_type for media_type in ['audio/', 'video/']):
                    return True
            except:
                pass
                
            return False
        except Exception as e:
            logger.error(f"检查媒体URL失败: {str(e)}")
            return False
    
    def get_video_info(self, url):
        """
        获取视频信息
        对于通用下载器，只返回基本信息
        
        参数:
            url: 视频URL
            
        返回:
            dict: 包含视频信息的字典
        """
        logger.info(f"通用下载器处理URL: {url}")

        try:
            cache_id = self.extract_video_id(url)
            if cache_id in self._cached_video_info:
                logger.debug(f"[实例缓存命中] 使用缓存的视频信息: {cache_id}")
                return self._cached_video_info[cache_id]
        except Exception:
            cache_id = None
        
        # 检查是否是直接的媒体文件链接
        if self._is_media_url(url):
            logger.info(f"检测到直接媒体文件链接: {url}")
            
            # 从URL中尝试提取文件名
            parsed_url = urlparse(url)
            path = unquote(parsed_url.path)
            filename = os.path.basename(path)
            
            # 如果没有文件名或文件名不合法，生成一个
            if not filename or not any(filename.endswith(ext) for ext in self.supported_extensions):
                # 根据时间戳生成文件名
                timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                # 尝试从HEAD请求获取文件类型
                ext = '.mp4'  # 默认扩展名
                try:
                    response = requests.head(url, allow_redirects=True, timeout=10)
                    content_type = response.headers.get('Content-Type', '').lower()
                    # 根据Content-Type确定扩展名
                    if 'audio/mpeg' in content_type or 'audio/mp3' in content_type:
                        ext = '.mp3'
                    elif 'audio/mp4' in content_type or 'audio/m4a' in content_type:
                        ext = '.m4a'
                    elif 'audio/' in content_type:
                        ext = '.mp3'  # 默认音频格式
                    elif 'video/' in content_type:
                        ext = '.mp4'  # 默认视频格式
                except:
                    pass
                filename = f"generic_{timestamp}{ext}"
            
            # 返回视频信息
            result = {
                "video_title": "",  # 留空，后续由LLM生成
                "author": "",
                "description": "",
                "download_url": url,
                "filename": filename,
                "platform": "generic",
                "video_id": self.extract_video_id(url),
                "is_generic": True  # 标记为通用下载
            }
            if cache_id:
                self._cached_video_info[cache_id] = result
            return result
        else:
            # 对于非直接媒体链接，尝试作为网页处理
            logger.warning(f"URL不是直接媒体文件链接，尝试作为网页处理: {url}")
            
            # 这里可以添加网页解析逻辑，尝试从网页中提取媒体链接
            # 目前暂时返回错误
            raise ValueError(f"无法处理该URL，不是有效的媒体文件链接: {url}")
    
    def get_subtitle(self, url):
        """
        获取字幕
        通用下载器不支持字幕
        
        参数:
            url: 视频URL
            
        返回:
            None
        """
        return None
    
    def extract_video_id(self, url):
        """
        提取视频ID
        对于通用URL，使用URL哈希作为ID
        
        参数:
            url: 视频URL
            
        返回:
            str: 视频ID
        """
        return hashlib.md5(url.encode()).hexdigest()[:16]
    
    def download_file(self, url, filename):
        """
        下载文件到本地（增强版，支持大文件和断点续传）
        
        参数:
            url: 文件URL
            filename: 本地文件名
            
        返回:
            str: 本地文件路径，如果下载失败则返回None
        """
        local_path = os.path.join(self.temp_dir, filename)
        
        # 创建目录（如果不存在）
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        
        # 最大重试次数
        max_retries = 3
        chunk_size = 1024 * 1024  # 1MB 块大小
        
        # 尝试导入企微通知器
        try:
            # 使用包内绝对导入，避免重复加载模块导致全局实例被初始化两次
            from ..utils.notifications import WechatNotifier
            wechat_notifier = WechatNotifier()
        except:
            wechat_notifier = None
        
        for attempt in range(max_retries):
            try:
                logger.info(f"开始下载文件 (尝试 {attempt + 1}/{max_retries}): {url}")
                
                # 检查是否已有部分下载的文件
                resume_header = {}
                initial_pos = 0
                
                if os.path.exists(local_path):
                    initial_pos = os.path.getsize(local_path)
                    if initial_pos > 0:
                        resume_header['Range'] = f'bytes={initial_pos}-'
                        logger.info(f"检测到部分下载文件，从 {initial_pos} 字节处续传")
                
                # 发起请求
                try:
                    response = requests.get(
                        url,
                        headers=resume_header,
                        stream=True,
                        timeout=(30, 300)  # 连接超时30秒，读取超时300秒
                    )
                    response.raise_for_status()
                except requests.exceptions.HTTPError as e:
                    # 处理 416 Range Not Satisfiable 错误（服务器不支持断点续传）
                    if e.response.status_code == 416:
                        logger.warning(f"服务器不支持断点续传 (416)，删除部分文件重新下载: {local_path}")
                        if os.path.exists(local_path):
                            os.remove(local_path)
                            logger.info("已删除部分下载文件，准备重新下载")
                        # 重新发起请求（不带 Range header）
                        response = requests.get(
                            url,
                            stream=True,
                            timeout=(30, 300)
                        )
                        response.raise_for_status()
                        initial_pos = 0  # 重置初始位置
                        resume_header = {}  # 清空 resume header
                    else:
                        raise
                
                # 获取文件总大小
                content_length = response.headers.get('content-length')
                if content_length:
                    total_size = int(content_length)
                    if initial_pos > 0:
                        total_size += initial_pos
                    logger.info(f"文件总大小: {total_size / (1024*1024):.2f} MB")
                
                # 打开文件进行写入
                mode = 'ab' if initial_pos > 0 else 'wb'
                with open(local_path, mode) as f:
                    downloaded = initial_pos
                    last_log_time = datetime.datetime.now()
                    
                    for chunk in response.iter_content(chunk_size=chunk_size):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            
                            # 每10秒打印一次进度
                            now = datetime.datetime.now()
                            if (now - last_log_time).seconds >= 10:
                                if content_length:
                                    progress = (downloaded / total_size) * 100
                                    progress_msg = f"下载进度: {progress:.1f}% ({downloaded / (1024*1024):.2f}/{total_size / (1024*1024):.2f} MB)"
                                    logger.info(progress_msg)
                                    
                                    # 对于大文件（>20MB），每30%进度发送企微通知
                                    if (total_size > 20 * 1024 * 1024 and 
                                        wechat_notifier and 
                                        progress % 30 < 10 and 
                                        progress > 10):
                                        try:
                                            wechat_notifier.send_text(f"【文件下载进度】\n链接: {url[:50]}...\n{progress_msg}")
                                        except:
                                            pass  # 通知失败不影响下载
                                else:
                                    logger.info(f"已下载: {downloaded / (1024*1024):.2f} MB")
                                last_log_time = now
                
                # 验证文件完整性
                final_size = os.path.getsize(local_path)
                if content_length and final_size != total_size:
                    logger.warning(f"文件大小不匹配: 期望 {total_size}, 实际 {final_size}")
                    # 不删除文件，下次重试时会续传
                    continue
                
                logger.info(f"文件下载成功: {local_path} (大小: {final_size / (1024*1024):.2f} MB)")
                return local_path
                
            except requests.exceptions.ChunkedEncodingError as e:
                logger.warning(f"分块编码错误 (尝试 {attempt + 1}/{max_retries}): {str(e)}")
                if attempt < max_retries - 1:
                    logger.info("将尝试断点续传...")
                    continue
                    
            except requests.exceptions.ConnectionError as e:
                logger.warning(f"连接错误 (尝试 {attempt + 1}/{max_retries}): {str(e)}")
                if attempt < max_retries - 1:
                    logger.info("将尝试重新连接...")
                    continue
                    
            except requests.exceptions.Timeout as e:
                logger.warning(f"下载超时 (尝试 {attempt + 1}/{max_retries}): {str(e)}")
                if attempt < max_retries - 1:
                    logger.info("将尝试重新下载...")
                    continue
                    
            except Exception as e:
                logger.error(f"下载异常 (尝试 {attempt + 1}/{max_retries}): {str(e)}")
                if attempt < max_retries - 1:
                    continue
                    
        # 所有重试都失败了
        logger.error(f"文件下载失败，已尝试 {max_retries} 次: {url}")
        
        # 清理不完整的文件
        if os.path.exists(local_path):
            try:
                os.remove(local_path)
                logger.info("已清理不完整的下载文件")
            except:
                pass
                
        return None

    def _fetch_metadata(self, url: str, video_id: str) -> VideoMetadata:
        info = self.get_video_info(url)
        return VideoMetadata(
            video_id=info.get("video_id", video_id),
            platform=info.get("platform", "generic"),
            title=info.get("video_title", ""),
            author=info.get("author", ""),
            description=info.get("description", ""),
            extra={"is_generic": True},
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
