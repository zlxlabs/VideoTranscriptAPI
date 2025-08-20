import os
import re
import json
import time
import datetime
from .base import BaseDownloader
from ..utils import setup_logger, create_debug_dir

# 创建日志记录器
logger = setup_logger("xiaohongshu_downloader")
# 创建调试目录
DEBUG_DIR = create_debug_dir()

class XiaohongshuDownloader(BaseDownloader):
    """
    小红书视频下载器
    """
    def can_handle(self, url):
        """
        判断是否可以处理该URL
        
        参数:
            url: 视频URL
            
        返回:
            bool: 是否可以处理
        """
        return "xiaohongshu.com" in url or "xhslink.com" in url
    
    def extract_note_id(self, url):
        """
        从URL中提取笔记ID的公共方法
        
        参数:
            url: 视频URL
            
        返回:
            str: 笔记ID
        """
        return self._extract_note_id(url)
    
    def _extract_note_id(self, url):
        """
        从URL中提取笔记ID
        
        参数:
            url: 视频URL
            
        返回:
            str: 笔记ID
        """
        # 解析短链接
        if "xhslink.com" in url:
            logger.info(f"解析小红书短链接: {url}")
            url = self.resolve_short_url(url)
            logger.info(f"解析后的完整链接: {url}")
        
        # 尝试多种模式提取笔记ID
        patterns = [
            r'explore/(\w+)',          # 旧版URL格式
            r'discovery/item/(\w+)',   # 新版URL格式
            r'items/(\w+)',            # 另一种可能的格式
            r'/(\w{24})'               # 通用格式，匹配24位的ID
        ]
        
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                note_id = match.group(1)
                logger.info(f"从URL中提取到小红书笔记ID: {note_id}")
                return note_id
        
        # 如果用户直接提供了ID，尝试验证其格式
        if re.match(r'^\w{24}$', url):
            logger.info(f"用户直接提供了小红书笔记ID: {url}")
            return url
        
        logger.error(f"无法从URL中提取小红书笔记ID: {url}")
        raise ValueError(f"无法从URL中提取小红书笔记ID: {url}")
    
    def get_video_info(self, url):
        """
        获取视频信息
        
        参数:
            url: 视频URL
            
        返回:
            dict: 包含视频信息的字典
        """
        try:
            # 直接使用URL调用新的API接口，无需提取笔记ID
            logger.info(f"使用新版API获取小红书笔记信息: url={url}")
            return self.get_video_info_v3(url)
        except Exception as e:
            logger.exception(f"获取小红书视频信息异常: {str(e)}")
            raise
    
    def get_video_info_v3(self, url):
        """
        使用新版API（v3）获取视频信息
        
        参数:
            url: 视频URL
            
        返回:
            dict: 包含视频信息的字典
        """
        try:
            # 调用新版API获取视频信息
            endpoint = f"/api/v1/xiaohongshu/web/get_note_info_v3"
            params = {"share_text": url}  # 直接传递原始URL
            
            logger.info(f"调用TikHub API v3获取小红书笔记信息: url={url}")
            response = self.make_api_request(endpoint, params)
            
            # 生成时间戳前缀
            timestamp_prefix = datetime.datetime.now().strftime("%y%m%d-%H%M%S")
            
            # 记录API响应摘要，帮助调试
            if isinstance(response, dict):
                response_code = response.get("code")
                response_msg = response.get("message", "无消息")
                logger.info(f"API响应状态: {response_code}, 消息: {response_msg}")
                
                # 保存完整响应到文件，用于调试
                debug_file = os.path.join(DEBUG_DIR, f"{timestamp_prefix}_debug_xiaohongshu_v3.json")
                with open(debug_file, 'w', encoding='utf-8') as f:
                    json.dump(response, f, ensure_ascii=False, indent=2)
                logger.debug(f"API完整响应已保存到: {debug_file}")
            
            # 检查响应格式并提供详细错误信息
            if not isinstance(response, dict):
                logger.error(f"API返回格式错误，预期字典，实际: {type(response)}")
                raise ValueError("API返回格式错误，无法解析响应")
            
            # TikHub API成功响应时返回code=200
            if response.get("code") != 200:
                error_msg = response.get("message", "未知错误")
                logger.error(f"API返回错误代码: {response.get('code')}, 错误信息: {error_msg}")
                
                # 保存错误响应到文件
                error_file = os.path.join(DEBUG_DIR, f"{timestamp_prefix}_error_xiaohongshu_v3.json")
                with open(error_file, 'w', encoding='utf-8') as f:
                    json.dump(response, f, ensure_ascii=False, indent=2)
                logger.debug(f"错误响应已保存到: {error_file}")
                
                raise ValueError(f"获取小红书笔记信息失败: {error_msg}")
            
            # 检查data字段
            if not response.get("data") or not isinstance(response.get("data"), dict):
                logger.error("API响应中缺少data字段或格式不正确")
                raise ValueError("API响应数据格式错误，缺少必要字段")
            
            # 获取笔记详情数据 - 新版API响应结构
            data = response.get("data", {})
            
            # 解析v3 API响应
            # 视频标题
            video_title = data.get("title", "")
            if not video_title or video_title.strip() == "":
                # 尝试从URL中提取笔记ID作为备用标题
                try:
                    note_id = self._extract_note_id(url)
                    video_title = f"xiaohongshu_{note_id}"
                except:
                    video_title = f"xiaohongshu_{int(time.time())}"
                logger.warning(f"未找到视频标题，使用ID作为标题: {video_title}")
            
            # 视频作者
            author = data.get("user", {}).get("nickname", "未知作者")
            
            # 视频描述
            description = data.get("desc", "")
            
            logger.info(f"获取到视频信息: 标题='{video_title}', 作者='{author}', 描述长度={len(description)}")
            
            # 解析视频URL
            video_url = None
            
            # 从新版API中获取视频URL
            video = data.get("video", {})
            if video and video.get("media", {}).get("stream", {}).get("h264"):
                h264_streams = video.get("media", {}).get("stream", {}).get("h264", [])
                if h264_streams and len(h264_streams) > 0 and h264_streams[0].get("backup_urls"):
                    video_url = h264_streams[0].get("backup_urls", [])[0]
                    logger.info(f"从v3 API中找到视频URL: {video_url[:50]}...")
            
            # 检查是否有可用的视频URL
            if not video_url:
                logger.error(f"无法从新版API获取视频链接: {url}")
                
                # 保存错误数据到文件
                error_file = os.path.join(DEBUG_DIR, f"{timestamp_prefix}_error_video_url_xiaohongshu_v3.json")
                with open(error_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    
                raise ValueError(f"小红书笔记可能不是视频类型或无法获取视频链接: {url}")
            
            # 提取笔记ID
            note_id = None
            try:
                note_id = self._extract_note_id(url)
            except:
                note_id = f"unknown_{int(time.time())}"
                logger.warning(f"无法从URL中提取笔记ID，使用时间戳: {note_id}")
            
            # 清理文件名中的非法字符
            safe_title = re.sub(r'[\\/*?:"<>|]', "_", video_title)
            filename = f"xiaohongshu_{note_id}_{int(time.time())}.mp4"
            
            result = {
                "video_id": note_id,
                "video_title": video_title,
                "author": author,
                "description": description,
                "download_url": video_url,
                "filename": filename,
                "platform": "xiaohongshu"
            }
            
            logger.info(f"成功获取小红书视频信息: ID={note_id}")
            return result
        except Exception as e:
            logger.exception(f"使用新版API获取小红书视频信息异常: {str(e)}")
            raise
    
    def get_video_info_legacy(self, url):
        """
        使用旧版API获取视频信息（保留但不使用）
        
        参数:
            url: 视频URL
            
        返回:
            dict: 包含视频信息的字典
        """
        try:
            # 提取笔记ID
            note_id = self._extract_note_id(url)
            
            # 调用API获取视频信息
            endpoint = f"/api/v1/xiaohongshu/web/get_note_info"
            params = {"note_id": note_id}
            
            logger.info(f"调用TikHub API获取小红书笔记信息: note_id={note_id}")
            response = self.make_api_request(endpoint, params)
            
            # 生成时间戳前缀
            timestamp_prefix = datetime.datetime.now().strftime("%y%m%d-%H%M%S")
            
            # 记录API响应摘要，帮助调试
            if isinstance(response, dict):
                response_code = response.get("code")
                response_msg = response.get("message", "无消息")
                logger.info(f"API响应状态: {response_code}, 消息: {response_msg}")
                
                # 保存完整响应到文件，用于调试
                debug_file = os.path.join(DEBUG_DIR, f"{timestamp_prefix}_debug_xiaohongshu_{note_id}.json")
                with open(debug_file, 'w', encoding='utf-8') as f:
                    json.dump(response, f, ensure_ascii=False, indent=2)
                logger.debug(f"API完整响应已保存到: {debug_file}")
            
            # 检查响应格式并提供详细错误信息
            if not isinstance(response, dict):
                logger.error(f"API返回格式错误，预期字典，实际: {type(response)}")
                raise ValueError("API返回格式错误，无法解析响应")
            
            # TikHub API成功响应时返回code=200
            if response.get("code") != 200:
                error_msg = response.get("message", "未知错误")
                logger.error(f"API返回错误代码: {response.get('code')}, 错误信息: {error_msg}")
                
                # 保存错误响应到文件
                error_file = os.path.join(DEBUG_DIR, f"{timestamp_prefix}_error_xiaohongshu_{note_id}.json")
                with open(error_file, 'w', encoding='utf-8') as f:
                    json.dump(response, f, ensure_ascii=False, indent=2)
                logger.debug(f"错误响应已保存到: {error_file}")
                
                raise ValueError(f"获取小红书笔记信息失败: {error_msg}")
            
            # 检查data字段
            if not response.get("data") or not isinstance(response.get("data"), dict):
                logger.error("API响应中缺少data字段或格式不正确")
                raise ValueError("API响应数据格式错误，缺少必要字段")
            
            # 获取笔记详情数据 - 小红书API响应结构已变更
            data = response.get("data", {})
            
            # 检查内层data字段
            inner_data = data.get("data", {})
            if not inner_data:
                logger.error("API响应中缺少内层data字段")
                
                # 保存错误响应到文件
                error_file = os.path.join(DEBUG_DIR, f"{timestamp_prefix}_error_data_xiaohongshu_{note_id}.json")
                with open(error_file, 'w', encoding='utf-8') as f:
                    json.dump(response, f, ensure_ascii=False, indent=2)
                    
                logger.debug(f"API完整响应: {json.dumps(response, ensure_ascii=False)[:500]}...")
                raise ValueError("获取笔记详情失败，API返回数据结构不符合预期")
            
            # 获取data.data.data[0]
            data_list = inner_data.get("data", [])
            if not data_list or len(data_list) == 0:
                logger.error("API响应中data.data.data字段为空数组")
                
                # 保存错误响应到文件
                error_file = os.path.join(DEBUG_DIR, f"{timestamp_prefix}_error_data_list_xiaohongshu_{note_id}.json")
                with open(error_file, 'w', encoding='utf-8') as f:
                    json.dump(response, f, ensure_ascii=False, indent=2)
                    
                logger.debug(f"API完整响应: {json.dumps(response, ensure_ascii=False)[:500]}...")
                raise ValueError("获取笔记详情失败，API返回数据结构不符合预期")
            
            # 获取第一个笔记数据
            note_data = data_list[0]
            
            # 获取note_list字段
            note_list_data = note_data.get("note_list", [])
            if not note_list_data or len(note_list_data) == 0:
                logger.error("API响应中note_list字段为空数组")
                
                # 保存错误响应到文件
                error_file = os.path.join(DEBUG_DIR, f"{timestamp_prefix}_error_note_list_xiaohongshu_{note_id}.json")
                with open(error_file, 'w', encoding='utf-8') as f:
                    json.dump(note_data, f, ensure_ascii=False, indent=2)
                    
                logger.debug(f"API完整响应: {json.dumps(response, ensure_ascii=False)[:500]}...")
                raise ValueError("获取笔记详情失败，API返回数据结构不符合预期")
            
            # 获取第一个笔记
            note = note_list_data[0]
            
            # 视频标题
            video_title = note.get("title", "")
            if not video_title or video_title.strip() == "":
                video_title = f"xiaohongshu_{note_id}"
                logger.warning(f"未找到视频标题，使用ID作为标题: {video_title}")
            
            # 视频作者
            author = note_data.get("user", {}).get("name", "未知作者")
            
            # 视频描述 - 旧版API中暂时设为空字符串
            description = ""
            
            logger.info(f"获取到视频信息: 标题='{video_title}', 作者='{author}'")
            
            # 检查笔记类型
            note_type = note.get("type", "")
            if note_type != "video":
                logger.warning(f"笔记类型不是视频，而是: {note_type}")
            
            # 解析视频信息
            video_url = None
            
            # 1. 从widgets_context中解析视频信息
            widgets_context = note.get("widgets_context", "{}")
            try:
                widgets_data = json.loads(widgets_context)
                if widgets_data.get("video") and widgets_data.get("note_sound_info"):
                    sound_info = widgets_data.get("note_sound_info", {})
                    video_url = sound_info.get("url")
                    if video_url:
                        logger.info(f"从widgets_context中找到视频URL: {video_url[:50]}...")
            except Exception as e:
                logger.warning(f"解析widgets_context失败: {str(e)}")
            
            # 2. 尝试从video字段获取
            if not video_url:
                video = note.get("video", {})
                if video:
                    video_url = video.get("url")
                    if video_url:
                        logger.info(f"从video字段找到视频URL: {video_url[:50]}...")
            
            # 3. 检查是否有可用的视频URL
            if not video_url:
                logger.error(f"小红书笔记可能不是视频类型或无法获取视频链接: {url}")
                
                # 保存错误数据到文件
                error_file = os.path.join(DEBUG_DIR, f"{timestamp_prefix}_error_video_url_xiaohongshu_{note_id}.json")
                with open(error_file, 'w', encoding='utf-8') as f:
                    json.dump(note, f, ensure_ascii=False, indent=2)
                    
                raise ValueError(f"小红书笔记可能不是视频类型或无法获取视频链接: {url}")
            
            # 清理文件名中的非法字符
            safe_title = re.sub(r'[\\/*?:"<>|]', "_", video_title)
            filename = f"xiaohongshu_{note_id}_{int(time.time())}.mp4"
            
            result = {
                "video_id": note_id,
                "video_title": video_title,
                "author": author,
                "description": description,
                "download_url": video_url,
                "filename": filename,
                "platform": "xiaohongshu"
            }
            
            logger.info(f"成功获取小红书视频信息: ID={note_id}")
            return result
                
        except Exception as e:
            logger.exception(f"获取小红书视频信息异常: {str(e)}")
            raise
    
    def get_subtitle(self, url):
        """
        获取字幕，小红书视频通常没有字幕，返回None
        
        参数:
            url: 视频URL
            
        返回:
            str: 字幕文本，小红书通常返回None
        """
        # 直接返回None，跳过尝试获取字幕步骤
        return None 