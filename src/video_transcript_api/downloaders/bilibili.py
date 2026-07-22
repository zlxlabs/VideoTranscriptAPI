import os
import re
import json
import time
import uuid
import datetime
import subprocess
import platform
import shutil
import signal
import threading
from collections import deque
import requests
from .base import BaseDownloader, get_temp_manager
from .models import VideoMetadata, DownloadInfo
from ..utils.logging import setup_logger
from ..utils import create_debug_dir

logger = setup_logger("bilibili_downloader")
DEBUG_DIR = create_debug_dir()

# 官方 view API 的风控/瞬时错误码，遇到这些值时重试而非直接放弃
#   -412 请求被拦截（风控）  -799 请求过于频繁  -509 请求过于频繁(过载)
_RETRYABLE_CODES = {-412, -799, -509}
_OFFICIAL_API_MAX_RETRIES = 3      # 官方 API 最大尝试次数
_OFFICIAL_API_BACKOFF_BASE = 0.5   # 指数退避基数（秒）

_BBDOWN_MAX_ATTEMPTS = 3
_BBDOWN_PREFLIGHT_IDLE_SECONDS = 20.0
_BBDOWN_DOWNLOAD_IDLE_SECONDS = 60.0
_BBDOWN_POLL_SECONDS = 1.0
_BBDOWN_RETRY_BACKOFF_SECONDS = 0.5
_BBDOWN_OUTPUT_TAIL_LINES = 8
_BBDOWN_OUTPUT_TAIL_LINE_CHARS = 512


def _generate_buvid3() -> str:
    """生成随机 buvid3 指纹 cookie。

    B 站对「完全无 cookie」的服务器 IP 风控最严，附带一个随机指纹即可显著降低
    被 -412/-799 拦截的概率，且无需真实账号、零维护（区别于 BBDown 登录态 cookie）。
    格式近似官方：<大写 UUID>infoc
    """
    return f"{str(uuid.uuid4()).upper()}infoc"


class BilibiliDownloader(BaseDownloader):
    """
    Bilibili视频下载器
    """
    def __init__(self):
        super().__init__()
        self._cached_video_info: dict[str, dict] = {}
        self._cached_metadata: dict[str, dict] = {}  # 缓存官方API的元数据
        # 实例级 buvid3 指纹，整个任务生命周期复用一个，避免每次请求都换指纹
        self._buvid3 = _generate_buvid3()

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

        # Cookie 策略：优先使用配置的完整 cookie（高级用户/真实账号），
        # 否则附带一个随机 buvid3 指纹以躲避 IP 级风控（零维护）。
        configured_cookie = (
            self.config.get("bbdown", {}).get("bilibili_cookie", "") or ""
        ).strip()
        if configured_cookie:
            headers["Cookie"] = configured_cookie
            cookies = None
        else:
            cookies = {"buvid3": self._buvid3}

        # 重试循环：对超时/网络异常以及风控 code(-412/-799/-509) 做指数退避重试。
        # 注意 B 站风控是 HTTP 200 + body code 异常，状态码级重试无效，必须判 code。
        last_err = None
        for attempt in range(1, _OFFICIAL_API_MAX_RETRIES + 1):
            try:
                logger.info(
                    f"调用B站官方API获取元数据: bvid={bvid} "
                    f"(尝试 {attempt}/{_OFFICIAL_API_MAX_RETRIES})"
                )
                response = requests.get(
                    api_url, headers=headers, cookies=cookies, timeout=5.0
                )
                response.raise_for_status()

                data = response.json()
                code = data.get("code")

                # 检查API返回状态
                if code != 0:
                    msg = data.get("message", "未知错误")
                    if code in _RETRYABLE_CODES and attempt < _OFFICIAL_API_MAX_RETRIES:
                        backoff = _OFFICIAL_API_BACKOFF_BASE * (2 ** (attempt - 1))
                        logger.warning(
                            f"B站官方API风控(code={code}, message={msg})，"
                            f"{backoff:.1f}s 后重试"
                        )
                        time.sleep(backoff)
                        continue
                    logger.warning(
                        f"B站官方API返回错误: code={code}, message={msg}"
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

            except (requests.exceptions.Timeout, requests.exceptions.RequestException) as e:
                last_err = e
                if attempt < _OFFICIAL_API_MAX_RETRIES:
                    backoff = _OFFICIAL_API_BACKOFF_BASE * (2 ** (attempt - 1))
                    logger.warning(
                        f"调用B站官方API异常({e})，{backoff:.1f}s 后重试"
                    )
                    time.sleep(backoff)
                    continue
                logger.warning(
                    f"调用B站官方API失败，已重试 {attempt} 次: {e}"
                )
                return {}
            except json.JSONDecodeError as e:
                logger.warning(f"解析B站官方API响应失败: {e}")
                return {}
            except Exception as e:
                logger.error(f"获取B站官方元数据时发生未知错误: {e}")
                return {}

        # 理论上不会到这里（循环内均有 return），保险返回空
        if last_err:
            logger.warning(f"调用B站官方API最终失败: {last_err}")
        return {}

    def _get_video_info_bbdown(self, url):
        """
        使用BBDown获取视频信息并下载

        参数:
            url: 视频URL

        返回:
            dict: 包含视频信息的字典
        """
        resolved_url = self.resolve_short_url(url) if "b23.tv" in url else url
        bv_id = self._extract_video_id(resolved_url)
        canonical_url = f"https://www.bilibili.com/video/{bv_id}"
        logger.info(f"使用BBDown下载Bilibili视频: bv_id={bv_id}, url={canonical_url}")

        bbdown_config = self.config.get("bbdown", {})
        system_platform = platform.system().lower()
        executable_key = (
            "executable" if system_platform == "windows"
            else "executable_mac" if system_platform == "darwin"
            else "executable_linux"
        )
        executable_default = (
            "BBDown/BBDown.exe" if system_platform == "windows"
            else "BBDown/BBDown_Mac" if system_platform == "darwin"
            else "BBDown/BBDown"
        )
        bbdown_path = bbdown_config.get(executable_key, executable_default)
        if not os.path.isabs(bbdown_path):
            bbdown_path = os.path.join(os.path.abspath(os.getcwd()), bbdown_path)
        if not os.path.exists(bbdown_path):
            raise FileNotFoundError(f"BBDown可执行文件不存在: {bbdown_path}")

        timeout = float(bbdown_config.get("timeout", 300))
        if timeout <= 0:
            raise ValueError("BBDown超时预算必须大于0秒")
        page_num = self._extract_page_number(resolved_url)
        download_args = [
            bbdown_path,
            canonical_url,
            "-p", str(page_num),
            "--skip-subtitle",
            "--skip-cover",
            "--skip-ai",
        ]
        if bbdown_config.get("audio_only", True):
            download_args.append("--audio-only")

        deadline = time.monotonic() + timeout
        last_stage = "not_started"
        last_output = ""
        attempts_started = 0

        for attempt in range(1, _BBDOWN_MAX_ATTEMPTS + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                last_stage = "total_budget_exhausted"
                break

            attempts_started = attempt
            temp_dir = str(self.temp_manager.create_temp_dir(prefix=f"bbdown_{bv_id}_"))
            output_tail = deque(maxlen=_BBDOWN_OUTPUT_TAIL_LINES)
            activity = [time.monotonic()]
            process = None
            reader_threads = []
            stage = "launch"

            try:
                popen_kwargs = {
                    "cwd": temp_dir,
                    "shell": False,
                    "stdin": subprocess.DEVNULL,
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.PIPE,
                }
                if system_platform == "windows":
                    popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                else:
                    popen_kwargs["start_new_session"] = True

                logger.info(
                    f"执行BBDown命令 (尝试 {attempt}/{_BBDOWN_MAX_ATTEMPTS}): "
                    f"{' '.join(download_args)}"
                )
                process = subprocess.Popen(download_args, **popen_kwargs)
                for stream in (process.stdout, process.stderr):
                    thread = threading.Thread(
                        target=self._collect_bbdown_output,
                        args=(stream, output_tail, activity),
                        daemon=True,
                    )
                    thread.start()
                    reader_threads.append(thread)

                previous_files = self._bbdown_file_signature(temp_dir)
                while True:
                    now = time.monotonic()
                    returncode = process.poll()
                    current_files = self._bbdown_file_signature(temp_dir)
                    if current_files != previous_files:
                        previous_files = current_files
                        activity[0] = now

                    has_download_progress = any(size > 0 for size, _ in current_files.values())
                    idle_limit = (
                        _BBDOWN_DOWNLOAD_IDLE_SECONDS if has_download_progress
                        else _BBDOWN_PREFLIGHT_IDLE_SECONDS
                    )
                    if returncode is not None:
                        break
                    if now >= deadline:
                        stage = "total_budget_exhausted"
                        self._terminate_bbdown_process(
                            process, max(0.0, deadline - now)
                        )
                        break
                    if now - activity[0] >= idle_limit:
                        stage = (
                            "download_stalled" if has_download_progress
                            else "preflight_stalled"
                        )
                        self._terminate_bbdown_process(
                            process, max(0.0, deadline - now)
                        )
                        break
                    time.sleep(min(_BBDOWN_POLL_SECONDS, deadline - now))

                returncode = process.poll()
                if stage in {"preflight_stalled", "download_stalled", "total_budget_exhausted"}:
                    pass
                elif returncode != 0:
                    stage = f"process_exit_{returncode}"
                else:
                    media_files = self._bbdown_media_files(temp_dir)
                    if not media_files:
                        stage = "media_missing"
                    else:
                        latest_file = max(media_files, key=os.path.getmtime)
                        if os.path.getsize(latest_file) <= 0:
                            stage = "media_empty"
                        elif not self._validate_media_file(latest_file):
                            stage = "media_invalid"
                        else:
                            result = self._build_bbdown_result(latest_file, bv_id)
                            shutil.rmtree(temp_dir, ignore_errors=True)
                            self._untrack_bbdown_temp_dir(temp_dir)
                            logger.info(
                                f"成功使用BBDown下载Bilibili视频: ID={bv_id}, "
                                f"标题={result['video_title']}"
                            )
                            return result
            except Exception as exc:
                stage = f"launch_or_monitor_error:{type(exc).__name__}"
                output_tail.append(str(exc)[-_BBDOWN_OUTPUT_TAIL_LINE_CHARS:])
                if process is not None:
                    self._terminate_bbdown_process(process)
            finally:
                for thread in reader_threads:
                    thread.join(timeout=0.1)
                last_stage = stage
                last_output = "".join(output_tail).strip()
                shutil.rmtree(temp_dir, ignore_errors=True)
                self._untrack_bbdown_temp_dir(temp_dir)

            if attempt < _BBDOWN_MAX_ATTEMPTS and deadline - time.monotonic() > 0:
                time.sleep(min(_BBDOWN_RETRY_BACKOFF_SECONDS, deadline - time.monotonic()))

        output_hint = f", output={last_output!r}" if last_output else ""
        raise ValueError(
            f"BBDown下载失败: attempt={attempts_started}, stage={last_stage}{output_hint}"
        )

    @staticmethod
    def _bbdown_file_signature(temp_dir):
        """返回尝试目录文件的大小与 mtime，用于检测下载进展。"""
        signature = {}
        for root, _, files in os.walk(temp_dir):
            for filename in files:
                path = os.path.join(root, filename)
                try:
                    stat = os.stat(path)
                    signature[path] = (stat.st_size, stat.st_mtime_ns)
                except OSError:
                    continue
        return signature

    @staticmethod
    def _bbdown_media_files(temp_dir):
        """查找 BBDown 已生成的候选媒体文件。"""
        files = []
        for root, _, filenames in os.walk(temp_dir):
            for filename in filenames:
                if filename.lower().endswith((".mp3", ".m4a", ".mp4")):
                    files.append(os.path.join(root, filename))
        return files

    @staticmethod
    def _collect_bbdown_output(stream, output_tail, activity):
        """读取有限输出尾部，同时把输出视为进程活动。"""
        if stream is None:
            return
        try:
            for line in iter(stream.readline, b""):
                text = line.decode("utf-8", errors="replace")
                output_tail.append(text[-_BBDOWN_OUTPUT_TAIL_LINE_CHARS:])
                activity[0] = time.monotonic()
        except (OSError, ValueError):
            return

    @staticmethod
    def _terminate_bbdown_process(process, grace_seconds=2.0):
        """停止当前 BBDown 及其子进程组，避免失败尝试遗留下载任务。"""
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                process.terminate()
            else:
                os.killpg(process.pid, signal.SIGTERM)
        except (AttributeError, OSError, ProcessLookupError):
            try:
                process.terminate()
            except OSError:
                return
        try:
            process.wait(timeout=min(2.0, max(0.0, grace_seconds)))
        except (subprocess.TimeoutExpired, OSError):
            try:
                process.kill()
            except OSError:
                pass

    def _build_bbdown_result(self, latest_file, bv_id):
        """将已验证媒体移动到统一临时文件并构造兼容旧接口的结果。"""
        file_ext = os.path.splitext(latest_file)[1][1:]
        file_basename = os.path.basename(latest_file)
        video_title_match = re.search(r"\[(BV\w+)\](.*)\." + file_ext, file_basename)
        video_title = (
            video_title_match.group(2).strip() if video_title_match
            else os.path.splitext(file_basename)[0].strip()
        ) or f"bilibili_{bv_id}"
        ext = os.path.splitext(latest_file)[1] or ".tmp"
        target_path = self.temp_manager.create_temp_file(suffix=ext)
        shutil.move(latest_file, target_path)
        return {
            "video_id": bv_id,
            "video_title": video_title,
            "author": "",
            "download_url": None,
            "filename": os.path.basename(target_path),
            "local_file": str(target_path),
            "platform": "bilibili",
            "downloaded": True,
        }

    def _untrack_bbdown_temp_dir(self, temp_dir):
        """目录被主动清理后，避免临时管理器保留失效跟踪项。"""
        untrack = getattr(self.temp_manager, "untrack_file", None)
        if untrack is not None:
            try:
                untrack(temp_dir)
            except (OSError, TypeError):
                logger.warning(f"取消跟踪BBDown临时目录失败: {temp_dir}")

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

        以 B 站官方 API 为标题/作者/简介的主数据源（廉价、无下载）。

        关键设计（L1 解耦）：
            BBDown 模式下，get_video_info() 会触发整段音频「下载」，绝不能放进
            元数据阶段——否则 BBDown 抖动/超时抛出的异常会连累已经成功拿到的官方
            元数据，导致标题/作者退化成短链码 / "Unknown"（历史 bug）。
            因此 BBDown 模式下元数据阶段零下载，实际下载由 _fetch_download_info 负责。
            TikHub 模式下 get_video_info() 是轻量元数据调用（且提供 cid），保留，
            但用 try/except 包住，使其失败同样不会拖垮元数据。

        参数:
            url: 视频URL
            video_id: 视频ID（BV号）

        返回:
            VideoMetadata: 标准化的视频元数据对象
        """
        # 1. 主数据源：官方 API（内置 cookie + 重试加固）
        official_metadata = self._fetch_bilibili_official_metadata(video_id)

        # 2. 仅 TikHub 模式才在元数据阶段调用 get_video_info（轻量 + 提供 cid）
        use_bbdown = self.config.get("bbdown", {}).get("use_bbdown", False)
        info: dict = {}
        if not use_bbdown:
            try:
                info = self.get_video_info(url)
            except Exception as e:
                logger.warning(f"TikHub get_video_info 失败，降级使用官方API元数据: {e}")
                info = {}

        # 3. 字段级合并：优先官方 API；官方失败时回退到 BV 号（稳定），而非短链垃圾
        title = official_metadata.get("title") or info.get("video_title") or video_id
        author = official_metadata.get("author") or info.get("author") or ""
        description = official_metadata.get("description", "")  # description 只能从官方API获取
        duration = official_metadata.get("duration")

        # 构造 extra 字段
        extra = {}
        if info.get("cid"):
            extra["cid"] = info.get("cid")
        if official_metadata.get("author_id"):
            extra["author_id"] = official_metadata.get("author_id")
        if official_metadata.get("pubdate"):
            extra["pubdate"] = official_metadata.get("pubdate")

        logger.info(
            f"合并元数据完成: 标题='{title[:30]}', "
            f"作者='{author}', "
            f"简介={'有' if description else '无'}, "
            f"来源={'官方API' if official_metadata.get('title') else '回退'}"
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
