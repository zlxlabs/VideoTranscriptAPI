import os
import json
import sys
from pathlib import Path
from loguru import logger

# 全局变量，标记logger是否已经配置
_logger_configured = False
# 全局配置缓存
_config_cache = None

# 加载配置文件
def load_config():
    """
    加载配置文件
    """
    global _config_cache

    # 如果已经加载过配置，直接返回缓存
    if _config_cache is not None:
        return _config_cache

    # 获取项目根目录下的配置文件路径
    current_file = Path(__file__).resolve()
    project_root = current_file.parents[4]
    config_path = project_root / "config" / "config.json"

    with config_path.open("r", encoding="utf-8") as f:
        _config_cache = json.load(f)

    return _config_cache

# 创建日志目录
def ensure_dir(directory):
    """
    确保目录存在
    """
    if not os.path.exists(directory):
        os.makedirs(directory)

# 创建日志对象
def setup_logger(name=None, config=None):
    """
    设置日志记录器（使用 loguru）

    参数:
        name: 日志记录器名称（为了兼容性保留，loguru使用全局logger）
        config: 配置信息，如果为None则从配置文件加载

    返回:
        logger: loguru 日志记录器对象
    """
    global _logger_configured

    # 如果已经配置过，直接返回 logger
    if _logger_configured:
        return logger

    if config is None:
        config = load_config()

    log_config = config.get("log", {})
    log_level = log_config.get("level", "INFO").upper()
    log_file = log_config.get("file", "./logs/app.log")
    max_size = log_config.get("max_size", 10 * 1024 * 1024)  # 默认10MB
    backup_count = log_config.get("backup_count", 5)

    # 确保日志目录存在
    log_dir = os.path.dirname(log_file)
    ensure_dir(log_dir)

    # 移除默认的 handler
    logger.remove()

    # 添加控制台处理程序
    logger.add(
        sys.stdout,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level=log_level,
        colorize=True
    )

    # 添加文件处理程序
    logger.add(
        log_file,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
        level=log_level,
        rotation=max_size,
        retention=backup_count,
        encoding="utf-8",
        enqueue=True  # 异步写入，提高性能
    )

    _logger_configured = True
    return logger 
