"""Utility subpackages for video_transcript_api.

This module exposes only the minimal helpers that remain widely used
across the codebase. Most features now live in dedicated subpackages
under ``video_transcript_api.utils``.
"""

import os
from .logging import setup_logger, load_config, ensure_dir


class _LazyConfiguredDir(os.PathLike):
    """Resolve and create a configured directory only when first used."""

    def __init__(self, field: str, default: str):
        self.field = field
        self.default = default

    def __fspath__(self) -> str:
        config = load_config()
        path = config.get("log", {}).get(self.field, self.default)
        os.makedirs(path, exist_ok=True)
        return path

    def __str__(self) -> str:
        return self.__fspath__()


def create_debug_dir() -> os.PathLike:
    """
    从配置文件读取并创建 debug 日志目录。

    Returns:
        str: debug 目录的完整路径
    """
    return _LazyConfiguredDir("debug_dir", "./data/logs/debug")


def get_llm_debug_dir() -> os.PathLike:
    """
    从配置文件读取并创建 LLM debug 日志目录。

    Returns:
        str: LLM debug 目录的完整路径
    """
    return _LazyConfiguredDir("llm_debug_dir", "./data/logs/llm_debug")


__all__ = ["setup_logger", "load_config", "ensure_dir", "create_debug_dir", "get_llm_debug_dir"]
