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
import logging
from pathlib import Path
from typing import Tuple, List, Optional, Dict, Any

import websockets

# 添加项目根目录到系统路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ..utils import load_config


class Config:
    """配置类"""
    # 默认配置
    server_addr = 'localhost'
    server_port = 6006
    file_seg_duration = 25
    file_seg_overlap = 2
    enable_hot_words = True
    generate_txt = True
    generate_merge_txt = False
    generate_srt = False
    generate_lrc = False
    generate_json = False
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


class CapsWriterClient:
    """CapsWriter客户端类"""
    
    def __init__(self, server_addr: str = None, server_port: int = None, 
                 output_dir: str = None, max_retries: int = None, retry_delay: int = None):
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
        self.output_dir = output_dir or project_config.get("storage", {}).get("output_dir", "./data/output")
        self.max_retries = max_retries or project_config.get("capswriter", {}).get("max_retries", 3)
        self.retry_delay = retry_delay or project_config.get("capswriter", {}).get("retry_delay", 5)
        self.websocket = None
        self.current_task_id = None
        
        # 确保输出目录存在
        os.makedirs(self.output_dir, exist_ok=True)
        
        # 设置日志
        self._setup_logger()
    
    def _setup_logger(self):
        """设置日志记录器"""
        self.logger = logging.getLogger("CapsWriterClient")
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
        
        self.logger.setLevel(logging.INFO if Config.verbose else logging.WARNING)
    
    def log(self, message: str, level: str = "info"):
        """记录日志"""
        if Config.verbose:
            if level == "info":
                self.logger.info(message)
            elif level == "warning":
                self.logger.warning(message)
            elif level == "error":
                self.logger.error(message)
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
            "-i", str(file_path),
            "-f", "f32le",  # 32位浮点格式
            "-ac", "1",     # 单声道
            "-ar", "16000", # 16kHz采样率
            "-"
        ]
        
        self.log("正在提取音频...")
        
        try:
            process = subprocess.Popen(
                ffmpeg_cmd, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.DEVNULL
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
        if self.websocket and not self.websocket.closed:
            return True
        
        max_retries = 5
        retry_delay = 2
        
        for attempt in range(1, max_retries + 1):
            try:
                server_url = f"ws://{Config.server_addr}:{Config.server_port}"
                self.log(f"连接到服务器: {server_url} (尝试 {attempt}/{max_retries})")
                
                self.websocket = await asyncio.wait_for(
                    websockets.connect(server_url, max_size=None),
                    timeout=10.0
                )
                
                self.log(f"已连接到服务器: {server_url}")
                return True
            except (ConnectionRefusedError, TimeoutError, OSError) as e:
                if attempt < max_retries:
                    self.log(f"连接失败，{retry_delay}秒后重试...", "warning")
                    await asyncio.sleep(retry_delay)
                else:
                    self.log(f"连接服务器失败，已达到最大重试次数 ({max_retries})", "error")
        
        return False
    
    async def _close_websocket(self):
        """关闭WebSocket连接"""
        if self.websocket and not self.websocket.closed:
            await self.websocket.close()
            self.websocket = None
            self.log("已关闭服务器连接", "warning")
    
    async def _send_audio_data(self, file_path: Path, audio_data: bytes, audio_duration: float) -> Optional[str]:
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
            is_final = (chunk_end >= len(audio_data))
            
            # 构建消息
            message = {
                'task_id': task_id,
                'seg_duration': Config.file_seg_duration,
                'seg_overlap': Config.file_seg_overlap,
                'is_final': is_final,
                'time_start': time.time(),
                'time_frame': time.time(),
                'source': 'file',
                'data': base64.b64encode(
                    audio_data[offset:chunk_end]
                ).decode('utf-8'),
            }
            
            # 发送消息
            await self.websocket.send(json.dumps(message))
            
            # 更新进度
            progress = min(chunk_end / 4 / 16000, audio_duration)
            progress_percent = progress / audio_duration * 100
            self.log(f"发送进度: {progress:.2f}秒 / {audio_duration:.2f}秒 ({progress_percent:.1f}%)")
            
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
                    
                    # 显示进度
                    if 'duration' in result:
                        self.log(f"转录进度: {result['duration']:.2f}秒")
                    
                    # 检查是否为最终结果
                    if result.get('is_final', False):
                        self.log("转录完成！")
                        process_time = result['time_complete'] - result['time_start']
                        rtf = process_time / result['duration']
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
    
    async def _save_results(self, file_path: Path, result: Dict[str, Any]) -> List[Path]:
        """保存转录结果"""
        if not result:
            return []
        
        base_path = file_path.with_suffix("")
        generated_files = []
        
        try:
            # 提取结果数据
            text = result.get('text', '')
            text_split = re.sub('[，。？]', '\n', text)
            timestamps = result.get('timestamps', [])
            tokens = result.get('tokens', [])
            
            # 定义输出文件路径
            json_file = Path(self.output_dir) / f"{base_path.name}.json"
            txt_file = Path(self.output_dir) / f"{base_path.name}.txt"
            merge_txt_file = Path(self.output_dir) / f"{base_path.name}.merge.txt"
            
            # 保存JSON文件
            if Config.generate_json:
                with open(json_file, "w", encoding="utf-8") as f:
                    json.dump({
                        'timestamps': timestamps, 
                        'tokens': tokens
                    }, f, ensure_ascii=False, indent=2)
                generated_files.append(json_file)
                self.log(f"已生成详细信息文件: {json_file}")
            
            # 保存分行文本文件
            if Config.generate_txt:
                with open(txt_file, "w", encoding="utf-8") as f:
                    f.write(text_split)
                generated_files.append(txt_file)
                self.log(f"已生成文本文件: {txt_file}")
            
            # 保存合并文本文件
            if Config.generate_merge_txt:
                with open(merge_txt_file, "w", encoding="utf-8") as f:
                    f.write(text)
                generated_files.append(merge_txt_file)
                self.log(f"已生成合并文本文件: {merge_txt_file}")
            
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
                self.log(f"开始转录文件: {file_path} (尝试 {attempts}/{self.max_retries})")
                success, generated_files = asyncio.run(self.transcribe_file_async(file_path))
                
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
    parser.add_argument("--port", type=int, help="服务器端口", default=Config.server_port)
    parser.add_argument("--output", help="输出目录", 
                       default=project_config.get("storage", {}).get("output_dir", "./data/output"))
    parser.add_argument("--format", choices=['txt', 'merge', 'json', 'all'], 
                       default='txt', help="输出格式")
    parser.add_argument("--retries", type=int, 
                       default=project_config.get("capswriter", {}).get("max_retries", 3), 
                       help="最大重试次数")
    parser.add_argument("--quiet", action="store_true", help="静默模式")
    
    args = parser.parse_args()
    
    # 设置输出格式
    if args.format == 'txt':
        Config.generate_txt = True
        Config.generate_merge_txt = False
        Config.generate_json = False
    elif args.format == 'merge':
        Config.generate_txt = False
        Config.generate_merge_txt = True
        Config.generate_json = False
    elif args.format == 'json':
        Config.generate_txt = False
        Config.generate_merge_txt = False
        Config.generate_json = True
    elif args.format == 'all':
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
        max_retries=args.retries
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