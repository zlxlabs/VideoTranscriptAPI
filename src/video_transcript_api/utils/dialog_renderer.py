import re
import json
import os
from typing import List, Dict, Tuple, Optional
from .logger import setup_logger
from .cache_analyzer import analyze_cache_capabilities, CacheCapabilities
from .speaker_mapping import infer_speaker_mapping_from_cache

logger = setup_logger("dialog_renderer")

class DialogRenderer:
    """
    对话内容渲染器
    支持两种模式：
    1. 多人对话模式：自动检测说话人，应用对话样式
    2. 普通文本模式：无说话人识别，使用常规样式
    """
    
    # 现代美观的说话人颜色系统
    SPEAKER_COLORS = [
        '#3B82F6',  # 现代蓝
        '#10B981',  # 翡翠绿  
        '#8B5CF6',  # 紫罗兰
        '#F59E0B',  # 琥珀黄
        '#EF4444',  # 珊瑚红
        '#06B6D4',  # 天青色
        '#84CC16',  # 青柠绿
        '#F97316',  # 橙色
    ]
    
    def __init__(self):
        """初始化对话渲染器"""
        # 说话人检测的正则模式
        self.speaker_patterns = [
            r'^([^：:]+)[：:](.+)$',  # 标准格式：姓名：内容
            r'^([^：:]+)说[：:](.+)$',  # 变体格式：姓名说：内容
        ]
        
    def detect_dialog_mode(self, text: str) -> bool:
        """
        检测文本是否为多人对话格式
        
        Args:
            text: 输入文本
            
        Returns:
            bool: True表示多人对话，False表示普通文本
        """
        if not text or not isinstance(text, str):
            return False
            
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        
        if len(lines) < 2:
            return False
        
        # 检测包含说话人标识的行数
        dialog_lines = 0
        speakers = set()
        
        for line in lines:
            for pattern in self.speaker_patterns:
                match = re.match(pattern, line)
                if match:
                    speaker_name = match.group(1).strip()
                    if speaker_name and len(speaker_name) <= 20:  # 合理的姓名长度限制
                        speakers.add(speaker_name)
                        dialog_lines += 1
                        break
        
        # 判断标准：
        # 1. 至少有2个不同的说话人
        # 2. 至少30%的行包含说话人标识
        return (len(speakers) >= 2 and 
                dialog_lines >= max(2, len(lines) * 0.3))
    
    def parse_dialog_content(self, text: str) -> List[Dict[str, str]]:
        """
        解析多人对话内容
        
        Args:
            text: 对话文本
            
        Returns:
            List[Dict]: 解析后的对话列表，每个元素包含speaker和content
        """
        if not text:
            return []
            
        lines = text.split('\n')
        dialogs = []
        current_speaker = None
        current_content = []
        
        for line in lines:
            line = line.strip()
            if not line:
                if current_content:
                    current_content.append('')  # 保持空行
                continue
            
            # 尝试匹配说话人模式
            speaker_match = None
            for pattern in self.speaker_patterns:
                match = re.match(pattern, line)
                if match:
                    speaker_match = match
                    break
            
            if speaker_match:
                # 保存前一个说话人的内容
                if current_speaker and current_content:
                    content = '\n'.join(current_content).strip()
                    if content:
                        dialogs.append({
                            'speaker': current_speaker,
                            'content': content
                        })
                
                # 开始新的说话人
                current_speaker = speaker_match.group(1).strip()
                current_content = [speaker_match.group(2).strip()]
            else:
                # 继续当前说话人的内容
                if current_speaker:
                    current_content.append(line)
                else:
                    # 没有说话人的内容，作为第一个说话人处理
                    if not dialogs:
                        current_speaker = "内容"  # 默认说话人
                        current_content = [line]
        
        # 保存最后一个说话人的内容
        if current_speaker and current_content:
            content = '\n'.join(current_content).strip()
            if content:
                dialogs.append({
                    'speaker': current_speaker,
                    'content': content
                })
        
        return dialogs
    
    def get_speaker_color(self, speaker: str, speaker_list: List[str]) -> str:
        """
        获取说话人的颜色
        
        Args:
            speaker: 说话人姓名
            speaker_list: 所有说话人列表
            
        Returns:
            str: 十六进制颜色代码
        """
        try:
            index = speaker_list.index(speaker)
            return self.SPEAKER_COLORS[index % len(self.SPEAKER_COLORS)]
        except ValueError:
            return self.SPEAKER_COLORS[0]  # 默认颜色
    
    def render_dialog_html(self, text: str) -> str:
        """
        渲染对话为HTML格式
        
        Args:
            text: 输入文本
            
        Returns:
            str: 渲染后的HTML
        """
        try:
            # 检测是否为对话模式
            is_dialog = self.detect_dialog_mode(text)
            
            if not is_dialog:
                # 普通文本模式，使用常规段落样式
                return self._render_normal_text(text)
            
            # 多人对话模式
            dialogs = self.parse_dialog_content(text)
            if not dialogs:
                return self._render_normal_text(text)
            
            # 获取说话人列表和颜色映射
            speakers = list(dict.fromkeys([d['speaker'] for d in dialogs]))  # 保持顺序的去重
            
            html_parts = ['<div class="dialog-container">']
            
            for dialog in dialogs:
                speaker = dialog['speaker']
                content = dialog['content']
                color = self.get_speaker_color(speaker, speakers)
                
                # 处理内容中的换行
                content_html = content.replace('\n\n', '</p><p>').replace('\n', '<br>')
                if content_html and not content_html.startswith('<p>'):
                    content_html = f'<p>{content_html}</p>'
                
                html_parts.append(f'''
                <div class="dialog-item">
                    <div class="speaker-tag" style="background-color: {color};">
                        {speaker}
                    </div>
                    <div class="dialog-content">
                        {content_html}
                    </div>
                </div>
                ''')
            
            html_parts.append('</div>')
            
            return '\n'.join(html_parts)
            
        except Exception as e:
            logger.error(f"对话渲染失败: {e}")
            return self._render_normal_text(text)
    
    def _render_normal_text(self, text: str) -> str:
        """
        渲染普通文本
        
        Args:
            text: 文本内容
            
        Returns:
            str: HTML格式的文本
        """
        if not text:
            return ""
        
        # 简单的段落处理
        paragraphs = text.split('\n\n')
        html_parts = []
        
        for paragraph in paragraphs:
            paragraph = paragraph.strip()
            if paragraph:
                # 处理单个换行为<br>，双换行为段落分隔
                paragraph_html = paragraph.replace('\n', '<br>')
                html_parts.append(f'<p>{paragraph_html}</p>')
        
        if html_parts:
            return '\n'.join(html_parts)
        else:
            return f'<p>{text.replace(chr(10), "<br>")}</p>'
    
    def render_with_cache_analysis(self, cache_dir: str, fallback_text: Optional[str] = None) -> str:
        """
        基于缓存分析的智能渲染
        
        Args:
            cache_dir: 缓存目录路径
            fallback_text: 降级文本内容
            
        Returns:
            str: 渲染后的HTML
        """
        try:
            # 分析缓存能力
            capabilities = analyze_cache_capabilities(cache_dir)
            
            # 根据能力选择渲染策略
            strategy = self._get_optimal_rendering_strategy(capabilities)
            
            logger.debug(f"缓存 {cache_dir} 使用渲染策略: {strategy}")
            
            if strategy == 'structured':
                return self._render_from_structured_data(cache_dir)
            elif strategy == 'mapped':
                return self._render_from_speaker_mapping(cache_dir)
            elif strategy == 'detected':
                return self._render_with_text_detection(cache_dir, fallback_text)
            else:
                return self._render_normal_text_from_cache(cache_dir, fallback_text)
                
        except Exception as e:
            logger.error(f"智能渲染失败 {cache_dir}: {e}")
            # 降级到基础文本渲染
            if fallback_text:
                return self.render_dialog_html(fallback_text)
            else:
                return self._render_normal_text_from_cache(cache_dir, fallback_text)
    
    def _get_optimal_rendering_strategy(self, capabilities: CacheCapabilities) -> str:
        """根据缓存能力选择最优渲染策略"""
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
    
    def _render_from_structured_data(self, cache_dir: str) -> str:
        """从结构化数据渲染 - 最优路径"""
        try:
            structured_file = os.path.join(cache_dir, 'llm_processed.json')
            
            with open(structured_file, 'r', encoding='utf-8') as f:
                structured_data = json.load(f)
            
            # 检查数据格式
            if 'dialogs' not in structured_data:
                raise ValueError("结构化数据格式不正确")
            
            dialogs = structured_data['dialogs']
            speaker_mapping = structured_data.get('speaker_mapping', {})
            
            # 获取说话人列表
            speakers = list(dict.fromkeys([d['speaker'] for d in dialogs]))
            
            html_parts = ['<div class="dialog-container">']
            
            for dialog in dialogs:
                speaker = dialog['speaker']
                content = dialog['content']
                color = self.get_speaker_color(speaker, speakers)
                
                # 处理内容中的换行
                content_html = content.replace('\n\n', '</p><p>').replace('\n', '<br>')
                if content_html and not content_html.startswith('<p>'):
                    content_html = f'<p>{content_html}</p>'
                
                html_parts.append(f'''
                <div class="dialog-item">
                    <div class="speaker-tag" style="background-color: {color};">
                        {speaker}
                    </div>
                    <div class="dialog-content">
                        {content_html}
                    </div>
                </div>
                ''')
            
            html_parts.append('</div>')
            
            return '\n'.join(html_parts)
            
        except Exception as e:
            logger.error(f"结构化数据渲染失败 {cache_dir}: {e}")
            raise
    
    def _render_from_speaker_mapping(self, cache_dir: str) -> str:
        """基于说话人映射渲染 - 次优路径"""
        try:
            # 推断说话人映射关系
            speaker_mapping = infer_speaker_mapping_from_cache(cache_dir)
            if not speaker_mapping:
                raise ValueError("说话人映射推断失败")
            
            # 读取FunASR原始数据
            funasr_file = os.path.join(cache_dir, 'transcript_funasr.json')
            with open(funasr_file, 'r', encoding='utf-8') as f:
                funasr_data = json.load(f)
            
            # 基于映射关系重构对话
            dialogs = self._reconstruct_dialogs_from_mapping(funasr_data, speaker_mapping)
            
            if not dialogs:
                raise ValueError("基于映射重构对话失败")
            
            # 渲染对话
            return self._render_dialog_list(dialogs)
            
        except Exception as e:
            logger.error(f"映射渲染失败 {cache_dir}: {e}")
            raise
    
    def _render_with_text_detection(self, cache_dir: str, fallback_text: Optional[str] = None) -> str:
        """基于文本检测渲染 - 基础路径"""
        try:
            # 读取校对文本
            calibrated_file = os.path.join(cache_dir, 'llm_calibrated.txt')
            
            if os.path.exists(calibrated_file):
                with open(calibrated_file, 'r', encoding='utf-8') as f:
                    text_content = f.read()
            else:
                text_content = fallback_text
            
            if not text_content:
                raise ValueError("无可用文本内容")
            
            # 使用现有的文本检测渲染
            return self.render_dialog_html(text_content)
            
        except Exception as e:
            logger.error(f"文本检测渲染失败 {cache_dir}: {e}")
            raise
    
    def _render_normal_text_from_cache(self, cache_dir: str, fallback_text: Optional[str] = None) -> str:
        """普通文本渲染 - 降级路径"""
        try:
            # 尝试从各种文件读取文本内容
            text_content = None
            
            # 优先级：校对文本 > CapsWriter转录 > 降级文本
            for filename in ['llm_calibrated.txt', 'transcript_capswriter.txt']:
                file_path = os.path.join(cache_dir, filename)
                if os.path.exists(file_path):
                    with open(file_path, 'r', encoding='utf-8') as f:
                        text_content = f.read()
                        break
            
            if not text_content:
                text_content = fallback_text
            
            if text_content:
                return self._render_normal_text(text_content)
            else:
                return '<p style="color: #666; font-style: italic;">内容暂不可用</p>'
                
        except Exception as e:
            logger.error(f"普通文本渲染失败 {cache_dir}: {e}")
            return '<p style="color: #666; font-style: italic;">内容加载失败</p>'
    
    def _reconstruct_dialogs_from_mapping(self, funasr_data: Dict, speaker_mapping: Dict[str, str]) -> List[Dict[str, str]]:
        """基于映射关系重构对话列表"""
        try:
            dialogs = []
            
            # 提取转录段落
            segments = []
            if isinstance(funasr_data, list):
                segments = funasr_data
            elif isinstance(funasr_data, dict):
                for key in ['segments', 'result', 'data']:
                    if key in funasr_data:
                        segments = funasr_data[key]
                        break
            
            # 重构对话
            current_speaker = None
            current_content = []
            
            for segment in segments:
                if not isinstance(segment, dict):
                    continue
                
                # 提取说话人标识
                original_speaker = None
                for field in ['spk', 'speaker', 'speaker_id']:
                    if field in segment:
                        original_speaker = str(segment[field])
                        break
                
                # 提取文本内容
                text_content = ""
                for field in ['text', 'content', 'transcript']:
                    if field in segment:
                        text_content = str(segment[field]).strip()
                        break
                
                if not original_speaker or not text_content:
                    continue
                
                # 映射到实际人名
                actual_speaker = speaker_mapping.get(original_speaker, original_speaker)
                
                # 合并连续同一说话人的内容
                if current_speaker == actual_speaker:
                    current_content.append(text_content)
                else:
                    # 保存前一个说话人的内容
                    if current_speaker and current_content:
                        dialogs.append({
                            'speaker': current_speaker,
                            'content': ' '.join(current_content)
                        })
                    
                    # 开始新的说话人
                    current_speaker = actual_speaker
                    current_content = [text_content]
            
            # 保存最后一个说话人的内容
            if current_speaker and current_content:
                dialogs.append({
                    'speaker': current_speaker,
                    'content': ' '.join(current_content)
                })
            
            return dialogs
            
        except Exception as e:
            logger.error(f"重构对话失败: {e}")
            return []
    
    def _render_dialog_list(self, dialogs: List[Dict[str, str]]) -> str:
        """渲染对话列表"""
        if not dialogs:
            return '<p style="color: #666; font-style: italic;">无对话内容</p>'
        
        # 获取说话人列表
        speakers = list(dict.fromkeys([d['speaker'] for d in dialogs]))
        
        html_parts = ['<div class="dialog-container">']
        
        for dialog in dialogs:
            speaker = dialog['speaker']
            content = dialog['content']
            color = self.get_speaker_color(speaker, speakers)
            
            # 处理内容中的换行
            content_html = content.replace('\n\n', '</p><p>').replace('\n', '<br>')
            if content_html and not content_html.startswith('<p>'):
                content_html = f'<p>{content_html}</p>'
            
            html_parts.append(f'''
            <div class="dialog-item">
                <div class="speaker-tag" style="background-color: {color};">
                    {speaker}
                </div>
                <div class="dialog-content">
                    {content_html}
                </div>
            </div>
            ''')
        
        html_parts.append('</div>')
        
        return '\n'.join(html_parts)

def render_transcript_content(text: str) -> str:
    """
    渲染转录内容的便捷函数（基于文本检测）
    
    Args:
        text: 转录文本
        
    Returns:
        str: 渲染后的HTML
    """
    renderer = DialogRenderer()
    return renderer.render_dialog_html(text)

def render_transcript_content_smart(cache_dir: str, fallback_text: Optional[str] = None) -> str:
    """
    智能渲染转录内容的便捷函数（基于缓存分析）
    
    Args:
        cache_dir: 缓存目录路径
        fallback_text: 降级文本内容
        
    Returns:
        str: 渲染后的HTML
    """
    renderer = DialogRenderer()
    return renderer.render_with_cache_analysis(cache_dir, fallback_text)