import json
import requests
import datetime
import re
from wecom_notifier import WeComNotifier
from ..logging import setup_logger, load_config

# 创建日志记录器
logger = setup_logger("wechat_notifier")

# 全局 WeComNotifier 实例（单例模式）
_global_wecom_notifier = None

def init_global_notifier():
    """
    初始化全局 WeComNotifier 实例

    应在应用启动时调用一次，确保所有通知共享同一个实例，
    从而实现正确的并发控制和消息顺序保证。

    如果已初始化，则静默跳过（幂等操作）。
    """
    global _global_wecom_notifier
    if _global_wecom_notifier is None:
        _global_wecom_notifier = WeComNotifier()
        logger.info("全局 WeComNotifier 实例已初始化")
    else:
        # 已初始化，静默跳过（幂等操作，不输出警告）
        logger.debug("全局 WeComNotifier 已存在，跳过重复初始化")

def shutdown_global_notifier():
    """
    关闭全局 WeComNotifier 实例

    应在应用关闭时调用，确保资源正确释放。
    """
    global _global_wecom_notifier
    if _global_wecom_notifier is not None:
        # WeComNotifier 会自动清理资源
        _global_wecom_notifier = None
        logger.info("全局 WeComNotifier 实例已关闭")

def _get_global_notifier():
    """
    获取全局 WeComNotifier 实例

    如果未初始化则自动初始化（用于兼容测试场景）
    """
    global _global_wecom_notifier
    if _global_wecom_notifier is None:
        logger.warning("全局 WeComNotifier 未初始化，自动初始化（建议在应用启动时显式初始化）")
        init_global_notifier()
    return _global_wecom_notifier

def _get_risk_control():
    """获取风控模块（每次都重新导入以确保获取最新状态）"""
    try:
        # 直接从 video_transcript_api.risk_control 导入，确保是同一个模块实例
        from video_transcript_api.risk_control import is_enabled, sanitize_text

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
    使用 wecom-notifier 包实现消息发送、自动分段和频率控制

    注意：所有实例共享同一个全局 WeComNotifier，确保正确的并发控制和消息顺序
    """
    def __init__(self, webhook=None):
        """
        初始化企业微信通知器

        参数:
            webhook: 企业微信webhook地址，如果为None则从配置文件加载
        """
        config = load_config()
        self.webhook = webhook or config.get("wechat", {}).get("webhook")
        # 使用全局共享的 WeComNotifier 实例
        self.notifier = _get_global_notifier()

        if not self.webhook:
            logger.warning("企业微信webhook未配置")
        else:
            logger.debug(f"企业微信通知器已初始化，使用全局 WeComNotifier 实例")

    def _protect_urls(self, content: str) -> tuple:
        """
        提取并保护内容中的 URL，避免被风控误处理

        参数:
            content: 原始内容

        返回:
            tuple: (处理后的内容, URL映射表)
        """
        if not content:
            return content, {}

        # 匹配 http/https URL，排除中文字符
        url_pattern = r'https?://[^\s\u4e00-\u9fff]+'
        urls = re.findall(url_pattern, content)

        if not urls:
            return content, {}

        # 用占位符替换 URL
        url_map = {}
        protected = content
        for i, url in enumerate(urls):
            placeholder = f"__URL_PLACEHOLDER_{i}__"
            url_map[placeholder] = url
            protected = protected.replace(url, placeholder, 1)

        logger.debug(f"[URL保护] 提取了 {len(urls)} 个URL")
        return protected, url_map

    def _restore_urls(self, content: str, url_map: dict) -> str:
        """
        恢复被保护的 URL

        参数:
            content: 处理后的内容
            url_map: URL映射表

        返回:
            恢复URL后的内容
        """
        if not url_map:
            return content

        restored = content
        for placeholder, url in url_map.items():
            restored = restored.replace(placeholder, url)

        return restored

    def _apply_risk_control_safe(self, content: str, text_type: str = "general") -> str:
        """
        安全的风控处理，保护 URL 不被误处理

        参数:
            content: 原始内容
            text_type: 文本类型 (general/title/author/summary)

        返回:
            消敏后的内容
        """
        if not content or not content.strip():
            return content

        # 1. 保护 URL
        protected_content, url_map = self._protect_urls(content)

        # 2. 应用风控处理
        rc = _get_risk_control()
        if rc and rc.is_enabled():
            try:
                result = rc.sanitize_text(protected_content, text_type=text_type)
                if result["has_sensitive"]:
                    logger.info(f"[风控] {text_type} 包含 {len(result['sensitive_words'])} 个敏感词，已处理")
                    logger.debug(f"[风控] 敏感词: {result['sensitive_words'][:5]}")
                sanitized = result["sanitized_text"]
            except Exception as e:
                logger.exception(f"[风控] 处理失败: {e}")
                sanitized = protected_content
        else:
            sanitized = protected_content

        # 3. 恢复 URL
        return self._restore_urls(sanitized, url_map)

    def send_text(self, content, skip_risk_control=False):
        """
        发送文本消息（兼容方法，内部调用send_markdown_v2）

        参数:
            content: 要发送的文本内容
            skip_risk_control: 是否跳过风控处理（当内容已被处理时使用）

        返回:
            bool: 发送是否成功
        """
        return self.send_markdown_v2(content, skip_risk_control=skip_risk_control)

    def send_markdown_v2(self, content, skip_risk_control=False):
        """
        发送markdown_v2消息，使用 wecom-notifier 自动处理频控和分段

        采用完全异步模式：消息提交后立即返回，不等待发送结果。
        wecom-notifier 会在后台自动处理限流、重试和分段发送。

        参数:
            content: 要发送的markdown内容
            skip_risk_control: 是否跳过风控处理（当内容已被处理时使用）

        返回:
            bool: 是否成功提交发送（True表示已提交，不代表已送达）
        """
        if not self.webhook:
            logger.warning("企业微信webhook未配置，无法发送通知")
            return False

        if not content or not content.strip():
            logger.warning("消息内容为空，跳过发送")
            return False

        # 应用风控处理（除非已被处理）
        if not skip_risk_control:
            sanitized_content = self._apply_risk_control_safe(content, text_type="general")
        else:
            sanitized_content = content

        # 使用 wecom-notifier 发送（完全异步模式）
        try:
            result = self.notifier.send_markdown(
                webhook_url=self.webhook,
                content=sanitized_content,
                async_send=True
            )

            # 完全异步：不等待结果，让消息在后台自动完成
            # wecom-notifier 会自动处理：
            # - 频率限制（自动等待65秒）
            # - 网络错误（自动重试）
            # - 文本分段（自动分段发送）
            logger.debug(f"markdown_v2消息已提交发送（异步模式）: {sanitized_content[:50]}...")
            return True  # 立即返回成功，不阻塞工作线程

        except Exception as e:
            logger.exception(f"提交markdown_v2消息异常: {e}")
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
        # 添加时间戳前缀（使用配置时区）
        from ..timeutil.timezone_helper import get_configured_timezone
        tz = get_configured_timezone()
        timestamp = datetime.datetime.now(tz).strftime("%y%m%d-%H%M%S")

        # 清洗URL
        clean_url = self._clean_url(url)

        # 根据状态选择对应的emoji
        status_emoji = self._get_status_emoji(status, error)

        # 对标题和作者进行风控处理（带 URL 保护）
        if title:
            title = self._apply_risk_control_safe(title, text_type="title")
        if author:
            author = self._apply_risk_control_safe(author, text_type="author")

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
            # 最多显示前100个字符
            preview = transcript[:100] + ("..." if len(transcript) > 100 else "")
            content += f"\n\n**转录预览：**\n```\n{preview}\n```"

        return self.send_text(content, skip_risk_control=True)

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

def send_long_text_wechat(title, url, text, is_summary=False, webhook=None, has_speaker_recognition=False, use_rate_limit=True, skip_content_type_header=False):
    """
    发送长文本到企业微信，使用 wecom-notifier 自动处理分段

    参数:
        title: 视频标题
        url: 视频链接
        text: 要发送的文本内容
        is_summary: 是否为总结文本
        webhook: 自定义webhook地址
        has_speaker_recognition: 是否包含说话人识别
        use_rate_limit: 是否启用限流（已废弃，保留仅为兼容性）
        skip_content_type_header: 是否跳过自动添加的内容类型标题（默认False）
    """
    if not text or not text.strip():
        logger.warning("文本内容为空，跳过发送")
        return

    notifier = WechatNotifier(webhook)

    # 1. URL 清洗
    clean_url = notifier._clean_url(url)

    # 2. 风控处理（带 URL 保护）
    safe_title = notifier._apply_risk_control_safe(title, text_type="title") if title else ""
    text_type = "summary" if is_summary else "general"
    safe_text = notifier._apply_risk_control_safe(text, text_type=text_type)

    # 3. 构建消息（简化版，不需要手动分段）
    if skip_content_type_header:
        # 跳过内容类型标题，直接使用传入的文本（文本中已包含格式化的标题）
        message = f"""## {safe_title}

{clean_url}

{safe_text}
"""
    else:
        # 传统模式：自动添加内容类型标题
        content_type = '**总结文本**' if is_summary else '**校对文本**'
        speaker_info = '（含说话人识别）' if has_speaker_recognition else ''

        message = f"""## {safe_title}

{clean_url}

{content_type}{speaker_info}

{safe_text}
"""

    # 4. 发送（wecom-notifier 自动处理分段和频控）
    logger.info(f"发送{'总结' if is_summary else '校对'}文本，长度: {len(safe_text)} 字符")
    success = notifier.send_markdown_v2(message, skip_risk_control=True)

    if success:
        logger.info(f"{'总结' if is_summary else '校对'}文本发送成功")
    else:
        logger.error(f"{'总结' if is_summary else '校对'}文本发送失败")

    return success


def send_view_link_wechat(title, view_token, webhook=None, original_url=None):
    """
    发送查看链接到企业微信

    Args:
        title: 视频标题
        view_token: 查看token
        webhook: 自定义企业微信webhook地址
        original_url: 原始媒体URL（可选）
    """
    from ..rendering import get_base_url

    try:
        notifier = WechatNotifier(webhook)

        # 对标题进行风控处理（带 URL 保护）
        if title:
            title = notifier._apply_risk_control_safe(title, text_type="title")

        base_url = get_base_url()
        view_url = f"{base_url}/view/{view_token}"

        if original_url:
            # 清洗原始URL
            clean_url = notifier._clean_url(original_url)
            message = f"# {title}\n\n{clean_url}\n\n🔗 点击查看转录进度和结果：\n{view_url}"
        else:
            # 保持原有格式作为后备
            message = f"# 🔗 【查看链接】{title}\n\n🔗 点击查看转录进度和结果：\n{view_url}"

        # 跳过风控（标题已经处理过，URL 不需要风控）
        success = notifier.send_text(message, skip_risk_control=True)

        if success:
            logger.debug(f"查看链接发送成功: {title}")
        else:
            logger.error(f"查看链接发送失败: {title}")

        return success

    except Exception as e:
        logger.exception(f"发送查看链接异常: {e}")
        return False 


def send_markdown_wechat(content, webhook=None, skip_risk_control=False):
    """
    兼容保留：发送 Markdown 消息。

    Args:
        content: Markdown 正文
        webhook: 可选自定义 webhook
        skip_risk_control: 是否跳过风控处理
    """
    notifier = WechatNotifier(webhook)
    return notifier.send_markdown_v2(content, skip_risk_control=skip_risk_control)


def send_wechat_notification(
    url,
    status,
    error=None,
    title=None,
    author=None,
    transcript=None,
    webhook=None,
):
    """
    兼容保留：发送任务状态通知。
    """
    notifier = WechatNotifier(webhook)
    return notifier.notify_task_status(
        url=url,
        status=status,
        error=error,
        title=title,
        author=author,
        transcript=transcript,
    )


def format_llm_config_markdown(models_used: dict) -> str:
    """
    将 LLM 模型配置格式化为 Markdown 文本（仅展示校对和总结模型）

    Args:
        models_used: 模型配置字典

    Returns:
        str: 格式化后的 Markdown 文本
    """
    if not models_used:
        return ""

    lines = ["**模型配置：**"]

    # 校对模型
    calibrate_model = models_used.get('calibrate_model', '')
    calibrate_effort = models_used.get('calibrate_reasoning_effort')
    if calibrate_model:
        effort_str = f" (reasoning: {calibrate_effort})" if calibrate_effort else ""
        lines.append(f"> 校对: {calibrate_model}{effort_str}")

    # 总结模型
    summary_model = models_used.get('summary_model', '')
    summary_effort = models_used.get('summary_reasoning_effort')
    if summary_model:
        effort_str = f" (reasoning: {summary_effort})" if summary_effort else ""
        lines.append(f"> 总结: {summary_model}{effort_str}")

    # 风险降级标记
    if models_used.get('has_risk', False):
        lines.append("> [风险降级]")

    return "\n".join(lines)
