import os
import json
import datetime
from . import setup_logger, load_config

# 创建日志记录器
logger = setup_logger("metadata_cache")

class MetadataCache:
    """
    视频元数据缓存管理器
    用于保存和读取视频的元数据信息（标题、作者、描述等）
    """
    
    def __init__(self):
        """初始化元数据缓存管理器"""
        self.config = load_config()
        self.output_dir = self.config.get("storage", {}).get("output_dir", "./data/output")
    
    def _get_metadata_filename(self, base_filename):
        """
        根据转录文件名生成元数据文件名
        
        参数:
            base_filename: 转录文件的基础文件名（不含扩展名）
            
        返回:
            str: 元数据文件名
        """
        return f"{base_filename}.metadata.json"
    
    def save_metadata(self, base_filename, video_info):
        """
        保存视频元数据到缓存文件
        
        参数:
            base_filename: 转录文件的基础文件名（不含扩展名）
            video_info: 视频信息字典，包含 title、author、description 等
        """
        try:
            metadata_filename = self._get_metadata_filename(base_filename)
            metadata_path = os.path.join(self.output_dir, metadata_filename)
            
            # 构建元数据
            metadata = {
                "video_title": video_info.get("video_title", ""),
                "author": video_info.get("author", ""),
                "description": video_info.get("description", ""),
                "platform": video_info.get("platform", ""),
                "video_id": video_info.get("video_id", ""),
                "url": video_info.get("url", ""),
                "cached_at": datetime.datetime.now().isoformat(),
                "version": "1.0"
            }
            
            # 确保输出目录存在
            os.makedirs(self.output_dir, exist_ok=True)
            
            # 保存元数据
            with open(metadata_path, 'w', encoding='utf-8') as f:
                json.dump(metadata, f, ensure_ascii=False, indent=2)
            
            logger.info(f"元数据已保存到: {metadata_path}")
            return True
            
        except Exception as e:
            logger.error(f"保存元数据失败: {str(e)}")
            return False
    
    def load_metadata(self, base_filename):
        """
        从缓存文件加载视频元数据
        
        参数:
            base_filename: 转录文件的基础文件名（不含扩展名）
            
        返回:
            dict: 包含元数据的字典，如果文件不存在或读取失败则返回 None
        """
        try:
            metadata_filename = self._get_metadata_filename(base_filename)
            metadata_path = os.path.join(self.output_dir, metadata_filename)
            
            if not os.path.exists(metadata_path):
                logger.debug(f"元数据文件不存在: {metadata_path}")
                return None
            
            with open(metadata_path, 'r', encoding='utf-8') as f:
                metadata = json.load(f)
            
            logger.info(f"成功加载元数据: {metadata_path}")
            return metadata
            
        except Exception as e:
            logger.error(f"加载元数据失败: {str(e)}")
            return None
    
    def find_metadata_for_cached_file(self, cached_file_path):
        """
        根据缓存转录文件路径查找对应的元数据
        
        参数:
            cached_file_path: 缓存转录文件的完整路径
            
        返回:
            dict: 包含元数据的字典，如果找不到则返回 None
        """
        try:
            # 获取文件的基础名称（不含扩展名）
            base_name = os.path.splitext(os.path.basename(cached_file_path))[0]
            
            # 首先尝试精确匹配
            metadata = self.load_metadata(base_name)
            if metadata:
                return metadata
            
            # 如果精确匹配失败，尝试通过平台和视频ID查找
            # 从文件名中提取平台和视频ID
            parts = base_name.split('_')
            if len(parts) >= 2:
                platform = parts[0]
                video_id = parts[1]
                
                # 在输出目录中查找所有元数据文件
                if os.path.exists(self.output_dir):
                    for file in os.listdir(self.output_dir):
                        if file.endswith('.metadata.json') and f"_{platform}_{video_id}_" in file:
                            metadata_path = os.path.join(self.output_dir, file)
                            try:
                                with open(metadata_path, 'r', encoding='utf-8') as f:
                                    metadata = json.load(f)
                                    
                                # 验证平台和视频ID是否匹配
                                if metadata.get('platform') == platform and metadata.get('video_id') == video_id:
                                    logger.info(f"通过平台和视频ID找到元数据: {file}")
                                    return metadata
                            except Exception as e:
                                logger.debug(f"读取元数据文件失败 {file}: {str(e)}")
                                continue
            
            logger.debug(f"未找到对应的元数据文件: {cached_file_path}")
            return None
            
        except Exception as e:
            logger.error(f"查找元数据失败: {str(e)}")
            return None