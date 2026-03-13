import os
import re
import json
import time
import datetime
import subprocess
import platform
import shutil
import requests
from .base import BaseDownloader, get_temp_manager
from .models import VideoMetadata, DownloadInfo
from ..utils.logging import setup_logger
from ..utils import create_debug_dir

logger = setup_logger("bilibili_downloader")
DEBUG_DIR = create_debug_dir()


class BilibiliDownloader(BaseDownloader):
    """
    Bilibili视频下载器
    """
    def __init__(self):
        super().__init__()
        self._cached_video_info: dict[str, dict] = {}
        self._cached_metadata: dict[str, dict] = {}  # 缓存官方API的元数据

    def can_handle(self, url):
        """
        判断是否可以处理该URL

        参数:
            url: 视频URL

        返回:
            bool: 是否可以处理
        """
        return "bilibili.com" in url or "b23.tv" in url

    def _extract_video_id(self, url):
        """
        从URL中提取视频ID (BV号)

        参数:
            url: 视频URL

        返回:
            str: 视频BV号
        """
        # 解析短链接
        if "b23.tv" in url:
            url = self.resolve_short_url(url)

        # 提取BV号
        match = re.search(r"BV(\w+)", url)
        if match:
            return f"BV{match.group(1)}"

        logger.error(f"无法从URL中提取Bilibili视频BV号: {url}")
        raise ValueError(f"无法从URL中提取Bilibili视频BV号: {url}")

    def _extract_page_number(self, url):
        """
        从URL中提取分P号

        参数:
            url: 视频URL，可能包含 ?p=X 或 &p=X 参数

        返回:
            int: 分P号，默认为1
        """
        # 匹配 ?p=X 或 &p=X
        match = re.search(r"[?&]p=(\d+)", url)
        if match:
            page_num = int(match.group(1))
            logger.info(f"从URL中提取到分P号: {page_num}")
            return page_num
        return 1

    def extract_video_id(self, url):
        """
        从URL中提取视频ID的公共方法

        参数:
            url: 视频URL
        返回:
            str: 视频ID
        """
        return self._extract_video_id(url)

    def _fetch_bilibili_official_metadata(self, bvid: str) -> dict:
        """
        调用Bilibili官方API获取视频元数据

        这个方法用于获取视频的完整元数据，包括标题、简介、作者等信息。
        API 无需登录即可访问公开视频的信息。

        参数:
            bvid: 视频的BV号（如 BV1zW2vB2Ey2）

        返回:
            dict: 包含以下字段的字典：
                - title (str): 视频标题
                - description (str): 视频简介
                - author (str): 作者昵称
                - author_id (int): 作者mid
                - duration (int): 视频时长（秒）
                - pubdate (int): 发布时间戳
            如果获取失败，返回空字典
        """
        # 检查实例级缓存
        if bvid in self._cached_metadata:
            logger.debug(f"[实例缓存命中] 使用缓存的B站官方元数据: {bvid}")
            return self._cached_metadata[bvid]

        api_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"

        # 构造请求头，模拟浏览器访问
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": f"https://www.bilibili.com/video/{bvid}",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

        try:
            logger.info(f"调用B站官方API获取元数据: bvid={bvid}")
            response = requests.get(api_url, headers=headers, timeout=5.0)
            response.raise_for_status()

            data = response.json()

            # 检查API返回状态
            if data.get("code") != 0:
                logger.warning(
                    f"B站官方API返回错误: code={data.get('code')}, "
                    f"message={data.get('message', '未知错误')}"
                )
                return {}

            # 提取视频数据
            video_data = data.get("data", {})
            if not video_data:
                logger.warning(f"B站官方API返回数据为空: bvid={bvid}")
                return {}

            # 构造元数据字典
            metadata = {
                "title": video_data.get("title", ""),
                "description": video_data.get("desc", ""),
                "author": video_data.get("owner", {}).get("name", ""),
                "author_id": video_data.get("owner", {}).get("mid", ""),
                "duration": video_data.get("duration", 0),
                "pubdate": video_data.get("pubdate", 0),
            }

            logger.info(
                f"成功获取B站官方元数据: 标题='{metadata['title']}', "
                f"作者='{metadata['author']}', "
                f"简介长度={len(metadata['description'])} 字符"
            )

            # 缓存结果（实例级缓存）
            self._cached_metadata[bvid] = metadata

            return metadata

        except requests.exceptions.Timeout:
            logger.warning(f"调用B站官方API超时: bvid={bvid}")
            return {}
        except requests.exceptions.RequestException as e:
            logger.warning(f"调用B站官方API网络异常: {e}")
            return {}
        except json.JSONDecodeError as e:
            logger.warning(f"解析B站官方API响应失败: {e}")
            return {}
        except Exception as e:
            logger.error(f"获取B站官方元数据时发生未知错误: {e}")
            return {}

    def _get_video_info_bbdown(self, url):
        """
        使用BBDown获取视频信息并下载

        参数:
            url: 视频URL

        返回:
            dict: 包含视频信息的字典
        """
        try:
            # 提取视频BV号
            bv_id = self._extract_video_id(url)
            logger.info(f"使用BBDown下载Bilibili视频: bv_id={bv_id}, url={url}")

            # 确定BBDown可执行文件路径
            bbdown_config = self.config.get("bbdown", {})
            system_platform = platform.system().lower()

            # 获取当前工作目录
            current_dir = os.path.abspath(os.getcwd())

            if system_platform == "windows":
                bbdown_path = bbdown_config.get("executable", "BBDown/BBDown.exe")
            elif system_platform == "darwin":
                # macOS
                bbdown_path = bbdown_config.get("executable_mac", "BBDown/BBDown_Mac")
            else:
                # Linux
                bbdown_path = bbdown_config.get("executable_linux", "BBDown/BBDown")

            # 将相对路径转换为绝对路径
            if not os.path.isabs(bbdown_path):
                bbdown_path = os.path.join(current_dir, bbdown_path)

            # 检查BBDown可执行文件是否存在
            if not os.path.exists(bbdown_path):
                logger.error(f"BBDown可执行文件不存在: {bbdown_path}")
                raise FileNotFoundError(f"BBDown可执行文件不存在: {bbdown_path}")

            audio_only = bbdown_config.get("audio_only", True)

            temp_dir = self.temp_manager.create_temp_dir(prefix=f"bbdown_{bv_id}_")

            # 提取分P号
            page_num = self._extract_page_number(url)

            # 统一使用列表形式执行命令（避免 shell=True 带来的命令注入风险）
            if system_platform == "windows":
                download_args = [bbdown_path, url, "-p", str(page_num)]
                if audio_only:
                    download_args.append("--audio-only")

                logger.info(f"执行BBDown命令: {' '.join(download_args)}")
                timeout = bbdown_config.get("timeout", 300)

                try:
                    process = subprocess.run(
                        download_args,
                        cwd=temp_dir,
                        shell=False,
                        check=True,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=timeout,
                    )

                    # 记录BBDown输出
                    logger.debug(f"BBDown输出: {process.stdout}")
                    if process.stderr:
                        logger.warning(f"BBDown错误输出: {process.stderr}")

                except subprocess.CalledProcessError as e:
                    logger.error(f"BBDown执行失败: {str(e)}")
                    logger.error(f"BBDown输出: {e.stdout}")
                    logger.error(f"BBDown错误: {e.stderr}")
                    raise ValueError(f"BBDown执行失败: {str(e)}")
                except subprocess.TimeoutExpired as e:
                    logger.error(f"BBDown执行超时: {str(e)}")
                    raise ValueError(f"BBDown执行超时，超过{timeout}秒")
            else:
                # 在Linux/macOS系统下使用列表参数执行命令
                download_args = [bbdown_path, url, "-p", str(page_num)]
                if audio_only:
                    download_args.append("--audio-only")

                logger.info(f"执行BBDown命令: {' '.join(download_args)}")
                timeout = bbdown_config.get("timeout", 300)

                try:
                    process = subprocess.run(
                        download_args,
                        cwd=temp_dir,
                        shell=False,
                        check=True,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",  # 显式指定UTF-8编码
                        errors="replace",  # 替换无法解码的字符
                        timeout=timeout,
                    )

                    # 记录BBDown输出
                    logger.debug(f"BBDown输出: {process.stdout}")
                    if process.stderr:
                        logger.warning(f"BBDown错误输出: {process.stderr}")

                except subprocess.CalledProcessError as e:
                    logger.error(f"BBDown执行失败: {str(e)}")
                    logger.error(f"BBDown输出: {e.stdout}")
                    logger.error(f"BBDown错误: {e.stderr}")
                    raise ValueError(f"BBDown执行失败: {str(e)}")
                except subprocess.TimeoutExpired as e:
                    logger.error(f"BBDown执行超时: {str(e)}")
                    raise ValueError(f"BBDown执行超时，超过{timeout}秒")

            # 查找下载的文件
            downloaded_files = []
            for root, dirs, files in os.walk(temp_dir):
                for file in files:
                    if file.endswith((".mp3", ".m4a", ".mp4")):
                        downloaded_files.append(os.path.join(root, file))

            if not downloaded_files:
                logger.error(f"BBDown下载成功，但找不到下载的文件: {temp_dir}")
                raise ValueError(f"BBDown下载成功，但找不到下载的文件")

            # 找到最新的文件
            latest_file = max(downloaded_files, key=os.path.getctime)
            file_ext = os.path.splitext(latest_file)[1][1:]  # 获取扩展名，去除前面的点
            logger.info(f"BBDown下载的文件: {latest_file}")

            # 从文件名提取视频标题
            file_basename = os.path.basename(latest_file)

            # 提取视频标题的逻辑
            # 首先尝试匹配 BBDown 的标准输出格式 "[BVxxx]视频标题.扩展名"
            video_title_match = re.search(
                r"\[(BV\w+)\](.*)\." + file_ext, file_basename
            )
            if video_title_match:
                video_title = video_title_match.group(2).strip()
                logger.info(f"从标准BBDown文件名格式提取到标题: {video_title}")
            else:
                # 如果标准格式匹配失败，直接使用文件名（去除扩展名）作为标题
                video_title = os.path.splitext(file_basename)[0]
                logger.info(f"从文件名直接提取标题: {video_title}")

                # 如果文件名为空或只包含空白字符，则使用默认值
                if not video_title or video_title.strip() == "":
                    video_title = f"bilibili_{bv_id}"
                    logger.warning(f"文件名为空，使用默认值作为标题: {video_title}")

            ext = os.path.splitext(latest_file)[1] if "." in latest_file else ".tmp"
            target_path = self.temp_manager.create_temp_file(suffix=ext)
            shutil.move(latest_file, target_path)

            result = {
                "video_id": bv_id,
                "video_title": video_title,
                "author": "",
                "download_url": None,
                "filename": os.path.basename(target_path),
                "local_file": str(target_path),
                "platform": "bilibili",
                "downloaded": True,
            }

            logger.info(
                f"成功使用BBDown下载Bilibili视频: ID={bv_id}, 标题={video_title}"
            )
            return result

        except Exception as e:
            logger.exception(f"使用BBDown获取视频信息异常: {str(e)}")
            raise

    def _get_video_info_api(self, url):
        """
        使用API获取视频信息（原方法）

        参数:
            url: 视频URL

        返回:
            dict: 包含视频信息的字典
        """
        try:
            # 提取视频BV号
            bv_id = self._extract_video_id(url)

            # 调用API获取视频信息
            endpoint = f"/api/v1/bilibili/web/fetch_one_video"
            params = {"bv_id": bv_id}

            logger.info(f"调用TikHub API获取Bilibili视频信息: bv_id={bv_id}")
            response = self.make_api_request(endpoint, params)

            # 生成时间戳前缀
            timestamp_prefix = datetime.datetime.now().strftime("%y%m%d-%H%M%S")

            # 记录API响应摘要，帮助调试
            if isinstance(response, dict):
                response_code = response.get("code")
                response_msg = response.get("message", "无消息")
                logger.info(f"API响应状态: {response_code}, 消息: {response_msg}")

                # 保存完整响应到文件，用于调试
                debug_file = os.path.join(
                    DEBUG_DIR, f"{timestamp_prefix}_debug_bilibili_{bv_id}.json"
                )
                with open(debug_file, "w", encoding="utf-8") as f:
                    json.dump(response, f, ensure_ascii=False, indent=2)
                logger.debug(f"API完整响应已保存到: {debug_file}")

            # 检查响应格式并提供详细错误信息
            if not isinstance(response, dict):
                logger.error(f"API返回格式错误，预期字典，实际: {type(response)}")
                raise ValueError("API返回格式错误，无法解析响应")

            # TikHub API成功响应时返回code=200
            if response.get("code") != 200:
                error_msg = response.get("message", "未知错误")
                logger.error(
                    f"API返回错误代码: {response.get('code')}, 错误信息: {error_msg}"
                )

                # 保存错误响应到文件
                error_file = os.path.join(
                    DEBUG_DIR, f"{timestamp_prefix}_error_bilibili_{bv_id}.json"
                )
                with open(error_file, "w", encoding="utf-8") as f:
                    json.dump(response, f, ensure_ascii=False, indent=2)
                logger.debug(f"错误响应已保存到: {error_file}")

                raise ValueError(f"获取Bilibili视频信息失败: {error_msg}")

            # 检查data字段
            if not response.get("data") or not isinstance(response.get("data"), dict):
                logger.error("API响应中缺少data字段或格式不正确")
                raise ValueError("API响应数据格式错误，缺少必要字段")

            # 提取必要信息
            data = response.get("data", {}).get("data", {})

            if not data:
                logger.error("无法获取视频详情数据")

                # 保存错误响应到文件
                error_file = os.path.join(
                    DEBUG_DIR, f"{timestamp_prefix}_error_data_bilibili_{bv_id}.json"
                )
                with open(error_file, "w", encoding="utf-8") as f:
                    json.dump(response, f, ensure_ascii=False, indent=2)

                logger.debug(
                    f"API完整响应: {json.dumps(response, ensure_ascii=False)[:500]}..."
                )
                raise ValueError("获取视频详情失败，API返回数据结构不符合预期")

            # 视频标题
            video_title = data.get("title", "")
            if not video_title or video_title.strip() == "":
                video_title = f"bilibili_{bv_id}"
                logger.warning(f"未找到视频标题，使用ID作为标题: {video_title}")

            # 视频作者
            author = data.get("owner", {}).get("name", "未知作者")

            logger.info(f"获取到视频信息: 标题='{video_title}', 作者='{author}'")

            # 获取cid
            cid = data.get("cid")
            if not cid:
                logger.error("无法获取Bilibili视频CID")
                raise ValueError(f"无法获取Bilibili视频CID: {url}")

            # 调用API获取视频流地址
            endpoint = f"/api/v1/bilibili/web/fetch_video_playurl"
            params = {"bv_id": bv_id, "cid": cid}

            logger.info(
                f"调用TikHub API获取Bilibili视频播放地址: bv_id={bv_id}, cid={cid}"
            )
            playurl_response = self.make_api_request(endpoint, params)

            # 更新时间戳前缀
            timestamp_prefix = datetime.datetime.now().strftime("%y%m%d-%H%M%S")

            # 记录API响应摘要
            if isinstance(playurl_response, dict):
                response_code = playurl_response.get("code")
                response_msg = playurl_response.get("message", "无消息")
                logger.info(
                    f"播放地址API响应状态: {response_code}, 消息: {response_msg}"
                )

                # 保存完整响应到文件
                debug_file = os.path.join(
                    DEBUG_DIR, f"{timestamp_prefix}_debug_bilibili_playurl_{bv_id}.json"
                )
                with open(debug_file, "w", encoding="utf-8") as f:
                    json.dump(playurl_response, f, ensure_ascii=False, indent=2)
                logger.debug(f"播放地址API完整响应已保存到: {debug_file}")

            # 检查响应格式
            if not isinstance(playurl_response, dict):
                logger.error(
                    f"播放地址API返回格式错误，预期字典，实际: {type(playurl_response)}"
                )
                raise ValueError("播放地址API返回格式错误，无法解析响应")

            # 检查响应状态
            if playurl_response.get("code") != 200:
                error_msg = playurl_response.get("message", "未知错误")
                logger.error(
                    f"获取播放地址API返回错误代码: {playurl_response.get('code')}, 错误信息: {error_msg}"
                )

                # 保存错误响应到文件
                error_file = os.path.join(
                    DEBUG_DIR, f"{timestamp_prefix}_error_bilibili_playurl_{bv_id}.json"
                )
                with open(error_file, "w", encoding="utf-8") as f:
                    json.dump(playurl_response, f, ensure_ascii=False, indent=2)
                logger.debug(f"播放地址错误响应已保存到: {error_file}")

                raise ValueError(f"获取Bilibili视频播放地址失败: {error_msg}")

            # 提取音频下载地址
            playurl_data = playurl_response.get("data", {}).get("data", {})

            if not playurl_data:
                logger.error("播放地址API响应中缺少data.data字段")

                # 保存错误响应到文件
                error_file = os.path.join(
                    DEBUG_DIR,
                    f"{timestamp_prefix}_error_playurl_data_bilibili_{bv_id}.json",
                )
                with open(error_file, "w", encoding="utf-8") as f:
                    json.dump(playurl_response, f, ensure_ascii=False, indent=2)

                raise ValueError("播放地址API响应数据格式错误，缺少必要字段")

            # 尝试获取音频下载地址
            download_url = None
            file_ext = "mp4"  # 默认扩展名

            if playurl_data.get("dash") and playurl_data["dash"].get("audio"):
                audio_list = playurl_data["dash"]["audio"]
                if audio_list and len(audio_list) > 0:
                    download_url = audio_list[0].get("baseUrl")
                    file_ext = "m4s"  # B站音频格式通常为m4s
                    logger.info(f"找到音频下载URL: {download_url[:50]}...")

            if not download_url:
                logger.error("无法获取Bilibili视频下载地址")

                # 保存错误数据到文件
                error_file = os.path.join(
                    DEBUG_DIR,
                    f"{timestamp_prefix}_error_no_download_url_bilibili_{bv_id}.json",
                )
                with open(error_file, "w", encoding="utf-8") as f:
                    json.dump(playurl_data, f, ensure_ascii=False, indent=2)

                raise ValueError(f"无法获取Bilibili视频下载地址: {url}")

            # 清理文件名中的非法字符
            safe_title = re.sub(r'[\\/*?:"<>|]', "_", video_title)
            filename = f"bilibili_{bv_id}_{int(time.time())}.{file_ext}"

            result = {
                "video_id": bv_id,
                "cid": cid,
                "video_title": video_title,
                "author": author,
                "download_url": download_url,
                "filename": filename,
                "platform": "bilibili",
                "downloaded": False,  # 标记为未下载
            }

            logger.info(f"成功获取Bilibili视频信息: ID={bv_id}, 文件类型={file_ext}")
            return result

        except Exception as e:
            logger.exception(f"获取Bilibili视频信息异常: {str(e)}")
            raise

    def get_video_info(self, url):
        """
        获取视频信息

        参数:
            url: 视频URL

        返回:
            dict: 包含视频信息的字典
        """
        try:
            bv_id = self._extract_video_id(url)
            if bv_id in self._cached_video_info:
                logger.debug(f"[实例缓存命中] 使用缓存的视频信息: {bv_id}")
                return self._cached_video_info[bv_id]
        except Exception:
            bv_id = None

        # 判断是否使用BBDown下载
        use_bbdown = self.config.get("bbdown", {}).get("use_bbdown", False)

        if use_bbdown:
            logger.info("使用BBDown下载Bilibili视频")
            result = self._get_video_info_bbdown(url)
        else:
            logger.info("使用API获取Bilibili视频信息")
            result = self._get_video_info_api(url)

        if bv_id:
            self._cached_video_info[bv_id] = result
        return result

    def download_file(self, url, filename):
        """
        下载文件到本地

        参数:
            url: 文件URL或本地文件路径
            filename: 本地文件名

        返回:
            str: 本地文件路径，如果下载失败则返回None
        """
        # 检查是否已经是本地文件
        if isinstance(url, str) and os.path.exists(url):
            logger.info(f"文件已存在于本地: {url}")
            return url

        # 调用父类方法下载文件
        return super().download_file(url, filename)

    def get_subtitle(self, url):
        """
        获取字幕，B站API目前不支持直接获取字幕，返回None

        参数:
            url: 视频URL

        返回:
            str: 字幕文本，B站API目前返回None
        """
        # 直接返回None，跳过尝试获取字幕步骤
        return None

    def _fetch_metadata(self, url: str, video_id: str) -> VideoMetadata:
        """
        获取视频元数据

        优先使用B站官方API获取完整元数据（包括description），
        然后用下载器API/BBDown的结果补充或覆盖某些字段。

        参数:
            url: 视频URL
            video_id: 视频ID（BV号）

        返回:
            VideoMetadata: 标准化的视频元数据对象
        """
        # 1. 首先尝试从官方API获取完整元数据
        official_metadata = self._fetch_bilibili_official_metadata(video_id)

        # 2. 获取下载器信息（TikHub API 或 BBDown）
        info = self.get_video_info(url)

        # 3. 合并元数据，优先使用官方API的数据
        # 如果官方API获取失败，则使用下载器提供的数据作为回退
        title = official_metadata.get("title") or info.get("video_title", "")
        author = official_metadata.get("author") or info.get("author", "")
        description = official_metadata.get("description", "")  # description 只能从官方API获取
        duration = official_metadata.get("duration")

        # 构造 extra 字段
        extra = {}
        if "cid" in info:
            extra["cid"] = info.get("cid")
        if official_metadata.get("author_id"):
            extra["author_id"] = official_metadata.get("author_id")
        if official_metadata.get("pubdate"):
            extra["pubdate"] = official_metadata.get("pubdate")

        logger.info(
            f"合并元数据完成: 标题='{title[:30]}...', "
            f"作者='{author}', "
            f"简介={'有' if description else '无'}"
        )

        return VideoMetadata(
            video_id=info.get("video_id", video_id),
            platform=info.get("platform", "bilibili"),
            title=title,
            author=author,
            description=description,
            duration=duration,
            extra=extra,
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
            local_file=info.get("local_file"),
            downloaded=bool(info.get("downloaded")),
        )
