#!/usr/bin/env python
# coding: utf-8

"""
CapsWriter语音转文字客户端 - 精简版
整合了原Client_Only文件夹的所有核心功能到单个文件中
"""

import os
import sys
import json
import base64
import time
import asyncio
import re
import uuid
import subprocess
import argparse
from pathlib import Path
from typing import Tuple, List, Optional, Dict, Any

import websockets
from loguru import logger

# 添加项目根目录到系统路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ..utils.logging import load_config


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


def _clean_token(token: str) -> str:
    """清理 token，去除 BPE 标记"""
    return token.replace("@@", "")


def _build_token_position_map(tokens: List[str]) -> Tuple[List[int], str]:
    """
    构建 token 到字符位置的映射

    Returns:
        (token_to_char_pos, reconstructed_text)
    """
    token_to_char_pos = []
    reconstructed = ""

    for token in tokens:
        token_to_char_pos.append(len(reconstructed))
        clean = _clean_token(token)
        reconstructed += clean

    token_to_char_pos.append(len(reconstructed))
    return token_to_char_pos, reconstructed


def _find_token_idx(token_positions: List[int], char_pos: int) -> int:
    """找到字符位置对应的 token 索引"""
    for i in range(len(token_positions) - 1):
        if token_positions[i] <= char_pos < token_positions[i + 1]:
            return i
    return len(token_positions) - 2


def _split_text_by_punctuation(text: str) -> List[str]:
    """按主要标点符号分句，保留标点"""
    primary_punct = r"([。！？!?])"
    parts = re.split(primary_punct, text)

    sentences = []
    i = 0
    while i < len(parts):
        sentence = parts[i]
        if i + 1 < len(parts) and parts[i + 1] in "。！？!?":
            sentence += parts[i + 1]
            i += 2
        else:
            i += 1

        if sentence.strip():
            sentences.append(sentence.strip())

    return sentences


def _remove_punctuation(text: str) -> str:
    """移除文本中的标点符号和空格"""
    return re.sub(r"[，。！？、；：,;:!?\s]", "", text)


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


def _split_long_segment(segment: Dict[str, Any], max_len: int) -> List[Dict[str, Any]]:
    """在次级标点处分割超长句子"""
    text = segment["text"]
    secondary_punct = r"([，,；;])"
    parts = re.split(secondary_punct, text)

    if len(parts) <= 1:
        return [segment]

    split_segments = []
    current = ""
    start_time = segment["start_time"]
    duration = segment["end_time"] - segment["start_time"]
    total_len = segment["length"]

    for part in parts:
        if len(current + part) <= max_len:
            current += part
        else:
            if current:
                progress = len(current) / total_len
                end_time = start_time + duration * progress

                split_segments.append(
                    {
                        "start_time": round(start_time, 2),
                        "end_time": round(end_time, 2),
                        "text": current,
                        "length": len(current),
                    }
                )

                start_time = end_time
                current = part
            else:
                current = part

    if current:
        split_segments.append(
            {
                "start_time": round(start_time, 2),
                "end_time": round(segment["end_time"], 2),
                "text": current,
                "length": len(current),
            }
        )

    return split_segments if split_segments else [segment]


def _create_segments_from_capswriter(
    text: str,
    tokens: List[str],
    timestamps: List[float],
    min_len: int = 80,
    max_len: int = 300,
) -> List[Dict[str, Any]]:
    """
    从 CapsWriter 数据创建 FunASR 格式的 segments

    Args:
        text: 带标点的完整文本
        tokens: BPE token 列表
        timestamps: 时间戳列表
        min_len: 最小段落长度
        max_len: 最大段落长度

    Returns:
        segments 列表
    """
    logger.debug(
        f"开始创建 segments: text={len(text)}, tokens={len(tokens)}, timestamps={len(timestamps)}"
    )

    # 检查长度是否匹配
    if len(tokens) != len(timestamps):
        logger.warning(
            f"长度不匹配: tokens={len(tokens)}, timestamps={len(timestamps)}"
        )
        min_len_val = min(len(tokens), len(timestamps))
        tokens = tokens[:min_len_val]
        timestamps = timestamps[:min_len_val]
        logger.info(f"已截断至相同长度: {min_len_val}")

    if not tokens or not timestamps:
        logger.error("tokens 或 timestamps 为空，无法创建 segments")
        return []

    # 构建 token 位置映射
    token_positions, reconstructed = _build_token_position_map(tokens)
    logger.debug(f"Token 位置映射完成: reconstructed length={len(reconstructed)}")

    # 分句
    sentences = _split_text_by_punctuation(text)
    logger.debug(f"按标点分句: {len(sentences)} 个句子")

    # 验证对齐
    text_clean = _remove_punctuation(text)
    alignment_diff = abs(len(text_clean) - len(reconstructed))

    if alignment_diff > 5:
        logger.warning(
            f"对齐警告: text_clean={len(text_clean)}, reconstructed={len(reconstructed)}, diff={alignment_diff}"
        )
        logger.warning(f"  这可能导致时间戳不准确，请检查输入数据")
    else:
        logger.debug(f"对齐检查通过: diff={alignment_diff}")

    # 映射每个句子到 token 范围
    segments = []
    char_offset = 0

    for idx, sentence in enumerate(sentences):
        sentence_clean = _remove_punctuation(sentence)
        sentence_len = len(sentence_clean)

        if sentence_len == 0:
            logger.debug(f"句子 {idx + 1} 为空，跳过")
            continue

        # 查找对应的 token 范围
        start_token_idx = _find_token_idx(token_positions, char_offset)
        end_token_idx = _find_token_idx(token_positions, char_offset + sentence_len - 1)

        # 安全范围检查
        start_token_idx = max(0, min(start_token_idx, len(timestamps) - 1))
        end_token_idx = max(0, min(end_token_idx, len(timestamps) - 1))

        # 提取时间
        start_time = timestamps[start_token_idx]
        end_time = timestamps[end_token_idx]

        segments.append(
            {
                "start_time": round(start_time, 2),
                "end_time": round(end_time, 2),
                "text": sentence,
                "length": len(sentence),
            }
        )

        logger.debug(
            f"句子 {idx + 1}: {sentence_len} 字符 -> tokens[{start_token_idx}:{end_token_idx}] -> {start_time:.2f}s-{end_time:.2f}s"
        )

        char_offset += sentence_len

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
        self.websocket = None
        self.current_task_id = None

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

    async def _extract_audio(self, file_path: Path) -> Tuple[Optional[bytes], float]:
        """
        从文件中提取音频数据

        参数:
            file_path: 文件路径

        返回:
            tuple: (音频数据, 音频时长)
        """
        ffmpeg_cmd = [
            "ffmpeg",
            "-i",
            str(file_path),
            "-f",
            "f32le",  # 32位浮点格式
            "-ac",
            "1",  # 单声道
            "-ar",
            "16000",  # 16kHz采样率
            "-",
        ]

        self.log("正在提取音频...")

        try:
            process = subprocess.Popen(
                ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
            audio_data = process.stdout.read()
            audio_duration = len(audio_data) / 4 / 16000  # 4字节/采样 * 16000采样/秒

            self.log(f"音频提取完成，时长: {audio_duration:.2f}秒")
            return audio_data, audio_duration
        except Exception as e:
            self.log(f"音频提取失败: {e}", "error")
            return None, 0

    async def _check_websocket(self) -> bool:
        """检查并建立WebSocket连接"""
        if self.websocket and self.websocket.close_code is None:
            return True

        max_retries = 5
        retry_delay = 2

        for attempt in range(1, max_retries + 1):
            try:
                server_url = f"ws://{Config.server_addr}:{Config.server_port}"
                self.log(f"连接到服务器: {server_url} (尝试 {attempt}/{max_retries})")

                self.websocket = await asyncio.wait_for(
                    websockets.connect(server_url, max_size=None), timeout=10.0
                )

                self.log(f"已连接到服务器: {server_url}")
                return True
            except (ConnectionRefusedError, TimeoutError, OSError) as e:
                if attempt < max_retries:
                    self.log(f"连接失败，{retry_delay}秒后重试...", "warning")
                    await asyncio.sleep(retry_delay)
                else:
                    self.log(
                        f"连接服务器失败，已达到最大重试次数 ({max_retries})", "error"
                    )

        return False

    async def _close_websocket(self):
        """关闭WebSocket连接"""
        if self.websocket and self.websocket.close_code is None:
            await self.websocket.close()
            self.websocket = None
            self.log("已关闭服务器连接", "warning")

    async def _send_audio_data(
        self, file_path: Path, audio_data: bytes, audio_duration: float
    ) -> Optional[str]:
        """发送音频数据到服务器"""
        if not await self._check_websocket():
            self.log("无法连接到服务器，取消转录", "error")
            return None

        # 生成任务ID
        task_id = str(uuid.uuid1())
        self.current_task_id = task_id

        self.log(f"任务ID: {task_id}")
        self.log(f"处理文件: {file_path}")

        # 分段发送音频数据
        offset = 0
        chunk_size = 16000 * 4 * 60  # 每分钟的数据大小

        while offset < len(audio_data):
            chunk_end = min(offset + chunk_size, len(audio_data))
            is_final = chunk_end >= len(audio_data)

            # 构建消息
            message = {
                "task_id": task_id,
                "seg_duration": Config.file_seg_duration,
                "seg_overlap": Config.file_seg_overlap,
                "is_final": is_final,
                "time_start": time.time(),
                "time_frame": time.time(),
                "source": "file",
                "data": base64.b64encode(audio_data[offset:chunk_end]).decode("utf-8"),
            }

            # 发送消息
            await self.websocket.send(json.dumps(message))

            # 更新进度（仅在关键里程碑时输出，降低日志噪音）
            progress = min(chunk_end / 4 / 16000, audio_duration)
            progress_percent = progress / audio_duration * 100

            # 只在 20%, 40%, 60%, 80%, 100% 时输出
            milestones = [20, 40, 60, 80, 100]
            for milestone in milestones:
                if not hasattr(self, f'_milestone_{milestone}') and progress_percent >= milestone:
                    setattr(self, f'_milestone_{milestone}', True)
                    self.log(
                        f"发送进度: {progress:.2f}秒 / {audio_duration:.2f}秒 ({progress_percent:.1f}%)"
                    )
                    break

            if is_final:
                break

            offset = chunk_end

        self.log("音频数据发送完成")
        return task_id

    async def _receive_results(self) -> Optional[Dict[str, Any]]:
        """接收服务器返回的转录结果"""
        if not self.websocket:
            self.log("WebSocket连接已关闭", "error")
            return None

        self.log("等待转录结果...")

        try:
            async for message in self.websocket:
                try:
                    result = json.loads(message)

                    # 显示进度（DEBUG 级别，避免日志噪音）
                    if "duration" in result:
                        self.log(f"转录进度: {result['duration']:.2f}秒", "debug")

                    # 检查是否为最终结果
                    if result.get("is_final", False):
                        self.log("转录完成！")
                        process_time = result["time_complete"] - result["time_start"]
                        rtf = process_time / result["duration"]
                        self.log(f"处理耗时: {process_time:.2f}秒, RTF: {rtf:.3f}")
                        return result
                except json.JSONDecodeError:
                    self.log("接收到非JSON数据，已忽略", "warning")
                    continue
        except websockets.ConnectionClosed:
            self.log("WebSocket连接已关闭", "error")
        except Exception as e:
            self.log(f"接收结果时出错: {e}", "error")

        return None

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

                    # 创建 segments
                    segments = _create_segments_from_capswriter(
                        text=text,
                        tokens=tokens,
                        timestamps=timestamps,
                        min_len=80,
                        max_len=300,
                    )

                    if not segments:
                        self.log(
                            "警告: 未生成任何 segments，跳过 FunASR 格式生成", "warning"
                        )
                        raise ValueError("no segments generated")

                    self.log(f"成功创建 {len(segments)} 个 segments")

                    # 构建 FunASR 兼容格式
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

                    # 统计信息
                    total_duration = sum(
                        seg["end_time"] - seg["start_time"] for seg in segments
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

    async def transcribe_file_async(self, file_path: str) -> Tuple[bool, List[Path]]:
        """
        异步转录文件

        参数:
            file_path: 要转录的文件路径

        返回:
            tuple: (bool成功状态, list生成的文件)
        """
        file_path = Path(file_path)

        try:
            # 1. 检查文件
            if not await self._check_file(file_path):
                return False, []

            # 2. 提取音频
            audio_data, audio_duration = await self._extract_audio(file_path)
            if not audio_data:
                return False, []

            # 3. 发送音频
            task_id = await self._send_audio_data(file_path, audio_data, audio_duration)
            if not task_id:
                return False, []

            # 4. 接收结果
            result = await self._receive_results()
            if not result:
                return False, []

            # 5. 保存结果
            generated_files = await self._save_results(file_path, result)

            # 6. 清理
            await self._close_websocket()

            return True, generated_files

        except Exception as e:
            self.log(f"转录过程中出错: {e}", "error")
            await self._close_websocket()
            return False, []

    def transcribe_file(self, file_path: str) -> Tuple[bool, List[Path]]:
        """
        同步转录文件（带重试逻辑）

        参数:
            file_path: 要转录的文件路径

        返回:
            tuple: (bool成功状态, list生成的文件)
        """
        attempts = 0
        last_error = None

        while attempts < self.max_retries:
            attempts += 1
            try:
                self.log(
                    f"开始转录文件: {file_path} (尝试 {attempts}/{self.max_retries})"
                )
                success, generated_files = asyncio.run(
                    self.transcribe_file_async(file_path)
                )

                if success and generated_files:
                    self.log(f"转录完成，生成文件: {[str(f) for f in generated_files]}")
                    return True, generated_files
                else:
                    last_error = "未生成任何文件或转录失败"

            except Exception as e:
                last_error = str(e)
                self.log(f"转录尝试 {attempts} 失败: {last_error}", "warning")

            # 如果不是最后一次尝试，等待后重试
            if attempts < self.max_retries:
                self.log(f"等待 {self.retry_delay} 秒后重试...")
                time.sleep(self.retry_delay)

        # 达到最大重试次数仍然失败
        error_msg = f"转录文件失败: {file_path}, 原因: {last_error}"
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
