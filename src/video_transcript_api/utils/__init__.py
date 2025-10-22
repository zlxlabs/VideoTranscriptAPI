"""Utility subpackages for video_transcript_api.

This module exposes only the minimal helpers that remain widely used
across the codebase. Most features now live in dedicated subpackages
under ``video_transcript_api.utils``.
"""

import os
from .logging import setup_logger, load_config, ensure_dir


def create_debug_dir() -> str:
    """
    从配置文件读取并创建 debug 日志目录。

    Returns:
        str: debug 目录的完整路径
    """
    config = load_config()
    log_config = config.get("log", {})
    debug_dir = log_config.get("debug_dir", "./data/logs/debug")

    if not os.path.exists(debug_dir):
        os.makedirs(debug_dir, exist_ok=True)

    return debug_dir


def get_llm_debug_dir() -> str:
    """
    从配置文件读取并创建 LLM debug 日志目录。

    Returns:
        str: LLM debug 目录的完整路径
    """
    config = load_config()
    log_config = config.get("log", {})
    llm_debug_dir = log_config.get("llm_debug_dir", "./data/logs/llm_debug")

    if not os.path.exists(llm_debug_dir):
        os.makedirs(llm_debug_dir, exist_ok=True)

    return llm_debug_dir


__all__ = ["setup_logger", "load_config", "ensure_dir", "create_debug_dir", "get_llm_debug_dir"]
