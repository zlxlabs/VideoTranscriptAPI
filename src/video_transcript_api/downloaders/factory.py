from .douyin import DouyinDownloader
from .bilibili import BilibiliDownloader
from .xiaohongshu import XiaohongshuDownloader
from .youtube import YoutubeDownloader
from .xiaoyuzhou import XiaoyuzhouDownloader
from .generic import GenericDownloader
from ..utils import setup_logger

# 创建日志记录器
logger = setup_logger("downloader_factory")

def create_downloader(url):
    """
    根据URL创建对应的下载器
    
    参数:
        url: 视频URL
        
    返回:
        BaseDownloader的子类实例，通用下载器作为兜底
    """
    # 平台特定的下载器
    platform_downloaders = [
        DouyinDownloader(),
        BilibiliDownloader(),
        XiaohongshuDownloader(),
        YoutubeDownloader(),
        XiaoyuzhouDownloader()
    ]
    
    # 先尝试平台特定的下载器
    for downloader in platform_downloaders:
        if downloader.can_handle(url):
            logger.info(f"为URL创建下载器: {url}, 类型: {downloader.__class__.__name__}")
            return downloader
    
    # 如果没有匹配的平台下载器，使用通用下载器作为兜底
    logger.info(f"使用通用下载器处理URL: {url}")
    return GenericDownloader() 