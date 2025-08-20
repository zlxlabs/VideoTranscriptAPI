#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
服务器模式转录功能测试

测试完整的 WebSocket 服务器转录流程：
1. 启动服务器
2. 连接客户端
3. 上传音频文件
4. 接收转录结果
5. 验证结果格式和质量
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

# 添加项目根目录到Python路径
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))


class ServerTranscriptionTester:
    """服务器转录测试器"""
    
    def __init__(self, server_url="ws://localhost:8767"):
        self.server_url = server_url
        self.websocket = None
        self.test_results = []
        
    async def connect_to_server(self):
        """连接到服务器"""
        try:
            logger.info(f"连接到服务器: {self.server_url}")
            # 增加 ping_interval 和 ping_timeout 避免长时间任务断开连接
            self.websocket = await websockets.connect(
                self.server_url,
                ping_interval=30,  # 每30秒发送一次 ping
                ping_timeout=60,   # ping 响应超时60秒（给服务器更多处理时间）
                close_timeout=60,  # 关闭连接超时60秒
                max_size=100 * 1024 * 1024  # 最大消息大小100MB
            )
            
            # 接收服务器的连接确认消息
            welcome_message = await self.receive_message(timeout=10)
            if welcome_message.get("type") != "connected":
                logger.warning(f"意外的欢迎消息类型: {welcome_message.get('type')}")
            else:
                logger.debug(f"接收到服务器欢迎消息: {welcome_message.get('data', {}).get('message', '')}")
            
            logger.info("服务器连接成功")
            
            # 不再需要自定义心跳任务，websockets 库已经内置了 ping/pong 机制
            
            return True
        except Exception as e:
            logger.error(f"连接服务器失败: {e}")
            return False
    
    async def disconnect_from_server(self):
        """断开服务器连接"""
        if self.websocket:
            await self.websocket.close()
            logger.info("已断开服务器连接")
    
    def calculate_file_hash(self, file_path):
        """计算文件哈希"""
        hash_md5 = hashlib.md5()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()
    
    def _process_cached_result(self, response, file_name, file_size, file_hash, start_time):
        """处理缓存结果"""
        transcription_result = response["data"]["result"]
        processing_time = time.time() - start_time
        
        # 验证结果
        if not transcription_result:
            raise Exception("未收到转录结果")
        
        # 记录测试结果
        test_result = {
            "file_name": file_name,
            "file_size": file_size,
            "file_hash": file_hash,
            "task_id": response["data"].get("task_id", "cached"),
            "processing_time": processing_time,
            "server_processing_time": transcription_result.get("processing_time", 0),
            "transcription_result": transcription_result,
            "test_success": True,
            "cached_result": True
        }
        
        self.test_results.append(test_result)
        
        logger.info(f"转录测试完成(缓存): {len(transcription_result.get('segments', []))} 个片段, "
                   f"{len(transcription_result.get('speakers', []))} 个说话人, "
                   f"总耗时 {processing_time:.2f}秒")
        
        return test_result
    
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
    
    async def upload_file_and_transcribe(self, audio_path, force_refresh=False, output_format="json"):
        """上传文件并进行转录"""
        logger.info(f"开始转录测试: {os.path.basename(audio_path)}")
        start_time = time.time()
        
        # 读取音频文件
        with open(audio_path, 'rb') as f:
            file_data = f.read()
        
        file_name = os.path.basename(audio_path)
        file_size = len(file_data)
        file_hash = self.calculate_file_hash(audio_path)
        file_data_b64 = base64.b64encode(file_data).decode('utf-8')
        
        logger.info(f"文件信息: {file_name}, 大小: {file_size/1024:.2f}KB, 哈希: {file_hash[:8]}...")
        
        # 1. 发送上传请求
        upload_request = {
            "type": "upload_request",
            "data": {
                "file_name": file_name,
                "file_size": file_size,
                "file_hash": file_hash,
                "force_refresh": force_refresh,
                "output_format": output_format
            }
        }
        
        await self.send_message(upload_request)
        response = await self.receive_message()
        
        if response["type"] == "error":
            raise Exception(f"上传请求失败: {response['data']['message']}")
        
        # 处理不同的响应类型
        if response["type"] == "upload_ready":
            task_id = response["data"]["task_id"]
            logger.info(f"获得任务ID: {task_id}")
        elif response["type"] == "task_complete":
            # 直接返回缓存结果的情况
            logger.info("直接使用缓存结果")
            return self._process_cached_result(response, file_name, file_size, file_hash, start_time)
        elif response["type"] == "upload_complete":
            # 某些情况下可能直接收到 upload_complete（例如服务器快速处理了请求）
            logger.info("收到 upload_complete，继续等待转录结果")
            # 获取 task_id（如果响应中包含）
            task_id = response["data"].get("task_id", "unknown")
            # 直接跳到等待转录结果的部分
            # 不需要再上传文件数据
        else:
            raise Exception(f"意外的响应类型: {response['type']}")
        
        # 2. 上传文件数据（仅在需要时）
        if response["type"] == "upload_ready":
            upload_data = {
                "type": "upload_data",
                "data": {
                    "task_id": task_id,
                    "file_data": file_data_b64
                }
            }
            
            await self.send_message(upload_data)
            response = await self.receive_message()
            
            if response["type"] == "error":
                raise Exception(f"文件上传失败: {response['data']['message']}")
        
        # 处理响应（仅在上传文件数据后需要）
        if response["type"] == "upload_ready":
            while True:
                # 检查是否是缓存结果（直接完成）
                if response["type"] == "task_complete":
                    transcription_result = response["data"]["result"]
                    logger.info("使用缓存结果，转录完成")
                    processing_time = time.time() - start_time
                    
                    # 验证结果
                    if not transcription_result:
                        raise Exception("未收到转录结果")
                    
                    # 记录测试结果
                    test_result = {
                        "file_name": file_name,
                        "file_size": file_size,
                        "file_hash": file_hash,
                        "task_id": response["data"]["task_id"],
                        "processing_time": processing_time,
                        "server_processing_time": transcription_result.get("processing_time", 0),
                        "transcription_result": transcription_result,
                        "test_success": True,
                        "cached_result": True
                    }
                    
                    self.test_results.append(test_result)
                    
                    logger.info(f"转录测试完成(缓存): {len(transcription_result.get('segments', []))} 个片段, "
                               f"{len(transcription_result.get('speakers', []))} 个说话人, "
                               f"总耗时 {processing_time:.2f}秒")
                    
                    return test_result
                
                # 处理 upload_complete 响应
                elif response["type"] == "upload_complete":
                    logger.info("文件上传成功，开始转录...")
                    break
                
                # 处理意外的 upload_ready 重复响应 
                elif response["type"] == "upload_ready":
                    logger.warning("收到重复的 upload_ready 响应，继续等待...")
                    response = await self.receive_message()
                    continue
                
                # 处理错误响应
                elif response["type"] == "error":
                    error_msg = response["data"].get("message", "未知错误")
                    raise Exception(f"服务器错误: {error_msg}")
                
                else:
                    raise Exception(f"意外的响应类型: {response['type']}")
        
        # 3. 等待转录结果
        transcription_result = None
        
        while True:
            try:
                response = await self.receive_message(timeout=300)  # 5分钟超时
                
                if response["type"] == "task_progress":
                    progress = response["data"]["progress"]
                    logger.info(f"转录进度: {progress}%")
                
                elif response["type"] == "transcription_progress":
                    progress = response["data"]["progress"]
                    logger.info(f"转录进度: {progress}%")
                
                elif response["type"] == "task_complete":
                    transcription_result = response["data"]["result"]
                    logger.info("转录完成")
                    break
                
                elif response["type"] == "transcription_complete":
                    transcription_result = response["data"]
                    logger.info("转录完成")
                    break
                
                elif response["type"] == "error":
                    raise Exception(f"转录失败: {response['data']['message']}")
                
            except Exception as e:
                logger.error(f"等待转录结果时出错: {e}")
                raise
        
        processing_time = time.time() - start_time
        
        # 验证结果
        if not transcription_result:
            raise Exception("未收到转录结果")
        
        # 记录测试结果
        test_result = {
            "file_name": file_name,
            "file_size": file_size,
            "file_hash": file_hash,
            "task_id": task_id,
            "processing_time": processing_time,
            "server_processing_time": transcription_result.get("processing_time", 0),
            "transcription_result": transcription_result,
            "test_success": True
        }
        
        self.test_results.append(test_result)
        
        logger.info(f"转录测试完成: {len(transcription_result.get('segments', []))} 个片段, "
                   f"{len(transcription_result.get('speakers', []))} 个说话人, "
                   f"总耗时 {processing_time:.2f}秒")
        
        return test_result
    
    def validate_transcription_result(self, result):
        """验证转录结果"""
        transcription = result["transcription_result"]
        validation_errors = []
        
        # 检查必需字段
        required_fields = ["task_id", "file_name", "file_hash", "duration", 
                          "segments", "speakers", "processing_time"]
        
        for field in required_fields:
            if field not in transcription:
                validation_errors.append(f"缺少必需字段: {field}")
        
        # 检查片段格式
        segments = transcription.get("segments", [])
        if len(segments) == 0:
            validation_errors.append("转录片段为空")
        else:
            for i, segment in enumerate(segments):
                segment_fields = ["start_time", "end_time", "text", "speaker"]
                for field in segment_fields:
                    if field not in segment:
                        validation_errors.append(f"片段 {i} 缺少字段: {field}")
        
        # 检查说话人
        speakers = transcription.get("speakers", [])
        if len(speakers) == 0:
            validation_errors.append("未识别到说话人")
        
        # 检查合并效果
        if len(segments) > 0:
            speaker_segments = {}
            for segment in segments:
                speaker = segment.get("speaker", "Unknown")
                speaker_segments[speaker] = speaker_segments.get(speaker, 0) + 1
            
            logger.info(f"说话人片段分布: {speaker_segments}")
        
        return validation_errors
    
    async def test_srt_format(self, audio_path):
        """测试SRT格式输出"""
        logger.info(f"测试SRT格式输出: {os.path.basename(audio_path)}")
        
        try:
            # 测试SRT格式转录
            result = await self.upload_file_and_transcribe(str(audio_path), output_format="srt")
            
            # 验证SRT格式结果
            transcription_data = result.get("transcription_result", {})
            
            if transcription_data.get("format") == "srt":
                srt_content = transcription_data.get("content", "")
                logger.info(f"✓ 成功获得SRT格式结果，长度: {len(srt_content)} 字符")
                
                # 保存SRT文件
                output_dir = project_root / "tests" / "output"
                output_dir.mkdir(exist_ok=True)
                
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                srt_filename = f"{timestamp}_srt_test_{audio_path.stem}.srt"
                srt_path = output_dir / srt_filename
                
                with open(srt_path, "w", encoding="utf-8") as f:
                    f.write(srt_content)
                
                logger.info(f"SRT文件已保存: {srt_filename}")
                return True
            else:
                logger.error("未收到SRT格式结果")
                return False
                
        except Exception as e:
            logger.error(f"SRT格式测试失败: {e}")
            return False
    
    async def run_test_suite(self):
        """运行完整的测试套件"""
        logger.info("=== 开始服务器转录功能测试 ===")
        
        # 连接服务器
        if not await self.connect_to_server():
            return False
        
        try:
            # 查找测试音频文件
            samples_dir = project_root / "samples"
            audio_files = []
            
            for ext in ['.wav', '.mp3', '.mp4', '.m4a', '.flac']:
                audio_files.extend(samples_dir.glob(f"*{ext}"))
            
            if not audio_files:
                logger.error("未找到测试音频文件")
                return False
            
            logger.info(f"找到 {len(audio_files)} 个测试文件")
            
            # 测试每个文件
            for i, audio_file in enumerate(audio_files, 1):
                logger.info(f"\n=== 测试文件 {i}/{len(audio_files)}: {audio_file.name} ===")
                
                try:
                    # 测试JSON格式转录
                    result = await self.upload_file_and_transcribe(str(audio_file))
                    
                    # 验证结果
                    validation_errors = self.validate_transcription_result(result)
                    
                    if validation_errors:
                        logger.warning(f"验证发现问题: {validation_errors}")
                        result["validation_errors"] = validation_errors
                    else:
                        logger.info("✓ 转录结果验证通过")
                    
                    # 保存单个测试结果
                    self.save_test_result(result, f"server_test_{audio_file.stem}")
                    
                    # 测试SRT格式（仅对第一个文件）
                    if i == 4:
                        logger.info("\n=== 测试SRT格式输出 ===")
                        await self.test_srt_format(audio_file)
                    
                except Exception as e:
                    logger.error(f"✗ 测试失败: {e}")
                    error_result = {
                        "file_name": audio_file.name,
                        "test_success": False,
                        "error": str(e)
                    }
                    self.test_results.append(error_result)
            
            # 保存完整测试报告
            self.save_test_summary()
            
            return True
            
        finally:
            await self.disconnect_from_server()
    
    def save_test_result(self, result, filename):
        """保存单个测试结果"""
        output_dir = project_root / "tests" / "output"
        output_dir.mkdir(exist_ok=True)
        
        # 添加时间戳前缀避免文件混合
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_filename = f"{timestamp}_{filename}.json"
        output_path = output_dir / output_filename
        
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        
        logger.info(f"测试结果已保存: {output_filename}")
    
    def save_test_summary(self):
        """保存测试总结"""
        summary = {
            "test_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "server_url": self.server_url,
            "total_tests": len(self.test_results),
            "successful_tests": len([r for r in self.test_results if r.get("test_success", False)]),
            "failed_tests": len([r for r in self.test_results if not r.get("test_success", False)]),
            "results": self.test_results
        }
        
        output_dir = project_root / "tests" / "output"
        
        # 添加时间戳前缀避免文件混合
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_filename = f"{timestamp}_server_test_summary.json"
        output_path = output_dir / output_filename
        
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        
        logger.info(f"测试总结已保存: {output_filename}")
        logger.info(f"测试完成: {summary['successful_tests']}/{summary['total_tests']} 成功")


async def main():
    """主函数"""
    print("=" * 60)
    print("FunASR 服务器转录功能测试")
    print("=" * 60)
    
    # 设置日志级别
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    
    # 创建测试器
    tester = ServerTranscriptionTester()
    
    try:
        success = await tester.run_test_suite()
        if success:
            print("\n✓ 服务器转录测试完成")
        else:
            print("\n✗ 服务器转录测试失败")
            return 1
    
    except KeyboardInterrupt:
        print("\n测试被用户中断")
        return 1
    except Exception as e:
        print(f"\n测试过程中发生错误: {e}")
        logger.exception("详细错误信息:")
        return 1
    
    return 0


if __name__ == "__main__":
    # 运行测试
    exit_code = asyncio.run(main())
    sys.exit(exit_code)