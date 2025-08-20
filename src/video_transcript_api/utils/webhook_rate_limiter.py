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
    sequence_id: int  # 添加全局序列号，确保FIFO顺序
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
    _instance_lock = threading.Lock()
    _init_lock = threading.Lock()
    
    def __new__(cls):
        """单例模式实现 - 线程安全的双重检查锁定"""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        """初始化限流器 - 确保只初始化一次"""
        # 使用独立的初始化锁，避免与__new__冲突
        with self._init_lock:
            if hasattr(self, '_initialized') and self._initialized:
                logger.debug("限流器已初始化，跳过重复初始化")
                return
            self._initialized = True
            logger.info("webhook限流器开始初始化...")
        
        # 配置参数
        self.max_messages_per_minute = 20  # 每分钟最大消息数
        self.min_interval_seconds = 0.8    # 最小发送间隔（秒）
        self.worker_timeout_seconds = 300  # worker线程空闲超时（秒）
        
        # 每个webhook的消息序列号计数器（确保同一webhook消息严格按顺序处理）
        self.webhook_sequence_counters: Dict[str, int] = defaultdict(int)
        self._sequence_locks: Dict[str, threading.RLock] = defaultdict(threading.RLock)  # 每个webhook一个锁
        
        # 每个webhook的消息队列
        self.webhook_queues: Dict[str, Queue] = defaultdict(lambda: Queue(maxsize=1000))
        
        # 每个webhook的发送时间记录（滑动窗口）
        self.send_history: Dict[str, list] = defaultdict(list)
        
        # 每个webhook的处理线程
        self.worker_threads: Dict[str, threading.Thread] = {}
        
        # 线程锁：保护线程管理操作
        self.thread_locks: Dict[str, threading.Lock] = defaultdict(threading.Lock)
        
        # 用于统计目的的全局发送锁（保护统计数据）
        self._stats_lock = threading.Lock()
        
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
            # 分配该webhook的序列号
            webhook_url_clean = webhook_url.strip()
            with self._sequence_locks[webhook_url_clean]:
                self.webhook_sequence_counters[webhook_url_clean] += 1
                sequence_id = self.webhook_sequence_counters[webhook_url_clean]
            
            message = WebhookMessage(
                webhook_url=webhook_url_clean,
                content=content.strip(),
                timestamp=time.time(),
                sequence_id=sequence_id
            )
            
            # 检查队列是否已满
            queue = self.webhook_queues[webhook_url_clean]
            if queue.full():
                logger.warning(f"webhook队列已满，丢弃消息: {webhook_url_clean[:50]}...")
                return False
            
            # 将消息加入对应webhook的队列
            queue.put_nowait(message)
            with self._stats_lock:
                self.stats['total_queued'] += 1
            
            # 增强日志：显示消息内容预览和序列号
            content_preview = content[:100].replace('\n', ' ')  # 取前100字符并替换换行符
            logger.info(f"[队列入队] Webhook序列号: {sequence_id}, 内容长度: {len(content)}, 预览: {content_preview}...")
            logger.debug(f"消息已加入队列: {webhook_url_clean[:50]}..., 时间戳: {message.timestamp}, 序列号: {sequence_id}")
            
            # 启动worker线程（如果尚未启动）
            self._ensure_worker_thread(webhook_url_clean)
            
            return True
            
        except Exception as e:
            logger.exception(f"消息入队失败: {e}")
            return False
    
    def _ensure_worker_thread(self, webhook_url: str):
        """确保指定webhook的worker线程已启动"""
        with self.thread_locks[webhook_url]:
            # 双重检查：先检查是否已存在活跃线程
            current_thread = self.worker_threads.get(webhook_url)
            if current_thread is not None and current_thread.is_alive():
                logger.debug(f"Worker线程已存在且活跃: {webhook_url[:50]}...")
                return
            
            # 如果线程不存在或已死亡，创建新线程
            logger.info(f"创建新的webhook worker线程: {webhook_url[:50]}...")
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
                    
                    # 发送消息（依赖Python Queue的FIFO特性保证顺序）
                    content_preview = message.content[:100].replace('\n', ' ')  # 消息内容预览
                    logger.info(f"[队列发送] 开始发送消息, Webhook序列号: {message.sequence_id}, 预览: {content_preview}...")
                    
                    success = self._send_message_now(message)
                    last_send_time = time.time()
                    
                    if success:
                        with self._stats_lock:
                            self.stats['total_sent'] += 1
                        self._record_send_time(webhook_url)
                        logger.info(f"[队列发送] 消息发送成功, Webhook序列号: {message.sequence_id}, 预览: {content_preview}...")
                        logger.debug(f"消息发送成功: {webhook_url[:50]}...")
                    else:
                        with self._stats_lock:
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


# 全局实例 - 延迟初始化
webhook_rate_limiter = None
_global_limiter_lock = threading.Lock()


def _get_global_limiter():
    """获取全局限流器实例 - 线程安全的延迟初始化"""
    global webhook_rate_limiter
    if webhook_rate_limiter is None:
        with _global_limiter_lock:
            if webhook_rate_limiter is None:
                import threading
                thread_name = threading.current_thread().name
                logger.info(f"在线程 {thread_name} 中创建全局webhook限流器实例")
                webhook_rate_limiter = WebhookRateLimiter()
                logger.info(f"全局webhook限流器实例已创建，实例ID: {id(webhook_rate_limiter)}")
    return webhook_rate_limiter


def send_rate_limited_message(webhook_url: str, content: str) -> bool:
    """
    发送限流消息的便捷函数
    
    Args:
        webhook_url: webhook地址
        content: 消息内容
        
    Returns:
        bool: 是否成功加入发送队列
    """
    return _get_global_limiter().send_message(webhook_url, content)


def get_rate_limiter_stats() -> dict:
    """获取限流器统计信息"""
    return _get_global_limiter().get_stats()


def get_webhook_status(webhook_url: str) -> dict:
    """获取指定webhook的状态信息"""
    return _get_global_limiter().get_webhook_status(webhook_url)