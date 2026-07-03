from .douyin import DouyinDownloader
from .bilibili import BilibiliDownloader
from .xiaohongshu import XiaohongshuDownloader
from .youtube import YoutubeDownloader
from .xiaoyuzhou import XiaoyuzhouDownloader
from .apple_podcast import ApplePodcastDownloader
from .generic import GenericDownloader
from .media_resolver import MediaResolverDownloader
from ..utils.logging import setup_logger, load_config

# 创建日志记录器
logger = setup_logger("downloader_factory")


def _use_media_resolver() -> bool:
    """读取配置开关 downloaders.use_media_resolver（默认 off）。"""
    try:
        config = load_config()
        return bool(config.get("downloaders", {}).get("use_media_resolver", False))
    except Exception as e:
        logger.warning(f"读取 use_media_resolver 配置失败，按 off 处理: {e}")
        return False


def create_downloader(url):
    """
    根据URL创建对应的下载器

    参数:
        url: 视频URL

    返回:
        BaseDownloader的子类实例，通用下载器作为兜底

    路由说明（#2-A）:
        use_media_resolver=on 时，MediaResolverDownloader 排在抖音/小红书下载器之前，
        且不实例化旧的 DouyinDownloader/XiaohongshuDownloader，避免双重命中；
        其余平台（B站/YouTube/小宇宙）不受影响。off 时走旧路径。
    """
    use_resolver = _use_media_resolver()

    # 平台特定的下载器（顺序即优先级，取第一个 can_handle=True）
    platform_downloaders = []
    if use_resolver:
        # resolver 接管抖音/小红书，排在最前；旧两个下载器迁移期不实例化
        platform_downloaders.append(MediaResolverDownloader())
        platform_downloaders.extend([
            BilibiliDownloader(),
            YoutubeDownloader(),
            XiaoyuzhouDownloader(),
            ApplePodcastDownloader(),
        ])
    else:
        platform_downloaders.extend([
            DouyinDownloader(),
            BilibiliDownloader(),
            XiaohongshuDownloader(),
            YoutubeDownloader(),
            XiaoyuzhouDownloader(),
            ApplePodcastDownloader(),
        ])

    # 先尝试平台特定的下载器
    for downloader in platform_downloaders:
        if downloader.can_handle(url):
            logger.info(f"为URL创建下载器: {url}, 类型: {downloader.__class__.__name__}")
            return downloader

    # 如果没有匹配的平台下载器，使用通用下载器作为兜底
    logger.info(f"使用通用下载器处理URL: {url}")
    return GenericDownloader()
