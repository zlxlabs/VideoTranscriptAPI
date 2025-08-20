import os
import json
import logging
from logging.handlers import RotatingFileHandler

# 加载配置文件
def load_config():
    """
    加载配置文件
    """
    # 获取项目根目录下的配置文件路径
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(current_dir)))
    config_path = os.path.join(project_root, "config", "config.json")
    
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)

# 创建日志目录
def ensure_dir(directory):
    """
    确保目录存在
    """
    if not os.path.exists(directory):
        os.makedirs(directory)

# 创建日志对象
def setup_logger(name, config=None):
    """
    设置日志记录器
    
    参数:
        name: 日志记录器名称
        config: 配置信息，如果为None则从配置文件加载
        
    返回:
        logger: 日志记录器对象
    """
    if config is None:
        config = load_config()
    
    log_config = config.get("log", {})
    log_level = getattr(logging, log_config.get("level", "INFO"))
    log_format = log_config.get("format", "%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    log_file = log_config.get("file", "./logs/app.log")
    max_size = log_config.get("max_size", 10 * 1024 * 1024)  # 默认10MB
    backup_count = log_config.get("backup_count", 5)
    
    # 确保日志目录存在
    log_dir = os.path.dirname(log_file)
    ensure_dir(log_dir)
    
    # 创建日志记录器
    logger = logging.getLogger(name)
    logger.setLevel(log_level)
    
    # 清除现有处理程序（避免重复添加）
    if logger.handlers:
        logger.handlers.clear()
    
    # 添加控制台处理程序
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_formatter = logging.Formatter(log_format)
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)
    
    # 添加文件处理程序
    file_handler = RotatingFileHandler(
        log_file, maxBytes=max_size, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setLevel(log_level)
    file_formatter = logging.Formatter(log_format)
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)
    
    return logger 