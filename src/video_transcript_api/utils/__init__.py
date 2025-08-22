from .logger import setup_logger, load_config, ensure_dir
from .wechat import WechatNotifier, wechat_notify
from .metadata_cache import MetadataCache
from .cache_manager import CacheManager
from .user_manager import UserManager, get_user_manager
from .audit_logger import AuditLogger, get_audit_logger
import os

def create_debug_dir():
    """
    创建调试日志目录
    
    返回:
        str: 调试日志目录路径
    """
    # 创建logs目录
    logs_dir = "logs"
    if not os.path.exists(logs_dir):
        os.makedirs(logs_dir)
    
    # 创建调试子目录
    debug_dir = os.path.join(logs_dir, "debug")
    if not os.path.exists(debug_dir):
        os.makedirs(debug_dir)
    
    return debug_dir

__all__ = [
    "setup_logger", 
    "load_config",
    "ensure_dir",
    "WechatNotifier",
    "wechat_notify",
    "create_debug_dir",
    "MetadataCache",
    "CacheManager",
    "UserManager",
    "get_user_manager",
    "AuditLogger",
    "get_audit_logger"
] 