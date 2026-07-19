import uvicorn

from .app import create_app
from .context import load_and_validate_config, setup_logger

app = create_app()


def start_server():
    """启动API服务器"""
    config = load_and_validate_config()
    runtime_app = create_app(config_loader=lambda: config)
    host = config.get("api", {}).get("host", "0.0.0.0")
    port = config.get("api", {}).get("port", 8000)
    setup_logger("api_server", config=config, bootstrap=False).info(
        "启动API服务器: %s:%s", host, port
    )
    uvicorn.run(runtime_app, host=host, port=port)
