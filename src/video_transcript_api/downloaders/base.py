import os
import re
import json
import requests
import time
from abc import ABC, abstractmethod
from urllib.parse import urlparse, parse_qs
from ..utils import setup_logger, load_config, ensure_dir

# 创建日志记录器
logger = setup_logger("downloaders")

class BaseDownloader(ABC):
    """
    下载器基类，定义了下载器的通用接口和功能
    """
    def __init__(self):
        """
        初始化下载器
        """
        self.config = load_config()
        self.api_key = self.config.get("tikhub", {}).get("api_key")
        self.temp_dir = self.config.get("storage", {}).get("temp_dir", "./data/temp")
        ensure_dir(self.temp_dir)
        
    @abstractmethod
    def can_handle(self, url):
        """
        判断是否可以处理该URL
        
        参数:
            url: 视频URL
            
        返回:
            bool: 是否可以处理
        """
        pass
    
    @abstractmethod
    def get_video_info(self, url):
        """
        获取视频信息
        
        参数:
            url: 视频URL
            
        返回:
            dict: 包含视频信息的字典，至少包含以下字段:
                - video_title: 视频标题
                - author: 视频作者
                - download_url: 音视频下载地址（可能是mp3或mp4等）
        """
        pass
    
    @abstractmethod
    def get_subtitle(self, url):
        """
        获取字幕，如果有的话
        
        参数:
            url: 视频URL
            
        返回:
            str: 字幕文本，如果没有则返回None
        """
        pass
    
    def resolve_short_url(self, url):
        """
        解析短链接，获取原始长链接
        
        参数:
            url: 短链接URL
            
        返回:
            str: 原始长链接
        """
        try:
            response = requests.head(url, allow_redirects=True, timeout=10)
            return response.url
        except Exception as e:
            logger.error(f"解析短链接失败: {url}, 错误: {str(e)}")
            return url
    
    def download_file(self, url, filename):
        """
        下载文件到本地
        
        参数:
            url: 文件URL
            filename: 本地文件名
            
        返回:
            str: 本地文件路径，如果下载失败则返回None
        """
        try:
            local_path = os.path.join(self.temp_dir, filename)
            
            # 创建目录（如果不存在）
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            
            response = requests.get(url, stream=True, timeout=60)
            response.raise_for_status()
            
            with open(local_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            
            logger.info(f"文件下载成功: {local_path}")
            return local_path
        except Exception as e:
            logger.error(f"文件下载失败: {url}, 错误: {str(e)}")
            return None
    
    def clean_up(self, file_path):
        """
        清理临时文件
        
        参数:
            file_path: 文件路径
            
        返回:
            bool: 是否成功清理
        """
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                logger.info(f"文件已删除: {file_path}")
                return True
            return False
        except Exception as e:
            logger.error(f"删除文件失败: {file_path}, 错误: {str(e)}")
            return False
    
    def make_api_request(self, endpoint, params=None):
        """
        调用TikHub API
        
        参数:
            endpoint: API端点
            params: 请求参数
            
        返回:
            dict: API响应
        """
        if not self.api_key:
            logger.error("TikHub API密钥未配置")
            raise ValueError("TikHub API密钥未配置")
        
        # 从配置中获取重试参数
        tikhub_config = self.config.get("tikhub", {})
        max_retries = tikhub_config.get("max_retries", 3)
        retry_delay = tikhub_config.get("retry_delay", 2)
        timeout = tikhub_config.get("timeout", 30)
        
        # 首先尝试主API密钥
        api_key = self.api_key
        
        # 确保API密钥不包含额外的空格和换行符
        api_key = api_key.strip()
        
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}"
        }
        
        url = f"https://api.tikhub.io{endpoint}"
        logger.info(f"发起API请求: {url}, 参数: {params}")
        logger.debug(f"API请求头: Accept: application/json, Authorization: Bearer {api_key[:5]}...{api_key[-5:]}")
        
        last_error = None
        
        # 尝试使用主API密钥
        response = self._try_api_request(url, headers, params, max_retries, retry_delay, timeout)
        if response:
            return response
            
        # 如果主API密钥失败，尝试备用API密钥
        alternate_api_key = tikhub_config.get("alternate_api_key")
        if alternate_api_key and alternate_api_key != "请替换为您的实际API密钥":
            # 确保备用API密钥不包含额外的空格和换行符
            alternate_api_key = alternate_api_key.strip()
            
            logger.info("主API密钥请求失败，尝试使用备用API密钥")
            headers["Authorization"] = f"Bearer {alternate_api_key}"
            logger.debug(f"备用API请求头: Accept: application/json, Authorization: Bearer {alternate_api_key[:5]}...{alternate_api_key[-5:]}")
            
            response = self._try_api_request(url, headers, params, max_retries, retry_delay, timeout)
            if response:
                return response
                
        # 如果所有尝试都失败
        error_message = "所有API请求尝试均失败"
        if last_error:
            error_message += f": {str(last_error)}"
        logger.error(error_message)
        raise ValueError(error_message)
    
    def _try_api_request(self, url, headers, params, max_retries, retry_delay, timeout):
        """
        尝试API请求，包含重试逻辑
        
        参数:
            url: API URL
            headers: 请求头
            params: 请求参数
            max_retries: 最大重试次数
            retry_delay: 重试间隔(秒)
            timeout: 请求超时(秒)
            
        返回:
            dict: 成功时返回API响应，失败时返回None
        """
        last_error = None
        
        for attempt in range(1, max_retries + 1):
            try:
                logger.info(f"API请求尝试 {attempt}/{max_retries}")
                
                # 记录完整请求信息（不包含敏感信息）
                logger.debug(f"请求URL: {url}")
                logger.debug(f"请求参数: {params}")
                logger.debug(f"请求头: {', '.join([f'{k}: {v[:15]}...' if k == 'Authorization' else f'{k}: {v}' for k, v in headers.items()])}")
                
                response = requests.get(url, headers=headers, params=params, timeout=timeout)
                
                # 记录HTTP状态码和响应头
                logger.info(f"API响应状态码: {response.status_code}")
                logger.debug(f"响应头: {dict(response.headers)}")
                
                # 保存前1000个字符的响应内容（仅调试模式）
                response_preview = response.text[:1000] if len(response.text) > 1000 else response.text
                logger.debug(f"响应内容预览: {response_preview}")
                
                # 检查是否返回错误状态码
                if response.status_code != 200:
                    error_message = f"API请求失败，状态码: {response.status_code}"
                    try:
                        error_data = response.json()
                        error_message += f", 错误信息: {error_data.get('message', '未知错误')}"
                        logger.debug(f"错误响应JSON: {error_data}")
                    except Exception as json_error:
                        error_message += f", 响应内容: {response.text[:100]}"
                        logger.debug(f"解析错误响应失败: {str(json_error)}")
                    
                    logger.error(error_message)
                    
                    # 如果是授权问题，直接失败不再重试
                    if response.status_code in (401, 403):
                        last_error = ValueError(f"API授权失败: {error_message}")
                        break
                    
                    # 如果是服务器错误，尝试重试
                    if response.status_code >= 500 and attempt < max_retries:
                        logger.warning(f"服务器错误，{retry_delay}秒后重试...")
                        time.sleep(retry_delay)
                        continue
                    
                    # 对于400错误，如果可能是URL编码问题，尝试不同的编码方式
                    if response.status_code == 400 and attempt < max_retries:
                        logger.warning(f"400错误，可能是参数编码问题，尝试不同的请求方式")
                        try:
                            # 构建完整URL，包含参数
                            param_str = "&".join([f"{k}={v}" for k, v in params.items()])
                            full_url = f"{url}?{param_str}"
                            logger.debug(f"尝试直接请求完整URL: {full_url}")
                            
                            # 直接请求完整URL
                            response = requests.get(full_url, headers=headers, timeout=timeout)
                            
                            if response.status_code == 200:
                                logger.info("使用完整URL成功获取响应")
                                try:
                                    result = response.json()
                                    return result
                                except json.JSONDecodeError:
                                    logger.error("解析JSON响应失败")
                        except Exception as retry_error:
                            logger.warning(f"重试请求失败: {str(retry_error)}")
                        
                        # 继续常规重试
                        time.sleep(retry_delay)
                        continue
                    
                    last_error = ValueError(error_message)
                    break
                
                # 尝试解析JSON响应
                try:
                    result = response.json()
                    
                    # 确保响应是字典类型
                    if not isinstance(result, dict):
                        logger.error(f"API响应格式错误，预期字典，实际: {type(result)}")
                        last_error = ValueError("API响应格式错误，无法解析响应")
                        break
                    
                    # 检查响应中的code字段
                    if "code" in result and result["code"] != 200:
                        logger.warning(f"API返回非成功状态码: {result.get('code')}, 消息: {result.get('message', '无消息')}")
                    
                    return result
                except json.JSONDecodeError as e:
                    logger.error(f"解析API响应JSON失败: {str(e)}, 响应内容: {response.text[:100]}")
                    last_error = ValueError(f"无法解析API响应: {str(e)}")
                    break
                
            except requests.RequestException as e:
                last_error = e
                logger.warning(f"API请求异常: {str(e)}, {type(e).__name__}")
                
                if attempt < max_retries:
                    logger.warning(f"API请求失败: {str(e)}, {retry_delay}秒后重试...")
                    time.sleep(retry_delay)
                else:
                    logger.error(f"API请求失败，已达到最大重试次数: {str(e)}")
        
        # 所有尝试都失败
        if last_error:
            logger.error(f"API请求失败: {str(last_error)}")
        return None 