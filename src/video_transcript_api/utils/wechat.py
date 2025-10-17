import json
import requests
import datetime
import re
from .logger import setup_logger, load_config
from .simple_rate_limiter import send_rate_limited_message

# 创建日志记录器
logger = setup_logger("wechat_notifier")

def _get_risk_control():
    """获取风控模块（每次都重新导入以确保获取最新状态）"""
    try:
        # 直接从 video_transcript_api.utils.risk_control 导入，确保是同一个模块实例
        from video_transcript_api.utils.risk_control import is_enabled, sanitize_text

        class RiskControlWrapper:
            @staticmethod
            def is_enabled():
                return is_enabled()

            @staticmethod
            def sanitize_text(text, text_type="general"):
                return sanitize_text(text, text_type)

        return RiskControlWrapper()
    except ImportError:
        return None

class WechatNotifier:
    """
    企业微信通知类
    支持自动限流：每个webhook地址每分钟最多20条消息，超限自动排队
    """
    def __init__(self, webhook=None, use_rate_limit=True):
        """
        初始化企业微信通知器
        
        参数:
            webhook: 企业微信webhook地址，如果为None则从配置文件加载
            use_rate_limit: 是否启用限流功能，默认为True
        """
        config = load_config()
        self.webhook = webhook or config.get("wechat", {}).get("webhook")
        self.use_rate_limit = use_rate_limit
        
        if not self.webhook:
            logger.warning("企业微信webhook未配置")
        else:
            logger.debug(f"企业微信通知器已初始化，限流: {'启用' if use_rate_limit else '禁用'}")
    
    def _apply_risk_control(self, content: str) -> str:
        """
        应用风控处理，对内容进行敏感词消敏

        参数:
            content: 原始内容

        返回:
            消敏后的内容
        """
        rc = _get_risk_control()
        if not rc or not rc.is_enabled():
            return content

        try:
            result = rc.sanitize_text(content)
            if result["has_sensitive"]:
                logger.info(f"[风控] 通用消息包含 {len(result['sensitive_words'])} 个敏感词，已移除")
                logger.debug(f"[风控] 敏感词: {result['sensitive_words'][:5]}")
            return result["sanitized_text"]
        except Exception as e:
            logger.exception(f"[风控] 处理失败: {e}")
            # 风控失败时返回原内容，不影响正常发送
            return content

    def send_text(self, content, skip_risk_control=False):
        """
        发送文本消息（兼容方法，内部调用send_markdown_v2）

        参数:
            content: 要发送的文本内容
            skip_risk_control: 是否跳过风控处理（当内容已被处理时使用）

        返回:
            bool: 发送是否成功（启用限流时返回是否成功加入队列）
        """
        return self.send_markdown_v2(content, skip_risk_control=skip_risk_control)

    def send_markdown_v2(self, content, skip_risk_control=False):
        """
        发送markdown_v2消息

        参数:
            content: 要发送的markdown内容
            skip_risk_control: 是否跳过风控处理（当内容已被处理时使用）

        返回:
            bool: 发送是否成功（启用限流时返回是否成功加入队列）
        """
        if not self.webhook:
            logger.warning("企业微信webhook未配置，无法发送通知")
            return False

        if not content or not content.strip():
            logger.warning("消息内容为空，跳过发送")
            return False

        # 应用风控处理（除非已被处理）
        if not skip_risk_control:
            sanitized_content = self._apply_risk_control(content)
        else:
            sanitized_content = content

        # 根据配置选择发送方式
        if self.use_rate_limit:
            # 使用限流发送
            success = send_rate_limited_message(self.webhook, sanitized_content, msgtype="markdown_v2")
            if success:
                logger.debug(f"markdown_v2消息已加入限流队列: {sanitized_content[:30]}...")
            else:
                logger.error(f"markdown_v2消息加入限流队列失败: {sanitized_content[:30]}...")
            return success
        else:
            # 直接发送（原有逻辑）
            return self._send_immediate_markdown_v2(sanitized_content)
    
    def _send_immediate(self, content):
        """
        立即发送文本消息（不经过限流，兼容方法）

        参数:
            content: 要发送的文本内容

        返回:
            bool: 发送是否成功
        """
        return self._send_immediate_markdown_v2(content)

    def _send_immediate_markdown_v2(self, content):
        """
        立即发送markdown_v2消息（不经过限流）

        参数:
            content: 要发送的markdown内容

        返回:
            bool: 发送是否成功
        """
        try:
            data = {
                "msgtype": "markdown_v2",
                "markdown_v2": {
                    "content": content
                }
            }

            response = requests.post(
                self.webhook,
                data=json.dumps(data, ensure_ascii=False).encode('utf-8'),
                headers={"Content-Type": "application/json; charset=utf-8"},
                timeout=10
            )

            if response.status_code == 200 and response.json().get("errcode") == 0:
                logger.debug(f"企业微信markdown_v2通知发送成功: {content[:50]}...")
                return True
            else:
                logger.error(f"企业微信markdown_v2通知发送失败: {response.text}")
                return False
        except Exception as e:
            logger.exception(f"企业微信markdown_v2通知发送异常: {str(e)}")
            return False

    def _clean_url(self, url):
        """
        清洗URL，移除问号后的追踪参数
        
        对于小红书链接，保留 xsec_token 参数，其他参数去除。
        对于 YouTube 链接，保留 v 参数（video_id）。
        
        参数:
            url: 原始URL
            
        返回:
            str: 清洗后的URL
        """
        if "xiaohongshu.com" in url or "xhslink.com" in url:
            # 只保留 xsec_token 参数
            if "?" in url:
                base, query = url.split("?", 1)
                params = query.split("&")
                kept = []
                for p in params:
                    if p.startswith("xsec_token="):
                        kept.append(p)
                if kept:
                    return base + "?" + "&".join(kept)
                else:
                    return base
            else:
                return url
        elif "youtube.com" in url or "youtu.be" in url:
            # YouTube 链接保留 v 参数（video_id）
            if "?" in url:
                base, query = url.split("?", 1)
                params = query.split("&")
                kept = []
                for p in params:
                    if p.startswith("v="):
                        kept.append(p)
                if kept:
                    return base + "?" + "&".join(kept)
                else:
                    return base
            else:
                return url
        else:
            if "?" in url:
                return url.split("?")[0]
            return url

    def _get_status_emoji(self, status, error=None):
        """
        根据任务状态获取对应的emoji

        参数:
            status: 任务状态
            error: 错误信息

        返回:
            str: 对应的emoji
        """
        # 错误状态优先
        if error or "失败" in status or "异常" in status or "错误" in status:
            return "❌"

        # 根据状态关键词匹配emoji
        status_lower = status.lower()

        if "开始" in status or "处理" in status:
            return "🔄"
        elif "下载" in status:
            if "正在下载" in status:
                return "⬇️"
            elif "下载完成" in status or "下载成功" in status:
                return "✅"
            else:
                return "📥"
        elif "转录" in status:
            if "正在转录" in status:
                return "🎤"
            elif "转录完成" in status or "转录成功" in status:
                return "✅"
            else:
                return "📝"
        elif "完成" in status or "成功" in status:
            return "✅"
        elif "等待" in status or "队列" in status:
            return "⏳"
        elif "缓存" in status:
            return "💾"
        elif "平台字幕" in status:
            return "📄"
        else:
            # 默认处理中状态
            return "🔄"

    def notify_task_status(self, url, status, error=None, title=None, author=None, transcript=None):
        """
        通知任务状态

        参数:
            url: 视频URL
            status: 当前状态
            error: 错误信息，如果有的话
            title: 视频标题，如果有的话
            author: 视频作者，如果有的话
            transcript: 转录文本，如果有的话

        返回:
            bool: 发送是否成功
        """
        # 添加时间戳前缀
        timestamp = datetime.datetime.now().strftime("%y%m%d-%H%M%S")

        # 清洗URL
        clean_url = self._clean_url(url)

        # 根据状态选择对应的emoji
        status_emoji = self._get_status_emoji(status, error)

        # 【新增】对标题和作者进行风控处理
        rc = _get_risk_control()
        if rc and rc.is_enabled():
            try:
                if title:
                    title_result = rc.sanitize_text(title, text_type="title")
                    if title_result["has_sensitive"]:
                        logger.info(f"[风控] 任务状态-标题包含 {len(title_result['sensitive_words'])} 个敏感词，已截断")
                        logger.debug(f"[风控] 敏感词: {title_result['sensitive_words'][:3]}")
                        title = title_result["sanitized_text"]

                if author:
                    author_result = rc.sanitize_text(author, text_type="author")
                    if author_result["has_sensitive"]:
                        logger.info(f"[风控] 任务状态-作者包含 {len(author_result['sensitive_words'])} 个敏感词，已截断")
                        logger.debug(f"[风控] 敏感词: {author_result['sensitive_words'][:3]}")
                        author = author_result["sanitized_text"]
            except Exception as e:
                logger.exception(f"Risk control in notify_task_status failed: {e}")
                # 风控失败时继续使用原内容

        # 构建通知内容（markdown_v2格式）
        content = f"## {timestamp}\n\n{status_emoji} **视频转录任务状态更新**\n\n{clean_url}\n\n**状态：** {status}"

        # 添加标题和作者信息（如果有）
        if title:
            content += f"\n\n**标题：** {title}"
        if author:
            content += f"\n\n**作者：** {author}"

        # 添加错误信息（如果有）
        if error:
            content += f"\n\n**错误：** {error}"

        # 添加转录文本预览（如果有）
        if transcript and status == "转录完成":
            # 最多显示前400个字符
            preview = transcript[:100] + ("..." if len(transcript) > 100 else "")
            content += f"\n\n**转录预览：**\n```\n{preview}\n```"

        return self.send_text(content)

def wechat_notify(message, webhook=None, config=None):
    """
    发送企业微信通知的简便函数
    
    参数:
        message: 要发送的通知内容
        webhook: 企业微信webhook地址，如果为None则从config或配置文件加载
        config: 配置字典，如果提供则从中获取webhook
        
    返回:
        bool: 发送是否成功
    """
    if config and not webhook:
        webhook = config.get("wechat", {}).get("webhook")
        
    notifier = WechatNotifier(webhook)
    return notifier.send_text(message)

def send_long_text_wechat(title, url, text, is_summary=False, webhook=None, has_speaker_recognition=False, use_rate_limit=True):
    """
    分段发送长文本到企业微信，自动按字节分割

    参数:
        title: 视频标题
        url: 视频链接
        text: 要发送的文本内容
        is_summary: 是否为总结文本
        webhook: 自定义webhook地址
        has_speaker_recognition: 是否包含说话人识别
        use_rate_limit: 是否启用限流，默认为True
    """
    if not text or not text.strip():
        logger.warning("文本内容为空，跳过发送")
        return

    # 【调试】记录函数入口参数
    logger.info(f"[DEBUG] send_long_text_wechat called: is_summary={is_summary}, text_len={len(text)}, title='{title[:50] if title else None}...'")

    # 【新增】在分段前对标题和文本内容进行风控处理
    rc = _get_risk_control()
    rc_exists = rc is not None

    # 【调试】检查风控模块状态
    if rc_exists:
        logger.info(f"[DEBUG] Risk control module loaded: {rc}")
        logger.info(f"[DEBUG] Checking rc._text_sanitizer: {getattr(rc, '_text_sanitizer', 'NOT FOUND')}")
        logger.info(f"[DEBUG] Checking rc._words_manager: {getattr(rc, '_words_manager', 'NOT FOUND')}")

    rc_enabled = rc.is_enabled() if rc else False
    logger.info(f"[DEBUG] Risk control status: exists={rc_exists}, enabled={rc_enabled}")

    if rc and rc.is_enabled():
        try:
            # 对标题进行消敏（移除敏感词后取前6字符）
            if title:
                logger.debug(f"[DEBUG] Processing title with risk control...")
                title_result = rc.sanitize_text(title, text_type="title")
                logger.debug(f"[DEBUG] Title risk control result: has_sensitive={title_result['has_sensitive']}, words={title_result['sensitive_words']}")
                if title_result["has_sensitive"]:
                    logger.info(f"[风控] 标题包含 {len(title_result['sensitive_words'])} 个敏感词: {title_result['sensitive_words'][:3]}{'...' if len(title_result['sensitive_words']) > 3 else ''}")
                    logger.info(f"[风控] 标题处理: '{title[:20]}...' -> '{title_result['sanitized_text']}'")
                    title = title_result["sanitized_text"]

            # 对文本内容进行消敏
            # 总结文本：如有敏感词则替换为"内容风险，请通过url查看"
            # 其他文本：移除所有敏感词
            text_type = "summary" if is_summary else "general"
            logger.info(f"[DEBUG] Processing text: text_type='{text_type}', text_len={len(text)}, preview='{text[:100].replace(chr(10), ' ')}...'")

            text_result = rc.sanitize_text(text, text_type=text_type)

            logger.info(f"[DEBUG] Risk control result: has_sensitive={text_result['has_sensitive']}, sensitive_count={len(text_result['sensitive_words'])}")
            if text_result['sensitive_words']:
                logger.info(f"[DEBUG] Detected sensitive words: {text_result['sensitive_words'][:10]}")
            logger.info(f"[DEBUG] Sanitized text preview: '{text_result['sanitized_text'][:100].replace(chr(10), ' ')}...'")

            if text_result["has_sensitive"]:
                action = "替换为风控提示" if text_type == "summary" else "移除敏感词"
                logger.info(f"[风控] {'总结' if is_summary else '校对'}文本包含 {len(text_result['sensitive_words'])} 个敏感词，已{action}")
                logger.info(f"[风控] 敏感词列表: {text_result['sensitive_words'][:5]}{'...' if len(text_result['sensitive_words']) > 5 else ''}")
                logger.info(f"[风控] 处理前: '{text[:80].replace(chr(10), ' ')}...'")
                logger.info(f"[风控] 处理后: '{text_result['sanitized_text'][:80].replace(chr(10), ' ')}...'")
                text = text_result["sanitized_text"]
            else:
                logger.info(f"[DEBUG] No sensitive words found in text (text_type={text_type})")
        except Exception as e:
            logger.exception(f"Risk control in send_long_text_wechat failed: {e}")
            # 风控失败时继续使用原内容
    else:
        logger.debug(f"[DEBUG] Risk control skipped (rc={rc is not None}, enabled={rc.is_enabled() if rc else False})")

    max_bytes = 4000
    clean_url = WechatNotifier()._clean_url(url)
    content_type = '**总结文本**' if is_summary else '**校对文本**'
    speaker_info = '（含说话人识别）' if has_speaker_recognition else ''
    prefix = f"## {title or ''}\n\n{clean_url}\n\n{content_type}{speaker_info}\n\n"
    prefix_bytes = len(prefix.encode('utf-8'))
    max_content_bytes = max_bytes - prefix_bytes

    if max_content_bytes <= 0:
        logger.error("前缀信息过长，无法发送内容")
        return

    notifier = WechatNotifier(webhook, use_rate_limit=use_rate_limit)
    
    start = 0
    text_len = len(text)
    part_count = 0
    
    while start < text_len:
        end = start
        curr_bytes = 0
        
        # 按字符递增，保证utf-8分割安全
        while end < text_len:
            char_bytes = len(text[end].encode('utf-8'))
            if curr_bytes + char_bytes > max_content_bytes:
                break
            curr_bytes += char_bytes
            end += 1
        
        # 构造分段内容
        part_count += 1
        part_text = text[start:end]
        
        # 如果是多段，添加段落标识
        if text_len > max_content_bytes:
            content = f"{prefix}**[第{part_count}段]**\n{part_text}"
        else:
            content = prefix + part_text
        
        # 发送内容（跳过风控，因为文本已在上面被处理过）
        content_preview = content[:100].replace('\n', ' ')  # 内容预览
        logger.debug(f"[分段发送] 发送第{part_count}段, 长度: {len(part_text)}, 预览: {content_preview}...")

        success = notifier.send_text(content, skip_risk_control=True)
        if not success:
            logger.error(f"发送第{part_count}段失败")
        else:
            logger.debug(f"发送第{part_count}段成功，长度: {len(part_text)}")
        
        # 添加短暂延迟，确保每个分段消息都能按顺序加入队列
        if use_rate_limit:
            import time
            logger.debug(f"[分段延迟] 第{part_count}段发送后延迟10ms")
            time.sleep(0.01)  # 每个分段后都延迟10ms，确保消息顺序
        
        start = end
    
    logger.debug(f"长文本发送完成，共{part_count}段，总长度: {text_len}")


def send_view_link_wechat(title, view_token, webhook=None, original_url=None):
    """
    发送查看链接到企业微信

    Args:
        title: 视频标题
        view_token: 查看token
        webhook: 自定义企业微信webhook地址
        original_url: 原始媒体URL（可选）
    """
    from utils.markdown_renderer import get_base_url

    try:
        # 【新增】对标题进行风控处理（移除敏感词后取前6字符）
        rc = _get_risk_control()
        if rc and rc.is_enabled() and title:
            try:
                title_result = rc.sanitize_text(title, text_type="title")
                if title_result["has_sensitive"]:
                    logger.info(f"[风控] 查看链接-标题包含 {len(title_result['sensitive_words'])} 个敏感词，已截断")
                    logger.debug(f"[风控] 敏感词: {title_result['sensitive_words'][:3]}")
                    title = title_result["sanitized_text"]
            except Exception as e:
                logger.exception(f"Risk control in send_view_link_wechat failed: {e}")
                # 风控失败时继续使用原标题

        base_url = get_base_url()
        view_url = f"{base_url}/view/{view_token}"

        if original_url:
            # 清洗原始URL
            clean_url = WechatNotifier()._clean_url(original_url)
            message = f"# {title}\n\n{clean_url}\n\n🔗 点击查看转录进度和结果：\n{view_url}"
        else:
            # 保持原有格式作为后备
            message = f"# 🔗 【查看链接】{title}\n\n🔗 点击查看转录进度和结果：\n{view_url}"

        notifier = WechatNotifier(webhook)
        success = notifier.send_text(message)
        
        if success:
            logger.debug(f"查看链接发送成功: {title}")
        else:
            logger.error(f"查看链接发送失败: {title}")
            
        return success
        
    except Exception as e:
        logger.exception(f"发送查看链接异常: {e}")
        return False 