from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class VideoMetadata:
    """标准化的视频元数据结构"""

    video_id: str
    platform: str
    title: str = ""
    author: str = ""
    description: str = ""
    duration: Optional[float] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DownloadInfo:
    """标准化的下载信息结构"""

    download_url: Optional[str]
    file_ext: Optional[str]
    filename: Optional[str] = None
    file_size: Optional[int] = None
    subtitle_url: Optional[str] = None
    local_file: Optional[str] = None
    downloaded: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)
