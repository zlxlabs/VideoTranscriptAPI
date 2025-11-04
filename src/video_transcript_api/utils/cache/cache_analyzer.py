import os
import json
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from ..logging import setup_logger

logger = setup_logger("cache_analyzer")

@dataclass
class CacheCapabilities:
    """缓存能力信息"""
    has_speaker_data: bool = False           # 是否包含说话人数据
    has_structured_output: bool = False      # 是否有结构化输出
    primary_engine: Optional[str] = None     # 主要转录引擎 (funasr/capswriter)
    speakers_list: List[str] = None         # 说话人列表
    format_version: str = 'v1'              # 格式版本
    cache_dir: str = ""                     # 缓存目录路径
    files_present: Dict[str, bool] = None   # 存在的文件列表
    upgrade_priority: str = 'low'           # 升级优先级 (high/medium/low/none)

    def __post_init__(self):
        if self.speakers_list is None:
            self.speakers_list = []
        if self.files_present is None:
            self.files_present = {}

class CacheCapabilityAnalyzer:
    """
    缓存能力分析器
    负责分析缓存目录的格式版本、数据源类型和说话人支持能力
    """
    
    def __init__(self):
        """初始化缓存能力分析器"""
        # 缓存文件映射
        self.cache_files = {
            'funasr_transcript': 'transcript_funasr.json',
            'capswriter_transcript': 'transcript_capswriter.txt',
            'structured_output': 'llm_processed.json',
            'calibrated_text': 'llm_calibrated.txt',
            'summary_text': 'llm_summary.txt',
            'speaker_mapping': 'speaker_mapping.json',
            'format_version': '.format_version'
        }
    
    def analyze_cache(self, cache_dir: str) -> CacheCapabilities:
        """
        分析缓存目录的完整能力信息

        Args:
            cache_dir: 缓存目录路径

        Returns:
            CacheCapabilities: 缓存能力信息
        """
        try:
            logger.info(f"开始分析缓存能力: {cache_dir}")
            capabilities = CacheCapabilities(cache_dir=cache_dir)

            # 检查文件存在性
            capabilities.files_present = self._check_files_existence(cache_dir)
            logger.info(f"  File existence status:")
            for file_key, exists in capabilities.files_present.items():
                if exists:
                    logger.info(f"    [OK] {file_key}: {self.cache_files.get(file_key, 'unknown')}")

            # 检测格式版本
            capabilities.format_version = self._detect_format_version(cache_dir, capabilities.files_present)
            capabilities.has_structured_output = (capabilities.format_version == 'v2')
            logger.info(f"  格式版本: {capabilities.format_version}")
            logger.info(f"  有结构化输出: {capabilities.has_structured_output}")

            # 分析转录引擎类型
            capabilities.primary_engine = self._detect_primary_engine(capabilities.files_present)
            logger.info(f"  主要引擎: {capabilities.primary_engine}")

            # 分析说话人数据
            if capabilities.primary_engine == 'funasr':
                capabilities.has_speaker_data = True
                capabilities.speakers_list = self._extract_speakers_from_funasr(cache_dir)
                logger.info(f"  有说话人数据: True (FunASR)")
                logger.info(f"  说话人列表: {capabilities.speakers_list}")
            else:
                capabilities.has_speaker_data = False
                capabilities.speakers_list = []
                logger.info(f"  有说话人数据: False (CapsWriter或其他)")

            # 计算升级优先级
            capabilities.upgrade_priority = self._calculate_upgrade_priority(cache_dir, capabilities)
            logger.info(f"  升级优先级: {capabilities.upgrade_priority}")

            logger.info(f"缓存分析完成: {cache_dir}")
            return capabilities

        except Exception as e:
            logger.error(f"缓存分析失败 {cache_dir}: {e}", exc_info=True)
            # 返回默认能力信息
            return CacheCapabilities(
                cache_dir=cache_dir,
                has_speaker_data=False,
                has_structured_output=False,
                primary_engine=None,
                format_version='unknown',
                upgrade_priority='none'
            )
    
    def should_upgrade_cache(self, capabilities: CacheCapabilities) -> bool:
        """
        判断缓存是否值得升级
        
        Args:
            capabilities: 缓存能力信息
            
        Returns:
            bool: 是否应该升级
        """
        # 已经是新格式，无需升级
        if capabilities.format_version == 'v2':
            return False
        
        # 没有说话人数据，无法升级
        if not capabilities.has_speaker_data:
            return False
        
        # 没有校对文本，无法升级
        if not capabilities.files_present.get('calibrated_text', False):
            return False
        
        # 根据优先级决定
        return capabilities.upgrade_priority in ['high', 'medium']
    
    def get_optimal_rendering_strategy(self, capabilities: CacheCapabilities) -> str:
        """
        根据缓存能力选择最优渲染策略
        
        Args:
            capabilities: 缓存能力信息
            
        Returns:
            str: 渲染策略 (structured/mapped/detected/normal)
        """
        # 策略1: 结构化渲染 - 最优
        if capabilities.has_structured_output:
            return 'structured'
        
        # 策略2: 映射渲染 - 次优，基于FunASR数据和映射关系
        if (capabilities.has_speaker_data and 
            capabilities.files_present.get('calibrated_text', False)):
            return 'mapped'
        
        # 策略3: 文本检测渲染 - 基础
        if capabilities.files_present.get('calibrated_text', False):
            return 'detected'
        
        # 策略4: 普通文本渲染 - 降级
        return 'normal'
    
    def _check_files_existence(self, cache_dir: str) -> Dict[str, bool]:
        """检查缓存文件存在性"""
        files_present = {}
        
        for file_key, filename in self.cache_files.items():
            file_path = os.path.join(cache_dir, filename)
            files_present[file_key] = os.path.exists(file_path) and os.path.getsize(file_path) > 0
        
        return files_present
    
    def _detect_format_version(self, cache_dir: str, files_present: Dict[str, bool]) -> str:
        """检测缓存格式版本"""
        # 检查版本标识文件
        if files_present.get('format_version', False):
            try:
                version_file = os.path.join(cache_dir, self.cache_files['format_version'])
                with open(version_file, 'r', encoding='utf-8') as f:
                    return f.read().strip()
            except Exception:
                pass
        
        # 检查结构化输出文件
        if files_present.get('structured_output', False):
            return 'v2'
        
        # 检查是否有任何转录文件
        if (files_present.get('funasr_transcript', False) or 
            files_present.get('capswriter_transcript', False) or
            files_present.get('calibrated_text', False)):
            return 'v1'
        
        return 'unknown'
    
    def _detect_primary_engine(self, files_present: Dict[str, bool]) -> Optional[str]:
        """检测主要转录引擎"""
        # 优先检查FunASR
        if files_present.get('funasr_transcript', False):
            return 'funasr'
        
        # 检查CapsWriter
        if files_present.get('capswriter_transcript', False):
            return 'capswriter'
        
        return None
    
    def _extract_speakers_from_funasr(self, cache_dir: str) -> List[str]:
        """从FunASR数据提取说话人列表"""
        try:
            funasr_file = os.path.join(cache_dir, self.cache_files['funasr_transcript'])
            if not os.path.exists(funasr_file):
                return []
            
            with open(funasr_file, 'r', encoding='utf-8') as f:
                funasr_data = json.load(f)
            
            speakers = set()
            
            # 方法1：从speakers字段直接获取
            if isinstance(funasr_data, dict) and 'speakers' in funasr_data:
                speakers_data = funasr_data['speakers']
                if isinstance(speakers_data, list):
                    return speakers_data
                elif isinstance(speakers_data, dict):
                    return list(speakers_data.keys())
            
            # 方法2：从转录段落中提取
            segments = []
            if isinstance(funasr_data, list):
                segments = funasr_data
            elif isinstance(funasr_data, dict):
                for key in ['segments', 'result', 'data']:
                    if key in funasr_data:
                        segments = funasr_data[key]
                        break
            
            for segment in segments:
                if isinstance(segment, dict):
                    for field in ['spk', 'speaker', 'speaker_id']:
                        if field in segment and segment[field]:
                            speakers.add(str(segment[field]))
                            break
            
            return sorted(list(speakers))
            
        except Exception as e:
            logger.error(f"提取FunASR说话人失败 {cache_dir}: {e}")
            return []
    
    def _calculate_upgrade_priority(self, cache_dir: str, capabilities: CacheCapabilities) -> str:
        """计算升级优先级"""
        try:
            # 没有说话人数据，无法升级
            if not capabilities.has_speaker_data:
                return 'none'
            
            # 已经是新格式，无需升级
            if capabilities.format_version == 'v2':
                return 'none'
            
            # 检查访问频率（基于文件修改时间）
            access_score = self._calculate_access_score(cache_dir)
            
            # 检查数据完整性
            completeness_score = self._calculate_completeness_score(capabilities)
            
            # 综合评分
            total_score = access_score + completeness_score
            
            if total_score >= 8:
                return 'high'
            elif total_score >= 5:
                return 'medium'
            elif total_score >= 2:
                return 'low'
            else:
                return 'none'
                
        except Exception as e:
            logger.error(f"计算升级优先级失败 {cache_dir}: {e}")
            return 'low'
    
    def _calculate_access_score(self, cache_dir: str) -> int:
        """计算访问频率评分 (0-5分)"""
        try:
            import time
            
            current_time = time.time()
            recent_access = False
            
            # 检查关键文件的修改时间
            for filename in ['llm_calibrated.txt', 'llm_summary.txt']:
                file_path = os.path.join(cache_dir, filename)
                if os.path.exists(file_path):
                    mtime = os.path.getmtime(file_path)
                    days_ago = (current_time - mtime) / 86400  # 转换为天数
                    
                    if days_ago <= 7:
                        return 5  # 最近一周访问过
                    elif days_ago <= 30:
                        return 3  # 最近一月访问过
                    elif days_ago <= 90:
                        return 1  # 最近三月访问过
            
            return 0  # 很久未访问
            
        except Exception:
            return 0
    
    def _calculate_completeness_score(self, capabilities: CacheCapabilities) -> int:
        """计算数据完整性评分 (0-5分)"""
        score = 0
        
        # FunASR数据存在 +2分
        if capabilities.files_present.get('funasr_transcript', False):
            score += 2
        
        # 校对文本存在 +2分
        if capabilities.files_present.get('calibrated_text', False):
            score += 2
        
        # 总结文本存在 +1分
        if capabilities.files_present.get('summary_text', False):
            score += 1
        
        return score
    
    def get_cache_statistics(self, cache_base_dir: str) -> Dict:
        """获取缓存统计信息"""
        try:
            stats = {
                'total_caches': 0,
                'by_format': {'v1': 0, 'v2': 0, 'unknown': 0},
                'by_engine': {'funasr': 0, 'capswriter': 0, 'unknown': 0},
                'upgrade_candidates': {'high': 0, 'medium': 0, 'low': 0, 'none': 0},
                'with_speaker_data': 0,
                'with_structured_output': 0
            }
            
            # 遍历所有缓存目录
            for root, dirs, files in os.walk(cache_base_dir):
                # 跳过没有转录文件的目录
                if not any(f in files for f in ['transcript_funasr.json', 'transcript_capswriter.txt', 'llm_calibrated.txt']):
                    continue
                
                capabilities = self.analyze_cache(root)
                stats['total_caches'] += 1
                
                # 按格式统计
                stats['by_format'][capabilities.format_version] += 1
                
                # 按引擎统计
                engine = capabilities.primary_engine or 'unknown'
                stats['by_engine'][engine] += 1
                
                # 按升级优先级统计
                stats['upgrade_candidates'][capabilities.upgrade_priority] += 1
                
                # 其他统计
                if capabilities.has_speaker_data:
                    stats['with_speaker_data'] += 1
                
                if capabilities.has_structured_output:
                    stats['with_structured_output'] += 1
            
            return stats
            
        except Exception as e:
            logger.error(f"获取缓存统计失败: {e}")
            return {}

def analyze_cache_capabilities(cache_dir: str) -> CacheCapabilities:
    """
    便捷函数：分析单个缓存目录的能力
    
    Args:
        cache_dir: 缓存目录路径
        
    Returns:
        CacheCapabilities: 缓存能力信息
    """
    analyzer = CacheCapabilityAnalyzer()
    return analyzer.analyze_cache(cache_dir)

def should_upgrade_cache(cache_dir: str) -> bool:
    """
    便捷函数：判断缓存是否值得升级
    
    Args:
        cache_dir: 缓存目录路径
        
    Returns:
        bool: 是否应该升级
    """
    analyzer = CacheCapabilityAnalyzer()
    capabilities = analyzer.analyze_cache(cache_dir)
    return analyzer.should_upgrade_cache(capabilities)
