import logging
import os
import sys
from pathlib import Path
from loguru import logger

try:
    import commentjson as json  # 支持 JSONC 格式（带注释的 JSON）
except ImportError:
    import json  # 降级使用标准 json（不支持注释）

# 当前 production sink ids。空列表表示仍处于只写 stdout 的 bootstrap 阶段。
_production_sink_ids = []
_bootstrap_sink_id = None
# 全局配置缓存
_config_cache = None


class _InterceptHandler(logging.Handler):
    """把 stdlib logging record 转发到 loguru，保留级别/异常信息。"""

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover
        try:
            level = logger.level(record.levelname).name
        except (ValueError, AttributeError):
            level = record.levelno

        # 跳过 logging 内部的帧，尽量定位到真实调用点
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )

# 加载配置文件
def load_config(config_path=None, *, use_cache=True):
    """
    加载配置文件
    """
    global _config_cache

    # 如果已经加载过配置，直接返回缓存
    if use_cache and _config_cache is not None:
        return _config_cache

    # 获取项目根目录下的配置文件路径
    current_file = Path(__file__).resolve()
    project_root = current_file.parents[4]
    config_path = Path(
        config_path
        or os.environ.get("VTAPI_CONFIG")
        or project_root / "config" / "config.jsonc"
    )

    with config_path.open("r", encoding="utf-8") as f:
        parsed = json.load(f)

    if use_cache:
        _config_cache = parsed
    return parsed

# 创建日志目录
def ensure_dir(directory):
    """
    确保目录存在
    """
    if not os.path.exists(directory):
        os.makedirs(directory)


def _validate_loguru_rotation_string(value: str) -> None:
    """校验 log.max_size 字符串是否是 loguru rotation 真正能解析的值。

    真实启动路径：setup_logger() 把 max_size 原样传给
    `logger.add(rotation=max_size, ...)`（loguru），其内部
    `FileSink._make_rotation_function` 对字符串依次尝试
    parse_size -> parse_duration -> parse_frequency -> parse_daytime，全部
    失败才抛 `ValueError("Cannot parse rotation from: ...")`。这里只复用
    前两种（size/duration）——本项目里这个字段语义上就是"多大轮转一次"的
    日志文件大小限制，不支持 frequency（"daily"）/daytime（"10:30"）这类
    按星期/按钟点轮转的写法；真要配置这两种生僻形式会被这里误判为非法，
    但这与字段名 max_size 的实际用途不符，不在本项目支持范围内。

    parse_size/parse_duration 来自 loguru 的私有模块 `_string_parsers`
    （无公开 API 暴露同等能力）。私有 API 存在未来版本改名/删除的风险，
    因此包一层 try/except ImportError：一旦真的失效，就降级为宽松放行
    （不阻塞 --check-config，只是退回到"仅检查是 int/str"这层弱校验），
    而不是让 --check-config 自身崩溃。测试
    test_loguru_string_parsers_private_api_is_importable 锁定了当前 loguru
    版本下这个导入总是可用，一旦未来升级 loguru 导致导入失效，会被那个
    测试先炸出来，而不是这里的校验静默变弱却无人察觉。

    注意：parse_size/parse_duration 在"字符串形状匹配但内容非法"时会直接
    抛 ValueError（而不是返回 None）——这里不捕获吞掉，让它照原样传播，
    因为真实的 logger.add() 在同样输入下也会抛同一个异常，我们要的就是
    让 --check-config 复现这个真实崩溃，而不是把它压成一个更弱的判断。

    Args:
        value: log.max_size 的字符串值（int 值不会走到这个函数）。

    Raises:
        ValueError: value 既不是合法的 size 表达式，也不是合法的 duration
            表达式（或 loguru 私有解析器自身对畸形输入抛出的 ValueError）。
    """
    try:
        from loguru._string_parsers import parse_duration, parse_size
    except ImportError:
        return

    if parse_size(value) is not None:
        return
    if parse_duration(value) is not None:
        return
    raise ValueError(
        f"log.max_size string is not a value loguru rotation can parse: {value!r}"
    )


def _validate_loguru_retention_string(value: str) -> None:
    """校验 log.backup_count 字符串是否是 loguru retention 真正能解析的值。

    真实启动路径同上一函数，唯一区别：loguru 的
    `FileSink._make_retention_function` 对字符串只尝试 parse_duration，
    没有 parse_size 这一档回退——一个看起来像大小的字符串（如 "10 MB"）
    或一个裸数字字符串（如 "5"，容易和整数 5 混淆）都不是合法的 retention
    值，真实启动会在这里抛 ValueError。私有 API 降级策略与上面的 rotation
    校验相同，见其 docstring。

    Args:
        value: log.backup_count 的字符串值（int 值不会走到这个函数）。

    Raises:
        ValueError: value 不是 loguru 能解析的 duration 表达式。
    """
    try:
        from loguru._string_parsers import parse_duration
    except ImportError:
        return

    if parse_duration(value) is not None:
        return
    raise ValueError(
        f"log.backup_count string is not a value loguru retention can parse: {value!r}"
    )


def _parse_log_settings(config: dict) -> dict:
    """从配置解析 log 段并做与真实启动一致的类型校验（纯函数，无 IO 副作用）。

    从 setup_logger() 里抽出的原因：setup_logger 在生产模式（bootstrap=False）
    下会真的创建 loguru sink 并写文件（副作用），无法安全地在 --check-config
    的演练路径（api/context.py::load_and_validate_config）里直接调用——这里把
    "log.level 是否为字符串、file 路径是否为非空字符串、max_size/backup_count
    是否为 loguru rotation/retention 能接受的 int/str，以及字符串值本身是否
    真的是 loguru 解析器能识别的形状"这部分类型约束抽成不产生 IO 的纯解析，
    供 setup_logger 本体与 --check-config 演练链共用，保证两边永远同步、
    不会出现"预检通过、真实启动才因垃圾类型/垃圾字符串崩溃"的缺口。

    Args:
        config: 完整配置字典（读取其中的 "log" 段，缺失时按内置默认值处理）。

    Returns:
        dict: {"level", "file", "max_size", "backup_count"}，level 已转大写。

    Raises:
        TypeError: 对应字段存在但类型不符（如 level 传数字、file 传对象）。
        ValueError: level 是字符串但不是 loguru 已注册的合法级别名（如拼写
            错误）；或 max_size/backup_count 是字符串但内容不是 loguru
            rotation/retention 解析器能接受的形状（见
            _validate_loguru_rotation_string/_validate_loguru_retention_string）。
    """
    log_config = config.get("log", {})
    if not isinstance(log_config, dict):
        raise TypeError("log configuration must be an object")

    log_level = log_config.get("level", "INFO")
    if not isinstance(log_level, str):
        raise TypeError("log.level must be a string")
    log_level = log_level.upper()
    # 纯查表校验级别名拼写（如 "INF"）：只读已注册级别，不新增/移除任何 sink。
    logger.level(log_level)

    log_file = log_config.get("file", "./logs/app.log")
    if not isinstance(log_file, str) or not log_file.strip():
        raise TypeError("log.file must be a non-empty string")

    max_size = log_config.get("max_size", 10 * 1024 * 1024)  # 默认10MB
    if not isinstance(max_size, (int, str)) or isinstance(max_size, bool):
        raise TypeError("log.max_size must be an integer or string")
    if isinstance(max_size, str):
        _validate_loguru_rotation_string(max_size)

    backup_count = log_config.get("backup_count", 5)
    if not isinstance(backup_count, (int, str)) or isinstance(backup_count, bool):
        raise TypeError("log.backup_count must be an integer or string")
    if isinstance(backup_count, str):
        _validate_loguru_retention_string(backup_count)

    return {
        "level": log_level,
        "file": log_file,
        "max_size": max_size,
        "backup_count": backup_count,
    }

# 创建日志对象
def setup_logger(name=None, config=None, *, bootstrap=None):
    """
    设置日志记录器（使用 loguru）

    参数:
        name: 日志记录器名称（为了兼容性保留，loguru使用全局logger）
        config: 配置信息，如果为None则从配置文件加载

    返回:
        logger: loguru 日志记录器对象
    """
    global _bootstrap_sink_id, _production_sink_ids

    # Import-time callers receive a console-only logger. Strict configuration
    # loading belongs to the application lifespan, never module import.
    if bootstrap is None:
        bootstrap = config is None
    if bootstrap:
        if _bootstrap_sink_id is None and not _production_sink_ids:
            logger.remove()
            _bootstrap_sink_id = logger.add(
                sys.stdout,
                format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
                level="INFO",
                colorize=False,
            )
        return logger

    if config is None:
        raise ValueError("production logger configuration is required")

    settings = _parse_log_settings(config)
    log_level = settings["level"]
    log_file = settings["file"]
    max_size = settings["max_size"]
    backup_count = settings["backup_count"]

    # 确保日志目录存在
    # log_file 若是无目录前缀的相对路径（如 "app.log"），os.path.dirname
    # 返回空字符串；ensure_dir("") 会走到 os.makedirs("") 崩溃
    # （FileNotFoundError: 空路径），故仅在确有目录部分时才创建。
    log_dir = os.path.dirname(log_file)
    if log_dir:
        ensure_dir(log_dir)

    # Lifespans may be created repeatedly in tests or during reload. Replace
    # previous sinks instead of letting a one-shot flag silently keep stale
    # paths and levels.
    logger.remove()
    _bootstrap_sink_id = None
    _production_sink_ids = []

    # 把 stdlib logging 的 record 转发到 loguru sink
    # （让第三方模块或本项目内用 logging.getLogger() 的代码也能被统一格式化）
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)

    # 添加控制台处理程序
    _production_sink_ids.append(logger.add(
        sys.stdout,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level=log_level,
        colorize=True
    ))

    # 添加文件处理程序
    _production_sink_ids.append(logger.add(
        log_file,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
        level=log_level,
        rotation=max_size,
        retention=backup_count,
        encoding="utf-8",
        enqueue=True  # 异步写入，提高性能
    ))

    return logger


def shutdown_logger():
    """Flush and remove production sinks, returning to bootstrap mode."""
    global _bootstrap_sink_id, _production_sink_ids
    logger.complete()
    logger.remove()
    _production_sink_ids = []
    _bootstrap_sink_id = None
    setup_logger(bootstrap=True)
