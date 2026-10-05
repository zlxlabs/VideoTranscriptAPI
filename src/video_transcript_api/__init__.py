"""视频转录API - 主要包初始化文件

一个基于Python的视频转录API服务，支持从多个平台下载视频并转录为文字。
"""

__version__ = "1.0.0"
__author__ = "视频转录API团队"

# 导出主要组件
from .api.server import app
from .utils.logging import setup_logger

__all__ = ['app', 'setup_logger']
