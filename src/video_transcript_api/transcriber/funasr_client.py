#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
FunASR Speaker Recognition Server Client

基于 WebSocket 的转录客户端，支持说话人识别功能。

对齐 funASR server「异步轮询契约 + 准入控制」更新（2026-06-16）：

    上传 ──► 拿 task_id ──► task_status_batch 周期轮询（断线可重连续轮）
      │
      ├─ queue_full（非致命）  → 按 retry_after 退避后整体重投
      ├─ task_expired/not_found→ 凭 file_hash 重投（命中 DB 缓存秒回）
      ├─ 连接中途断开          → 重连后继续轮询，绝不整文件重传
      └─ 终态全集 completed/failed/timed_out/cancelled 都识别，避免无限轮询

设计文档：docs/development/funasr_long_queue_client.md
"""

import os
import sys
import json
import time
import asyncio
import websockets
import hashlib
import base64
from pathlib import Path
from datetime import datetime
from loguru import logger

from ..utils.logging import load_config


# --------------------------------------------------------------------------- #
# 控制流异常：让外层 while 干净分流，避免重复 if/else（DRY）
# --------------------------------------------------------------------------- #
class FunASRQueueFull(Exception):
    """队列满（准入控制拒绝，非致命）。携带 retry_after 秒。"""

    def __init__(self, retry_after, message=""):
        super().__init__(message or "queue_full")
        self.retry_after = retry_after


class FunASRPollMiss(Exception):
    """轮询命中 task_expired / task_not_found，需凭 file_hash 重投。"""


class FunASRConnDropped(Exception):
    """连接中途断开（长任务的预期行为）。重连续轮，不重传。"""


class FunASRFatal(Exception):
    """服务端终态失败（failed/timed_out/cancelled）或致命错误，停止重试。"""


class FunASRTimeout(Exception):
    """单任务总超时（跨所有 phase/retry 的硬上限）。"""


class _RecvTimeout(Exception):
    """单次 recv 超时（内部用，视作连接异常触发重连）。"""


# 钳制边界
_FIRST_DELAY_MIN, _FIRST_DELAY_MAX = 1, 120
_RETRY_AFTER_MIN, _RETRY_AFTER_MAX = 1, 60

# 终态集合（停轮询）
_TERMINAL_FAIL = {"failed", "timed_out", "cancelled"}
_POLL_MISS_ERRORS = {"task_expired", "task_not_found"}


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


class FunASRSpeakerClient:
    """FunASR 说话人识别服务器客户端"""

    def __init__(self):
        self.config = load_config()
        self.server_config = self.config.get("funasr_spk_server", {})
        self.server_url = self.server_config.get("server_url", "ws://localhost:8767")
        self.max_retries = self.server_config.get("max_retries", 3)
        self.retry_delay = self.server_config.get("retry_delay", 5)
        self.connection_timeout = self.server_config.get("connection_timeout", 30)
        # 异步轮询相关配置（带默认值，向后兼容旧 config）
        self.poll_interval = self.server_config.get("poll_interval", 8)
        self.poll_recv_timeout = self.server_config.get("poll_recv_timeout", 60)
        self.total_timeout = self.server_config.get("total_timeout", 3600)
        self.first_delay_fallback = self.server_config.get("first_delay_fallback", 5)
        self.websocket = None

    # ----------------------------------------------------------------- #
    # 连接管理
    # ----------------------------------------------------------------- #
    async def connect_to_server(self):
        """连接到服务器"""
        try:
            logger.info(f"连接到 FunASR 服务器: {self.server_url}")
            # 使用推荐的连接配置（适配服务器端设置）
            # 注意：read_limit 和 write_limit 参数在 websockets 12.0+ 中已被移除
            self.websocket = await websockets.connect(
                self.server_url,
                ping_interval=60,  # 60秒发送一次心跳（与服务器一致）
                ping_timeout=120,  # 心跳响应超时120秒
                close_timeout=60,  # 关闭连接超时60秒
                max_size=10 * 1024 * 1024,  # 单消息最大10MB
            )

            # 接收服务器的连接确认消息
            welcome_message = await self.receive_message(timeout=10)
            if welcome_message.get("type") != "connected":
                logger.warning(f"意外的欢迎消息类型: {welcome_message.get('type')}")
            else:
                logger.debug(
                    f"接收到服务器欢迎消息: {welcome_message.get('data', {}).get('message', '')}"
                )

            logger.info("FunASR 服务器连接成功")
            return True
        except Exception as e:
            logger.error(f"连接 FunASR 服务器失败: {e}")
            return False

    async def connect_with_retry(self, max_retry=None):
        """带重试的连接"""
        if max_retry is None:
            max_retry = self.max_retries
        for attempt in range(max_retry):
            try:
                if await self.connect_to_server():
                    return True

                if attempt < max_retry - 1:
                    wait_time = 2**attempt
                    logger.info(f"连接失败，{wait_time}秒后重试...")
                    await asyncio.sleep(wait_time)
            except Exception as e:
                if attempt < max_retry - 1:
                    wait_time = 2**attempt
                    logger.warning(f"连接异常: {e}，{wait_time}秒后重试...")
                    await asyncio.sleep(wait_time)
                else:
                    raise e
        return False

    async def disconnect_from_server(self):
        """断开服务器连接"""
        if self.websocket:
            try:
                await self.websocket.close()
                logger.info("已断开 FunASR 服务器连接")
            except Exception as e:
                logger.debug(f"断开连接时忽略异常: {e}")
            finally:
                self.websocket = None

    def calculate_file_hash(self, file_path):
        """计算文件哈希"""
        hash_md5 = hashlib.md5()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()

    # ----------------------------------------------------------------- #
    # 收发
    # ----------------------------------------------------------------- #
    async def send_message(self, message):
        """发送消息到服务器"""
        if not self.websocket:
            raise Exception("未连接到服务器")

        message_json = json.dumps(message, ensure_ascii=False)
        await self.websocket.send(message_json)
        logger.debug(f"发送消息: {message['type']}")

    async def receive_message(self, timeout=30):
        """接收服务器消息。超时抛 _RecvTimeout，连接异常向上传播。"""
        if not self.websocket:
            raise Exception("未连接到服务器")

        try:
            message_json = await asyncio.wait_for(
                self.websocket.recv(), timeout=timeout
            )
            message = json.loads(message_json)
            logger.debug(f"接收消息: {message.get('type', 'unknown')}")
            return message
        except asyncio.TimeoutError:
            raise _RecvTimeout(f"接收消息超时 ({timeout}秒)")

    # ----------------------------------------------------------------- #
    # 报文解析辅助
    # ----------------------------------------------------------------- #
    @staticmethod
    def _parse_first_delay(data, fallback):
        """从 task_queued 回执解析首轮延迟（秒），兼容旧 minutes 字段。"""
        seconds = data.get("estimated_wait_seconds")
        if seconds is None:
            minutes = data.get("estimated_wait_minutes")
            seconds = minutes * 60 if minutes is not None else fallback
        return _clamp(int(seconds), _FIRST_DELAY_MIN, _FIRST_DELAY_MAX)

    def _queue_full(self, message):
        """把 queue_full 报文转成 FunASRQueueFull（兼容 data/顶层字段）。"""
        data = message.get("data", {}) or {}
        retry_after = data.get("retry_after", message.get("retry_after"))
        if retry_after is None:
            retry_after = self.retry_delay
        retry_after = _clamp(int(retry_after), _RETRY_AFTER_MIN, _RETRY_AFTER_MAX)
        return FunASRQueueFull(retry_after, message=str(data.get("message", "queue_full")))

    @staticmethod
    def _is_queue_full(message):
        """queue_full 是独立类型，也兼容 error 字段里带 queue_full 标记。"""
        if message.get("type") == "queue_full":
            return True
        if message.get("type") == "error":
            data = message.get("data", {}) or {}
            blob = f"{data.get('error', '')}{data.get('code', '')}"
            return "queue_full" in blob
        return False

    # ----------------------------------------------------------------- #
    # phase 1：提交（上传 / 命中缓存 / 入队）
    # ----------------------------------------------------------------- #
    async def _submit(self, file_path, file_size, file_hash, output_format, force_refresh):
        """发起 upload_request 并完成上传。

        返回:
            {"kind": "result", "result": <dict>}                 缓存命中或快速完成
            {"kind": "task", "task_id": <str>, "first_delay": <int>}  已入队待轮询
        异常: FunASRQueueFull / FunASRFatal
        """
        if file_size > 5 * 1024 * 1024:  # >5MB 使用分片
            return await self._upload_chunked(
                file_path, file_size, file_hash, output_format, force_refresh
            )
        return await self._upload_single(
            file_path, file_size, file_hash, output_format, force_refresh
        )

    def _post_upload_outcome(self, response):
        """解析「上传完成后」的服务端响应为统一 outcome。"""
        rtype = response.get("type")
        if rtype == "upload_complete":
            # task_id 由上层在 upload_ready 阶段已持有；这里无入队回执，用兜底首延迟
            return {"kind": "task", "task_id": None, "first_delay": self.first_delay_fallback}
        if rtype == "task_queued":
            data = response.get("data", {}) or {}
            return {
                "kind": "task",
                "task_id": data.get("task_id"),
                "first_delay": self._parse_first_delay(data, self.first_delay_fallback),
            }
        if rtype == "task_complete":
            return {"kind": "result", "result": response["data"]["result"]}
        if self._is_queue_full(response):
            raise self._queue_full(response)
        if rtype == "error":
            raise self._classify_error(response)
        raise Exception(f"上传完成后收到意外响应: {response}")

    def _classify_error(self, response):
        """error 报文 → PollMiss / Fatal。"""
        data = response.get("data", {}) or {}
        err = data.get("error") or data.get("message", "")
        if err in _POLL_MISS_ERRORS:
            return FunASRPollMiss(err)
        return FunASRFatal(f"转录失败: {err}")

    async def _upload_single(
        self, file_path, file_size, file_hash, output_format, force_refresh
    ):
        """单文件上传"""
        logger.info("使用单文件上传模式")

        request = {
            "type": "upload_request",
            "data": {
                "file_name": file_path.name,
                "file_size": file_size,
                "file_hash": file_hash,
                "output_format": output_format,
                "force_refresh": force_refresh,
            },
        }
        await self.send_message(request)
        response = await self.receive_message()

        if response.get("type") == "task_complete":
            logger.info("使用缓存结果")
            return {"kind": "result", "result": response["data"]["result"]}
        if self._is_queue_full(response):
            raise self._queue_full(response)
        if response.get("type") != "upload_ready":
            if response.get("type") == "error":
                raise self._classify_error(response)
            raise Exception(f"上传请求失败: {response}")

        task_id = response["data"]["task_id"]
        logger.info(f"获得任务ID: {task_id}")

        with open(file_path, "rb") as f:
            file_data = f.read()

        await self.send_message({
            "type": "upload_data",
            "data": {
                "task_id": task_id,
                "file_data": base64.b64encode(file_data).decode(),
            },
        })
        outcome = self._post_upload_outcome(await self.receive_message())
        if outcome["kind"] == "task" and outcome["task_id"] is None:
            outcome["task_id"] = task_id
        return outcome

    async def _upload_chunked(
        self, file_path, file_size, file_hash, output_format, force_refresh
    ):
        """分片上传"""
        chunk_size = 1024 * 1024  # 1MB分片
        total_chunks = (file_size + chunk_size - 1) // chunk_size
        logger.info(f"使用分片上传模式（{total_chunks}个分片）")

        request = {
            "type": "upload_request",
            "data": {
                "file_name": file_path.name,
                "file_size": file_size,
                "file_hash": file_hash,
                "chunk_size": chunk_size,
                "total_chunks": total_chunks,
                "upload_mode": "chunked",
                "output_format": output_format,
                "force_refresh": force_refresh,
            },
        }
        await self.send_message(request)
        response = await self.receive_message()

        if response.get("type") == "task_complete":
            logger.info("使用缓存结果")
            return {"kind": "result", "result": response["data"]["result"]}
        if self._is_queue_full(response):
            raise self._queue_full(response)
        if response.get("type") != "upload_ready":
            if response.get("type") == "error":
                raise self._classify_error(response)
            raise Exception(f"分片上传请求失败: {response}")

        task_id = response["data"]["task_id"]
        logger.info(f"获得任务ID: {task_id}")

        with open(file_path, "rb") as f:
            for chunk_index in range(total_chunks):
                chunk_data = f.read(chunk_size)
                await self.send_message({
                    "type": "upload_chunk",
                    "data": {
                        "task_id": task_id,
                        "chunk_index": chunk_index,
                        "chunk_size": len(chunk_data),
                        "chunk_hash": hashlib.md5(chunk_data).hexdigest(),
                        "chunk_data": base64.b64encode(chunk_data).decode(),
                        "is_last": chunk_index == total_chunks - 1,
                    },
                })
                chunk_response = await self.receive_message(timeout=60)
                if chunk_response.get("type") != "chunk_received":
                    raise Exception(f"分片 {chunk_index} 上传失败: {chunk_response}")
                progress = chunk_response.get("data", {}).get("progress", 0)
                logger.info(f"上传进度: {progress:.1f}% ({chunk_index + 1}/{total_chunks})")

        logger.info("所有分片上传完成，等待处理...")
        outcome = self._post_upload_outcome(await self.receive_message())
        if outcome["kind"] == "task" and outcome["task_id"] is None:
            outcome["task_id"] = task_id
        return outcome

    # ----------------------------------------------------------------- #
    # phase 2：轮询 task_status_batch 直到终态
    # ----------------------------------------------------------------- #
    async def _poll(self, task_id, first_delay, deadline):
        """周期轮询单个 task_id 直到终态。

        返回 result（dict 或 srt 字符串）。
        异常: FunASRFatal / FunASRPollMiss / FunASRConnDropped / FunASRTimeout
        """
        await asyncio.sleep(_clamp(int(first_delay), 0, _FIRST_DELAY_MAX))

        while time.time() < deadline:
            try:
                await self.send_message({
                    "type": "task_status_batch",
                    "data": {"task_ids": [task_id]},
                })
                message = await self.receive_message(timeout=self.poll_recv_timeout)
            except _RecvTimeout as e:
                logger.warning(f"轮询响应超时，触发重连: {e}")
                raise FunASRConnDropped(str(e))
            except (websockets.ConnectionClosed, ConnectionError, OSError) as e:
                logger.warning(f"轮询期间连接断开，将重连续轮: {e}")
                raise FunASRConnDropped(str(e))

            done = self._handle_poll_message(message, task_id)
            if done is not None:
                return done
            # pending/processing 或杂音 → 继续轮询
            if message.get("type") == "task_status_batch":
                await asyncio.sleep(self.poll_interval)

        raise FunASRTimeout(f"任务 {task_id} 轮询超过总超时 {self.total_timeout}s")

    def _handle_poll_message(self, message, task_id):
        """解析轮询期间收到的一条消息。

        返回 result 表示终态完成；返回 None 表示继续轮询。
        终态失败/poll-miss 直接抛异常。
        """
        rtype = message.get("type")

        if rtype == "task_status_batch":
            items = message.get("data", {}).get("items", []) or []
            item = next((it for it in items if it.get("task_id") == task_id), None)
            if item is None:
                item = items[0] if items else {}
            status = item.get("status")
            if status == "completed":
                logger.info("转录完成")
                return item.get("result") or item.get("srt_content")
            if status in _TERMINAL_FAIL:
                raise FunASRFatal(f"转录失败({status}): {item.get('error')}")
            if status is None:
                # poll-miss：task_expired / task_not_found
                raise FunASRPollMiss(item.get("error", "poll_miss"))
            # pending / processing
            return None

        # 期间可能夹杂的 push：自身完成直接收下，杂音忽略续轮
        if rtype == "task_complete":
            logger.info("收到 task_complete 推送，转录完成")
            return message["data"]["result"]
        if self._is_queue_full(message):
            raise self._queue_full(message)
        if rtype == "error":
            raise self._classify_error(message)
        if rtype in ("task_progress", "task_queued", "chunk_received", "upload_complete"):
            data = message.get("data", {}) or {}
            logger.debug(f"轮询期间杂音消息 {rtype}: {data.get('message', '')}")
            return None
        logger.debug(f"轮询期间未知消息，忽略: {rtype}")
        return None

    # ----------------------------------------------------------------- #
    # 主入口
    # ----------------------------------------------------------------- #
    async def transcribe_with_speaker_recognition(
        self, audio_path, output_format="json", force_refresh=False
    ):
        """
        使用说话人识别功能进行转录（对齐异步轮询契约）。

        参数:
            audio_path: 音频文件路径
            output_format: 输出格式 ("json" 或 "srt")
            force_refresh: 是否强制刷新缓存

        返回:
            dict: 转录结果，包含说话人信息
        """
        audio_path = Path(audio_path)
        file_name = audio_path.name
        file_size = audio_path.stat().st_size
        file_hash = self.calculate_file_hash(audio_path)
        logger.info(
            f"开始转录（带说话人识别）: {file_name}, "
            f"大小: {file_size / 1024 / 1024:.2f}MB, 哈希: {file_hash[:8]}..."
        )

        start_time = time.time()
        deadline = start_time + self.total_timeout

        task_id = None          # 已入队任务；断线重连时保留以避免重传
        first_delay = 0
        unexpected_errors = 0

        while time.time() < deadline:
            try:
                if not await self.connect_with_retry():
                    raise Exception("无法连接到 FunASR 服务器")

                # 已有 task_id（断线续轮）→ 跳过上传，直接轮询
                if task_id is None:
                    outcome = await self._submit(
                        audio_path, file_size, file_hash, output_format, force_refresh
                    )
                    if outcome["kind"] == "result":
                        return self._finalize(outcome["result"], start_time)
                    task_id = outcome["task_id"]
                    first_delay = outcome["first_delay"]

                result = await self._poll(task_id, first_delay, deadline)
                return self._finalize(result, start_time)

            except FunASRFatal:
                raise  # 服务端终态失败，重试无意义
            except FunASRQueueFull as e:
                unexpected_errors = 0
                task_id = None  # 未被准入，无任务，需整体重投
                logger.info(f"队列满，{e.retry_after}秒后重投（非致命）")
                await self._sleep_within(e.retry_after, deadline)
            except FunASRPollMiss as e:
                unexpected_errors = 0
                task_id = None  # 任务已过期/不存在 → 凭 file_hash 重投
                logger.info(f"轮询未命中({e})，凭 file_hash 重投")
                await self._sleep_within(1, deadline)
            except (FunASRConnDropped, _RecvTimeout) as e:
                unexpected_errors = 0
                first_delay = 0  # 已等过，重连立即轮询；task_id 保留，不重传
                logger.info(f"连接中断({e})，重连后继续轮询，不重传")
                await self._sleep_within(1, deadline)
            except FunASRTimeout:
                raise
            except Exception as e:
                unexpected_errors += 1
                logger.error(
                    f"转录过程出错 (连续第 {unexpected_errors} 次): {e}"
                )
                if unexpected_errors > self.max_retries:
                    raise
                first_delay = 0  # 若已有 task_id 则续轮，否则重投
                await self._sleep_within(self.retry_delay, deadline)
            finally:
                await self.disconnect_from_server()

        raise FunASRTimeout(f"转录超过总超时 {self.total_timeout}s 仍未完成")

    async def _sleep_within(self, seconds, deadline):
        """退避睡眠，但不超过总 deadline。"""
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        await asyncio.sleep(min(seconds, remaining))

    def _finalize(self, result, start_time):
        """校验并记录转录结果。"""
        if not result:
            raise Exception("未收到转录结果")
        if isinstance(result, dict):
            processing_time = time.time() - start_time
            logger.info(
                f"转录完成: {len(result.get('segments', []))} 个片段, "
                f"{len(result.get('speakers', []))} 个说话人, "
                f"总耗时 {processing_time:.2f}秒"
            )
        return result

    # ----------------------------------------------------------------- #
    # 结果格式化 / 同步接口
    # ----------------------------------------------------------------- #
    def format_transcript_with_speakers(self, transcription_result):
        """
        格式化带说话人的转录文本

        参数:
            transcription_result: FunASR服务器返回的转录结果

        返回:
            str: 格式化后的文本
        """
        segments = transcription_result.get("segments", [])
        if not segments:
            return ""

        formatted_text = []
        current_speaker = None
        current_text = []

        for segment in segments:
            speaker = segment.get("speaker", "Unknown")
            text = segment.get("text", "").strip()

            if not text:
                continue

            if speaker != current_speaker:
                if current_text:
                    formatted_text.append(f"{current_speaker}：{''.join(current_text)}")
                current_speaker = speaker
                current_text = [text]
            else:
                current_text.append(text)

        if current_text:
            formatted_text.append(f"{current_speaker}：{''.join(current_text)}")

        return "\n\n".join(formatted_text)

    def transcribe_sync(self, audio_path, output_format="json", force_refresh=False):
        """
        同步接口：使用说话人识别功能进行转录

        参数:
            audio_path: 音频文件路径
            output_format: 输出格式 ("json" 或 "srt")
            force_refresh: 是否强制刷新缓存

        返回:
            dict: 包含转录结果和格式化文本的字典
        """
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result = loop.run_until_complete(
                self.transcribe_with_speaker_recognition(
                    audio_path, output_format, force_refresh
                )
            )
            formatted_text = self.format_transcript_with_speakers(result)
            return {"transcription_result": result, "formatted_text": formatted_text}
        finally:
            loop.close()
