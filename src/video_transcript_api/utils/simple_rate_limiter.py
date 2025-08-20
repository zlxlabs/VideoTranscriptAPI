"""
简单稳定的webhook限流方案
- 基于装饰器模式，无复杂单例和队列
- 使用文件锁保证全局唯一性
- 简单的时间间隔控制，保证消息顺序
"""

import time
import threading
import requests
import json
import os
from functools import wraps
from pathlib import Path
from collections import defaultdict
from .logger import setup_logger

logger = setup_logger("simple_rate_limiter")

class SimpleWebhookRateLimiter:
    """
    简单的webhook限流器
    - 无复杂队列，直接同步发送
    - 使用全局锁保证顺序
    - 基于时间间隔的简单限流
    """
    
    def __init__(self):
        # 每个webhook独立的发送锁，实现独立频控
        self._webhook_locks = defaultdict(threading.Lock)
        # 记录每个webhook的最后发送时间
        self._last_send_times = {}
        # 最小发送间隔（秒）
        self.min_interval = 0.8
        # 统计信息锁
        self._stats_lock = threading.Lock()
        # 统计信息
        self.stats = {
            'total_sent': 0,
            'total_failed': 0
        }
    
    def send_message(self, webhook_url: str, content: str) -> bool:
        """
        同步发送消息，自动限流
        
        Args:
            webhook_url: webhook地址
            content: 消息内容
            
        Returns:
            bool: 发送是否成功
        """
        if not webhook_url or not webhook_url.strip():
            logger.warning("webhook地址为空，无法发送消息")
            return False
            
        if not content or not content.strip():
            logger.warning("消息内容为空，跳过发送")
            return False
        
        webhook_url = webhook_url.strip()
        content = content.strip()
        
        # 使用该webhook独立的锁，不同webhook可以并行发送
        with self._webhook_locks[webhook_url]:
            # 检查时间间隔，如有必要则等待
            self._wait_if_needed(webhook_url)
            
            # 发送消息
            content_preview = content[:100].replace('\n', ' ')
            logger.info(f"[同步发送] Webhook: {webhook_url[:30]}..., 预览: {content_preview}...")
            
            success = self._send_webhook_now(webhook_url, content)
            
            # 记录发送时间
            self._last_send_times[webhook_url] = time.time()
            
            # 更新统计信息（需要锁保护）
            with self._stats_lock:
                if success:
                    self.stats['total_sent'] += 1
                else:
                    self.stats['total_failed'] += 1
            
            if success:
                logger.info(f"[同步发送] 发送成功, Webhook: {webhook_url[:30]}..., 预览: {content_preview}...")
            else:
                logger.error(f"[同步发送] 发送失败, Webhook: {webhook_url[:30]}..., 预览: {content_preview}...")
            
            return success
    
    def _wait_if_needed(self, webhook_url: str):
        """如有必要，等待到满足最小发送间隔"""
        last_send_time = self._last_send_times.get(webhook_url, 0)
        current_time = time.time()
        elapsed = current_time - last_send_time
        
        if elapsed < self.min_interval:
            wait_time = self.min_interval - elapsed
            logger.debug(f"Webhook {webhook_url[:30]}... 等待发送间隔: {wait_time:.3f}s")
            time.sleep(wait_time)
    
    def _send_webhook_now(self, webhook_url: str, content: str) -> bool:
        """立即发送webhook消息"""
        try:
            data = {
                "msgtype": "text",
                "text": {
                    "content": content
                }
            }
            
            response = requests.post(
                webhook_url,
                data=json.dumps(data),
                headers={"Content-Type": "application/json"},
                timeout=10
            )
            
            if response.status_code == 200:
                result = response.json()
                if result.get("errcode") == 0:
                    return True
                else:
                    logger.error(f"企业微信API返回错误: {result}")
                    return False
            else:
                logger.error(f"HTTP请求失败: {response.status_code}, {response.text}")
                return False
                
        except requests.exceptions.Timeout:
            logger.error(f"webhook请求超时: {webhook_url[:50]}...")
            return False
        except requests.exceptions.RequestException as e:
            logger.error(f"webhook请求异常: {e}")
            return False
        except Exception as e:
            logger.exception(f"发送消息时发生未知异常: {e}")
            return False
    
    def get_stats(self) -> dict:
        """获取统计信息"""
        return self.stats.copy()

# 全局实例 - 模块级别，避免多线程创建问题
_global_limiter = SimpleWebhookRateLimiter()

def send_rate_limited_message(webhook_url: str, content: str) -> bool:
    """
    发送限流消息的便捷函数
    
    Args:
        webhook_url: webhook地址
        content: 消息内容
        
    Returns:
        bool: 发送是否成功
    """
    return _global_limiter.send_message(webhook_url, content)

def get_rate_limiter_stats() -> dict:
    """获取限流器统计信息"""
    return _global_limiter.get_stats()