"""统一的"带时间片段"读取适配器。

背景（均来自生产环境实测，而非猜测）：
    - `transcript_funasr.json`（生产 FunASR 转写产物）顶层含 `segments` 数组，
      每条字段为 `start_time`/`end_time`（float 秒）、`text`、`speaker`、`words`。
    - 仓库内部分历史测试样例（test_cache_dir/ 下）使用 `start`/`end` 命名
      （无 `_time` 后缀），需要同时兼容两种命名。
    - `llm_processed.json` 的 dialogs 时间是 `"00:00:41"` 这种 HH:MM:SS 字符串，
      需要能解析为秒数。
    - `transcript_capswriter.txt` 是纯文本、无时间信息；未来会出现 FunASR 兼容
      格式的 `transcript_capswriter.json`（结构同 funasr，但没有 speaker 字段）。

本模块把这些落盘格式统一读取/规范化为一份 `list[dict]`，供上层（如字幕分片、
摘要定位等）消费。核心不变式——**文本永不丢失**：只要一条记录有非空 `text`，
即便时间字段缺失或损坏，也必须保留该条目（把时间置为 `None`），绝不能因为
时间脏就整条丢弃。
"""

import json
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from ..utils.logging import setup_logger

logger = setup_logger("segments_adapter")

# 允许作为"时间片段来源文件"的候选文件名，按优先级从高到低排列。
# funasr 原生输出优先；capswriter 的 FunASR 兼容 JSON（无 speaker 字段）次之。
_SEGMENT_SOURCE_FILENAMES = ("transcript_funasr.json", "transcript_capswriter.json")

# "HH:MM:SS"/"MM:SS" 字符串里，非末位分量（小时/分钟）合法的写法：纯数字。
# 用于在 float() 转换之前拒绝 float() 能接受、但时钟分量不该出现的写法——
# 小数（"0.5"）、科学计数法（"1e2"）、PEP 515 数字分组下划线（"1_0"）——
# 见 parse_time_to_seconds 中的用法说明（gate-r25 P2）。
_CLOCK_INTEGER_COMPONENT_PATTERN = re.compile(r"\d+")
# 末位分量（秒）额外放宽：允许一个可选的小数部分，兼容 "00:01:23.4" 这类
# 带小数秒精度的合法写法——秒是唯一可以合法带小数的时钟单位。
_CLOCK_SECONDS_COMPONENT_PATTERN = re.compile(r"\d+(\.\d+)?")


def parse_time_to_seconds(value: Any) -> Optional[float]:
    """把多种时间表示形式解析为秒数（float）。

    支持的输入形态：
        - int / float：直接当作秒数。
        - 纯数字字符串，如 `"12.5"`。
        - `"HH:MM:SS"` / `"MM:SS"` 字符串（如 llm_processed.json 里的
          dialog 时间 `"00:00:41"`）。

    无法解析的情况（None、空串/纯空白、非法结构、垃圾字符串、负数、
    非数字类型等）一律返回 `None`，绝不抛异常——调用方无需 try/except 包裹。
    负数被视为非法时间（时间不可能为负），同样返回 `None`。

    非有限值同样视为非法时间，一律返回 `None`：`float("inf")`、`nan`、
    以及会因溢出变成 `inf` 的超大数字字符串（如 `"1e309"`）都不例外——
    否则下游对时间做 `int()` 转换时会直接崩溃。用 `math.isfinite` 兜底，
    覆盖 int/float 直传和字符串解析两类入口。

    损坏的时钟分量不得伪装成合法时间：`"HH:MM:SS"`（三段）要求分钟、秒
    分量均 `< 60`（小时不限——长录音的小时数可以合法超过 99）；
    `"MM:SS"`（两段）只要求秒分量 `< 60`（分钟不限，`"125:30"` 这类
    "总分钟数" 表示是合法输入）。任一分量越界（如 `"00:99:00"`、
    `"00:00:99"`）一律返回 `None`，而不是像 `total = h*3600+m*60+s` 那样
    静默换算出一个看似合理、实则被污染的秒数。

    JSON 允许任意精度整数，反序列化后可能拿到 `10**400` 这类超出 double 可
    表示范围的 Python int——`float()` 转换会抛 `OverflowError`（而不是像
    字符串解析那样静默变成 `inf`），同样按非法时间处理，返回 `None`。

    Args:
        value: 任意来源的原始时间值。

    Returns:
        解析成功返回秒数（float，>= 0 且为有限值）；解析失败返回 None。
    """
    if value is None:
        return None

    # bool 是 int 的子类，显式排除，避免 True/False 被误当成 1/0 秒
    if isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        try:
            seconds = float(value)
        except OverflowError:
            # JSON 允许任意精度整数，反序列化后可能拿到 10**400 这类天文数字的
            # Python int；float() 转换会因超出 double 可表示范围抛
            # OverflowError，与本函数"绝不抛异常"的契约冲突，按非法时间处理。
            return None
        return seconds if math.isfinite(seconds) and seconds >= 0 else None

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None

        if ":" in text:
            parts = [p.strip() for p in text.split(":")]
            if len(parts) not in (2, 3):
                return None
            # 逐分量校验格式，而不是等 float() 转换完再兜底（gate-r25 P2）：
            # float() 能接受的写法比"时钟分量"该有的写法宽得多——小数
            # ("0.5")、科学计数法（"1e2"）、PEP 515 数字分组下划线
            # ("1_0")——这些对小时/分钟这类整数时钟单位而言都是非法输入，
            # 不能被静默接受成合法时间（如 "0.5:00:00"、"1e2:00"、"1_0:00"
            # 均须判定为非法）。唯一例外是末位分量（秒）：额外放宽允许一个
            # 可选的小数部分，兼容 "00:01:23.4" 这类带小数秒的合法写法。
            #
            # 这层校验同时覆盖了负号分量的拒绝：像 "01:-01:00" 这样单个分量
            # 为负、但 hours*3600+minutes*60+seconds 累加后总和恰好仍是正数
            # 的畸形时间，之前需要专门的字符串前缀判断（不能等 float() 转换
            # 完再用 `n < 0` 判断，因为 "-00" 会被解析成 -0.0，而 -0.0 < 0
            # 在 IEEE 754 下为 False）——纯数字正则天然不匹配任何带 "-" 前缀
            # 的分量，因此这条规则不需要再单独写一遍。
            for idx, part in enumerate(parts):
                is_seconds_component = idx == len(parts) - 1
                component_pattern = (
                    _CLOCK_SECONDS_COMPONENT_PATTERN
                    if is_seconds_component
                    else _CLOCK_INTEGER_COMPONENT_PATTERN
                )
                if not component_pattern.fullmatch(part):
                    return None
            try:
                numbers = [float(p) for p in parts]
            except ValueError:
                return None
            if not all(math.isfinite(n) for n in numbers):
                return None
            if len(numbers) == 2:
                hours = 0.0
                minutes, seconds_part = numbers
                # 两段 "MM:SS"：分钟不设上限（"125:30" 这类总分钟数表示是
                # 合法输入），但秒分量仍是一个真实的时钟单位，必须 < 60，
                # 否则 "00:99" 这类损坏值会被静默换算成 99 秒。
                if not (seconds_part < 60):
                    return None
            else:
                hours, minutes, seconds_part = numbers
                # 三段 "HH:MM:SS"：分钟、秒都是时钟单位，均须 < 60；小时
                # 不限（长录音的小时数可以合法超过 99）。
                if not (minutes < 60 and seconds_part < 60):
                    return None
            total = hours * 3600 + minutes * 60 + seconds_part
            return total if math.isfinite(total) and total >= 0 else None

        try:
            seconds = float(text)
        except ValueError:
            return None
        return seconds if math.isfinite(seconds) and seconds >= 0 else None

    # 其他类型（list/dict/自定义对象等）一律视为垃圾值
    return None


def sanitize_time_pair(
    start: Optional[float], end: Optional[float]
) -> Tuple[Optional[float], Optional[float]]:
    """校验一对时间戳的合理性，摘掉不合逻辑的值，但绝不影响调用方对文本的保留。

    权威实现归位于此（时间解析工具的统一家，`parse_time_to_seconds` 也在这里）。
    三条 YouTube 字幕解析路径（youtube-transcript-api 分段 / TikHub XML / SRT，
    见 `downloaders/subtitle_types.py` 的历史文档）以及本模块自身的
    `normalize_segments` 都要经过这同一道校验，统一"诚实降级"口径：宁可时间
    字段是 None，也不能是一个误导下游的负数或倒挂区间。`downloaders/
    subtitle_types.py` 从本模块导入并 re-export，保持既有 import 路径可用。

    校验只做数值合理性判断，不做有限性 (isfinite) 检查——那一层容错由各调用方
    在算出 start / end 之前（如经 `parse_time_to_seconds`）就已经做过。

    规则（按顺序应用）：
        1. start 为负数 -> start 置 None（负的起始时间没有物理意义，常见
           于上游 start 字段本身就是脏数据）
        2. end 为负数 -> end 置 None（常见于 end = start + duration 中
           duration 为负、且求和结果本身也变成负数的情况）
        3. start、end 均非 None 且 end < start（区间倒挂：duration 为负但
           求和结果仍非负、或时间轴书写顺序颠倒）-> end 置 None

    参数:
        start: 起始时间（秒），None 表示不可用
        end: 结束时间（秒），None 表示不可用

    返回:
        (start, end): 按上述规则校验后的元组；text 字段的保留完全不受
        此函数影响，调用方应始终保留原文本
    """
    if start is not None and start < 0:
        start = None
    if end is not None and end < 0:
        end = None
    if start is not None and end is not None and end < start:
        end = None
    return start, end


def normalize_segments(
    raw: Union[Dict[str, Any], List[Any], None]
) -> Optional[List[Dict[str, Any]]]:
    """把裸 list 或 `{"segments": [...]}` 两种形态的原始数据规范化。

    产出的每条记录形如：
        `{"start_time": float|None, "end_time": float|None, "text": str}`
        若原始数据里有 `speaker` 字段，则额外携带 `"speaker": str`；
        若原始数据没有 speaker，绝不编造 "unknown" 之类占位值。

    字段名兼容 `start_time|start`、`end_time|end`（优先取 `_time` 后缀版本）。

    解析出的 (start_time, end_time) 对会再经 `sanitize_time_pair` 清洗一遍——
    与三条 YouTube 字幕解析路径同一口径：end < start 的倒挂区间会把 end_time
    置 None，不放行给下游。

    核心不变式——文本永不丢失：只要 `text` 非空，即便时间解析失败，也保留该
    条目（时间置 None）；只有 `text` 缺失/为空（含纯空白）的条目才会被跳过。

    Args:
        raw: 顶层 dict（含 `segments` 键）或裸 list；其他类型视为非法。

    Returns:
        规范化后的 list[dict]；若没有任何有效条目（含结构非法、全部无效），
        返回 None。
    """
    if isinstance(raw, dict):
        items = raw.get("segments")
    elif isinstance(raw, list):
        items = raw
    else:
        return None

    if not isinstance(items, list):
        return None

    normalized: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            # 非 dict 的脏条目直接跳过，不影响其余合法条目
            continue

        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            # 文本缺失/为空（含纯空白）的条目没有留存价值，跳过
            continue

        start_raw = item.get("start_time", item.get("start"))
        end_raw = item.get("end_time", item.get("end"))

        start_time, end_time = sanitize_time_pair(
            parse_time_to_seconds(start_raw), parse_time_to_seconds(end_raw)
        )
        entry: Dict[str, Any] = {
            "start_time": start_time,
            "end_time": end_time,
            "text": text,
        }

        speaker = item.get("speaker")
        if speaker is not None:
            entry["speaker"] = str(speaker)

        normalized.append(entry)

    return normalized if normalized else None


def load_segments(cache_dir: Path) -> Optional[List[Dict[str, Any]]]:
    """从缓存目录读取带时间片段的转写数据。

    读取优先级：`transcript_funasr.json` > `transcript_capswriter.json`。
    若较高优先级的来源不存在、JSON 损坏，或规范化后没有任何有效条目
    （视为"该来源无可用数据"），则依次尝试下一优先级来源；全部尝试失败时
    记一条 warning 日志并返回 None——本函数不抛异常，调用方无需
    try/except 包裹。

    注：`transcript_capswriter.txt` 是纯文本、没有时间信息，不作为片段来源。

    Args:
        cache_dir: 单个媒体的缓存目录（即包含 transcript_*.json 的目录）。

    Returns:
        规范化后的 list[dict]；找不到可用数据时返回 None。
    """
    cache_dir = Path(cache_dir)

    for filename in _SEGMENT_SOURCE_FILENAMES:
        file_path = cache_dir / filename

        try:
            # `exists()` 也纳入这同一个 try：pathlib 只静默 ENOENT/ENOTDIR 之类
            # "路径本就不存在"的错误，但当缓存目录的父目录缺少执行位（EACCES，
            # 如生产环境权限配置错误）时会重抛 PermissionError（OSError 子类）。
            # 若 `exists()` 留在 try 之外，这种权限问题会让本函数破坏"从不抛
            # 异常"的契约，直接崩溃给调用方，而非按现有语义降级到下一来源。
            if not file_path.exists():
                continue
            with file_path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
            # UnicodeDecodeError 是 ValueError 的子类而非 OSError，必须单独列出，
            # 否则遇到非法 UTF-8 字节（如生产环境偶发的编码损坏文件）会绕过这个
            # except 直接抛出，违反本模块"从不抛异常"的契约。RecursionError 同理
            # 是 RuntimeError 的子类而非 OSError/ValueError——病态深度嵌套的 JSON
            # （如构造攻击或损坏导出文件里的 "[[[[...]]]]"）会让 json.load() 的
            # 递归下降解析器超出 Python 默认递归深度抛出 RecursionError，同样必须
            # 单独列出。三者与 OSError/JSONDecodeError 同等对待：记 warning，
            # 尝试下一优先级来源。PermissionError 是 OSError 子类，同样被覆盖。
            logger.warning(f"读取时间片段文件失败，尝试下一优先级来源: {file_path} ({exc})")
            continue

        segments = normalize_segments(raw)
        if segments:
            return segments

        logger.warning(f"时间片段文件无有效条目，尝试下一优先级来源: {file_path}")

    logger.warning(f"未找到可用的时间片段来源: {cache_dir}")
    return None
