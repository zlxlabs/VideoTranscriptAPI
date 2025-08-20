import json
import requests
import datetime
import re
from .logger import setup_logger, load_config
from .simple_rate_limiter import send_rate_limited_message

# 创建日志记录器
logger = setup_logger("wechat_notifier")

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
    
    def send_text(self, content):
        """
        发送文本消息
        
        参数:
            content: 要发送的文本内容
            
        返回:
            bool: 发送是否成功（启用限流时返回是否成功加入队列）
        """
        if not self.webhook:
            logger.warning("企业微信webhook未配置，无法发送通知")
            return False
        
        if not content or not content.strip():
            logger.warning("消息内容为空，跳过发送")
            return False
        
        # 根据配置选择发送方式
        if self.use_rate_limit:
            # 使用限流发送
            success = send_rate_limited_message(self.webhook, content)
            if success:
                logger.debug(f"消息已加入限流队列: {content[:30]}...")
            else:
                logger.error(f"消息加入限流队列失败: {content[:30]}...")
            return success
        else:
            # 直接发送（原有逻辑）
            return self._send_immediate(content)
    
    def _send_immediate(self, content):
        """
        立即发送消息（不经过限流）
        
        参数:
            content: 要发送的文本内容
            
        返回:
            bool: 发送是否成功
        """
        try:
            data = {
                "msgtype": "text",
                "text": {
                    "content": content
                }
            }
            
            response = requests.post(
                self.webhook,
                data=json.dumps(data),
                headers={"Content-Type": "application/json"},
                timeout=10
            )
            
            if response.status_code == 200 and response.json().get("errcode") == 0:
                logger.info(f"企业微信通知发送成功: {content[:50]}...")
                return True
            else:
                logger.error(f"企业微信通知发送失败: {response.text}")
                return False
        except Exception as e:
            logger.exception(f"企业微信通知发送异常: {str(e)}")
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
        
        # 构建通知内容
        content = f"{timestamp}\n视频转录任务状态更新:\n链接: {clean_url}\n状态: {status}"
        
        # 添加标题和作者信息（如果有）
        if title:
            content += f"\n标题: {title}"
        if author:
            content += f"\n作者: {author}"
            
        # 添加错误信息（如果有）
        if error:
            content += f"\n错误: {error}"
            
        # 添加转录文本预览（如果有）
        if transcript and status == "转录完成":
            # 最多显示前400个字符
            preview = transcript[:100] + ("..." if len(transcript) > 100 else "")
            content += f"\n\n{preview}"
            
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
    
    max_bytes = 4000
    clean_url = WechatNotifier()._clean_url(url)
    content_type = '总结文本' if is_summary else '校对文本'
    speaker_info = '（含说话人识别）' if has_speaker_recognition else ''
    prefix = f"标题：{title or ''}\nurl：{clean_url}\n{content_type}{speaker_info}\n"
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
            content = f"{prefix}[第{part_count}段]\n{part_text}"
        else:
            content = prefix + part_text
        
        # 发送内容
        content_preview = content[:100].replace('\n', ' ')  # 内容预览
        logger.info(f"[分段发送] 发送第{part_count}段, 长度: {len(part_text)}, 预览: {content_preview}...")
        
        success = notifier.send_text(content)
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
    
    logger.info(f"长文本发送完成，共{part_count}段，总长度: {text_len}")


def send_view_link_wechat(title, view_token, webhook=None):
    """
    发送查看链接到企业微信
    
    Args:
        title: 视频标题
        view_token: 查看token
        webhook: 自定义企业微信webhook地址
    """
    from utils.markdown_renderer import get_base_url
    
    try:
        base_url = get_base_url()
        view_url = f"{base_url}/view/{view_token}"
        
        message = f"🔗 【查看链接】{title}\n\n点击查看转录进度和结果：{view_url}"
        
        notifier = WechatNotifier(webhook)
        success = notifier.send_text(message)
        
        if success:
            logger.info(f"查看链接发送成功: {title}")
        else:
            logger.error(f"查看链接发送失败: {title}")
            
        return success
        
    except Exception as e:
        logger.exception(f"发送查看链接异常: {e}")
        return False 