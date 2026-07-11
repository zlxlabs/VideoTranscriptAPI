import os
import mimetypes
import hashlib
import time
import requests
from urllib.parse import urlparse, unquote, urljoin
from .base import BaseDownloader
from .models import VideoMetadata, DownloadInfo
from ..errors import InvalidURLError
from ..utils.logging import setup_logger
# 模块级导入（而非 from...import 具名导入）：保持 url_validator.validate_url_safe
# 可被测试通过 monkeypatch.setattr("...url_validator.validate_url_safe", ...) 打桩，
# 具名导入会在导入时绑定函数对象，使得对源模块属性的打桩失效。
from ..utils import url_validator
from ..utils.pinned_ip_adapter import PinnedIPHTTPAdapter
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

    # 重定向最大跟随跳数：既要允许正常的 CDN/短链跳转，又要防止恶意或
    # 异常服务器无限重定向拖垮下载线程
    _MAX_REDIRECTS = 5

    def _validate_or_raise(self, url: str) -> None:
        """
        对 URL 做 SSRF 安全校验（协议白名单 + 私网/回环/链路本地/云元数据拦截 +
        DNS 二次解析校验），失败时转换为面向用户可读的 InvalidURLError。

        GenericDownloader 是兜底处理器（can_handle 恒为 True），任何未被其他
        平台专用下载器识别的 URL 都会落到这里，必须在发起任何网络请求前拦截。

        参数:
            url: 待校验的 URL

        抛出:
            InvalidURLError: URL 指向内网/回环/链路本地/云元数据等不安全地址，
                              或协议不在 http/https 白名单内
        """
        try:
            url_validator.validate_url_safe(url)
        except url_validator.URLValidationError as e:
            logger.error(f"URL 安全校验未通过，已阻止请求: {url}, 原因: {e}")
            raise InvalidURLError("URL 指向内部网络地址，已被安全策略拦截") from e

    def _safe_request(self, method: str, url: str, **kwargs) -> requests.Response:
        """
        对 URL 做 SSRF 校验后发起请求，并逐跳校验重定向目标。

        不使用 requests 自带的 allow_redirects=True 自动跳转，而是手动跟随并
        在每一跳都重新校验 + 钉定 IP（见 _dispatch_pinned_request），防止
        公网 URL 通过 302 等方式跳转到内网/云元数据地址造成 SSRF 绕过。

        参数:
            method: 'head' 或 'get'
            url: 请求 URL
            **kwargs: 透传给底层请求的参数（如 timeout、stream、headers）。
                      不要传入 allow_redirects，本方法自己手动处理重定向，
                      从不让底层库自动跳转

        返回:
            requests.Response: 最终（非重定向）响应

        抛出:
            InvalidURLError: 任意一跳未通过 SSRF 校验，或重定向跳数超过上限
        """
        current_url = url
        redirect_count = 0

        while True:
            response = self._dispatch_pinned_request(method, current_url, **kwargs)

            if response.is_redirect:
                redirect_count += 1
                if redirect_count > self._MAX_REDIRECTS:
                    if kwargs.get("stream"):
                        response.close()
                    raise InvalidURLError(
                        f"重定向次数超过上限（{self._MAX_REDIRECTS}），已终止请求: {url}"
                    )
                next_url = urljoin(current_url, response.headers["Location"])
                logger.info(
                    f"跟随重定向 ({redirect_count}/{self._MAX_REDIRECTS}): "
                    f"{current_url} -> {next_url}"
                )
                if kwargs.get("stream"):
                    response.close()
                current_url = next_url
                continue

            return response

    def _dispatch_pinned_request(self, method: str, url: str, **kwargs) -> requests.Response:
        """
        校验 URL 安全性，并把真正发起的网络连接"钉"在校验时已解析、已检查过
        的那个 IP 上，再发起一次请求。

        背景（DNS rebinding TOCTOU）：如果只是校验通过后就把原始 URL（域名
        形式）交给 requests 发起请求，requests/urllib3 会独立地重新解析一次
        DNS —— 攻击者控制的域名完全可能在"校验时解析"和"连接时解析"这两次
        解析之间切换 DNS 记录（先给公网 IP 通过校验，连接时再给内网/云元数据
        IP），从而绕过 SSRF 防护。这里改为：校验函数把它解析并检查过的 IP
        原样返回，网络连接直接使用这个 IP（PinnedIPHTTPAdapter），不再触发
        任何针对该域名的新 DNS 查询，彻底消除这个窗口。

        不经过 requests.Session 的自动重定向/Cookie/Hook 流水线 —— 那些都由
        _safe_request 的手动循环自己实现，这里只需要"发一次请求、拿到一个
        Response"，用 Session.prepare_request 只是为了复用默认请求头
        （User-Agent 等）的构造逻辑。

        参数:
            method: 'head' 或 'get'
            url: 请求 URL（尚未做过 SSRF 校验）
            **kwargs: 透传给 HTTPAdapter.send 的参数（timeout、stream 等）；
                      headers 会被合并进构造出的请求中

        返回:
            requests.Response

        抛出:
            InvalidURLError: URL 未通过 SSRF 校验
        """
        try:
            _, pinned_ip = url_validator.validate_url_safe_with_ip(url)
        except url_validator.URLValidationError as e:
            logger.error(f"URL 安全校验未通过，已阻止请求: {url}, 原因: {e}")
            raise InvalidURLError("URL 指向内部网络地址，已被安全策略拦截") from e

        request_func = requests.head if method == "head" else requests.get

        if pinned_ip is None:
            # validate_url_safe 对 DNS 解析失败采用"放行，可能是瞬时故障"的
            # 宽松策略，此时没有已验证的 IP 可钉，只能退化为不钉 IP 的普通
            # 请求 —— 这与本次修复之前的行为一致，不引入新的失败模式；真正
            # 发起连接时如果 DNS 仍然失败会在这里自然报错。
            logger.warning(f"DNS 解析失败，无法钉定已校验 IP，回退为未钉 IP 的请求: {url}")
            return request_func(url, **kwargs)

        parsed_url = urlparse(url)
        headers = kwargs.pop("headers", None)
        prepared = requests.Session().prepare_request(
            requests.Request(method.upper(), url, headers=headers)
        )

        adapter = PinnedIPHTTPAdapter(
            hostname=parsed_url.hostname,
            pinned_ip=pinned_ip,
            is_https=(parsed_url.scheme == "https"),
        )
        try:
            return adapter.send(prepared, **kwargs)
        finally:
            adapter.close()

    def _is_media_url(self, url):
        """
        检查URL是否直接指向媒体文件

        参数:
            url: 文件URL

        返回:
            bool: 是否是媒体文件URL

        抛出:
            InvalidURLError: HEAD 探测过程中命中不安全的重定向目标
        """
        try:
            parsed_url = urlparse(url)
            path = unquote(parsed_url.path.lower())

            # 检查URL路径中的文件扩展名
            _, ext = os.path.splitext(path)
            if ext in self.supported_extensions:
                return True

            # 尝试HEAD请求获取Content-Type（经 SSRF 校验 + 逐跳重定向校验）
            try:
                response = self._safe_request("head", url, timeout=10)
                content_type = response.headers.get('Content-Type', '').lower()

                # 检查Content-Type是否是音视频类型
                if any(media_type in content_type for media_type in ['audio/', 'video/']):
                    return True
            except InvalidURLError:
                # SSRF 拦截需要向上抛出终止整个处理流程，不能当作"探测失败"忽略
                raise
            except requests.exceptions.RequestException:
                pass

            return False
        except InvalidURLError:
            raise
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

        抛出:
            InvalidURLError: URL 未通过 SSRF 安全校验
        """
        logger.info(f"通用下载器处理URL: {url}")

        # SSRF 校验：generic 下载器是兜底处理器，任何 URL 都可能落到这里，
        # 必须先过安全校验再发起任何网络请求（含下方的媒体类型 HEAD 探测）
        self._validate_or_raise(url)

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
                # 尝试从HEAD请求获取文件类型（经 SSRF 校验 + 逐跳重定向校验）
                ext = '.mp4'  # 默认扩展名
                try:
                    response = self._safe_request('head', url, timeout=10)
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
                except InvalidURLError:
                    # SSRF 拦截需要向上抛出终止整个处理流程，不能静默回退默认扩展名
                    raise
                except requests.exceptions.RequestException:
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

        抛出:
            InvalidURLError: URL 未通过 SSRF 安全校验（永久性错误，不重试）
        """
        # SSRF 校验：generic 下载器是兜底处理器，任何 URL 都可能落到这里，
        # 必须先过安全校验再发起任何网络请求
        self._validate_or_raise(url)

        # 落到当前任务的专属目录（data/temp/task_<id>/），实现：
        # 1) 任务结束时由 clean_up_task 一并 rmtree，不残留；
        # 2) 不同任务即使同名文件也写在各自目录，避免同名碰撞互相覆盖/误删。
        task_dir = self.temp_manager.get_current_task_dir()
        local_path = os.path.join(str(task_dir), filename)

        # 创建目录（如果不存在）
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        
        # 最大重试次数
        max_retries = 3
        # 重试退避（秒）：给瞬态故障（如文件服务重启的部署窗口）留恢复时间
        retry_backoff = (5, 15)
        chunk_size = 1024 * 1024  # 1MB 块大小
        
        # 尝试导入企微通知器
        try:
            # 使用包内绝对导入，避免重复加载模块导致全局实例被初始化两次
            from ..utils.notifications import WechatNotifier
            wechat_notifier = WechatNotifier()
        except:
            wechat_notifier = None
        
        for attempt in range(max_retries):
            if attempt > 0:
                delay = retry_backoff[min(attempt - 1, len(retry_backoff) - 1)]
                logger.info(f"等待 {delay}s 后重试下载...")
                time.sleep(delay)
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
                
                # 发起请求（经 SSRF 校验 + 逐跳重定向校验）
                try:
                    response = self._safe_request(
                        'get',
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
                        response = self._safe_request(
                            'get',
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

            except InvalidURLError:
                # SSRF 拦截是永久性错误（重定向目标不安全），重试无意义，
                # 必须直接向上抛出终止整个下载，不能被当作瞬态故障重试或吞掉
                logger.error(f"下载中止：URL 未通过 SSRF 安全校验 (尝试 {attempt + 1}/{max_retries}): {url}")
                raise

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
