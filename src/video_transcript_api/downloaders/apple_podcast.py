import os
import re
import time
import requests
from .base import BaseDownloader
from .models import VideoMetadata, DownloadInfo
from ..utils.logging import setup_logger

# 创建日志记录器
logger = setup_logger("apple_podcast_downloader")

# iTunes Lookup API：根据节目 ID 批量返回剧集列表（含音频直链），无需鉴权
# 参考 podpull 项目的实现思路：https://github.com/xiaoleiy/podpull
ITUNES_LOOKUP_URL = "https://itunes.apple.com/lookup"

# iTunes Lookup 单次返回的剧集数量上限（API 硬限制，无分页参数）
ITUNES_LOOKUP_LIMIT = 200

# 部分播客 CDN 会对可识别的 bot UA 返回 403，统一伪装成普通浏览器
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 音频扩展名白名单（与小宇宙下载器保持一致）
AUDIO_EXTENSIONS = (".mp3", ".m4a", ".wav", ".ogg", ".aac")


class ApplePodcastDownloader(BaseDownloader):
    """
    Apple Podcast 下载器

    通过 iTunes Lookup API 解析剧集音频直链和元数据：
    1. 从 URL 提取节目 ID（idNNN）和剧集 ID（?i=NNN）
    2. 请求 lookup?id=<节目ID>&entity=podcastEpisode&limit=200
    3. 在返回结果中按 trackId 匹配剧集，取 episodeUrl 作为音频直链

    限制：iTunes Lookup 最多返回最近 200 集，更早的剧集无法解析
    （会抛出带 RSS feed 地址提示的异常，用户可改用音频直链提交）。
    """

    def __init__(self):
        super().__init__()
        self._cached_video_info: dict[str, dict] = {}
        # 音频下载也带浏览器 UA：部分播客 CDN 对 python-requests 默认 UA 返回 403
        self.download_headers = {"User-Agent": USER_AGENT}

    def can_handle(self, url):
        """
        判断是否可以处理该URL

        参数:
            url: 播客URL

        返回:
            bool: 是否可以处理
        """
        return "podcasts.apple.com" in url

    def _extract_ids(self, url):
        """
        从URL中提取节目 ID 和剧集 ID

        Apple Podcast 剧集链接格式：
        https://podcasts.apple.com/<国家>/podcast/<slug>/id<节目ID>?i=<剧集ID>

        参数:
            url: 播客URL

        返回:
            tuple: (show_id, episode_id)

        异常:
            ValueError: URL 中缺少节目 ID 或剧集 ID（如节目主页链接）
        """
        show_match = re.search(r'/id(\d+)', url)
        episode_match = re.search(r'[?&]i=(\d+)', url)

        if not show_match:
            logger.error(f"无法从URL中提取Apple Podcast节目ID: {url}")
            raise ValueError(f"无法从URL中提取Apple Podcast节目ID: {url}")

        if not episode_match:
            logger.error(f"URL缺少剧集参数(?i=)，无法定位具体剧集: {url}")
            raise ValueError(
                "Apple Podcast 链接缺少剧集参数（?i=剧集ID）。"
                "请在 Apple Podcast 中打开具体某一集，分享该剧集的链接后重新提交。"
            )

        show_id = show_match.group(1)
        episode_id = episode_match.group(1)
        logger.info(f"从URL中提取到Apple Podcast节目ID: {show_id}, 剧集ID: {episode_id}")
        return show_id, episode_id

    @staticmethod
    def _extract_country(url):
        """
        从URL中提取商店地区代码（如 us/cn/gb）

        iTunes Lookup 不传 country 时默认按美区目录返回，
        非美区独占节目可能查不到，因此透传链接中的地区段。

        参数:
            url: 播客URL

        返回:
            str | None: 两位地区代码，URL 中没有时返回 None
        """
        match = re.search(r'podcasts\.apple\.com/([a-z]{2})/', url, re.IGNORECASE)
        return match.group(1).lower() if match else None

    def extract_video_id(self, url):
        """
        从URL中提取视频ID的公共方法（与其他下载器保持一致）

        参数:
            url: 播客URL

        返回:
            str: 剧集ID
        """
        _, episode_id = self._extract_ids(url)
        return episode_id

    def _lookup_episodes(self, show_id, country=None):
        """
        请求 iTunes Lookup API，返回节目及其最近剧集列表

        参数:
            show_id: Apple Podcast 节目ID
            country: 商店地区代码（如 "cn"），None 时 API 默认按美区处理

        返回:
            list: lookup 结果列表（首条为节目记录，其余为剧集记录）
        """
        params = {
            "id": show_id,
            "entity": "podcastEpisode",
            "limit": ITUNES_LOOKUP_LIMIT,
        }
        if country:
            params["country"] = country
        headers = {"User-Agent": USER_AGENT}

        logger.info(f"请求iTunes Lookup API: show_id={show_id}")
        response = requests.get(
            ITUNES_LOOKUP_URL, params=params, headers=headers, timeout=30
        )
        response.raise_for_status()

        results = response.json().get("results", [])
        if not results:
            logger.error(f"iTunes Lookup未返回任何结果: show_id={show_id}")
            raise ValueError(f"iTunes Lookup 未找到节目（show_id={show_id}），请检查链接是否有效")

        return results

    @staticmethod
    def _detect_audio_ext(audio_url, itunes_ext):
        """
        推断音频文件扩展名

        优先使用 iTunes 返回的 episodeFileExtension 字段，
        其次从音频 URL 路径探测，默认 .mp3。

        参数:
            audio_url: 音频直链
            itunes_ext: iTunes 返回的 episodeFileExtension（如 "mp3"，可能为空）

        返回:
            str: 带点的扩展名（如 ".mp3"）
        """
        if itunes_ext:
            candidate = f".{itunes_ext.lower().lstrip('.')}"
            if candidate in AUDIO_EXTENSIONS:
                return candidate

        audio_path = audio_url.split("?")[0]
        if "." in audio_path.split("/")[-1]:
            detected_ext = os.path.splitext(audio_path.split("/")[-1])[1].lower()
            if detected_ext in AUDIO_EXTENSIONS:
                return detected_ext

        return ".mp3"

    def get_video_info(self, url):
        """
        获取播客剧集信息

        参数:
            url: 播客URL

        返回:
            dict: 包含播客信息的字典
        """
        try:
            show_id, episode_id = self._extract_ids(url)

            if episode_id in self._cached_video_info:
                logger.debug(f"[实例缓存命中] 使用缓存的视频信息: {episode_id}")
                return self._cached_video_info[episode_id]

            results = self._lookup_episodes(show_id, self._extract_country(url))

            # 首条结果为节目记录（wrapperType=track, kind=podcast），用于兜底信息
            show_record = next(
                (r for r in results if r.get("kind") == "podcast"), {}
            )

            # 按 trackId 匹配目标剧集
            episode = next(
                (
                    r for r in results
                    if r.get("kind") == "podcast-episode"
                    and str(r.get("trackId")) == episode_id
                ),
                None,
            )

            if episode is None or not episode.get("episodeUrl"):
                feed_url = show_record.get("feedUrl", "")
                feed_hint = f"，可尝试从节目 RSS 获取音频直链后提交：{feed_url}" if feed_url else ""
                logger.error(
                    f"剧集不在iTunes Lookup返回的最近{ITUNES_LOOKUP_LIMIT}集内: "
                    f"show_id={show_id}, episode_id={episode_id}"
                )
                raise ValueError(
                    f"该剧集不在节目最近 {ITUNES_LOOKUP_LIMIT} 集内（iTunes API 限制）{feed_hint}"
                )

            audio_url = episode["episodeUrl"]
            video_title = (episode.get("trackName") or "").strip() or f"apple_podcast_{episode_id}"
            # 播客场景下"作者"取节目名（与小宇宙下载器语义一致），兜底用主播名
            author = (
                (episode.get("collectionName") or "").strip()
                or (show_record.get("artistName") or "").strip()
                or "未知作者"
            )
            description = (
                episode.get("description") or episode.get("shortDescription") or ""
            ).strip()

            # 时长：iTunes 返回毫秒，部分剧集可能缺失
            duration = None
            track_time_millis = episode.get("trackTimeMillis")
            if track_time_millis:
                duration = track_time_millis / 1000.0

            audio_ext = self._detect_audio_ext(
                audio_url, episode.get("episodeFileExtension")
            )
            filename = f"apple_podcast_{episode_id}_{int(time.time())}{audio_ext}"

            result = {
                "video_id": episode_id,
                "video_title": video_title,
                "author": author,
                "description": description,
                "duration": duration,
                "download_url": audio_url,
                "filename": filename,
                "platform": "apple_podcast",
            }

            self._cached_video_info[episode_id] = result
            logger.info(f"成功获取Apple Podcast剧集信息: ID={episode_id}, 标题={video_title}")
            return result

        except Exception as e:
            logger.exception(f"获取Apple Podcast剧集信息异常: {str(e)}")
            raise

    def get_subtitle(self, url):
        """
        获取字幕，播客通常没有字幕，返回None

        参数:
            url: 播客URL

        返回:
            None: 播客无字幕
        """
        return None

    def _fetch_metadata(self, url: str, video_id: str) -> VideoMetadata:
        info = self.get_video_info(url)
        return VideoMetadata(
            video_id=info.get("video_id", video_id),
            platform=info.get("platform", "apple_podcast"),
            title=info.get("video_title", ""),
            author=info.get("author", ""),
            description=info.get("description", ""),
            duration=info.get("duration"),
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
