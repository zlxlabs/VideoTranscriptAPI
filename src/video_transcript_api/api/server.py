import uvicorn

from .app import create_app
from .context import get_config, get_logger

app = create_app()


def start_server():
    """启动API服务器"""
    config = get_config()
    host = config.get("api", {}).get("host", "0.0.0.0")
    port = config.get("api", {}).get("port", 8000)
    get_logger().info("启动API服务器: %s:%s", host, port)
    uvicorn.run(app, host=host, port=port)
