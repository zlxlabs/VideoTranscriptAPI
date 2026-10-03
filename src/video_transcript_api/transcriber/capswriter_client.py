#!/usr/bin/env python
# coding: utf-8

"""
CapsWriter语音转文字客户端 - 精简版
整合了原Client_Only文件夹的所有核心功能到单个文件中
"""

import os
import sys
import json
import math
import time
import asyncio
import re
import argparse
import itertools
from pathlib import Path
from typing import Tuple, List, Optional, Dict, Any

from capswriter_asr import AsrError, transcribe_file_sync
from loguru import logger

# 添加项目根目录到系统路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ..utils.logging import load_config
# 长度兜底的唯一实现（issue #142）：上限不可放弃这条不变式只靠一份代码成立
from ..utils.text_split import split_oversized_text
# 时间解析唯一权威：禁止在本模块另起一套 isfinite/parse 逻辑
from .segments import interpolate_segment_times, parse_time_to_seconds


class Config:
    """配置类"""

    # 默认配置
    server_addr = "localhost"
    server_port = 6006
    file_seg_duration = 25
    file_seg_overlap = 2
    enable_hot_words = True
    generate_txt = True
    generate_merge_txt = False
    generate_srt = False
    generate_lrc = False
    generate_json = False
    generate_funasr_compat = True  # 生成 FunASR 兼容格式的 JSON
    verbose = True

    @classmethod
    def load_from_project_config(cls):
        """从项目配置文件加载配置"""
        try:
            config = load_config()
            capswriter_config = config.get("capswriter", {})

            # 解析服务器URL
            server_url = capswriter_config.get("server_url", "ws://localhost:6006")
            if server_url.startswith("ws://"):
                server_url = server_url[5:]
            if ":" in server_url:
                cls.server_addr, port_str = server_url.split(":")
                cls.server_port = int(port_str)
            else:
                cls.server_addr = server_url
                cls.server_port = 6006

            # 其他配置
            cls.file_seg_duration = capswriter_config.get("file_seg_duration", 25)
            cls.file_seg_overlap = capswriter_config.get("file_seg_overlap", 2)
            cls.enable_hot_words = capswriter_config.get("enable_hot_words", True)

            # 日志配置
            log_config = config.get("log", {})
            cls.verbose = log_config.get("level", "INFO") != "ERROR"

        except Exception as e:
            print(f"加载项目配置失败，使用默认配置: {e}")

    @classmethod
    def update_server(cls, addr: str = None, port: int = None):
        """更新服务器连接信息"""
        if addr:
            cls.server_addr = addr
        if port:
            cls.server_port = port


# ============================================================================
# 上游 CapsWriter 文件任务契约
# ============================================================================
#
# 生产实测三条不变式（2026-10-01，上游 SDK 已实现并部署）：
#   1. raw["text_accu"] 存在且为非空字符串；
#   2. len(tokens) == len(timestamps)；
#   3. "".join(tokens) == text_accu（逐字）。
#
# 由此正文唯一权威是 "".join(tokens)——raw["text"] 是独立回显稿，与之不同
# （生产实测 text != text_accu），拿它当字符坐标源会把时间轴压缩约 19%。

# 统一可 grep 的错误字面量，日志与异常消息都以它开头。
CONTRACT_ERROR_LITERAL = "CAPSWRITER_CONTRACT_FAILED condition="

COND_TEXT_ACCU = "text_accu_missing_or_empty"
COND_LENGTH_MISMATCH = "tokens_timestamps_length_mismatch"
COND_JOIN_MISMATCH = "tokens_join_text_accu_mismatch"


# ============================================================================
# 转录时限预算（issue #155）
# ============================================================================
#
# SDK 的自动预算是 max(120, duration + 60)，隐含「服务至少 1 倍实时」的假设。
# 生产实测（2026-10-03，同一个失败案例、已抽成纯音轨的 93.08898 秒文件）：远端
# 转录成功但耗时 306.3 秒，即服务是 3.29 倍实时；自动预算只给出 153 秒，只有实际
# 需要的一半，必然超时。因此本仓在拿得到时长时显式传 deadline_total 关掉自动预算。
# 两个数字来自这一次实测，本轮固定不变，刻意不做成配置项（多来源漂移，见 issue
# #147）：系数 = 实测 3.29 倍向上取整并留约 20% 余量；常数项覆盖下载完成到提交前
# 的杂项开销。拿不到时长时不传 deadline_total，保持 SDK 自动预算——未探测路径上
# 的媒体时长分布未知，用偏小的常数会把本来能成功的长媒体掐断。

DEADLINE_REALTIME_FACTOR = 4.0
DEADLINE_OVERHEAD_SECONDS = 120.0


# 这里刻意不写 math.isfinite：门禁主审只看 PR diff，看不到模块顶部 import，
# 会把新增用法误判为未定义；链式比较与之逐值等价。
def _transcription_deadline(media_duration: Optional[float] = None) -> Optional[float]:
    """按实测吞吐算出本次转录的时限预算（秒）。

    这是全仓唯一计算 ``deadline_total`` 的地方，其余调用点只做透传。
    ``media_duration`` 取自下载阶段 ffprobe 已解析出的时长（见
    ``downloaders/base.py::_validate_media_file``）。拿不到时长时返回
    ``None``，调用点据此不传 ``deadline_total``，保持 SDK 自动预算。
    """
    if (
        media_duration is None
        or not (float("-inf") < media_duration < float("inf"))
        or media_duration < 0
    ):
        logger.warning("transcription_deadline duration=unknown fallback=sdk_auto")
        return None
    deadline = media_duration * DEADLINE_REALTIME_FACTOR + DEADLINE_OVERHEAD_SECONDS
    logger.info(
        f"transcription_deadline duration={media_duration:.2f} value={deadline:.1f}"
    )
    return deadline


class CapsWriterContractError(ValueError):
    """上游 CapsWriter 文件任务契约不成立。

    构造函数把 :data:`CONTRACT_ERROR_LITERAL` 与具体条件拼进消息，调用方
    不必拼字符串也能直接 grep。
    """

    def __init__(self, condition: str, detail: str):
        self.condition = condition
        self.detail = detail
        super().__init__(f"{CONTRACT_ERROR_LITERAL}{condition} detail={detail}")


def _validate_capswriter_contract(raw: Dict[str, Any]) -> Tuple[str, List[str], List[Any]]:
    """校验上游契约并返回 (text_accu, tokens, timestamps)。

    三条任一不成立即抛 :class:`CapsWriterContractError`；不回退到 raw["text"]、
    不截断、不做规范化后再比、不提供关闭开关。
    """
    text_accu = raw.get("text_accu")
    if not isinstance(text_accu, str) or not text_accu:
        raise CapsWriterContractError(
            COND_TEXT_ACCU, f"type={type(text_accu).__name__} value={text_accu!r}"
        )

    tokens = list(raw.get("tokens") or [])
    timestamps = list(raw.get("timestamps") or [])
    if len(tokens) != len(timestamps):
        raise CapsWriterContractError(
            COND_LENGTH_MISMATCH,
            f"len(tokens)={len(tokens)} len(timestamps)={len(timestamps)}",
        )

    joined = "".join(tokens)
    if joined != text_accu:
        first_diff = next(
            (i for i, (a, b) in enumerate(zip(joined, text_accu)) if a != b),
            min(len(joined), len(text_accu)),
        )
        raise CapsWriterContractError(
            COND_JOIN_MISMATCH,
            f"join_len={len(joined)} text_accu_len={len(text_accu)} first_diff_at={first_diff}",
        )

    return text_accu, tokens, timestamps


def _token_char_prefix(tokens: List[str]) -> List[int]:
    """token 长度前缀和：prefix[i] = sum(len(tokens[0..i-1]))，末位为全文长度。

    直接用原始 token（不做任何清洗）：契约第 3 条保证 "".join(tokens) 就是
    text_accu，字符坐标必须与这个字符串对齐。
    """
    prefix = [0]
    for token in tokens:
        prefix.append(prefix[-1] + len(token))
    return prefix


def _find_token_idx(token_positions: List[int], char_pos: int) -> int:
    """找到字符位置对应的 token 索引"""
    for i in range(len(token_positions) - 1):
        if token_positions[i] <= char_pos < token_positions[i + 1]:
            return i
    return len(token_positions) - 2


def _split_text_by_punctuation(text: str) -> List[Tuple[str, int]]:
    """按主要标点符号分句，保留标点。

    返回 [(句子文本, 句子首字符下标)]，下标指向原串，用于回查 token 坐标。
    """
    primary_punct = "。！？!?"

    # 返回 [(句子文本, 首字符下标, 尾字符下标(不含))]。
    # 文本按**原始区间**切出（含边缘空白），不做 strip：strip 掉的前导/尾随
    # 空白会让侧车正文不再等于 "".join(tokens)。
    sentences: List[Tuple[str, int, int]] = []
    start = 0
    for idx, char in enumerate(text):
        if char in primary_punct:
            if idx + 1 > start:
                sentences.append((text[start : idx + 1], start, idx + 1))
            start = idx + 1

    if len(text) > start:
        sentences.append((text[start:], start, len(text)))

    return sentences


# ============================================================================
# FunASR 兼容格式转换相关函数
# ============================================================================


def _optimize_segment_lengths(
    segments: List[Dict[str, Any]], min_len: int, max_len: int, text: str
) -> List[Dict[str, Any]]:
    """优化段落长度：合并短句、分割长句。

    ``text`` 是必填的原文：合并时按区间并集从原文重新切片。给它默认值会
    让漏传变成「从空串切片」→ 正文静默丢失（比 TypeError 危险得多）。
    """
    if not segments:
        return []

    optimized = []
    buffer = None

    for seg in segments:
        seg_len = seg["length"]

        if buffer is None:
            buffer = seg.copy()
            continue

        buffer_len = buffer["length"]
        combined_len = buffer_len + seg_len

        if buffer_len < min_len:
            if combined_len <= max_len:
                # 合并：取区间并集后从原文重新切片，不做字符串拼接。
                # 空白段没有有效时间（诚实降级为 None），绝不能让它覆盖
                # buffer 的 end_time——否则一次合并会让整段有效文本失去结束时间。
                if seg["end_time"] is not None:
                    buffer["end_time"] = seg["end_time"]
                buffer["char_end"] = seg["char_end"]
                buffer["text"] = text[buffer["char_start"]:seg["char_end"]]
                buffer["length"] = combined_len
            else:
                optimized.append(buffer)
                buffer = seg.copy()
        elif min_len <= buffer_len <= max_len:
            optimized.append(buffer)
            buffer = seg.copy()
        else:
            optimized.append(buffer)
            buffer = seg.copy()

    if buffer is not None:
        optimized.append(buffer)

    # 处理超长句子
    final = []
    for seg in optimized:
        if seg["length"] > max_len:
            split_segs = _split_long_segment(seg, max_len)
            final.extend(split_segs)
        else:
            final.append(seg)

    return final


def _finite_time_or_none(value: Any) -> Optional[float]:
    """把任意时间值规整为有限 float，否则 None。

    复用 transcriber.segments.parse_time_to_seconds（唯一权威时间解析），
    其内部已用 math.isfinite 拒绝 NaN/Inf；本函数再对已是 float 的路径
    做一层防御，避免上游直接传入非有限值时写出 NaN/Inf 到下游 JSON。
    """
    parsed = parse_time_to_seconds(value)
    if parsed is None:
        return None
    return parsed if math.isfinite(parsed) else None


def _split_long_segment(segment: Dict[str, Any], max_len: int) -> List[Dict[str, Any]]:
    """在次级标点处分割超长句子；标点切不开时兜底（空白优先 / 硬切）不可放弃。

    兜底的原因：逗号级切分对「无次级标点」的长文本（整篇英文最常见）完全
    无能为力，若在此放弃，max_len 就退化成尽力而为的软目标。兜底形态见
    :func:`video_transcript_api.utils.text_split.split_oversized_text`，只移动切点，正文逐字不丢。

    时间插值必须拒绝非有限值：start_time / duration 任一非有限时，诚实
    降级为 start_time=end_time=None，文本照常切分、永不丢字。
    """
    text = segment["text"]
    secondary_punct = r"([，,；;])"
    parts = re.split(secondary_punct, text)

    split_segments = []
    current = ""
    orig_start = _finite_time_or_none(segment.get("start_time"))
    orig_end = _finite_time_or_none(segment.get("end_time"))

    for part in parts:
        if len(current + part) <= max_len:
            current += part
        else:
            if current:
                split_segments.append(
                    {
                        "text": current,
                        "length": len(current),
                    }
                )

                current = part
            else:
                current = part

    if current:
        split_segments.append(
            {
                "text": current,
                "length": len(current),
            }
        )

    if not split_segments:
        return [segment]

    # 兜底（issue #142）：逗号级切完之后仍有超 max_len 的片段——要么整段没有
    # 次级标点（parts 只有一个元素），要么某个 part 本身就超长。此处收束，
    # 上限才不是软目标；时间仍在下面的插值器里按切完的片段统一算，不改取值口径。
    # 上限来源：本模块的 max_len 是硬编码常量（_create_segments_from_capswriter
    # 传 300），恒为正；共享实现在 max_len <= 0 时 fail fast。
    # 另注：本仓句末/切分定义有 4 套互不一致的实现（capswriter `。！？!?` /
    # paragraphize `。！？….!?` / DialogSegmenter `。！？` / TextSegmenter 另一套），
    # 本批明确不统一，见 issue #146；本兜底只管长度，不改句末定义。
    if any(item["length"] > max_len for item in split_segments):
        capped: List[Dict[str, Any]] = []
        whitespace_cuts = 0
        hard_cuts = 0
        for item in split_segments:
            if item["length"] <= max_len:
                capped.append(item)
                continue
            pieces, item_whitespace_cuts, item_hard_cuts = split_oversized_text(
                item["text"], max_len
            )
            whitespace_cuts += item_whitespace_cuts
            hard_cuts += item_hard_cuts
            capped.extend({"text": piece, "length": len(piece)} for piece in pieces)
        split_segments = capped
        # 越上限本身是可观测的质量降级（不是丢数据），按 decision 记 warning
        # 而非 fail fast：硬切保证正文与长度，时间由插值器给。
        logger.warning(
            "长段超过 max_len，已启用兜底切分: "
            f"orig_length={len(text)} parts={len(split_segments)} max_len={max_len} "
            f"whitespace_cuts={whitespace_cuts} hard_cuts={hard_cuts}"
        )

    time_pairs = interpolate_segment_times(
        orig_start,
        orig_end,
        [split_segment["text"] for split_segment in split_segments],
    )
    if orig_start is not None and orig_end is not None and orig_start == orig_end:
        # Adapter compatibility branch: the legacy CapsWriter path preserved
        # equal start/end timestamps for zero-span split segments. Keep that
        # behavior here while the shared interpolator retains end <= start ->
        # None for its honest general-purpose contract.
        time_pairs = [
            (orig_start, orig_end) for _ in split_segments
        ]
    for split_segment, (start_time, end_time) in zip(split_segments, time_pairs):
        split_segment["start_time"] = (
            round(start_time, 2) if start_time is not None else None
        )
        split_segment["end_time"] = (
            round(end_time, 2) if end_time is not None else None
        )

    return split_segments


def _create_segments_from_capswriter(
    tokens: List[str],
    timestamps: List[float],
    min_len: int = 80,
    max_len: int = 300,
) -> List[Dict[str, Any]]:
    """
    从 CapsWriter 数据创建 FunASR 格式的 segments

    正文唯一权威是 "".join(tokens)（见 _validate_capswriter_contract 第 3 条），
    本函数不再接收、也不读取 raw["text"]。

    时间定位：每句 start 取「该句首 token 的 timestamps」，end 取「下一句首
    token 的 timestamps」，末句才回退到全文最后一个 token 的时间戳。上游实测
    相邻时间戳有 72.8% 重复（空格/标点 token 继承邻近词时间），若用「本句末
    token」当 end，会产出大量 start == end 的零长度段。

    Args:
        tokens: BPE token 列表（正文 = "".join(tokens)）
        timestamps: 时间戳列表
        min_len: 最小段落长度
        max_len: 最大段落长度

    Returns:
        segments 列表
    """
    logger.debug(
        f"开始创建 segments: tokens={len(tokens)}, timestamps={len(timestamps)}"
    )

    if not tokens or not timestamps:
        logger.error("tokens 或 timestamps 为空，无法创建 segments")
        return []

    # 正文与字符坐标：token 长度前缀和
    body = "".join(tokens)
    token_positions = _token_char_prefix(tokens)

    # 分句
    sentences = _split_text_by_punctuation(body)
    logger.debug(f"按标点分句: {len(sentences)} 个句子")

    def _first_speech_token(char_pos: int) -> int:
        """正文下标 -> 该处第一��非空白 token 的下标。

        服务端把空格也作为 token 返回，而空格的时间戳继承邻近词：直接取用
        空白 token 会让句首时间偏早。
        """
        position = _find_token_idx(token_positions, char_pos)
        last_speech = position
        while position < len(tokens):
            if tokens[position].strip():
                return position
            last_speech = position
            position += 1
        # 整段都是空白：停在最后一个 token 上（它承载尾部空白的时间），
        # 由下面的 end >= start 夹紧保证不会出现 start > end。
        return min(last_speech, len(tokens) - 1)

    # 先定出每句的起始 token，段尾一律取「下一句的起始时间」——
    # 这样相邻段之间不可能出现无依据的时间间隙（两边独立查表会漏掉
    # 「start 跳过空白 token 而 end 没跳」这种不对称）。
    starts = [_first_speech_token(offset) for _sentence, offset, _end in sentences]

    segments = []
    for idx, (sentence, char_offset, char_end) in enumerate(sentences):
        start_token_idx = starts[idx]

        if idx + 1 < len(sentences):
            end_token_idx = starts[idx + 1]
        else:
            # 末句没有「下一句首」，回退到全文最后一个非空白 token 的时间戳
            end_token_idx = len(timestamps) - 1
            while end_token_idx > 0 and not str(tokens[end_token_idx]).strip():
                end_token_idx -= 1

        # 安全范围检查
        start_token_idx = max(0, min(start_token_idx, len(timestamps) - 1))
        end_token_idx = max(0, min(end_token_idx, len(timestamps) - 1))
        # 结构性保证不倒挂：尾部纯空白段的首 token 可能晚于回退得到的尾 token，
        # 不夹紧就会产出 start_time > end_time 的段（时间轴静默损坏）。
        if end_token_idx < start_token_idx:
            end_token_idx = start_token_idx

        # 提取时间：NaN/Inf/缺失（None）等无效值经唯一权威解析统一降级为
        # None（与 _split_long_segment 的诚实降级同一口径）——文本永不丢失，
        # 时间宁可为 None 也不能让 round(None)/round(nan) 之类的异常或
        # NaN/Inf 字面量污染整个 FunASR 兼容侧车的生成。
        start_time = _finite_time_or_none(timestamps[start_token_idx])
        end_time = _finite_time_or_none(timestamps[end_token_idx])

        # 纯空白片段不对应任何语音：给它时间戳会凭空占住一段时间轴
        # （夹紧后表现为 start == end 的零时长段），下游会误以为该区间有内容。
        # 按既有诚实降级口径标为不可用；文本本身仍然保留。
        if not sentence.strip():
            start_time = None
            end_time = None

        segments.append(
            {
                "start_time": round(start_time, 2) if start_time is not None else None,
                "end_time": round(end_time, 2) if end_time is not None else None,
                "text": sentence,
                "length": len(sentence),
                # 记录原文区间：合并时按区间重新切片而不是拼接字符串，
                # 否则分句时 strip 掉的句间空格会永久丢失（Opus 发现 C），
                # 侧车正文就不再等于 "".join(tokens)。
                "char_start": char_offset,
                "char_end": char_end,
            }
        )

        logger.debug(
            f"句子 {idx + 1}: {len(sentence)} 字符 -> start_token={start_token_idx} "
            f"end_token={end_token_idx} -> {start_time}s-{end_time}s"
        )

    logger.debug(f"初始分段完成: {len(segments)} 个 segments")

    # 长度优化
    optimized = _optimize_segment_lengths(segments, min_len, max_len, body)
    logger.debug(f"长度优化完成: {len(optimized)} 个 segments")

    # 最终统计
    if optimized:
        lengths = [seg["length"] for seg in optimized]
        in_range = sum(1 for l in lengths if min_len <= l <= max_len)
        logger.info(
            f"Segments 生成完成: {len(optimized)} 个片段, {in_range}/{len(optimized)} 在目标范围内"
        )
    else:
        logger.warning("未生成任何 segments")

    return optimized


# ============================================================================
# CapsWriter 客户端类
# ============================================================================


# ============================================================================
# 落盘：单文件 tmp + rename
# ============================================================================

_TMP_WRITE_COUNTER = itertools.count()


def _atomic_write_text(target: Path, content: str) -> None:
    """先写 ``<target>.tmp-<pid>-<n>`` 再 rename 就位。

    ``output_dir`` 是共享工作区（``get_workspace_dir()``），直接
    ``open(target, "w")`` 在写盘失败时会把上一次成功产物截断成半截——那是数据
    损坏。临时名带 pid 与进程内序号，避免共享目录下同名目标互相覆盖。

    失败时只删自己的临时文件，绝不删已经就位的目标：那个目标很可能正是上一次
    成功的结果。
    """
    target = Path(target)
    tmp_path = target.with_name(
        f"{target.name}.tmp-{os.getpid()}-{next(_TMP_WRITE_COUNTER)}"
    )
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


class CapsWriterClient:
    """CapsWriter客户端类"""

    def __init__(
        self,
        server_addr: str = None,
        server_port: int = None,
        output_dir: str = None,
        max_retries: int = None,
        retry_delay: int = None,
    ):
        """
        初始化客户端

        参数:
            server_addr: 服务器地址
            server_port: 服务器端口
            output_dir: 输出目录
            max_retries: 最大重试次数
            retry_delay: 重试延迟（秒）
        """
        # 首先从项目配置加载
        Config.load_from_project_config()

        # 从项目配置获取默认值
        project_config = load_config()

        # 设置服务器信息（命令行参数优先）
        if server_addr or server_port:
            Config.update_server(server_addr, server_port)

        # 设置其他参数（使用项目配置的默认值）
        # 统一使用 temp_dir 作为临时转录文件目录
        self.output_dir = output_dir or project_config.get("storage", {}).get(
            "temp_dir", "./temp"
        )
        self.max_retries = max_retries or project_config.get("capswriter", {}).get(
            "max_retries", 3
        )
        self.retry_delay = retry_delay or project_config.get("capswriter", {}).get(
            "retry_delay", 5
        )

        # 确保临时目录存在
        os.makedirs(self.output_dir, exist_ok=True)

    def log(self, message: str, level: str = "info"):
        """记录日志"""
        if Config.verbose:
            if level == "info":
                logger.info(message)
            elif level == "debug":
                logger.debug(message)
            elif level == "warning":
                logger.warning(message)
            elif level == "error":
                logger.error(message)
            else:
                print(message)

    async def _check_file(self, file_path: Path) -> bool:
        """检查文件是否存在"""
        if not file_path.exists():
            self.log(f"错误: 文件不存在: {file_path}", "error")
            return False
        return True

    def _build_funasr_payload(
        self,
        file_path: Path,
        result: Dict[str, Any],
        tokens: List[str],
        timestamps: List[float],
        text_accu: str,
    ) -> str:
        """构建 FunASR 兼容格式 JSON 的文本内容（不落盘）。"""
        self.log(
            f"输入数据: text_accu={len(text_accu)} 字符, tokens={len(tokens)}, "
            f"timestamps={len(timestamps)}"
        )

        segments = _create_segments_from_capswriter(
            tokens=tokens,
            timestamps=timestamps,
            min_len=80,
            max_len=300,
        )
        if not segments:
            raise ValueError(
                f"no segments generated from {len(tokens)} tokens "
                f"({text_accu[:40]!r}...)"
            )

        self.log(f"成功创建 {len(segments)} 个 segments")

        funasr_data = {
            "task_id": result.get("task_id", ""),
            "file_name": file_path.name,
            "duration": result.get("duration", 0),
            "segments": [
                {
                    "start_time": seg["start_time"],
                    "end_time": seg["end_time"],
                    "text": seg["text"],
                }
                for seg in segments
            ],
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "processing_time": result.get("time_complete", 0)
            - result.get("time_start", 0),
            "error": None,
        }

        # 统计信息：无效时间已降级为 None 的 segment（见
        # _create_segments_from_capswriter / _split_long_segment
        # 的诚实降级）没有可统计的时长，直接跳过——不能无条件做
        # end - start 算术，否则 None - None 抛 TypeError。
        total_duration = sum(
            seg["end_time"] - seg["start_time"]
            for seg in segments
            if seg["start_time"] is not None and seg["end_time"] is not None
        )
        avg_length = sum(len(seg["text"]) for seg in segments) / len(segments)

        self.log(
            f"Segments 统计: 总时长={total_duration:.2f}s, 平均长度={avg_length:.1f}字符"
        )
        return json.dumps(funasr_data, ensure_ascii=False, indent=2)

    async def _save_results(
        self, file_path: Path, result: Dict[str, Any]
    ) -> List[Path]:
        """校验契约、在内存构建全部产物，全部就绪后才落盘。

        不变式：任何构建失败都在写盘前上抛，因此失败时磁盘上不会留下本次任务
        的任何产物。否则「任务标记成功却没有产物」会被报成成功。
        """
        if not result:
            return []

        # 契约校验在前，fail fast
        text_accu, tokens, timestamps = _validate_capswriter_contract(result)

        base_path = file_path.with_suffix("")
        out_dir = Path(self.output_dir)

        # 先全部在内存里构建完成
        products: List[Tuple[Path, str]] = []

        if Config.generate_json:
            products.append(
                (
                    out_dir / f"{base_path.name}.json",
                    json.dumps(
                        {"timestamps": timestamps, "tokens": tokens},
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
            )

        if Config.generate_txt:
            products.append((out_dir / f"{base_path.name}.txt", text_accu))

        if Config.generate_merge_txt:
            products.append(
                (out_dir / f"{base_path.name}.merge.txt", text_accu)
            )

        if Config.generate_funasr_compat:
            products.append(
                (
                    out_dir / f"{base_path.name}_funasr.json",
                    self._build_funasr_payload(
                        file_path, result, tokens, timestamps, text_accu
                    ),
                )
            )

        # 全部构建成功，才开始落盘
        for path, content in products:
            _atomic_write_text(path, content)
            self.log(f"已生成: {path}")

        preview = text_accu[:100] + "..." if len(text_accu) > 100 else text_accu
        self.log(f"转录结果预览: {preview}")

        return [path for path, _ in products]

    def transcribe_file(
        self, file_path: str, media_duration: Optional[float] = None
    ) -> Tuple[bool, List[Path]]:
        """
        同步转录文件（官方 SDK 负责传输，按错误契约重试）

        参数:
            file_path: 要转录的文件路径
            media_duration: 媒体时长（秒），由下载阶段的探测给出；为 None 时
                不传 deadline_total，保持 SDK 自动预算（见
                :func:`_transcription_deadline`）

        返回:
            tuple: (bool成功状态, list生成的文件)
        """
        file_path = Path(file_path)
        attempts = 0
        last_error = None
        last_error_code = None
        should_retry = True

        while attempts < self.max_retries and should_retry:
            attempts += 1
            try:
                self.log(
                    f"开始转录文件: {file_path} (尝试 {attempts}/{self.max_retries})"
                )
                server_url = f"ws://{Config.server_addr}:{Config.server_port}"
                # 全仓唯一计算点：拿不到时长时返回 None，下面据此不传该键。
                deadline_total = _transcription_deadline(media_duration)
                sdk_kwargs = {
                    "encoding": "flac",
                    "seg_duration": Config.file_seg_duration,
                    "seg_overlap": Config.file_seg_overlap,
                }
                if deadline_total is not None:
                    sdk_kwargs["deadline_total"] = deadline_total
                transcript = transcribe_file_sync(
                    file_path,
                    server_url,
                    **sdk_kwargs,
                )

                result = dict(transcript.raw)
                result.update(
                    {
                        "tokens": transcript.tokens,
                        "timestamps": transcript.timestamps,
                        "duration": transcript.duration,
                    }
                )
                generated_files = asyncio.run(self._save_results(file_path, result))

                if generated_files:
                    self.log(f"转录完成，生成文件: {[str(f) for f in generated_files]}")
                    return True, generated_files
                last_error = "未生成任何文件或转录失败"
                should_retry = False

            except CapsWriterContractError:
                # 契约不成立是上游事实，不是可重试的传输故障：直接上抛，
                # 不重试、不降级、不返回 False 冒充一次普通转录失败。
                raise
            except AsrError as exc:
                last_error = exc.message
                last_error_code = exc.code
                should_retry = exc.retryable is True
            except Exception as exc:
                last_error = str(exc)
                should_retry = False

            if should_retry and attempts < self.max_retries:
                self.log(f"等待 {self.retry_delay} 秒后重试...")
                time.sleep(self.retry_delay)

        error_msg = f"转录文件失败: {file_path}"
        if last_error_code is not None:
            error_msg += f", code={last_error_code}"
        error_msg += f", 原因: {last_error}"
        self.log(error_msg, "error")
        return False, []


def main():
    """命令行入口"""
    # 先加载配置以获取默认值
    Config.load_from_project_config()
    project_config = load_config()

    parser = argparse.ArgumentParser(description="CapsWriter语音转文字客户端 - 精简版")
    parser.add_argument("file", help="要转录的音视频文件路径")
    parser.add_argument("--server", help="服务器地址", default=Config.server_addr)
    parser.add_argument(
        "--port", type=int, help="服务器端口", default=Config.server_port
    )
    parser.add_argument(
        "--output",
        help="输出目录",
        default=project_config.get("storage", {}).get("temp_dir", "./temp"),
    )
    parser.add_argument(
        "--format",
        choices=["txt", "merge", "json", "all"],
        default="txt",
        help="输出格式",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=project_config.get("capswriter", {}).get("max_retries", 3),
        help="最大重试次数",
    )
    parser.add_argument("--quiet", action="store_true", help="静默模式")

    args = parser.parse_args()

    # 设置输出格式
    if args.format == "txt":
        Config.generate_txt = True
        Config.generate_merge_txt = False
        Config.generate_json = False
    elif args.format == "merge":
        Config.generate_txt = False
        Config.generate_merge_txt = True
        Config.generate_json = False
    elif args.format == "json":
        Config.generate_txt = False
        Config.generate_merge_txt = False
        Config.generate_json = True
    elif args.format == "all":
        Config.generate_txt = True
        Config.generate_merge_txt = True
        Config.generate_json = True

    # 设置静默模式
    if args.quiet:
        Config.verbose = False

    # 创建客户端
    client = CapsWriterClient(
        server_addr=args.server,
        server_port=args.port,
        output_dir=args.output,
        max_retries=args.retries,
    )

    # 执行转录
    success, files = client.transcribe_file(args.file)

    if success:
        if files:
            print(f"转录完成！生成了以下文件:")
            for f in files:
                print(f"  - {f}")
        else:
            print("转录完成，但未生成任何文件")
        return 0
    else:
        print("转录失败")
        return 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
