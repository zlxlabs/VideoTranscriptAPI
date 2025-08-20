"""
企业微信webhook限流管理器

实现功能:
- 每个webhook地址独立限流，互不干扰
- 每分钟最多20条消息，超限后自动排队
- 消息间隔至少300ms
- 支持滑动窗口限流算法
- 线程安全，自动管理worker线程
"""

import json
import time
import threading
import requests
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Optional
from queue import Queue, Empty

from .logger import setup_logger

# 创建日志记录器
logger = setup_logger("webhook_rate_limiter")


@dataclass
class WebhookMessage:
    """webhook消息数据结构"""
    webhook_url: str
    content: str
    timestamp: float
    retry_count: int = 0
    max_retries: int = 3


class WebhookRateLimiter:
    """
    基于webhook地址的限流管理器
    
    特性:
    - 单例模式，全局统一管理
    - 每个webhook地址独立限流队列
    - 滑动窗口算法：60秒内最多20条消息
    - 消息间隔：至少300ms
    - 自动线程管理：空闲自动退出，需要时自动启动
    """
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        """单例模式实现"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        """初始化限流器"""
        if hasattr(self, '_initialized'):
            return
        self._initialized = True
        
        # 配置参数
        self.max_messages_per_minute = 20  # 每分钟最大消息数
        self.min_interval_seconds = 0.3    # 最小发送间隔（秒）
        self.worker_timeout_seconds = 300  # worker线程空闲超时（秒）
        
        # 每个webhook的消息队列
        self.webhook_queues: Dict[str, Queue] = defaultdict(lambda: Queue(maxsize=1000))
        
        # 每个webhook的发送时间记录（滑动窗口）
        self.send_history: Dict[str, list] = defaultdict(list)
        
        # 每个webhook的处理线程
        self.worker_threads: Dict[str, threading.Thread] = {}
        
        # 线程锁：保护线程管理操作
        self.thread_locks: Dict[str, threading.Lock] = defaultdict(threading.Lock)
        
        # 统计信息
        self.stats = {
            'total_queued': 0,      # 总排队消息数
            'total_sent': 0,        # 总发送成功数
            'total_failed': 0,      # 总发送失败数
            'active_webhooks': 0    # 活跃webhook数
        }
        
        logger.info("webhook限流器已初始化")
    
    def send_message(self, webhook_url: str, content: str) -> bool:
        """
        发送消息（异步加入队列）
        
        Args:
            webhook_url: webhook地址
            content: 消息内容
            
        Returns:
            bool: 是否成功加入队列
        """
        if not webhook_url or not webhook_url.strip():
            logger.warning("webhook地址为空，无法发送消息")
            return False
            
        if not content or not content.strip():
            logger.warning("消息内容为空，跳过发送")
            return False
            
        try:
            message = WebhookMessage(
                webhook_url=webhook_url.strip(),
                content=content.strip(),
                timestamp=time.time()
            )
            
            # 检查队列是否已满
            queue = self.webhook_queues[webhook_url]
            if queue.full():
                logger.warning(f"webhook队列已满，丢弃消息: {webhook_url[:50]}...")
                return False
            
            # 将消息加入对应webhook的队列
            queue.put_nowait(message)
            self.stats['total_queued'] += 1
            
            logger.debug(f"消息已加入队列: {webhook_url[:50]}..., 内容长度: {len(content)}")
            
            # 启动worker线程（如果尚未启动）
            self._ensure_worker_thread(webhook_url)
            
            return True
            
        except Exception as e:
            logger.exception(f"消息入队失败: {e}")
            return False
    
    def _ensure_worker_thread(self, webhook_url: str):
        """确保指定webhook的worker线程已启动"""
        with self.thread_locks[webhook_url]:
            # 检查线程是否存在且活跃
            if (webhook_url not in self.worker_threads or 
                not self.worker_threads[webhook_url].is_alive()):
                
                thread = threading.Thread(
                    target=self._worker_thread,
                    args=(webhook_url,),
                    daemon=True,
                    name=f"webhook-worker-{hash(webhook_url) % 10000}"
                )
                thread.start()
                self.worker_threads[webhook_url] = thread
                
                logger.info(f"启动webhook worker线程: {webhook_url[:50]}...")
                self._update_active_webhooks_count()
    
    def _worker_thread(self, webhook_url: str):
        """处理指定webhook的消息队列"""
        logger.info(f"webhook worker线程已启动: {webhook_url[:50]}...")
        
        last_send_time = 0  # 上次发送时间
        
        try:
            while True:
                try:
                    # 获取消息（带超时）
                    message = self.webhook_queues[webhook_url].get(
                        timeout=self.worker_timeout_seconds
                    )
                    
                    # 计算需要等待的时间
                    current_time = time.time()
                    
                    # 检查最小间隔
                    time_since_last = current_time - last_send_time
                    if time_since_last < self.min_interval_seconds:
                        wait_time = self.min_interval_seconds - time_since_last
                        logger.debug(f"等待最小间隔: {wait_time:.3f}s")
                        time.sleep(wait_time)
                        current_time = time.time()
                    
                    # 检查频率限制
                    if not self._can_send_now(webhook_url):
                        wait_time = self._calculate_wait_time(webhook_url)
                        if wait_time > 0:
                            logger.info(f"webhook频率限制，等待 {wait_time:.1f}s: {webhook_url[:50]}...")
                            time.sleep(wait_time)
                    
                    # 发送消息
                    success = self._send_message_now(message)
                    last_send_time = time.time()
                    
                    if success:
                        self.stats['total_sent'] += 1
                        self._record_send_time(webhook_url)
                        logger.debug(f"消息发送成功: {webhook_url[:50]}...")
                    else:
                        self.stats['total_failed'] += 1
                        # 重试逻辑
                        if message.retry_count < message.max_retries:
                            message.retry_count += 1
                            logger.info(f"消息发送失败，重试第{message.retry_count}次")
                            # 重新放入队列
                            self.webhook_queues[webhook_url].put_nowait(message)
                        else:
                            logger.error(f"消息发送失败，超过最大重试次数: {webhook_url[:50]}...")
                    
                except Empty:
                    # 超时无消息，退出线程
                    logger.info(f"webhook worker线程空闲超时，退出: {webhook_url[:50]}...")
                    break
                    
        except Exception as e:
            logger.exception(f"webhook worker线程异常退出: {e}")
        finally:
            # 清理线程记录
            with self.thread_locks[webhook_url]:
                if webhook_url in self.worker_threads:
                    del self.worker_threads[webhook_url]
            
            self._update_active_webhooks_count()
            logger.info(f"webhook worker线程已退出: {webhook_url[:50]}...")
    
    def _can_send_now(self, webhook_url: str) -> bool:
        """检查当前是否可以发送消息（基于滑动窗口）"""
        current_time = time.time()
        
        # 清理60秒前的记录
        history = self.send_history[webhook_url]
        self.send_history[webhook_url] = [
            t for t in history if current_time - t < 60
        ]
        
        # 检查是否超过频率限制
        current_count = len(self.send_history[webhook_url])
        can_send = current_count < self.max_messages_per_minute
        
        if not can_send:
            logger.debug(f"webhook已达到频率限制: {current_count}/{self.max_messages_per_minute}")
        
        return can_send
    
    def _calculate_wait_time(self, webhook_url: str) -> float:
        """计算需要等待的时间"""
        current_time = time.time()
        history = self.send_history[webhook_url]
        
        if len(history) < self.max_messages_per_minute:
            return max(0, self.min_interval_seconds)
        
        # 找到最早的记录
        oldest_time = min(history)
        
        # 计算需要等待多久才能发送下一条消息
        time_passed = current_time - oldest_time
        wait_time = max(
            self.min_interval_seconds,  # 最小间隔
            60 - time_passed + 0.1      # 确保滑动窗口生效
        )
        
        return wait_time
    
    def _send_message_now(self, message: WebhookMessage) -> bool:
        """立即发送消息到企业微信"""
        try:
            data = {
                "msgtype": "text",
                "text": {
                    "content": message.content
                }
            }
            
            response = requests.post(
                message.webhook_url,
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
            logger.error(f"webhook请求超时: {message.webhook_url[:50]}...")
            return False
        except requests.exceptions.RequestException as e:
            logger.error(f"webhook请求异常: {e}")
            return False
        except Exception as e:
            logger.exception(f"发送消息时发生未知异常: {e}")
            return False
    
    def _record_send_time(self, webhook_url: str):
        """记录发送时间到历史记录"""
        self.send_history[webhook_url].append(time.time())
    
    def _update_active_webhooks_count(self):
        """更新活跃webhook统计"""
        self.stats['active_webhooks'] = len([
            t for t in self.worker_threads.values() if t.is_alive()
        ])
    
    def get_stats(self) -> dict:
        """获取统计信息"""
        self._update_active_webhooks_count()
        
        # 计算队列统计
        total_queued_current = sum(
            q.qsize() for q in self.webhook_queues.values()
        )
        
        return {
            **self.stats,
            'current_queued': total_queued_current,
            'webhooks_with_queues': len(self.webhook_queues),
            'send_history_size': sum(len(h) for h in self.send_history.values())
        }
    
    def get_webhook_status(self, webhook_url: str) -> dict:
        """获取指定webhook的状态"""
        current_time = time.time()
        
        # 清理历史记录
        history = self.send_history[webhook_url]
        recent_history = [t for t in history if current_time - t < 60]
        self.send_history[webhook_url] = recent_history
        
        return {
            'webhook_url': webhook_url[:50] + "..." if len(webhook_url) > 50 else webhook_url,
            'queue_size': self.webhook_queues[webhook_url].qsize(),
            'recent_sends_count': len(recent_history),
            'can_send_now': self._can_send_now(webhook_url),
            'worker_active': (
                webhook_url in self.worker_threads and 
                self.worker_threads[webhook_url].is_alive()
            ),
            'next_available_time': current_time + self._calculate_wait_time(webhook_url)
        }


# 全局实例
webhook_rate_limiter = WebhookRateLimiter()


def send_rate_limited_message(webhook_url: str, content: str) -> bool:
    """
    发送限流消息的便捷函数
    
    Args:
        webhook_url: webhook地址
        content: 消息内容
        
    Returns:
        bool: 是否成功加入发送队列
    """
    return webhook_rate_limiter.send_message(webhook_url, content)


def get_rate_limiter_stats() -> dict:
    """获取限流器统计信息"""
    return webhook_rate_limiter.get_stats()


def get_webhook_status(webhook_url: str) -> dict:
    """获取指定webhook的状态信息"""
    return webhook_rate_limiter.get_webhook_status(webhook_url)