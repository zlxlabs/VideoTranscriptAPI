"""
文本分段校对模块
用于处理超长转录文本的分段校对功能
"""
import json
import re
import os
from typing import List, Dict, Any, Tuple, Optional
from ..logging import setup_logger
from .llm import call_llm_api

logger = setup_logger(__name__)


class TextSegmentationProcessor:
    """文本分段处理器"""
    
    def __init__(self, config: Dict[str, Any]):
        """
        初始化分段处理器
        
        Args:
            config: 包含分段配置的字典
        """
        self.config = config
        segmentation_config = config.get('llm', {}).get('segmentation', {})
        
        # 分段配置参数 - 必须从config读取，不使用默认值
        if not segmentation_config:
            raise ValueError("配置文件中缺少 llm.segmentation 配置节")
        
        required_keys = ['enable_threshold', 'segment_size', 'max_segment_size']
        for key in required_keys:
            if key not in segmentation_config:
                raise ValueError(f"配置文件中缺少 llm.segmentation.{key} 配置项")
        
        self.enable_threshold = segmentation_config['enable_threshold']
        self.segment_size = segmentation_config['segment_size']
        self.max_segment_size = segmentation_config['max_segment_size']

        # 读取 reasoning_effort 配置
        llm_config = config.get('llm', {})
        self.calibrate_reasoning_effort = llm_config.get('calibrate_reasoning_effort', None)

        logger.info(f"文本分段处理器初始化完成 - 阈值: {self.enable_threshold}, 分段大小: {self.segment_size}")
    
    def get_text_length(self, file_path: str, file_type: str) -> int:
        """
        获取文件的文本长度
        
        Args:
            file_path: 文件路径
            file_type: 文件类型 ('txt' 或 'json')
            
        Returns:
            文本长度（字符数）
        """
        try:
            if file_type == 'txt':
                return self._get_txt_length(file_path)
            elif file_type == 'json':
                return self._get_json_text_length(file_path)
            else:
                raise ValueError(f"不支持的文件类型: {file_type}")
        except Exception as e:
            logger.error(f"获取文本长度失败 {file_path}: {e}")
            return 0
    
    def _get_txt_length(self, file_path: str) -> int:
        """获取TXT文件的文本长度"""
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return len(content)
    
    def _get_json_text_length(self, file_path: str) -> int:
        """获取JSON文件中所有text字段的文本总长度"""
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        total_length = 0
        segments = data.get('segments', [])
        
        for segment in segments:
            text = segment.get('text', '')
            total_length += len(text)
        
        return total_length
    
    def need_segmentation(self, file_path: str, file_type: str) -> bool:
        """
        判断是否需要分段处理
        
        Args:
            file_path: 文件路径
            file_type: 文件类型
            
        Returns:
            是否需要分段
        """
        text_length = self.get_text_length(file_path, file_type)
        need_seg = text_length > self.enable_threshold
        
        logger.info(f"文本长度检查 {os.path.basename(file_path)}: {text_length} 字符, 需要分段: {need_seg}")
        return need_seg
    
    def segment_txt_content(self, content: str) -> List[str]:
        """
        对TXT内容进行分段

        Args:
            content: 文本内容

        Returns:
            分段后的文本列表
        """
        segments = []

        # 检测文本格式：判断是否为 CapsWriter 格式（短句换行，无标点符号）
        # 统计标点符号密度：每1000字符中的句号、问号、感叹号数量
        text_length = len(content)
        if text_length > 0:
            punctuation_count = content.count('。') + content.count('！') + content.count('？') + content.count('!') + content.count('?')
            punctuation_density = (punctuation_count / text_length) * 1000  # 每1000字符的标点数

            # 如果标点密度小于5（即每1000字符少于5个标点），认为是 CapsWriter 格式
            is_capswriter_format = punctuation_density < 5
        else:
            is_capswriter_format = False

        if is_capswriter_format:
            logger.info("检测到短句换行格式（CapsWriter），按行分段处理")
            # CapsWriter 格式：按行分割，每行是一个短句
            lines = [line.strip() for line in content.split('\n') if line.strip()]

            if len(lines) <= 1:
                logger.info("CapsWriter 文本缺少有效换行，回退到标点分段策略")
                segments = self._segment_by_sentences(content)
                logger.info(f"TXT文本分段完成: {len(segments)} 个段落")
                return segments

            current_segment = ""
            for line in lines:
                # 超长单行直接切片，避免整段写入
                while len(line) > self.max_segment_size:
                    chunk = line[: self.max_segment_size]
                    line = line[self.max_segment_size :]
                    if current_segment:
                        chunk = current_segment + chunk
                        current_segment = ""
                    segments.append(chunk.strip())

                # 如果添加这一行不会超过最大限制
                if len(current_segment + line) < self.max_segment_size:
                    current_segment = (current_segment + line) if current_segment else line
                else:
                    # 如果当前段落已经达到合适大小，保存并开始新段落
                    if len(current_segment) >= self.segment_size:
                        segments.append(current_segment.strip())
                        current_segment = line
                    else:
                        # 当前段落还不够大，但加上新行会超限，强制添加
                        current_segment += line
                        segments.append(current_segment.strip())
                        current_segment = ""
        else:
            segments = self._segment_by_sentences(content)
            logger.info(f"TXT文本分段完成: {len(segments)} 个段落")
            return segments

        # 添加最后一段
        if current_segment.strip():
            segments.append(current_segment.strip())

        logger.info(f"TXT文本分段完成: {len(segments)} 个段落")
        return segments

    def _segment_by_sentences(self, content: str) -> List[str]:
        """按标点符号分段"""
        segments = []
        sentences = re.split(r'[。！？!?]', content)

        current_segment = ""
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue

            if len(current_segment + sentence) < self.max_segment_size:
                current_segment = (current_segment + sentence + "。") if current_segment else sentence + "。"
            else:
                if len(current_segment) >= self.segment_size:
                    segments.append(current_segment.strip())
                    current_segment = sentence + "。"
                else:
                    current_segment += sentence + "。"
                    segments.append(current_segment.strip())
                    current_segment = ""

        if current_segment.strip():
            segments.append(current_segment.strip())

        return segments
    
    def extract_speaker_mapping_from_json(self, file_path: str, title: str = "", description: str = "") -> Dict[str, str]:
        """
        从JSON文件中提取前1000字符，结合标题描述，生成说话人映射
        
        Args:
            file_path: JSON文件路径
            title: 视频标题
            description: 视频描述
            
        Returns:
            说话人映射字典 {speaker_id: speaker_name}
        """
        logger.info(f"开始提取说话人映射: {os.path.basename(file_path)}")
        
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # 提取前1000字符的文本，保留说话人信息
        segments = data.get('segments', [])
        sample_texts_by_speaker = {}
        total_chars = 0
        
        for segment in segments:
            text = segment.get('text', '').strip()
            speaker = segment.get('speaker', '')
            
            if text and speaker:
                if speaker not in sample_texts_by_speaker:
                    sample_texts_by_speaker[speaker] = []
                
                # 控制总字符数不超过1000
                if total_chars + len(text) <= 1000:
                    sample_texts_by_speaker[speaker].append(text)
                    total_chars += len(text)
                else:
                    break
        
        # 获取所有说话人ID
        speakers = list(sample_texts_by_speaker.keys())
        logger.info(f"检测到说话人: {speakers}")
        
        if not speakers:
            logger.warning("未检测到说话人信息")
            return {}
        
        # 使用LLM进行说话人推断
        try:
            speaker_mapping = self._infer_speakers_with_llm(
                sample_texts_by_speaker, title, description
            )
            logger.info(f"LLM说话人推断完成: {speaker_mapping}")
            return speaker_mapping
        except Exception as e:
            logger.error(f"LLM说话人推断失败: {e}，使用默认映射")
            # fallback到简单映射
            speaker_mapping = {}
            for i, speaker in enumerate(sorted(speakers)):
                speaker_mapping[speaker] = f"说话人{i+1}"
            return speaker_mapping
    
    def segment_json_content(self, file_path: str, speaker_mapping: Dict[str, str]) -> List[Dict[str, Any]]:
        """
        对JSON内容进行分段，并应用说话人映射
        
        Args:
            file_path: JSON文件路径
            speaker_mapping: 说话人映射字典
            
        Returns:
            分段后的JSON数据列表
        """
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        segments = data.get('segments', [])
        segment_groups = []
        current_group = []
        current_text_length = 0
        
        for segment in segments:
            text = segment.get('text', '')
            text_length = len(text)
            
            # 应用说话人映射
            original_speaker = segment.get('speaker', '')
            if original_speaker in speaker_mapping:
                segment['speaker'] = speaker_mapping[original_speaker]
            
            # 检查是否需要开始新的分组
            if current_text_length + text_length > self.max_segment_size and current_group:
                # 保存当前分组
                group_data = {
                    'task_id': data.get('task_id', ''),
                    'file_name': data.get('file_name', ''),
                    'segments': current_group
                }
                segment_groups.append(group_data)
                
                # 开始新分组
                current_group = [segment]
                current_text_length = text_length
            else:
                current_group.append(segment)
                current_text_length += text_length
        
        # 添加最后一组
        if current_group:
            group_data = {
                'task_id': data.get('task_id', ''),
                'file_name': data.get('file_name', ''),
                'segments': current_group
            }
            segment_groups.append(group_data)
        
        logger.info(f"JSON文本分段完成: {len(segment_groups)} 个段落")
        return segment_groups
    
    def merge_txt_segments(self, calibrated_segments: List[str]) -> str:
        """
        合并TXT分段校对结果
        
        Args:
            calibrated_segments: 校对后的文本段落列表
            
        Returns:
            合并后的完整文本
        """
        merged_text = "\n\n".join(calibrated_segments)
        logger.info(f"TXT分段合并完成，总长度: {len(merged_text)} 字符")
        return merged_text
    
    def merge_json_segments(self, calibrated_segments: List[str]) -> str:
        """
        合并JSON分段校对结果
        
        Args:
            calibrated_segments: 校对后的文本段落列表
            
        Returns:
            合并后的完整文本
        """
        merged_text = "\n\n".join(calibrated_segments)
        logger.info(f"JSON分段合并完成，总长度: {len(merged_text)} 字符")
        return merged_text
    
    def _infer_speakers_with_llm(self, sample_texts_by_speaker: Dict[str, List[str]], 
                                title: str = "", description: str = "") -> Dict[str, str]:
        """
        使用LLM推断说话人身份
        
        Args:
            sample_texts_by_speaker: 按说话人分组的样本文本
            title: 视频标题  
            description: 视频描述
            
        Returns:
            说话人映射字典 {原始speaker_id: 推断的人名}
        """
        # 构建样本文本
        sample_content = ""
        for speaker_id, texts in sample_texts_by_speaker.items():
            speaker_texts = " ".join(texts[:3])  # 取前3句话
            sample_content += f"{speaker_id}: {speaker_texts}\n"
        
        # 构建LLM prompt
        prompt = f"""你是一个专业的音频转录分析专家。请根据提供的转录样本、视频标题和描述，推断各个说话人的身份。

视频标题: {title if title else "未提供"}
视频描述: {description if description else "未提供"}

转录样本：
{sample_content}

请分析每个说话人的身份，并返回JSON格式的映射关系。

**重要：请严格按照以下格式返回，用括号分隔人名和身份信息：**
{{
  "speaker0": "人名（身份信息）",
  "speaker1": "人名（身份信息）",
  ...
}}

**示例格式：**
- "张三（主持人）"
- "李四（嘉宾）" 
- "王五（专家）"
- "主持人（节目主持）"（如果无法确定具体姓名）

说话人身份可能包括：主持人、嘉宾、讲师、学生、采访者、被采访者、创始人、CEO等。
如果无法确定具体姓名，请使用描述性角色名称+身份信息的格式。
确保返回的是有效的JSON格式，且每个值都包含括号分隔的格式。"""

        # 调用LLM API
        response = call_llm_api(
            model=self.config.get('llm', {}).get('calibrate_model', 'gpt-4.1-mini'),
            prompt=prompt,
            api_key=self.config.get('llm', {}).get('api_key', ''),
            base_url=self.config.get('llm', {}).get('base_url', ''),
            max_retries=self.config.get('llm', {}).get('max_retries', 2),
            retry_delay=self.config.get('llm', {}).get('retry_delay', 5),
            reasoning_effort=self.calibrate_reasoning_effort,
            task_type="speaker_mapping"
        )
        
        # 解析LLM响应
        try:
            import json
            # 提取JSON部分
            response_clean = response.strip()
            if response_clean.startswith('```json'):
                response_clean = response_clean[7:]
            if response_clean.endswith('```'):
                response_clean = response_clean[:-3]
            
            speaker_mapping = json.loads(response_clean.strip())
            
            # 验证并修正映射结果（处理大小写不匹配）
            original_speakers = set(sample_texts_by_speaker.keys())
            mapped_speakers = set(speaker_mapping.keys())
            
            # 创建大小写不敏感的映射
            valid_mapping = {}
            for original_speaker in original_speakers:
                # 尝试精确匹配
                if original_speaker in speaker_mapping:
                    valid_mapping[original_speaker] = speaker_mapping[original_speaker]
                else:
                    # 尝试大小写不敏感匹配
                    found = False
                    for mapped_speaker, mapped_name in speaker_mapping.items():
                        if mapped_speaker.lower() == original_speaker.lower():
                            valid_mapping[original_speaker] = mapped_name
                            found = True
                            break
                    
                    # 如果没找到匹配，使用默认名称
                    if not found:
                        valid_mapping[original_speaker] = f"说话人{original_speaker[-1] if original_speaker[-1].isdigit() else '1'}"
            
            if len(valid_mapping) != len(original_speakers):
                logger.warning(f"部分说话人ID未能匹配: 原始{original_speakers} -> 映射{valid_mapping}")
            
            # 清理说话人名称，去掉括号内的描述信息
            cleaned_mapping = {}
            for speaker_id, speaker_name in valid_mapping.items():
                # 只保留第一个括号之前的内容
                clean_name = speaker_name.split('（')[0].split('(')[0].strip()
                # 如果清理后为空，使用原名称
                if not clean_name:
                    clean_name = speaker_name
                cleaned_mapping[speaker_id] = clean_name
            
            return cleaned_mapping
            
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"解析LLM响应失败: {e}, 响应内容: {response[:200]}...")
            raise
