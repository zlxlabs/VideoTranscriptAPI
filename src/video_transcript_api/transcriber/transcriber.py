import os
import sys
import json
import shutil
import time
from pathlib import Path
from ..utils import setup_logger, load_config, ensure_dir

# 导入精简版CapsWriter客户端
from .capswriter_client import CapsWriterClient, Config as ClientConfig

# 创建日志记录器
logger = setup_logger("transcriber")

class Transcriber:
    """
    音视频转录器，基于CapsWriter-Offline客户端
    """
    def __init__(self, config=None):
        """
        初始化转录器
        
        参数:
            config: 配置信息，如果为None则从配置文件加载
        """
        if config is None:
            config = load_config()
        
        self.config = config
        self.output_dir = config.get("storage", {}).get("output_dir", "./data/output")
        self.max_retries = config.get("capswriter", {}).get("max_retries", 3)
        self.retry_delay = config.get("capswriter", {}).get("retry_delay", 5)
        
        # 确保输出目录存在
        ensure_dir(self.output_dir)
        
        # 设置Client_Only配置
        self._setup_client_config()
        
    def _setup_client_config(self):
        """
        设置CapsWriter客户端的配置
        """
        try:
            # 从项目配置中获取CapsWriter服务器信息
            server_url = self.config.get("capswriter", {}).get("server_url", "ws://localhost:6006")
            
            # 解析服务器地址和端口
            if server_url.startswith("ws://"):
                server_url = server_url[5:]
            
            if ":" in server_url:
                server_addr, server_port = server_url.split(":")
                server_port = int(server_port)
            else:
                server_addr = server_url
                server_port = 6006
                
            # 更新客户端配置
            ClientConfig.server_addr = server_addr
            ClientConfig.server_port = server_port
            
            # 设置输出格式 - 只生成txt文件
            ClientConfig.generate_txt = True  # 生成标准文本
            ClientConfig.generate_merge_txt = False
            ClientConfig.generate_srt = False
            ClientConfig.generate_lrc = False
            ClientConfig.generate_json = False
            
            # 创建CapsWriter客户端实例
            self.capswriter_client = CapsWriterClient(
                server_addr=server_addr,
                server_port=server_port,
                output_dir=self.output_dir,
                max_retries=self.max_retries,
                retry_delay=self.retry_delay
            )
            
            logger.info(f"已配置CapsWriter客户端，服务器: {server_addr}:{server_port}")
        except Exception as e:
            logger.exception(f"设置CapsWriter客户端配置失败: {str(e)}")
            raise
    
    def transcribe(self, audio_path, output_base=None):
        """
        转录音频文件
        
        参数:
            audio_path: 音频文件路径
            output_base: 输出文件基础名，如果为None则使用音频文件名
            
        返回:
            dict: 包含转录结果的字典
                - transcript: 纯文本转录结果
                - merge_txt_path: 合并文本文件路径
        """
        try:
            logger.info(f"开始转录音频文件: {audio_path}")
            
            # 如果未指定输出基础名，则使用音频文件名（不含扩展名）
            if output_base is None:
                output_base = os.path.splitext(os.path.basename(audio_path))[0]
            
            # 准备输出文件路径
            output_base_path = os.path.join(self.output_dir, output_base)
            merge_txt_path = f"{output_base_path}.merge.txt"
            final_txt_path = f"{output_base_path}.txt"
            
            # 确保音频文件存在
            if not os.path.exists(audio_path):
                raise FileNotFoundError(f"音频文件不存在: {audio_path}")
            
            # 使用CapsWriter客户端进行转录（客户端内部已有重试逻辑）
            logger.info(f"调用CapsWriter客户端转录文件: {audio_path}")
            success, generated_files = self.capswriter_client.transcribe_file(audio_path)
            
            if success and generated_files:
                logger.info(f"转录完成，生成文件: {[str(f) for f in generated_files]}")
                
                # 准备返回结果
                result = {
                    "transcript": "",
                    "txt_path": None
                }
                
                # 处理生成的文件
                for file_path in generated_files:
                    # 将Path对象转换为字符串
                    file_path_str = str(file_path)
                    
                    # 处理txt文件
                    if file_path_str.endswith(".txt") and not file_path_str.endswith(".merge.txt"):
                        result["txt_path"] = file_path_str
                        
                        # 读取转录文本
                        try:
                            with open(file_path_str, 'r', encoding='utf-8') as f:
                                result["transcript"] = f.read().strip()
                            logger.info(f"已从文本文件提取转录文本")
                        except Exception as e:
                            logger.warning(f"读取转录文本失败: {str(e)}")
                
                # 确保找到了txt文件
                if not result["txt_path"]:
                    error_msg = f"未找到文本文件: {audio_path}"
                    logger.error(error_msg)
                    raise RuntimeError(error_msg)
                
                return result
            else:
                error_msg = f"转录文件失败: {audio_path}"
                logger.error(error_msg)
                raise RuntimeError(error_msg)
            
        except Exception as e:
            logger.exception(f"转录音频文件失败: {str(e)}")
            raise