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
from pathlib import Path
from dataclasses import dataclass
from typing import Tuple, List, Optional, Dict, Any

from capswriter_asr import AsrError, transcribe_file_sync
from loguru import logger

# 添加项目根目录到系统路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ..utils.logging import load_config
# 时间解析唯一权威：禁止在本模块另起一套 isfinite/parse 逻辑
from .segments import interpolate_segment_times, parse_time_to_seconds
# token -> 时间轴映射唯一权威：坐标系封在 TokenTimeline 内，本模块只持有 text 下标
from .token_timeline import (
    TextSpan,
    TimelineQuality,
    TokenTimeline,
    canonical_projection,
    clean_token,
)

# 运行时守卫阈值：低于即标记 degraded（文本没坏，时间轴质量差，不判任务失败）
TIMELINE_COVERAGE_THRESHOLD = 0.96
TIMELINE_ALIGNED_RATIO_THRESHOLD = 0.90


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
# FunASR 兼容格式转换相关函数
# ============================================================================


# 历史遗留兼容别名：仓内生产路径已不再使用这两个函数，仅
# tests/transcript/test_funasr_conversion.py 直接导入它们（该文件不在本卡
# 修改边界内）。新代码一律走 TokenTimeline。
_clean_token = clean_token


def _build_token_position_map(tokens: List[str]) -> Tuple[List[int], str]:
    """遗留兼容：token 起始字符下标 + 原样累加文本。生产路径不再使用。"""
    positions: List[int] = []
    reconstructed = ""
    for token in tokens:
        positions.append(len(reconstructed))
        reconstructed += clean_token(token)
    positions.append(len(reconstructed))
    return positions, reconstructed


def _split_text_by_punctuation(text: str) -> List[TextSpan]:
    """按主要标点分句，返回原始 text 上的半开区间列表。

    - 不做 ``strip()``：区间必须与原文严格对齐，strip 会吃掉句间空白，使段落文本
      偏离 ``transcript_capswriter.txt``。
    - 英文句号 ``.`` 也切：历史实现只在 ``。！？!?`` 切，英文陈述句从来就没按句断过
      （issue #109 附带修复的独立文本质量缺陷）。
    - **不丢弃纯空白/纯标点的区间**：它们是原文的一部分。丢一段等于丢文本
      （#111 finding：全标点输入曾整段消失、尾部空白曾被吃掉）。只有真正为空的区间
      （零长度）才跳过——那不是文本。
    """
    spans: List[TextSpan] = []
    cursor = 0
    for match in re.finditer(r"[。！？!?]+|\.(?=\s|$)", text):
        end = match.end()
        if end > cursor:
            spans.append(TextSpan(start=cursor, end=end))
        cursor = end
    if len(text) > cursor:
        spans.append(TextSpan(start=cursor, end=len(text)))
    return spans


def _input_mismatch(tokens: Any, timestamps: Any) -> Optional[Dict[str, int]]:
    """tokens 与 timestamps 不等长时返回详情（两侧长度 + 截断后长度），否则 None。

    这类输入的可对齐部分仍可能正确，因此**不抛异常**（抛错会把可读文本一起丢掉），
    但必须强制 degraded 并留下可 grep 的结构化日志——静默截断会让产物看起来完全正常，
    只有产物级覆盖率才暴露，而覆盖率可能被其他因素掩盖。
    """
    tokens_len = len(tokens)
    timestamps_len = len(timestamps)
    if tokens_len == timestamps_len:
        return None
    return {
        "tokens_len": tokens_len,
        "timestamps_len": timestamps_len,
        "truncated_to": min(tokens_len, timestamps_len),
    }


def _optimize_segment_lengths(
    segments: List[Dict[str, Any]],
    min_len: int,
    max_len: int,
    text: str,
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
                # 合并：取区间并集，文本稍后由 text[span] 切出（不拼字符串）
                merged_span = TextSpan(
                    start=min(buffer["span"].start, seg["span"].start),
                    end=max(buffer["span"].end, seg["span"].end),
                )
                buffer["span"] = merged_span
                buffer["end_time"] = seg["end_time"]
                buffer["text"] = text[merged_span.start:merged_span.end]
                buffer["length"] = len(buffer["text"])
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


def _timeline_coverage(
    segments: List[Dict[str, Any]], duration: Any
) -> float:
    """末段 end_time / duration；无法判定时返回 1.0（不误报降级）。"""
    parsed_duration = _finite_time_or_none(duration)
    if not parsed_duration or parsed_duration <= 0 or not segments:
        return 1.0
    last_end = _finite_time_or_none(segments[-1].get("end_time"))
    if last_end is None:
        return 0.0
    return last_end / parsed_duration


def _create_segments_from_capswriter(
    text: str,
    tokens: List[str],
    timestamps: List[float],
    min_len: int = 80,
    max_len: int = 300,
    timeline: Optional[TokenTimeline] = None,
) -> List[Dict[str, Any]]:
    """
    从 CapsWriter 数据创建 FunASR 格式的 segments

    Args:
        text: 带标点的完整文本
        tokens: BPE token 列表
        timestamps: 时间戳列表
        min_len: 最小段落长度
        max_len: 最大段落长度
        timeline: 已构建好的时间轴（调用方复用时传入；缺省就地构建）

    Returns:
        segments 列表
    """
    logger.debug(
        f"开始创建 segments: text={len(text)}, tokens={len(tokens)}, timestamps={len(timestamps)}"
    )

    # 检查长度是否匹配：不静默（结构化日志 + 强制 degraded，见 _input_mismatch）
    mismatch = _input_mismatch(tokens, timestamps)
    if mismatch is not None:
        logger.warning(
            f"capswriter timeline input_mismatch: tokens_len={mismatch['tokens_len']} "
            f"timestamps_len={mismatch['timestamps_len']} "
            f"truncated_to={mismatch['truncated_to']}"
        )
        tokens = tokens[: mismatch["truncated_to"]]
        timestamps = timestamps[: mismatch["truncated_to"]]

    if not tokens or not timestamps:
        logger.error("tokens 或 timestamps 为空，无法创建 segments")
        return []

    # 时间轴：唯一知道 tokens 与规范投影的对象，坐标系不出这一层
    if timeline is None:
        timeline = TokenTimeline.align(text, tokens, timestamps)
    quality = timeline.quality
    logger.debug(
        f"TokenTimeline 已就绪: aligned_ratio={quality.aligned_ratio:.4f}, "
        f"unmatched_chars={quality.unmatched_chars}"
    )

    # 分句（返回 text 区间，不 strip）
    spans = _split_text_by_punctuation(text)
    logger.debug(f"按标点分句: {len(spans)} 个句子")

    # 每个 span 独立向时间轴查时间——没有累加游标，误差不累积
    segments: List[Dict[str, Any]] = []

    for idx, span in enumerate(spans):
        sentence = text[span.start:span.end]
        if not sentence:
            # 零长度区间不是文本（分句器不会产出，这里只是显式兜底）
            logger.debug(f"句子 {idx + 1} 为空，跳过")
            continue
        if not canonical_projection(sentence):
            # 纯标点/纯空白片段：文本照常输出，时间诚实降级为 None
            #（#111 finding：这里曾 continue 跳过，导致全标点输入整段消失、尾部空白丢失）
            logger.debug(f"句子 {idx + 1} 规范化后为空，保留文本并降级时间为 None")

        # 时间：NaN/Inf/缺失（None）等无效值经唯一权威解析统一降级为
        # None（与 _split_long_segment 的诚实降级同一口径）——文本永不丢失，
        # 时间宁可为 None 也不能让 round(None)/round(nan) 之类的异常或
        # NaN/Inf 字面量污染整个 FunASR 兼容侧车的生成。
        start_time, _start_anchored = timeline.start_of(span)
        end_time, _end_anchored = timeline.end_of(span)

        segments.append(
            {
                "start_time": round(start_time, 2) if start_time is not None else None,
                "end_time": round(end_time, 2) if end_time is not None else None,
                "text": sentence,
                "span": span,
                "length": len(sentence),
            }
        )

        logger.debug(
            f"句子 {idx + 1}: text[{span.start}:{span.end}] -> {start_time}s-{end_time}s"
        )

    logger.debug(f"初始分段完成: {len(segments)} 个 segments")

    # 长度优化（合并取区间并集，文本仍由 text[span] 切出）
    optimized = _optimize_segment_lengths(segments, min_len, max_len, text)
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

    async def _save_results(
        self, file_path: Path, result: Dict[str, Any]
    ) -> List[Path]:
        """保存转录结果"""
        if not result:
            return []

        base_path = file_path.with_suffix("")
        generated_files = []

        try:
            # 提取结果数据
            text = result.get("text", "")
            timestamps = result.get("timestamps", [])
            tokens = result.get("tokens", [])

            # 定义输出文件路径
            json_file = Path(self.output_dir) / f"{base_path.name}.json"
            txt_file = Path(self.output_dir) / f"{base_path.name}.txt"
            merge_txt_file = Path(self.output_dir) / f"{base_path.name}.merge.txt"

            # 保存JSON文件
            if Config.generate_json:
                with open(json_file, "w", encoding="utf-8") as f:
                    json.dump(
                        {"timestamps": timestamps, "tokens": tokens},
                        f,
                        ensure_ascii=False,
                        indent=2,
                    )
                generated_files.append(json_file)
                self.log(f"已生成详细信息文件: {json_file}")

            # 保存文本文件（单行完整文本，供LLM处理）
            if Config.generate_txt:
                with open(txt_file, "w", encoding="utf-8") as f:
                    f.write(text)
                generated_files.append(txt_file)
                self.log(f"已生成文本文件: {txt_file}")

            # 保存合并文本文件（兼容旧格式）
            if Config.generate_merge_txt:
                with open(merge_txt_file, "w", encoding="utf-8") as f:
                    f.write(text)
                generated_files.append(merge_txt_file)
                self.log(f"已生成合并文本文件: {merge_txt_file}")

            # 保存 FunASR 兼容格式的 JSON
            if Config.generate_funasr_compat:
                self.log("开始生成 FunASR 兼容格式 JSON...")
                try:
                    # 使用带前缀的文件名，临时保存在 output_dir（后续会被复制到缓存目录）
                    funasr_file = (
                        Path(self.output_dir) / f"{base_path.name}_funasr.json"
                    )

                    # 验证输入数据
                    if not text:
                        self.log("警告: 文本为空，跳过 FunASR 格式生成", "warning")
                        raise ValueError("text is empty")

                    if not tokens or not timestamps:
                        self.log(
                            f"警告: tokens 或 timestamps 为空 (tokens={len(tokens)}, timestamps={len(timestamps)})",
                            "warning",
                        )
                        raise ValueError("tokens or timestamps is empty")

                    self.log(
                        f"输入数据: text={len(text)} 字符, tokens={len(tokens)}, timestamps={len(timestamps)}"
                    )

                    duration = result.get("duration", 0)
                    timeline = TokenTimeline.align(text, tokens, timestamps, duration)
                    quality = timeline.quality

                    # 创建 segments
                    segments = _create_segments_from_capswriter(
                        text=text,
                        tokens=tokens,
                        timestamps=timestamps,
                        min_len=80,
                        max_len=300,
                        timeline=timeline,
                    )

                    if not segments:
                        self.log(
                            "警告: 未生成任何 segments，跳过 FunASR 格式生成", "warning"
                        )
                        raise ValueError("no segments generated")

                    self.log(f"成功创建 {len(segments)} 个 segments")

                    # 运行时守卫：衡量**产物**（覆盖率/对齐率），不再拿两套不同规范的
                    # 字符串长度差当检测器（旧 alignment_diff 就是用 bug 的症状当
                    # bug 的检测器，修好坐标系后必然变小、阈值 5 对英文恒触发）。
                    coverage = _timeline_coverage(segments, duration)
                    # 长度不一致强制 degraded，且独立于两个阈值：即使覆盖率碰巧达标
                    # 也不得放行（#111 finding：静默截断的产物看起来完全正常）
                    input_mismatch = _input_mismatch(tokens, timestamps)
                    degraded = (
                        input_mismatch is not None
                        or coverage < TIMELINE_COVERAGE_THRESHOLD
                        or quality.aligned_ratio < TIMELINE_ALIGNED_RATIO_THRESHOLD
                    )
                    timeline_quality = {
                        "coverage": coverage,
                        "aligned_ratio": quality.aligned_ratio,
                        "unmatched_chars": quality.unmatched_chars,
                        "coverage_threshold": TIMELINE_COVERAGE_THRESHOLD,
                        "aligned_ratio_threshold": TIMELINE_ALIGNED_RATIO_THRESHOLD,
                        "input_mismatch": input_mismatch,
                        "degraded": degraded,
                    }
                    if degraded:
                        # 降级不判任务失败（文本没坏），但必须留可 grep 的结构化日志
                        logger.warning(
                            f"capswriter timeline degraded: file={file_path.name} "
                            f"task_id={result.get('task_id', '')} "
                            f"coverage={coverage:.4f} aligned_ratio={quality.aligned_ratio:.4f} "
                            f"unmatched_chars={quality.unmatched_chars} "
                            f"coverage_threshold={TIMELINE_COVERAGE_THRESHOLD} "
                            f"aligned_ratio_threshold={TIMELINE_ALIGNED_RATIO_THRESHOLD}"
                        )
                    else:
                        logger.info(
                            f"capswriter timeline metrics: file={file_path.name} "
                            f"coverage={coverage:.4f} aligned_ratio={quality.aligned_ratio:.4f} "
                            f"unmatched_chars={quality.unmatched_chars}"
                        )

                    # 构建 FunASR 兼容格式
                    funasr_data = {
                        "task_id": result.get("task_id", ""),
                        "file_name": file_path.name,
                        "duration": result.get("duration", 0),
                        "timeline_quality": timeline_quality,
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
                    # end - start 算术，否则 None - None 抛 TypeError，整个
                    # FunASR 兼容侧车会被 except 吞掉、跳过生成。
                    total_duration = sum(
                        seg["end_time"] - seg["start_time"]
                        for seg in segments
                        if seg["start_time"] is not None and seg["end_time"] is not None
                    )
                    avg_length = (
                        sum(len(seg["text"]) for seg in segments) / len(segments)
                        if segments
                        else 0
                    )

                    self.log(
                        f"Segments 统计: 总时长={total_duration:.2f}s, 平均长度={avg_length:.1f}字符"
                    )

                    with open(funasr_file, "w", encoding="utf-8") as f:
                        json.dump(funasr_data, f, ensure_ascii=False, indent=2)

                    generated_files.append(funasr_file)
                    self.log(
                        f"✓ 已生成 FunASR 兼容文件: {funasr_file} ({len(segments)} 个片段)"
                    )

                except Exception as e:
                    self.log(f"✗ 生成 FunASR 兼容格式失败: {e}", "warning")
                    self.log(
                        f"  提示: 主要转录文件（txt）已正常生成，可忽略此警告",
                        "warning",
                    )
                    import traceback

                    self.log(f"  详细错误: {traceback.format_exc()}", "warning")

            # 显示转录结果摘要
            if text:
                preview = text[:100] + "..." if len(text) > 100 else text
                self.log(f"转录结果预览: {preview}")

        except Exception as e:
            self.log(f"保存结果时出错: {e}", "error")

        return generated_files

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
                        "text": transcript.text,
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
