import json
import requests
import datetime
import re
from .logger import setup_logger, load_config

# 创建日志记录器
logger = setup_logger("wechat_notifier")

class WechatNotifier:
    """
    企业微信通知类
    """
    def __init__(self, webhook=None):
        """
        初始化企业微信通知器
        
        参数:
            webhook: 企业微信webhook地址，如果为None则从配置文件加载
        """
        config = load_config()
        self.webhook = webhook or config.get("wechat", {}).get("webhook")
        if not self.webhook:
            logger.warning("企业微信webhook未配置")
    
    def send_text(self, content):
        """
        发送文本消息
        
        参数:
            content: 要发送的文本内容
            
        返回:
            bool: 发送是否成功
        """
        if not self.webhook:
            logger.warning("企业微信webhook未配置，无法发送通知")
            return False
        
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
                timeout=5
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

def send_long_text_wechat(title, url, text, is_summary=False, webhook=None, has_speaker_recognition=False):
    """
    分段发送长文本到企业微信，自动按2048字节分割，格式为：标题、url、正文
    """
    max_bytes = 4000
    clean_url = WechatNotifier()._clean_url(url)
    content_type = '总结文本' if is_summary else '校对文本'
    speaker_info = '（含说话人识别）' if has_speaker_recognition else ''
    prefix = f"标题：{title or ''}\nurl：{clean_url}\n{content_type}{speaker_info}\n"
    prefix_bytes = len(prefix.encode('utf-8'))
    max_content_bytes = max_bytes - prefix_bytes
    start = 0
    text_len = len(text)
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
        content = prefix + text[start:end]
        WechatNotifier(webhook).send_text(content)
        start = end


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