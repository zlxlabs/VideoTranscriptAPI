#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
FunASR Speaker Recognition Server Client

基于 WebSocket 的转录客户端，支持说话人识别功能
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

from ..utils import load_config


class FunASRSpeakerClient:
    """FunASR 说话人识别服务器客户端"""
    
    def __init__(self):
        self.config = load_config()
        self.server_config = self.config.get("funasr_spk_server", {})
        self.server_url = self.server_config.get("server_url", "ws://localhost:8767")
        self.max_retries = self.server_config.get("max_retries", 3)
        self.retry_delay = self.server_config.get("retry_delay", 5)
        self.connection_timeout = self.server_config.get("connection_timeout", 30)
        self.websocket = None
        
    async def connect_to_server(self):
        """连接到服务器"""
        try:
            logger.info(f"连接到 FunASR 服务器: {self.server_url}")
            # 使用推荐的连接配置（适配服务器端设置）
            self.websocket = await websockets.connect(
                self.server_url,
                ping_interval=60,      # 60秒发送一次心跳（与服务器一致）
                ping_timeout=120,      # 心跳响应超时120秒
                close_timeout=60,      # 关闭连接超时60秒
                max_size=10 * 1024 * 1024,  # 单消息最大10MB
                read_limit=2**20,      # 1MB读缓冲
                write_limit=2**20      # 1MB写缓冲
            )
            
            # 接收服务器的连接确认消息
            welcome_message = await self.receive_message(timeout=10)
            if welcome_message.get("type") != "connected":
                logger.warning(f"意外的欢迎消息类型: {welcome_message.get('type')}")
            else:
                logger.debug(f"接收到服务器欢迎消息: {welcome_message.get('data', {}).get('message', '')}")
            
            logger.info("FunASR 服务器连接成功")
            return True
        except Exception as e:
            logger.error(f"连接 FunASR 服务器失败: {e}")
            return False
    
    async def connect_with_retry(self, max_retry=3):
        """带重试的连接"""
        for attempt in range(max_retry):
            try:
                if await self.connect_to_server():
                    return True
                
                if attempt < max_retry - 1:
                    wait_time = 2 ** attempt
                    logger.info(f"连接失败，{wait_time}秒后重试...")
                    await asyncio.sleep(wait_time)
            except Exception as e:
                if attempt < max_retry - 1:
                    wait_time = 2 ** attempt
                    logger.warning(f"连接异常: {e}，{wait_time}秒后重试...")
                    await asyncio.sleep(wait_time)
                else:
                    raise e
        return False
    
    async def disconnect_from_server(self):
        """断开服务器连接"""
        if self.websocket:
            await self.websocket.close()
            logger.info("已断开 FunASR 服务器连接")
    
    def calculate_file_hash(self, file_path):
        """计算文件哈希"""
        hash_md5 = hashlib.md5()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()
    
    async def send_message(self, message):
        """发送消息到服务器"""
        if not self.websocket:
            raise Exception("未连接到服务器")
        
        message_json = json.dumps(message, ensure_ascii=False)
        await self.websocket.send(message_json)
        logger.debug(f"发送消息: {message['type']}")
    
    async def receive_message(self, timeout=30):
        """接收服务器消息"""
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
            raise Exception(f"接收消息超时 ({timeout}秒)")
    
    async def _upload_single(self, file_path, file_size, file_hash, output_format, force_refresh):
        """单文件上传"""
        logger.info("使用单文件上传模式")
        
        # 发送上传请求
        request = {
            "type": "upload_request",
            "data": {
                "file_name": file_path.name,
                "file_size": file_size,
                "file_hash": file_hash,
                "output_format": output_format,
                "force_refresh": force_refresh
            }
        }
        
        await self.send_message(request)
        response = await self.receive_message()
        
        if response["type"] == "task_complete":
            # 直接返回缓存结果
            logger.info("✓ 使用缓存结果")
            return response["data"]["result"]
        
        if response["type"] != "upload_ready":
            raise Exception(f"上传请求失败: {response}")
        
        task_id = response["data"]["task_id"]
        logger.info(f"✓ 获得任务ID: {task_id}")
        
        # 读取文件并发送
        with open(file_path, 'rb') as f:
            file_data = f.read()
        
        upload_data = {
            "type": "upload_data",
            "data": {
                "task_id": task_id,
                "file_data": base64.b64encode(file_data).decode()
            }
        }
        
        await self.send_message(upload_data)
        response = await self.receive_message()
        
        # 处理上传后的响应
        if response["type"] == "upload_complete":
            logger.info("✓ 文件上传完成，等待转录结果...")
            return await self._wait_for_result()
            
        elif response["type"] == "task_complete":
            # 服务器可能直接返回转录结果（快速处理）
            logger.info("✓ 转录已完成（快速处理）")
            return response["data"]["result"]
            
        elif response["type"] == "task_queued":
            # 任务排队中
            position = response["data"]["queue_position"]
            wait_time = response["data"].get("estimated_wait_minutes", "未知")
            logger.info(f"⏳ 任务排队中，位置: {position}，预计等待: {wait_time}分钟")
            return await self._wait_for_result()
            
        else:
            raise Exception(f"文件上传失败: {response}")
    
    async def _upload_chunked(self, file_path, file_size, file_hash, output_format, force_refresh):
        """分片上传"""
        chunk_size = 1024 * 1024  # 1MB分片
        total_chunks = (file_size + chunk_size - 1) // chunk_size
        
        logger.info(f"使用分片上传模式（{total_chunks}个分片）")
        
        # 发送分片上传请求
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
                "force_refresh": force_refresh
            }
        }
        
        await self.send_message(request)
        response = await self.receive_message()
        
        if response["type"] == "task_complete":
            logger.info("✓ 使用缓存结果")
            return response["data"]["result"]
        
        if response["type"] != "upload_ready":
            raise Exception(f"分片上传请求失败: {response}")
        
        task_id = response["data"]["task_id"]
        logger.info(f"✓ 获得任务ID: {task_id}")
        
        # 分片上传
        with open(file_path, 'rb') as f:
            for chunk_index in range(total_chunks):
                chunk_data = f.read(chunk_size)
                chunk_hash = hashlib.md5(chunk_data).hexdigest()
                
                chunk_message = {
                    "type": "upload_chunk",
                    "data": {
                        "task_id": task_id,
                        "chunk_index": chunk_index,
                        "chunk_size": len(chunk_data),
                        "chunk_hash": chunk_hash,
                        "chunk_data": base64.b64encode(chunk_data).decode(),
                        "is_last": chunk_index == total_chunks - 1
                    }
                }
                
                await self.send_message(chunk_message)
                
                # 等待分片确认
                chunk_response = await self.receive_message(timeout=60)
                if chunk_response["type"] != "chunk_received":
                    raise Exception(f"分片 {chunk_index} 上传失败")
                
                progress = chunk_response["data"]["progress"]
                logger.info(f"上传进度: {progress:.1f}% ({chunk_index + 1}/{total_chunks})")
        
        logger.info("✓ 所有分片上传完成，等待处理...")
        
        # 等待上传完成通知
        response = await self.receive_message()
        
        # 处理三种可能的情况
        if response["type"] == "upload_complete":
            # 标准流程：上传完成 → 等待转录
            logger.info("文件上传完成，开始转录处理...")
            return await self._wait_for_result()
            
        elif response["type"] == "task_queued":
            # 排队流程：上传完成 → 排队 → 等待转录
            position = response["data"]["queue_position"]
            wait_time = response["data"]["estimated_wait_minutes"]
            logger.info(f"⏳ 任务排队中，位置: {position}，预计等待: {wait_time}分钟")
            return await self._wait_for_result()
            
        elif response["type"] == "task_complete":
            # 快速流程：上传完成 → 直接返回转录结果
            logger.info("✓ 转录已完成（快速处理）")
            return response["data"]["result"]
            
        else:
            # 其他未预期的响应
            raise Exception(f"分片上传完成后收到意外响应: {response}")
    
    async def _wait_for_result(self):
        """等待转录结果"""
        while True:
            response = await self.receive_message(timeout=300)  # 5分钟超时
            
            if response["type"] == "task_progress":
                progress = response["data"]["progress"]
                status = response["data"]["status"]
                message = response["data"].get("message", "")
                logger.info(f"转录进度: {progress}% - {status} - {message}")
            
            elif response["type"] == "task_queued":
                position = response["data"]["queue_position"]
                wait_time = response["data"].get("estimated_wait_minutes", "未知")
                logger.info(f"⏳ 任务排队中，位置: {position}，预计等待: {wait_time}分钟")
            
            elif response["type"] == "task_complete":
                result = response["data"]["result"]
                logger.info("✓ 转录完成")
                return result
            
            elif response["type"] == "error":
                error_msg = response["data"]["message"]
                raise Exception(f"转录失败: {error_msg}")
    
    async def transcribe_with_speaker_recognition(self, audio_path, output_format="json", force_refresh=False):
        """
        使用说话人识别功能进行转录
        
        参数:
            audio_path: 音频文件路径
            output_format: 输出格式 ("json" 或 "srt")
            force_refresh: 是否强制刷新缓存
            
        返回:
            dict: 转录结果，包含说话人信息
        """
        retry_count = 0
        while retry_count <= self.max_retries:
            try:
                # 使用带重试的连接
                if not await self.connect_with_retry():
                    raise Exception("无法连接到 FunASR 服务器")
                
                audio_path = Path(audio_path)
                logger.info(f"开始转录（带说话人识别）: {audio_path.name}")
                start_time = time.time()
                
                # 获取文件信息
                file_name = audio_path.name
                file_size = audio_path.stat().st_size
                file_hash = self.calculate_file_hash(audio_path)
                
                logger.info(f"文件信息: {file_name}, 大小: {file_size/1024/1024:.2f}MB, 哈希: {file_hash[:8]}...")
                
                # 判断使用单文件还是分片上传
                if file_size > 5 * 1024 * 1024:  # >5MB使用分片
                    result = await self._upload_chunked(
                        audio_path, file_size, file_hash, output_format, force_refresh
                    )
                else:
                    result = await self._upload_single(
                        audio_path, file_size, file_hash, output_format, force_refresh
                    )
                
                # 验证结果
                if not result:
                    raise Exception("未收到转录结果")
                
                processing_time = time.time() - start_time
                logger.info(f"转录完成: {len(result.get('segments', []))} 个片段, "
                           f"{len(result.get('speakers', []))} 个说话人, "
                           f"总耗时 {processing_time:.2f}秒")
                
                return result
                
            except Exception as e:
                logger.error(f"转录过程出错 (尝试 {retry_count + 1}/{self.max_retries + 1}): {e}")
                retry_count += 1
                
                if retry_count <= self.max_retries:
                    logger.info(f"{self.retry_delay}秒后重试...")
                    await asyncio.sleep(self.retry_delay)
                else:
                    raise
            finally:
                await self.disconnect_from_server()
        
        raise Exception("转录失败，已达到最大重试次数")
    
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
                # 说话人变化，输出之前的内容
                if current_text:
                    formatted_text.append(f"{current_speaker}：{''.join(current_text)}")
                
                current_speaker = speaker
                current_text = [text]
            else:
                # 同一说话人，累积文本
                current_text.append(text)
        
        # 输出最后一个说话人的内容
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
            # 在新的事件循环中运行异步方法
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result = loop.run_until_complete(
                self.transcribe_with_speaker_recognition(audio_path, output_format, force_refresh)
            )
            
            # 格式化转录文本
            formatted_text = self.format_transcript_with_speakers(result)
            
            return {
                "transcription_result": result,
                "formatted_text": formatted_text
            }
        finally:
            loop.close()