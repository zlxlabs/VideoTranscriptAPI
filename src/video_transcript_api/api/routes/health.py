"""健康检查路由

提供系统健康状态检查端点，检查 SQLite、ASR 服务、磁盘空间等组件状态。
"""

import os
import sqlite3
import asyncio
from typing import Dict, Any

from fastapi import APIRouter

from ..context import get_cache_manager, get_config, get_logger, lazy_resource

logger = lazy_resource(get_logger)
config = lazy_resource(get_config)

router = APIRouter(tags=["health"])


@router.get("/livez")
async def liveness_probe():
    """纯存活探针端点

    仅表明进程可响应，**不查询任何下游依赖**（CapsWriter / FunASR / 磁盘）。
    供部署探针与 Uptime Kuma 24×7 探活使用：下游抖动不应触发误判/回滚。
    需要下游状态时请用 /health（深度检查）。

    Returns:
        dict: 固定 {"status": "ok"}，恒返回 200
    """
    return {"status": "ok"}


@router.get("/health")
async def health_check():
    """系统健康检查端点

    检查各核心组件的状态，返回整体健康状况。

    Returns:
        dict: 健康状态摘要
    """
    checks = {}

    # 并发检查各组件
    checks["sqlite"] = _check_sqlite()
    checks["capswriter"] = await _check_websocket_service(
        config.get("capswriter", {}).get("server_url", "ws://localhost:6016"),
        "CapsWriter",
    )
    checks["funasr"] = await _check_websocket_service(
        config.get("funasr_spk_server", {}).get("server_url", "ws://localhost:8767"),
        "FunASR",
    )
    checks["disk_space"] = _check_disk_space()

    all_healthy = all(c.get("healthy", False) for c in checks.values())
    status = "healthy" if all_healthy else "degraded"

    return {
        "status": status,
        "checks": checks,
    }


def _check_sqlite() -> Dict[str, Any]:
    """检查 SQLite 数据库连通性"""
    try:
        cache_manager = get_cache_manager()
        conn = sqlite3.connect(str(cache_manager.db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        cursor.close()
        conn.close()
        return {"healthy": True}
    except Exception as e:
        logger.warning(f"SQLite health check failed: {e}")
        return {"healthy": False, "error": str(e)}


async def _check_websocket_service(url: str, name: str) -> Dict[str, Any]:
    """检查 WebSocket 服务连通性

    Args:
        url: WebSocket 服务地址
        name: 服务名称（用于日志）

    Returns:
        dict: 健康状态
    """
    try:
        import websockets
        async with asyncio.timeout(5):
            async with websockets.connect(url, close_timeout=3):
                pass
        return {"healthy": True}
    except ImportError:
        # websockets 未安装，尝试用 socket 检测端口
        return _check_tcp_port(url, name)
    except asyncio.TimeoutError:
        logger.warning(f"{name} health check timed out: {url}")
        return {"healthy": False, "error": "connection timed out"}
    except Exception as e:
        logger.warning(f"{name} health check failed: {url}, error: {e}")
        return {"healthy": False, "error": str(e)}


def _check_tcp_port(url: str, name: str) -> Dict[str, Any]:
    """通过 TCP 连接检查端口可达性（websockets 不可用时的后备方案）

    Args:
        url: WebSocket URL（ws://host:port 格式）
        name: 服务名称

    Returns:
        dict: 健康状态
    """
    import socket
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
        host = parsed.hostname or "localhost"
        port = parsed.port or 80

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        result = sock.connect_ex((host, port))
        sock.close()

        if result == 0:
            return {"healthy": True}
        else:
            return {"healthy": False, "error": f"port {port} unreachable"}
    except Exception as e:
        logger.warning(f"{name} TCP health check failed: {e}")
        return {"healthy": False, "error": str(e)}


def _check_disk_space() -> Dict[str, Any]:
    """检查磁盘空间

    当可用空间低于 1GB 时标记为不健康。
    """
    try:
        stat = os.statvfs(".")
        free_bytes = stat.f_bavail * stat.f_frsize
        free_gb = free_bytes / (1024 ** 3)

        healthy = free_gb >= 1.0
        result = {
            "healthy": healthy,
            "free_gb": round(free_gb, 2),
        }
        if not healthy:
            result["error"] = f"low disk space: {free_gb:.2f} GB"
        return result
    except Exception as e:
        logger.warning(f"Disk space check failed: {e}")
        return {"healthy": False, "error": str(e)}
