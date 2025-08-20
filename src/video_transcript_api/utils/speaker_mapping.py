import re
import json
from typing import List, Dict, Tuple, Optional
from .logger import setup_logger

logger = setup_logger("speaker_mapping")

class SpeakerMappingInference:
    """
    说话人映射推断器
    负责从FunASR原始数据和LLM校对文本中推断speaker1/2/3到实际人名的映射关系
    """
    
    def __init__(self):
        """初始化说话人映射推断器"""
        # 说话人检测的正则模式
        self.speaker_patterns = [
            r'^([^：:]+)[：:](.+)$',  # 标准格式：姓名：内容
            r'^([^：:]+)说[：:](.+)$',  # 变体格式：姓名说：内容
        ]
        
        # 常见的非人名模式，用于过滤
        self.non_name_patterns = [
            r'^(内容|文本|转录|摘要|总结)$',
            r'^(speaker|spk)\d*$',
            r'^(旁白|解说|主持人|记者)$',
        ]
    
    def infer_mapping_from_cache(self, cache_dir: str) -> Optional[Dict[str, str]]:
        """
        从缓存目录推断说话人映射关系
        
        Args:
            cache_dir: 缓存目录路径
            
        Returns:
            Dict[str, str]: 映射关系字典，如 {"speaker1": "知白", "speaker2": "少楠"}
            None: 推断失败
        """
        try:
            # 读取FunASR原始数据
            funasr_data = self._load_funasr_data(cache_dir)
            if not funasr_data:
                return None
            
            # 读取LLM校对文本
            calibrated_text = self._load_calibrated_text(cache_dir)
            if not calibrated_text:
                return None
            
            # 执行映射推断
            return self.infer_mapping(funasr_data, calibrated_text)
            
        except Exception as e:
            logger.error(f"从缓存推断说话人映射失败 {cache_dir}: {e}")
            return None
    
    def infer_mapping(self, funasr_data: Dict, calibrated_text: str) -> Optional[Dict[str, str]]:
        """
        推断说话人映射关系
        
        Args:
            funasr_data: FunASR原始数据
            calibrated_text: LLM校对后的文本
            
        Returns:
            Dict[str, str]: 映射关系字典
        """
        try:
            # 1. 从FunASR数据提取原始speaker标识
            original_speakers = self.extract_speakers_from_funasr(funasr_data)
            if not original_speakers:
                logger.warning("FunASR数据中未找到说话人信息")
                return None
            
            # 2. 从校对文本提取实际人名
            detected_names = self.extract_names_from_calibrated_text(calibrated_text)
            if not detected_names:
                logger.warning("校对文本中未找到说话人姓名")
                return None
            
            # 3. 执行智能匹配
            mapping = self.smart_speaker_matching(
                funasr_data, calibrated_text, 
                original_speakers, detected_names
            )
            
            if mapping:
                logger.info(f"成功推断说话人映射: {mapping}")
            else:
                logger.warning("说话人映射推断失败")
            
            return mapping
            
        except Exception as e:
            logger.error(f"说话人映射推断异常: {e}")
            return None
    
    def extract_speakers_from_funasr(self, funasr_data: Dict) -> List[str]:
        """
        从FunASR数据中提取说话人列表
        
        Args:
            funasr_data: FunASR数据结构
            
        Returns:
            List[str]: 说话人标识列表，如 ["speaker1", "speaker2"]
        """
        speakers = set()
        
        try:
            # 方法1：从speakers字段直接获取
            if isinstance(funasr_data, dict) and 'speakers' in funasr_data:
                speakers_list = funasr_data['speakers']
                if isinstance(speakers_list, list):
                    return speakers_list
                elif isinstance(speakers_list, dict):
                    return list(speakers_list.keys())
            
            # 方法2：从转录段落中提取
            segments = []
            if isinstance(funasr_data, list):
                segments = funasr_data
            elif isinstance(funasr_data, dict) and 'segments' in funasr_data:
                segments = funasr_data['segments']
            elif isinstance(funasr_data, dict) and 'result' in funasr_data:
                segments = funasr_data['result']
            
            for segment in segments:
                if isinstance(segment, dict):
                    # 尝试不同的字段名
                    for field in ['spk', 'speaker', 'speaker_id']:
                        if field in segment and segment[field]:
                            speakers.add(str(segment[field]))
                            break
            
            # 排序确保一致性
            speakers_list = sorted(list(speakers))
            logger.debug(f"从FunASR数据提取到说话人: {speakers_list}")
            
            return speakers_list
            
        except Exception as e:
            logger.error(f"提取FunASR说话人失败: {e}")
            return []
    
    def extract_names_from_calibrated_text(self, calibrated_text: str) -> List[str]:
        """
        从校对文本中提取实际人名
        
        Args:
            calibrated_text: LLM校对后的文本
            
        Returns:
            List[str]: 人名列表，按出现顺序
        """
        names = []
        seen_names = set()
        
        try:
            lines = calibrated_text.split('\n')
            
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                
                # 尝试匹配说话人模式
                for pattern in self.speaker_patterns:
                    match = re.match(pattern, line)
                    if match:
                        speaker_name = match.group(1).strip()
                        
                        # 验证是否为有效人名
                        if self._is_valid_speaker_name(speaker_name):
                            if speaker_name not in seen_names:
                                names.append(speaker_name)
                                seen_names.add(speaker_name)
                        break
            
            logger.debug(f"从校对文本提取到人名: {names}")
            return names
            
        except Exception as e:
            logger.error(f"提取校对文本人名失败: {e}")
            return []
    
    def smart_speaker_matching(self, funasr_data: Dict, calibrated_text: str, 
                             original_speakers: List[str], detected_names: List[str]) -> Optional[Dict[str, str]]:
        """
        智能匹配说话人和人名
        
        Args:
            funasr_data: FunASR原始数据
            calibrated_text: 校对文本
            original_speakers: 原始说话人标识列表
            detected_names: 检测到的人名列表
            
        Returns:
            Dict[str, str]: 映射关系
        """
        try:
            # 简单情况：数量匹配，直接按顺序映射
            if len(original_speakers) == len(detected_names):
                mapping = dict(zip(original_speakers, detected_names))
                logger.info(f"简单匹配成功: {mapping}")
                return mapping
            
            # 复杂情况：需要基于内容相似度匹配
            return self._advanced_matching(funasr_data, calibrated_text, original_speakers, detected_names)
            
        except Exception as e:
            logger.error(f"智能匹配失败: {e}")
            return None
    
    def _advanced_matching(self, funasr_data: Dict, calibrated_text: str,
                          original_speakers: List[str], detected_names: List[str]) -> Optional[Dict[str, str]]:
        """
        高级匹配算法 - 基于内容相似度
        """
        try:
            # 构建说话人内容映射
            speaker_contents = self._build_speaker_contents(funasr_data)
            name_contents = self._build_name_contents(calibrated_text, detected_names)
            
            mapping = {}
            used_names = set()
            
            # 为每个原始speaker找最佳匹配
            for speaker in original_speakers:
                if speaker not in speaker_contents:
                    continue
                
                speaker_content = speaker_contents[speaker]
                best_name = None
                best_score = 0
                
                for name in detected_names:
                    if name in used_names or name not in name_contents:
                        continue
                    
                    name_content = name_contents[name]
                    score = self._calculate_content_similarity(speaker_content, name_content)
                    
                    if score > best_score:
                        best_score = score
                        best_name = name
                
                if best_name and best_score > 0.3:  # 阈值可调整
                    mapping[speaker] = best_name
                    used_names.add(best_name)
            
            logger.info(f"高级匹配结果: {mapping}")
            return mapping if mapping else None
            
        except Exception as e:
            logger.error(f"高级匹配算法失败: {e}")
            return None
    
    def _build_speaker_contents(self, funasr_data: Dict) -> Dict[str, str]:
        """构建原始speaker的内容映射"""
        speaker_contents = {}
        
        segments = []
        if isinstance(funasr_data, list):
            segments = funasr_data
        elif isinstance(funasr_data, dict):
            segments = funasr_data.get('segments', funasr_data.get('result', []))
        
        for segment in segments:
            if isinstance(segment, dict):
                speaker = None
                content = ""
                
                # 提取说话人标识
                for field in ['spk', 'speaker', 'speaker_id']:
                    if field in segment:
                        speaker = str(segment[field])
                        break
                
                # 提取内容
                for field in ['text', 'content', 'transcript']:
                    if field in segment:
                        content = str(segment[field])
                        break
                
                if speaker and content:
                    if speaker not in speaker_contents:
                        speaker_contents[speaker] = ""
                    speaker_contents[speaker] += content + " "
        
        return speaker_contents
    
    def _build_name_contents(self, calibrated_text: str, detected_names: List[str]) -> Dict[str, str]:
        """构建人名的内容映射"""
        name_contents = {name: "" for name in detected_names}
        
        lines = calibrated_text.split('\n')
        current_speaker = None
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # 检查是否为说话人行
            for pattern in self.speaker_patterns:
                match = re.match(pattern, line)
                if match:
                    speaker_name = match.group(1).strip()
                    content = match.group(2).strip()
                    
                    if speaker_name in detected_names:
                        current_speaker = speaker_name
                        name_contents[speaker_name] += content + " "
                    break
            else:
                # 继续上一个说话人的内容
                if current_speaker:
                    name_contents[current_speaker] += line + " "
        
        return name_contents
    
    def _calculate_content_similarity(self, content1: str, content2: str) -> float:
        """计算内容相似度（简单的字符级别相似度）"""
        if not content1 or not content2:
            return 0.0
        
        # 移除标点和空格，转为小写
        clean1 = re.sub(r'[^\w]', '', content1.lower())
        clean2 = re.sub(r'[^\w]', '', content2.lower())
        
        if not clean1 or not clean2:
            return 0.0
        
        # 简单的字符级别相似度
        common_chars = set(clean1) & set(clean2)
        total_chars = set(clean1) | set(clean2)
        
        return len(common_chars) / len(total_chars) if total_chars else 0.0
    
    def _is_valid_speaker_name(self, name: str) -> bool:
        """验证是否为有效的说话人姓名"""
        if not name or len(name) > 20:  # 长度限制
            return False
        
        # 检查是否匹配非人名模式
        for pattern in self.non_name_patterns:
            if re.match(pattern, name, re.IGNORECASE):
                return False
        
        # 基本验证：至少包含中文或字母
        if not re.search(r'[\u4e00-\u9fff]|[a-zA-Z]', name):
            return False
        
        return True
    
    def _load_funasr_data(self, cache_dir: str) -> Optional[Dict]:
        """从缓存目录加载FunASR数据"""
        import os
        
        funasr_file = os.path.join(cache_dir, 'transcript_funasr.json')
        if not os.path.exists(funasr_file):
            return None
        
        try:
            with open(funasr_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"加载FunASR数据失败 {funasr_file}: {e}")
            return None
    
    def _load_calibrated_text(self, cache_dir: str) -> Optional[str]:
        """从缓存目录加载校对文本"""
        import os
        
        calibrated_file = os.path.join(cache_dir, 'llm_calibrated.txt')
        if not os.path.exists(calibrated_file):
            return None
        
        try:
            with open(calibrated_file, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception as e:
            logger.error(f"加载校对文本失败 {calibrated_file}: {e}")
            return None

def infer_speaker_mapping_from_cache(cache_dir: str) -> Optional[Dict[str, str]]:
    """
    便捷函数：从缓存目录推断说话人映射关系
    
    Args:
        cache_dir: 缓存目录路径
        
    Returns:
        Dict[str, str]: 映射关系，如 {"speaker1": "知白", "speaker2": "少楠"}
    """
    inference = SpeakerMappingInference()
    return inference.infer_mapping_from_cache(cache_dir)