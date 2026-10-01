#!/usr/bin/env python
# coding: utf-8

"""
CapsWriter语音转文字客户端 - 精简版
整合了原Client_Only文件夹的所有核心功能到单个文件中
"""

import os
import sys
import json
import hashlib
import math
import time
import asyncio
import json as _json
import re
import argparse
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
    max_segment_length = 300  # 超长段落切分上限（字符数）
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
    def update_server(cls, addr: Optional[str] = None, port: Optional[int] = None):
        """更新服务器连接信息"""
        if addr:
            cls.server_addr = addr
        if port:
            cls.server_port = port


# ============================================================================
# FunASR 兼容格式转换相关函数
# ============================================================================


def _text_summary(value: Any) -> str:
    """Return a non-sensitive summary for contract diagnostics."""
    if not isinstance(value, str):
        return f"type={type(value).__name__}"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"len={len(value)} sha256={digest}"


def _validate_capswriter_contract(
    result: Dict[str, Any],
) -> Tuple[str, List[str], List[Any]]:
    """Validate the upstream file-task contract before any output is written."""
    text_accu = result.get("text_accu")
    tokens = result.get("tokens", [])
    timestamps = result.get("timestamps", [])

    try:
        joined_text = "".join(tokens)
    except TypeError:
        joined_text = None

    token_count = len(tokens) if hasattr(tokens, "__len__") else -1
    timestamp_count = len(timestamps) if hasattr(timestamps, "__len__") else -1
    joined_summary = _text_summary(joined_text)
    accu_summary = _text_summary(text_accu)

    if not isinstance(text_accu, str) or not text_accu:
        message = (
            "CAPSWRITER_CONTRACT_FAILED "
            "condition=text_accu_missing_or_empty "
            f"text_accu={accu_summary} joined={joined_summary} "
            f"tokens_len={token_count} timestamps_len={timestamp_count}"
        )
        logger.warning(message)
        raise ValueError(message)

    if token_count != timestamp_count:
        message = (
            "CAPSWRITER_CONTRACT_FAILED "
            "condition=tokens_timestamps_length_mismatch "
            f"text_accu={accu_summary} joined={joined_summary} "
            f"tokens_len={token_count} timestamps_len={timestamp_count}"
        )
        logger.warning(message)
        raise ValueError(message)

    if joined_text != text_accu:
        message = (
            "CAPSWRITER_CONTRACT_FAILED "
            "condition=tokens_join_text_accu_mismatch "
            f"text_accu={accu_summary} joined={joined_summary} "
            f"tokens_len={token_count} timestamps_len={timestamp_count}"
        )
        logger.warning(message)
        raise ValueError(message)

    # 上游契约允许相邻时间戳重复（空格/标点 token 继承邻近词时间，实测相邻
    # 重复率约 73%），但要求非递减。倒退属于异常输入：不拦的话分段仍会生成
    # 并落盘，产出「结果错但不报错」的时间轴（本仓 P1 红线）。fail fast（#121）。
    previous = None
    for index, stamp in enumerate(timestamps):
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
            continue
        value = float(stamp)
        if previous is not None and value < previous:
            message = (
                "CAPSWRITER_CONTRACT_FAILED "
                "condition=timestamps_not_non_decreasing "
                f"index={index} value={value} previous={previous}"
            )
            logger.warning(message)
            raise ValueError(message)
        previous = value

    return "".join(tokens), list(tokens), list(timestamps)


def _write_temp(target: Path, content: str) -> Path:
    """Write content to ``<target>.tmp`` and return that path.

    写临时文件再 rename 是为了让「本次产物」与「目录里已有产物」在失败时
    互不干扰：失败只需删掉自己的 .tmp（#121）。
    """
    temporary = target.with_name(target.name + ".tmp")
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(content)
    except OSError:
        # 半写的 .tmp 不会进入 pending（append 在返回之后），必须就地清理，
        # 否则会在输出目录留下被误认为产物的残片。
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return temporary


def _split_oversized_segments(
    segments: List[Dict[str, Any]],
    text: str,
    token_prefixes: List[int],
    timestamps: List[Any],
    duration: Any,
    max_len: int,
) -> List[Dict[str, Any]]:
    """按次级标点切分超长段落，时间取自 token 起点（不做字符比例插值）。

    只切分、不合并短段：合并需要拼接字符串，会丢掉句间空格并让区间与原文
    脱节（Opus 发现 C），而按标点独立成段是更诚实的单位。
    """
    splitter = re.compile(r"(?<=[，,；;、])")
    result: List[Dict[str, Any]] = []
    for segment in segments:
        if segment["length"] <= max_len:
            result.append(segment)
            continue
        char_start = segment["char_start"]
        body = text[char_start:segment["char_end"]]
        if len(body) <= max_len:
            result.append(segment)
            continue
        # 先按次级标点切，再把仍然超长的块按上限硬切。
        # 只做前一步会漏掉「逗号间隔本身就超过 max_len」的输入。
        pieces = []
        for piece in splitter.split(body):
            if not piece:
                continue
            pieces.extend(
                piece[offset:offset + max_len]
                for offset in range(0, len(piece), max_len)
            )
        if len(pieces) <= 1:
            result.append(segment)
            continue
        offset = char_start
        last_index = len(pieces) - 1
        for piece_index, piece in enumerate(pieces):
            piece_end = offset + len(piece)
            start_token_idx = _find_token_idx(token_prefixes, offset)
            end_token_idx = _find_token_idx(token_prefixes, max(offset, piece_end - 1))
            end_time = _finite_time_or_none(timestamps[end_token_idx])
            if end_time is None and piece_index == last_index:
                # 末块无有效 token 时间时回退到音频时长（与整段切分口径一致）
                end_time = _finite_time_or_none(duration)
            result.append(
                {
                    "start_time": _finite_time_or_none(timestamps[start_token_idx]),
                    "end_time": end_time,
                    "text": piece,
                    "length": len(piece),
                    "char_start": offset,
                    "char_end": piece_end,
                    "start_token_idx": start_token_idx,
                }
            )
            offset = piece_end
    return result or segments


def _token_prefixes(tokens: List[str]) -> List[int]:
    """Build exact half-open character boundaries for the raw token stream."""
    prefixes = [0]
    for token in tokens:
        prefixes.append(prefixes[-1] + len(token))
    return prefixes


def _find_token_idx(token_prefixes: List[int], char_pos: int) -> int:
    """Find the token containing a character position."""
    for index in range(len(token_prefixes) - 1):
        if token_prefixes[index] <= char_pos < token_prefixes[index + 1]:
            return index
    return len(token_prefixes) - 2


def _split_text_by_punctuation(text: str) -> List[Tuple[int, int]]:
    """Split text into contiguous half-open spans without dropping characters."""
    spans: List[Tuple[int, int]] = []
    cursor = 0
    for match in re.finditer(r"[。！？!?]+|\.(?=\s|$)", text):
        end = match.end()
        if cursor < end:
            spans.append((cursor, end))
        cursor = end
    if cursor < len(text):
        spans.append((cursor, len(text)))
    return spans


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


def _timeline_coverage(timestamps: List[Any], duration: Any) -> Optional[float]:
    """Measure the last token start time against the audio duration."""
    parsed_duration = _finite_time_or_none(duration)
    last_timestamp = _finite_time_or_none(timestamps[-1]) if timestamps else None
    if parsed_duration is None or parsed_duration <= 0 or last_timestamp is None:
        return None
    return last_timestamp / parsed_duration


def _split_long_segment(
    segment: Dict[str, Any], max_len: int
) -> List[Dict[str, Any]]:
    """Split an already-built generic segment for legacy adapter callers.

    CapsWriter production segments no longer use this helper: their boundaries
    come directly from token timestamps. It remains for the shared subtitle
    adapter's existing finite-time tests and does not participate in token
    coordinate calculation.
    """
    text = segment["text"]
    parts = re.split(r"([，,；;])", text)
    if len(parts) <= 1:
        return [segment]

    split_segments: List[Dict[str, Any]] = []
    current = ""
    for part in parts:
        if len(current + part) <= max_len or not current:
            current += part
        else:
            split_segments.append({"text": current, "length": len(current)})
            current = part
    if current:
        split_segments.append({"text": current, "length": len(current)})

    if not split_segments:
        return [segment]

    original_start = _finite_time_or_none(segment.get("start_time"))
    original_end = _finite_time_or_none(segment.get("end_time"))
    time_pairs = interpolate_segment_times(
        original_start,
        original_end,
        [part["text"] for part in split_segments],
    )
    if (
        original_start is not None
        and original_end is not None
        and original_start == original_end
    ):
        time_pairs = [(original_start, original_end) for _ in split_segments]

    for part, (start_time, end_time) in zip(split_segments, time_pairs):
        part["start_time"] = (
            round(start_time, 2) if start_time is not None else None
        )
        part["end_time"] = round(end_time, 2) if end_time is not None else None
    return split_segments


def _create_segments_from_capswriter(
    text: str,
    tokens: List[str],
    timestamps: List[Any],
    max_len: int = 300,
    duration: Any = None,
) -> List[Dict[str, Any]]:
    """
    从 CapsWriter 数据创建 FunASR 格式的 segments

    Args:
        text: 仅为兼容旧调用方保留；正文始终由 tokens 原样拼接
        tokens: BPE token 列表
        timestamps: 时间戳列表
        duration: 音频时长，末句末 token 时间缺失时使用

    Returns:
        segments 列表
    """
    if not text:
        return []
    del text
    if not tokens or not timestamps:
        logger.error("tokens 或 timestamps 为空，无法创建 segments")
        return []
    if len(tokens) != len(timestamps):
        # 上游文件任务契约要求两者等长。不等长时截断后继续会把不一致的
        # 数据当成可交付结果（旧行为），或静默返回空（更早的旧行为）——
        # 两者都会让调用方误以为成功。fail fast（#121）。
        message = (
            "CAPSWRITER_CONTRACT_FAILED "
            "condition=tokens_timestamps_length_mismatch "
            f"tokens_len={len(tokens)} timestamps_len={len(timestamps)}"
        )
        logger.warning(message)
        raise ValueError(message)

    text = "".join(tokens)
    token_prefixes = _token_prefixes(tokens)
    spans = _split_text_by_punctuation(text)
    segments: List[Dict[str, Any]] = []

    for index, (char_start, char_end) in enumerate(spans):
        start_token_idx = _find_token_idx(token_prefixes, char_start)
        start_time = _finite_time_or_none(timestamps[start_token_idx])

        if index + 1 < len(spans):
            next_start, _ = spans[index + 1]
            next_token_idx = _find_token_idx(token_prefixes, next_start)
            end_time = _finite_time_or_none(timestamps[next_token_idx])
        else:
            end_time = _finite_time_or_none(timestamps[-1])
            if end_time is None:
                end_time = _finite_time_or_none(duration)

        segments.append(
            {
                "start_time": start_time,
                "end_time": end_time,
                "text": text[char_start:char_end],
                "length": char_end - char_start,
                "char_start": char_start,
                "char_end": char_end,
                "start_token_idx": start_token_idx,
            }
        )

    if max_len and max_len > 0:
        segments = _split_oversized_segments(
            segments, text, token_prefixes, timestamps, duration, max_len
        )

    logger.info(f"Segments 生成完成: {len(segments)} 个片段")
    return segments


# ============================================================================
# CapsWriter 客户端类
# ============================================================================


class CapsWriterClient:
    """CapsWriter客户端类"""

    def __init__(
        self,
        server_addr: Optional[str] = None,
        server_port: Optional[int] = None,
        output_dir: Optional[str] = None,
        max_retries: Optional[int] = None,
        retry_delay: Optional[int] = None,
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

    async def _save_results(
        self, file_path: Path, result: Dict[str, Any]
    ) -> List[Path]:
        """Validate and persist the complete CapsWriter result."""
        text, tokens, timestamps = _validate_capswriter_contract(result)
        base_path = file_path.with_suffix("")
        output_dir = Path(self.output_dir)
        json_file = output_dir / f"{base_path.name}.json"
        txt_file = output_dir / f"{base_path.name}.txt"
        merge_txt_file = output_dir / f"{base_path.name}.merge.txt"
        funasr_file = output_dir / f"{base_path.name}_funasr.json"

        # Build every artifact in memory first. A sidecar failure therefore
        # cannot leave a successful-looking text-only result on disk.
        funasr_data = None
        if Config.generate_funasr_compat:
            self.log("开始生成 FunASR 兼容格式 JSON...")
            segments = _create_segments_from_capswriter(
                text=text,
                tokens=tokens,
                timestamps=timestamps,
                max_len=int(Config.max_segment_length),
                duration=result.get("duration"),
            )
            if not segments:
                raise ValueError("no segments generated")

            time_start = result.get("time_start", 0)
            time_complete = result.get("time_complete", 0)
            processing_time = time_complete - time_start
            funasr_data = {
                "task_id": result.get("task_id", ""),
                "file_name": file_path.name,
                "duration": result.get("duration", 0),
                "timeline_quality": {
                    "coverage": _timeline_coverage(
                        timestamps, result.get("duration")
                    )
                },
                "segments": [
                    {
                        "start_time": segment["start_time"],
                        "end_time": segment["end_time"],
                        "text": segment["text"],
                    }
                    for segment in segments
                ],
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "processing_time": processing_time,
                "error": None,
            }

            total_duration = sum(
                segment["end_time"] - segment["start_time"]
                for segment in segments
                if segment["start_time"] is not None
                and segment["end_time"] is not None
            )
            average_length = sum(len(segment["text"]) for segment in segments) / len(
                segments
            )
            self.log(
                f"Segments 统计: 总时长={total_duration:.2f}s, "
                f"平均长度={average_length:.1f}字符"
            )

        generated_files: List[Path] = []

        # 原子落盘：全部先写 <目标>.tmp，全部成功后再 rename 就位。
        # 失败只删除本次自己的 .tmp，绝不碰目录里已存在的产物——
        # 否则同一 output_dir 下的上一次成功转录会被误删（真实数据丢失）。
        pending: List[Tuple[Path, Path]] = []
        try:
            if Config.generate_txt:
                pending.append((_write_temp(txt_file, text), txt_file))
                generated_files.append(txt_file)
                self.log(f"已生成文本文件: {txt_file}")

            if Config.generate_merge_txt:
                pending.append((_write_temp(merge_txt_file, text), merge_txt_file))
                generated_files.append(merge_txt_file)
                self.log(f"已生成合并文本文件: {merge_txt_file}")

            if funasr_data is not None:
                pending.append(
                    (_write_temp(funasr_file, _json.dumps(funasr_data, ensure_ascii=False, indent=2)),
                     funasr_file)
                )
                generated_files.append(funasr_file)
                self.log(
                    f"已生成 FunASR 兼容文件: {funasr_file} "
                    f"({len(funasr_data['segments'])} 个片段)"
                )

            if Config.generate_json:
                pending.append((_write_temp(json_file, _json.dumps(result, ensure_ascii=False, indent=2)), json_file))
                generated_files.append(json_file)
                self.log(f"已生成详细信息文件: {json_file}")

            # 就位阶段：先把已存在的目标挪到 .bak，再逐个 rename。
            # 若中途失败则回滚，避免留下「新的 txt + 旧的侧车」这种混合版本
            # 产物——调用方收到失败状态，但目录里的产物已被换掉一半。
            # 逐个就位。刻意不做跨文件回滚：备份+回滚在备份阶段中途失败时
            # 会把未备份的上一次成功产物删掉（真实数据丢失），风险大于收益。
            # 单文件原子性已由 tmp+rename 保证；失败时已就位的是新版本、
            # 未就位的是旧版本，不删除任何已存在的产物。
            for temporary, target in pending:
                temporary.replace(target)
            pending = []
        except OSError as exc:
            for temporary, _target in pending:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError as cleanup_exc:
                    logger.warning(f"清理临时文件失败: {temporary} ({cleanup_exc})")
            raise OSError(
                f"转录产物写盘失败，本次临时文件已清理（已有产物未受影响）: {exc}"
            ) from exc

        preview = text[:100] + "..." if len(text) > 100 else text
        self.log(f"转录结果预览: {preview}")
        return generated_files

    def transcribe_file(self, file_path: str) -> Tuple[bool, List[Path]]:
        """
        同步转录文件（官方 SDK 负责传输，按错误契约重试）

        参数:
            file_path: 要转录的文件路径

        返回:
            tuple: (bool成功状态, list生成的文件)
        """
        media_path = Path(file_path)
        attempts = 0
        last_error = None
        last_error_code = None
        should_retry = True

        while attempts < self.max_retries and should_retry:
            attempts += 1
            try:
                self.log(
                    f"开始转录文件: {media_path} (尝试 {attempts}/{self.max_retries})"
                )
                server_url = f"ws://{Config.server_addr}:{Config.server_port}"
                transcript = transcribe_file_sync(
                    media_path,
                    server_url,
                    encoding="flac",
                    seg_duration=Config.file_seg_duration,
                    seg_overlap=Config.file_seg_overlap,
                )

                result = dict(transcript.raw)
                result.setdefault("duration", transcript.duration)
                generated_files = asyncio.run(self._save_results(media_path, result))

                if generated_files:
                    self.log(f"转录完成，生成文件: {[str(f) for f in generated_files]}")
                    return True, generated_files
                last_error = "未生成任何文件或转录失败"
                should_retry = False

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

        error_msg = f"转录文件失败: {media_path}"
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
