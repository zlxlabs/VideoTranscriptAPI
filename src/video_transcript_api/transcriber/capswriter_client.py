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

    sentences: List[Tuple[str, int]] = []
    start = 0
    for idx, char in enumerate(text):
        if char in primary_punct:
            chunk = text[start : idx + 1]
            if chunk.strip():
                sentences.append((chunk.strip(), start))
            start = idx + 1

    tail = text[start:]
    if tail.strip():
        sentences.append((tail.strip(), start))

    return sentences


# ============================================================================
# FunASR 兼容格式转换相关函数
# ============================================================================


def _optimize_segment_lengths(
    segments: List[Dict[str, Any]], min_len: int, max_len: int
) -> List[Dict[str, Any]]:
    """优化段落长度：合并短句、分割长句"""
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
                # 合并
                buffer["end_time"] = seg["end_time"]
                buffer["text"] = buffer["text"] + seg["text"]
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
    """在次级标点处分割超长句子。

    时间插值必须拒绝非有限值：start_time / duration 任一非有限时，诚实
    降级为 start_time=end_time=None，文本照常切分、永不丢字。
    """
    text = segment["text"]
    secondary_punct = r"([，,；;])"
    parts = re.split(secondary_punct, text)

    if len(parts) <= 1:
        return [segment]

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

    segments = []
    for idx, (sentence, char_offset) in enumerate(sentences):
        # 该句首 token：正文下标 -> token 下标
        start_token_idx = _find_token_idx(token_positions, char_offset)

        if idx + 1 < len(sentences):
            end_token_idx = _find_token_idx(token_positions, sentences[idx + 1][1])
        else:
            # 末句没有「下一句首」，回退到全文最后一个 token 的时间戳
            end_token_idx = len(timestamps) - 1

        # 安全范围检查
        start_token_idx = max(0, min(start_token_idx, len(timestamps) - 1))
        end_token_idx = max(0, min(end_token_idx, len(timestamps) - 1))

        # 提取时间：NaN/Inf/缺失（None）等无效值经唯一权威解析统一降级为
        # None（与 _split_long_segment 的诚实降级同一口径）——文本永不丢失，
        # 时间宁可为 None 也不能让 round(None)/round(nan) 之类的异常或
        # NaN/Inf 字面量污染整个 FunASR 兼容侧车的生成。
        start_time = _finite_time_or_none(timestamps[start_token_idx])
        end_time = _finite_time_or_none(timestamps[end_token_idx])

        segments.append(
            {
                "start_time": round(start_time, 2) if start_time is not None else None,
                "end_time": round(end_time, 2) if end_time is not None else None,
                "text": sentence,
                "length": len(sentence),
            }
        )

        logger.debug(
            f"句子 {idx + 1}: {len(sentence)} 字符 -> start_token={start_token_idx} "
            f"end_token={end_token_idx} -> {start_time}s-{end_time}s"
        )

    logger.debug(f"初始分段完成: {len(segments)} 个 segments")

    # 长度优化
    optimized = _optimize_segment_lengths(segments, min_len, max_len)
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

    def transcribe_file(self, file_path: str) -> Tuple[bool, List[Path]]:
        """
        同步转录文件（官方 SDK 负责传输，按错误契约重试）

        参数:
            file_path: 要转录的文件路径

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
                transcript = transcribe_file_sync(
                    file_path,
                    server_url,
                    encoding="flac",
                    seg_duration=Config.file_seg_duration,
                    seg_overlap=Config.file_seg_overlap,
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
