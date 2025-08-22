"""
API调用审计日志模块

提供API调用统计和审计功能，支持多用户监控。
"""

import os
import sqlite3
import threading
import json
from datetime import datetime
from typing import Optional, Dict, Any, List
from .logger import setup_logger

logger = setup_logger("audit_logger")


class AuditLogger:
    """API调用审计日志记录器"""
    
    def __init__(self, db_path: str = None):
        """
        初始化审计日志记录器
        
        Args:
            db_path: SQLite数据库文件路径，默认为 data/audit.db
        """
        if db_path is None:
            # 默认数据库路径
            current_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
            data_dir = os.path.join(current_dir, "data")
            os.makedirs(data_dir, exist_ok=True)
            db_path = os.path.join(data_dir, "audit.db")
        
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_database()
        logger.info(f"审计日志记录器初始化完成，数据库路径: {self.db_path}")
    
    def _init_database(self):
        """初始化数据库表结构"""
        with self._lock:
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                
                # 创建API审计日志表
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS api_audit_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        api_key_masked TEXT NOT NULL,      -- API密钥（脱敏存储）
                        user_id TEXT,                      -- 用户ID
                        endpoint TEXT NOT NULL,            -- 请求的端点路径
                        video_url TEXT,                    -- 请求中的视频URL（可选）
                        request_time DATETIME DEFAULT CURRENT_TIMESTAMP,
                        processing_time_ms INTEGER,        -- 处理耗时（毫秒）
                        status_code INTEGER,               -- 响应状态码
                        task_id TEXT,                      -- 生成的任务ID（便于关联）
                        user_agent TEXT,                   -- 用户代理
                        remote_ip TEXT                     -- 客户端IP地址
                    )
                ''')
                
                # 创建索引提升查询性能
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_api_key ON api_audit_logs(api_key_masked)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_id ON api_audit_logs(user_id)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_request_time ON api_audit_logs(request_time)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_endpoint ON api_audit_logs(endpoint)')
                
                conn.commit()
                conn.close()
                logger.info("API审计日志数据库表初始化完成")
                
            except Exception as e:
                logger.error(f"初始化审计日志数据库失败: {str(e)}")
                raise
    
    def _mask_api_key(self, api_key: str) -> str:
        """
        对API密钥进行脱敏处理
        
        Args:
            api_key: 原始API密钥
            
        Returns:
            str: 脱敏后的API密钥（保留前4位和后4位）
        """
        if not api_key or len(api_key) < 8:
            return "****"
        
        return f"{api_key[:4]}{'*' * (len(api_key) - 8)}{api_key[-4:]}"
    
    def log_api_call(self, 
                     api_key: str,
                     user_id: Optional[str],
                     endpoint: str,
                     video_url: Optional[str] = None,
                     processing_time_ms: Optional[int] = None,
                     status_code: Optional[int] = None,
                     task_id: Optional[str] = None,
                     user_agent: Optional[str] = None,
                     remote_ip: Optional[str] = None) -> bool:
        """
        记录API调用日志
        
        Args:
            api_key: API密钥
            user_id: 用户ID
            endpoint: 请求端点
            video_url: 视频URL（可选）
            processing_time_ms: 处理耗时（毫秒）
            status_code: HTTP状态码
            task_id: 任务ID
            user_agent: 用户代理
            remote_ip: 客户端IP
            
        Returns:
            bool: 记录是否成功
        """
        try:
            with self._lock:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                
                # 脱敏处理API密钥
                api_key_masked = self._mask_api_key(api_key)
                
                cursor.execute('''
                    INSERT INTO api_audit_logs 
                    (api_key_masked, user_id, endpoint, video_url, processing_time_ms, 
                     status_code, task_id, user_agent, remote_ip)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    api_key_masked, user_id, endpoint, video_url, processing_time_ms,
                    status_code, task_id, user_agent, remote_ip
                ))
                
                conn.commit()
                conn.close()
                
                logger.debug(f"API调用日志记录成功: {endpoint}, 用户: {user_id}")
                return True
                
        except Exception as e:
            logger.error(f"记录API调用日志失败: {str(e)}")
            return False
    
    def get_user_stats(self, user_id: str, days: int = 30) -> Dict[str, Any]:
        """
        获取指定用户的使用统计
        
        Args:
            user_id: 用户ID
            days: 统计天数，默认30天
            
        Returns:
            dict: 用户使用统计信息
        """
        try:
            with self._lock:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                
                # 查询指定天数内的统计数据
                cursor.execute('''
                    SELECT 
                        COUNT(*) as total_calls,
                        COUNT(DISTINCT DATE(request_time)) as active_days,
                        AVG(processing_time_ms) as avg_processing_time,
                        MAX(request_time) as last_call_time,
                        MIN(request_time) as first_call_time
                    FROM api_audit_logs 
                    WHERE user_id = ? 
                    AND request_time >= datetime('now', '-{} days')
                '''.format(days), (user_id,))
                
                stats = cursor.fetchone()
                
                # 查询端点使用统计
                cursor.execute('''
                    SELECT endpoint, COUNT(*) as count
                    FROM api_audit_logs 
                    WHERE user_id = ? 
                    AND request_time >= datetime('now', '-{} days')
                    GROUP BY endpoint
                    ORDER BY count DESC
                '''.format(days), (user_id,))
                
                endpoint_stats = cursor.fetchall()
                
                # 查询状态码统计
                cursor.execute('''
                    SELECT status_code, COUNT(*) as count
                    FROM api_audit_logs 
                    WHERE user_id = ? 
                    AND request_time >= datetime('now', '-{} days')
                    AND status_code IS NOT NULL
                    GROUP BY status_code
                    ORDER BY count DESC
                '''.format(days), (user_id,))
                
                status_stats = cursor.fetchall()
                
                conn.close()
                
                return {
                    "user_id": user_id,
                    "days": days,
                    "total_calls": stats[0] if stats[0] else 0,
                    "active_days": stats[1] if stats[1] else 0,
                    "avg_processing_time_ms": round(stats[2], 2) if stats[2] else 0,
                    "last_call_time": stats[3],
                    "first_call_time": stats[4],
                    "endpoint_stats": [{"endpoint": ep[0], "count": ep[1]} for ep in endpoint_stats],
                    "status_stats": [{"status_code": st[0], "count": st[1]} for st in status_stats]
                }
                
        except Exception as e:
            logger.error(f"获取用户统计失败: {str(e)}")
            return {"error": str(e)}
    
    def get_all_users_stats(self, days: int = 30) -> List[Dict[str, Any]]:
        """
        获取所有用户的使用统计
        
        Args:
            days: 统计天数，默认30天
            
        Returns:
            list: 所有用户的使用统计列表
        """
        try:
            with self._lock:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                
                # 获取活跃用户列表
                cursor.execute('''
                    SELECT DISTINCT user_id
                    FROM api_audit_logs 
                    WHERE user_id IS NOT NULL
                    AND request_time >= datetime('now', '-{} days')
                '''.format(days))
                
                user_ids = [row[0] for row in cursor.fetchall()]
                conn.close()
                
                # 获取每个用户的统计
                all_stats = []
                for user_id in user_ids:
                    user_stats = self.get_user_stats(user_id, days)
                    if "error" not in user_stats:
                        all_stats.append(user_stats)
                
                return all_stats
                
        except Exception as e:
            logger.error(f"获取所有用户统计失败: {str(e)}")
            return []
    
    def get_recent_calls(self, user_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """
        获取最近的API调用记录
        
        Args:
            user_id: 用户ID，为空则获取所有用户的记录
            limit: 返回记录数量限制
            
        Returns:
            list: API调用记录列表
        """
        try:
            with self._lock:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                
                if user_id:
                    cursor.execute('''
                        SELECT api_key_masked, user_id, endpoint, video_url, 
                               request_time, processing_time_ms, status_code, 
                               task_id, user_agent, remote_ip
                        FROM api_audit_logs 
                        WHERE user_id = ?
                        ORDER BY request_time DESC
                        LIMIT ?
                    ''', (user_id, limit))
                else:
                    cursor.execute('''
                        SELECT api_key_masked, user_id, endpoint, video_url, 
                               request_time, processing_time_ms, status_code, 
                               task_id, user_agent, remote_ip
                        FROM api_audit_logs 
                        ORDER BY request_time DESC
                        LIMIT ?
                    ''', (limit,))
                
                rows = cursor.fetchall()
                conn.close()
                
                # 转换为字典列表
                calls = []
                for row in rows:
                    calls.append({
                        "api_key_masked": row[0],
                        "user_id": row[1],
                        "endpoint": row[2],
                        "video_url": row[3],
                        "request_time": row[4],
                        "processing_time_ms": row[5],
                        "status_code": row[6],
                        "task_id": row[7],
                        "user_agent": row[8],
                        "remote_ip": row[9]
                    })
                
                return calls
                
        except Exception as e:
            logger.error(f"获取最近API调用记录失败: {str(e)}")
            return []
    
    def cleanup_old_logs(self, days: int = 90) -> int:
        """
        清理指定天数之前的日志记录
        
        Args:
            days: 保留天数，默认90天
            
        Returns:
            int: 删除的记录数量
        """
        try:
            with self._lock:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                
                cursor.execute('''
                    DELETE FROM api_audit_logs 
                    WHERE request_time < datetime('now', '-{} days')
                '''.format(days))
                
                deleted_count = cursor.rowcount
                conn.commit()
                conn.close()
                
                logger.info(f"清理了 {deleted_count} 条超过 {days} 天的审计日志记录")
                return deleted_count
                
        except Exception as e:
            logger.error(f"清理审计日志失败: {str(e)}")
            return 0


# 全局审计日志记录器实例
_audit_logger = None
_audit_logger_lock = threading.Lock()


def get_audit_logger() -> AuditLogger:
    """
    获取全局审计日志记录器实例（单例模式）
    
    Returns:
        AuditLogger: 审计日志记录器实例
    """
    global _audit_logger
    
    if _audit_logger is None:
        with _audit_logger_lock:
            if _audit_logger is None:
                _audit_logger = AuditLogger()
    
    return _audit_logger