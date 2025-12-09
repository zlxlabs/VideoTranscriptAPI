import re
import json
import os
from typing import List, Dict, Tuple, Optional
from ..logging import setup_logger
from ..cache.cache_analyzer import analyze_cache_capabilities, CacheCapabilities
from ..llm.speaker_mapping import infer_speaker_mapping_from_cache

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
    
    def smart_paragraph_split(self, text: str) -> str:
        """
        智能分段，通过中英文标点符号自动分段
        
        Args:
            text: 输入文本
            
        Returns:
            str: 分段后的文本
        """
        if not text or len(text) < 100:  # 短文本无需分段
            return text
            
        # 中英文句号、问号、感叹号等结束符号
        sentence_endings = ['。', '！', '？', '.', '!', '?']
        # 逗号、分号等暂停符号（用于较长句子的断点）
        pause_marks = ['，', '；', ',', ';']
        
        # 分割成句子
        sentences = []
        current_sentence = ""
        
        i = 0
        while i < len(text):
            char = text[i]
            current_sentence += char
            
            # 检查是否是句子结束符
            if char in sentence_endings:
                # 检查下一个字符是否是引号、括号等
                next_chars = text[i+1:i+3] if i+1 < len(text) else ""
                if not next_chars or not any(c in '"）】"' for c in next_chars):
                    sentences.append(current_sentence.strip())
                    current_sentence = ""
            # 对于很长的句子，在逗号等处强制断句
            elif char in pause_marks and len(current_sentence) > 80:
                sentences.append(current_sentence.strip())
                current_sentence = ""
            
            i += 1
        
        # 添加最后的未完成句子
        if current_sentence.strip():
            sentences.append(current_sentence.strip())
        
        # 将句子组合成段落（2-4个句子为一段）
        paragraphs = []
        current_paragraph = []
        current_length = 0
        
        for sentence in sentences:
            current_paragraph.append(sentence)
            current_length += len(sentence)
            
            # 条件：达到2-4个句子且长度合适，或者单个句子太长
            if (len(current_paragraph) >= 2 and current_length > 120) or \
               (len(current_paragraph) >= 4) or \
               (len(sentence) > 150):  # 单个长句独立成段
                paragraphs.append(' '.join(current_paragraph))
                current_paragraph = []
                current_length = 0
        
        # 添加剩余的句子
        if current_paragraph:
            paragraphs.append(' '.join(current_paragraph))
        
        return '\n\n'.join(paragraphs)
    
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
                content = dialog.get('text', dialog.get('content', ''))
                color = self.get_speaker_color(speaker, speakers)
                
                # 智能分段处理
                smart_content = self.smart_paragraph_split(content)
                
                # 处理内容中的换行，为段落间增加特殊样式类
                content_html = smart_content.replace('\n\n', '</p><p class="paragraph-break">').replace('\n', '<br>')
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
        渲染普通文本，支持Markdown格式

        Args:
            text: 文本内容

        Returns:
            str: HTML格式的文本
        """
        if not text:
            return ""

        # 尝试使用Markdown渲染
        try:
            from .markdown_renderer import render_markdown_to_html
            html_result = render_markdown_to_html(text)
            # 如果markdown渲染成功且包含了表格等元素，直接返回
            if html_result and (
                '<table>' in html_result or
                '<h1>' in html_result or
                '<h2>' in html_result or
                '<ul>' in html_result or
                '<ol>' in html_result or
                '<blockquote>' in html_result or
                '<pre>' in html_result
            ):
                logger.debug("使用Markdown渲染成功，发现结构化内容")
                return html_result
        except Exception as e:
            logger.warning(f"Markdown渲染失败，降级到普通文本处理: {e}")

        # 降级到简单的段落处理
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
            logger.info(f"开始智能渲染，缓存目录: {cache_dir}")

            # 分析缓存能力
            capabilities = analyze_cache_capabilities(cache_dir)

            # 根据能力选择渲染策略
            strategy = self._get_optimal_rendering_strategy(capabilities)

            logger.info(f"最终选择的渲染策略: {strategy}")

            if strategy == 'structured':
                logger.info("执行结构化渲染路径")
                return self._render_from_structured_data(cache_dir)
            elif strategy == 'mapped':
                logger.info("执行映射渲染路径")
                return self._render_from_speaker_mapping(cache_dir)
            elif strategy == 'capswriter_long_text':
                logger.info("执行CapsWriter长文本渲染路径")
                return self._render_capswriter_long_text(cache_dir, fallback_text)
            elif strategy == 'detected':
                logger.info("执行文本检测渲染路径")
                return self._render_with_text_detection(cache_dir, fallback_text)
            else:
                logger.info("执行普通文本渲染路径（降级）")
                return self._render_normal_text_from_cache(cache_dir, fallback_text)

        except Exception as e:
            logger.error(f"智能渲染失败 {cache_dir}: {e}", exc_info=True)
            # 降级到基础文本渲染
            if fallback_text:
                logger.warning("降级到基础文本渲染（使用fallback_text）")
                return self.render_dialog_html(fallback_text)
            else:
                logger.warning("降级到基础文本渲染（从缓存读取）")
                return self._render_normal_text_from_cache(cache_dir, fallback_text)
    
    def render_original_transcript_with_cache_analysis(self, cache_dir: str, fallback_text: Optional[str] = None) -> str:
        """
        基于缓存分析的智能渲染（专门用于原始转录文本）
        当存在校对文本时，优先显示原始转录内容以避免重复

        Args:
            cache_dir: 缓存目录路径
            fallback_text: 降级文本内容

        Returns:
            str: 渲染后的HTML
        """
        try:
            logger.info(f"开始原始转录智能渲染，缓存目录: {cache_dir}")

            # 分析缓存能力
            capabilities = analyze_cache_capabilities(cache_dir)

            # 检查是否存在校对文本
            calibrated_file = os.path.join(cache_dir, 'llm_calibrated.txt')
            has_calibrated = os.path.exists(calibrated_file)

            # 根据能力选择渲染策略
            strategy = self._get_optimal_rendering_strategy(capabilities)

            logger.info(f"原始转录渲染策略: {strategy}, 有校对文本: {has_calibrated}")

            if strategy == 'structured':
                logger.info("执行结构化渲染路径（原始转录）")
                return self._render_from_structured_data(cache_dir)
            elif strategy == 'mapped':
                logger.info("执行映射渲染路径（原始转录）")
                return self._render_from_speaker_mapping(cache_dir)
            elif strategy == 'capswriter_long_text':
                logger.info("执行CapsWriter长文本渲染路径（原始转录）")
                return self._render_capswriter_long_text(cache_dir, fallback_text)
            elif strategy == 'detected' and has_calibrated:
                # 如果有校对文本，尝试显示原始转录而不是校对文本
                logger.info("执行原始转录检测渲染（跳过校对文本）")
                return self._render_original_transcript_detection(cache_dir, fallback_text)
            elif strategy == 'detected':
                logger.info("执行文本检测渲染路径（原始转录）")
                return self._render_with_text_detection(cache_dir, fallback_text)
            else:
                logger.info("执行普通文本渲染路径（原始转录，降级）")
                return self._render_normal_text_from_cache(cache_dir, fallback_text)

        except Exception as e:
            logger.error(f"原始转录智能渲染失败 {cache_dir}: {e}", exc_info=True)
            # 降级到基础文本渲染
            if fallback_text:
                logger.warning("降级到基础文本渲染（原始转录，使用fallback_text）")
                return self.render_dialog_html(fallback_text)
            else:
                logger.warning("降级到基础文本渲染（原始转录，从缓存读取）")
                return self._render_normal_text_from_cache(cache_dir, fallback_text)
    
    def _get_optimal_rendering_strategy(self, capabilities: CacheCapabilities) -> str:
        """根据缓存能力选择最优渲染策略"""
        logger.info(f"开始选择渲染策略，缓存目录: {capabilities.cache_dir}")
        logger.info(f"  - 主要引擎: {capabilities.primary_engine}")
        logger.info(f"  - 有说话人数据: {capabilities.has_speaker_data}")
        logger.info(f"  - 有结构化输出: {capabilities.has_structured_output}")
        logger.info(f"  - 文件存在情况: {capabilities.files_present}")

        # 策略1: 结构化渲染 - 最优
        if capabilities.has_structured_output:
            logger.info("  [Strategy] 'structured' - Structured rendering based on llm_processed.json")
            return 'structured'

        # 策略2: 映射渲染 - 次优，基于FunASR数据和映射关系
        if (capabilities.has_speaker_data and
            capabilities.files_present.get('calibrated_text', False)):
            logger.info("  [Strategy] 'mapped' - Mapping rendering based on FunASR data + calibrated text")
            return 'mapped'

        # 策略3: CapsWriter长文本渲染 - 针对CapsWriter转录且无说话人数据
        # 重要：CapsWriter转录的文本没有说话人信息，应该使用长文本分段逻辑
        if (capabilities.primary_engine == 'capswriter' and
            not capabilities.has_speaker_data):
            logger.info("  [Strategy] 'capswriter_long_text' - CapsWriter long text rendering (no speaker data)")
            return 'capswriter_long_text'

        # 策略4: 文本检测渲染 - 基础（仅当有说话人数据时才尝试检测对话）
        if (capabilities.files_present.get('calibrated_text', False) and
            capabilities.has_speaker_data):
            logger.info("  [Strategy] 'detected' - Text detection rendering (dialog detection)")
            return 'detected'

        # 策略5: 普通文本渲染 - 降级
        logger.info("  [Strategy] 'normal' - Normal text rendering (fallback)")
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
                content = dialog.get('text', dialog.get('content', ''))
                color = self.get_speaker_color(speaker, speakers)
                
                # 获取开始时间
                start_time = dialog.get('start_time', '')
                time_display = f'<span class="time-tag">{start_time}</span>' if start_time else ''
                
                # 智能分段处理
                smart_content = self.smart_paragraph_split(content)
                
                # 处理内容中的换行，为段落间增加特殊样式类
                content_html = smart_content.replace('\n\n', '</p><p class="paragraph-break">').replace('\n', '<br>')
                if content_html and not content_html.startswith('<p>'):
                    content_html = f'<p>{content_html}</p>'
                
                html_parts.append(f'''
                <div class="dialog-item">
                    <div class="speaker-header">
                        <div class="speaker-tag" style="background-color: {color};">
                            {speaker}
                        </div>
                        {time_display}
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
    
    def _render_capswriter_long_text(self, cache_dir: str, fallback_text: Optional[str] = None) -> str:
        """
        CapsWriter长文本渲染 - 针对无说话人数据的纯文本转录
        强制使用长文本分段逻辑，不尝试检测对话格式

        Args:
            cache_dir: 缓存目录路径
            fallback_text: 降级文本内容

        Returns:
            str: 渲染后的HTML
        """
        try:
            logger.info(f"开始CapsWriter长文本渲染: {cache_dir}")

            # 文本优先级：校对文本 > 原始转录文本 > 降级文本
            text_content = None
            source_file = None

            # 优先使用校对文本（LLM处理后的文本质量更高）
            calibrated_file = os.path.join(cache_dir, 'llm_calibrated.txt')
            if os.path.exists(calibrated_file):
                with open(calibrated_file, 'r', encoding='utf-8') as f:
                    text_content = f.read().strip()
                source_file = 'llm_calibrated.txt'
                logger.info(f"  使用校对文本作为源: {source_file}")

            # 降级到原始CapsWriter转录文本
            if not text_content:
                capswriter_file = os.path.join(cache_dir, 'transcript_capswriter.txt')
                if os.path.exists(capswriter_file):
                    with open(capswriter_file, 'r', encoding='utf-8') as f:
                        text_content = f.read().strip()
                    source_file = 'transcript_capswriter.txt'
                    logger.info(f"  使用CapsWriter原始转录: {source_file}")

            # 最后降级到fallback_text
            if not text_content:
                text_content = fallback_text
                source_file = 'fallback_text'
                logger.info(f"  使用降级文本: {source_file}")

            if not text_content:
                raise ValueError("无可用文本内容")

            logger.info(f"  文本长度: {len(text_content)} 字符")

            # 强制使用长文本渲染，不检测对话格式
            # 直接调用 _render_normal_text，跳过 detect_dialog_mode 检测
            html_result = self._render_normal_text(text_content)

            logger.info(f"  CapsWriter长文本渲染完成，HTML长度: {len(html_result)} 字符")
            return html_result

        except Exception as e:
            logger.error(f"CapsWriter长文本渲染失败 {cache_dir}: {e}", exc_info=True)
            raise

    def _render_with_text_detection(self, cache_dir: str, fallback_text: Optional[str] = None) -> str:
        """基于文本检测渲染 - 基础路径"""
        try:
            logger.info(f"开始文本检测渲染: {cache_dir}")

            # 读取校对文本
            calibrated_file = os.path.join(cache_dir, 'llm_calibrated.txt')

            if os.path.exists(calibrated_file):
                with open(calibrated_file, 'r', encoding='utf-8') as f:
                    text_content = f.read()
                logger.info(f"  使用校对文本，长度: {len(text_content)} 字符")
            else:
                text_content = fallback_text
                logger.info(f"  使用降级文本，长度: {len(text_content) if text_content else 0} 字符")

            if not text_content:
                raise ValueError("无可用文本内容")

            # 使用现有的文本检测渲染（会检测对话格式）
            is_dialog = self.detect_dialog_mode(text_content)
            logger.info(f"  检测到对话模式: {is_dialog}")

            html_result = self.render_dialog_html(text_content)
            logger.info(f"  文本检测渲染完成，HTML长度: {len(html_result)} 字符")

            return html_result

        except Exception as e:
            logger.error(f"文本检测渲染失败 {cache_dir}: {e}", exc_info=True)
            raise
    
    def _render_calibrated_text_detection(self, cache_dir: str) -> str:
        """专门用于校对文本的文本检测渲染，支持Markdown"""
        try:
            calibrated_file = os.path.join(cache_dir, 'llm_calibrated.txt')

            with open(calibrated_file, 'r', encoding='utf-8') as f:
                calibrated_text = f.read().strip()

            if not calibrated_text:
                raise ValueError("校对文本为空")

            # 首先尝试使用Markdown渲染（针对LLM生成的结构化内容）
            try:
                from .markdown_renderer import render_markdown_to_html
                html_result = render_markdown_to_html(calibrated_text)
                # 如果包含结构化内容，直接返回Markdown渲染结果
                if html_result and (
                    '<table>' in html_result or
                    '<h1>' in html_result or
                    '<h2>' in html_result or
                    '<ul>' in html_result or
                    '<ol>' in html_result or
                    '<blockquote>' in html_result or
                    '<pre>' in html_result
                ):
                    logger.debug("校对文本使用Markdown渲染成功，发现结构化内容")
                    return html_result
            except Exception as e:
                logger.warning(f"校对文本Markdown渲染失败，降级到对话检测: {e}")

            # 降级到原有的对话检测渲染
            return self.render_dialog_html(calibrated_text)

        except Exception as e:
            logger.error(f"校对文本检测渲染失败 {cache_dir}: {e}")
            raise
    
    def _render_original_transcript_detection(self, cache_dir: str, fallback_text: Optional[str] = None) -> str:
        """专门用于原始转录文本的文本检测渲染，优先显示非校对版本"""
        try:
            # 优先级：CapsWriter转录 > FunASR转录 > 降级文本
            for filename in ['transcript_capswriter.txt', 'transcript_funasr.json']:
                file_path = os.path.join(cache_dir, filename)
                if os.path.exists(file_path):
                    if filename.endswith('.txt'):
                        with open(file_path, 'r', encoding='utf-8') as f:
                            text_content = f.read().strip()
                    else:
                        # 处理FunASR JSON文件
                        import json
                        with open(file_path, 'r', encoding='utf-8') as f:
                            funasr_data = json.load(f)
                        # 提取纯文本内容（简化版本，不包含校对）
                        if isinstance(funasr_data, list):
                            text_parts = []
                            for item in funasr_data:
                                if isinstance(item, dict) and 'text' in item:
                                    text_parts.append(item['text'])
                            text_content = '\n'.join(text_parts)
                        else:
                            text_content = str(funasr_data)
                    
                    if text_content:
                        logger.debug(f"使用原始转录文件: {filename}")
                        return self.render_dialog_html(text_content)
            
            # 如果没有找到原始转录文件，使用降级文本
            if fallback_text:
                logger.debug("使用降级文本作为原始转录")
                return self.render_dialog_html(fallback_text)
            
            # 最后才使用校对文本
            calibrated_file = os.path.join(cache_dir, 'llm_calibrated.txt')
            if os.path.exists(calibrated_file):
                with open(calibrated_file, 'r', encoding='utf-8') as f:
                    calibrated_text = f.read().strip()
                if calibrated_text:
                    logger.debug("降级使用校对文本作为原始转录")
                    return self.render_dialog_html(calibrated_text)
            
            raise ValueError("没有找到任何可用的转录文本")
            
        except Exception as e:
            logger.error(f"原始转录文本检测渲染失败 {cache_dir}: {e}")
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
            content = dialog.get('text', dialog.get('content', ''))
            color = self.get_speaker_color(speaker, speakers)
            
            # 获取开始时间
            start_time = dialog.get('start_time', '')
            time_display = f'<span class="time-tag">{start_time}</span>' if start_time else ''
            
            # 智能分段处理
            smart_content = self.smart_paragraph_split(content)
            
            # 处理内容中的换行，为段落间增加特殊样式类
            content_html = smart_content.replace('\n\n', '</p><p class="paragraph-break">').replace('\n', '<br>')
            if content_html and not content_html.startswith('<p>'):
                content_html = f'<p>{content_html}</p>'
            
            html_parts.append(f'''
            <div class="dialog-item">
                <div class="speaker-header">
                    <div class="speaker-tag" style="background-color: {color};">
                        {speaker}
                    </div>
                    {time_display}
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
    专门用于"完整转录文本"区块，当存在校对文本时优先显示原始转录
    
    Args:
        cache_dir: 缓存目录路径
        fallback_text: 降级文本内容
        
    Returns:
        str: 渲染后的HTML
    """
    renderer = DialogRenderer()
    return renderer.render_original_transcript_with_cache_analysis(cache_dir, fallback_text)

def render_calibrated_content_smart(cache_dir: str, capabilities: Optional['CacheCapabilities'] = None) -> Optional[str]:
    """
    智能渲染校对文本内容的便捷函数，专门用于校对文本区块

    Args:
        cache_dir: 缓存目录路径
        capabilities: 可选的缓存能力信息，如果提供则直接使用，避免重复分析

    Returns:
        str: 渲染后的HTML，如果没有校对文本则返回None
    """
    if not cache_dir or not os.path.exists(cache_dir):
        logger.debug(f"校对文本渲染：缓存目录不存在: {cache_dir}")
        return None

    # 检查是否存在校对文本文件
    calibrated_file = os.path.join(cache_dir, 'llm_calibrated.txt')
    if not os.path.exists(calibrated_file):
        logger.debug(f"校对文本渲染：校对文本不存在: {calibrated_file}")
        return None

    try:
        logger.info(f"开始校对文本专用渲染: {cache_dir}")

        # 使用智能渲染系统处理校对文本，但强制使用校对文本内容
        renderer = DialogRenderer()

        # 分析缓存能力（如果未提供则进行分析）
        if capabilities is None:
            capabilities = analyze_cache_capabilities(cache_dir)
        else:
            logger.debug("复用已有的缓存能力分析结果")

        # 根据能力选择渲染策略，但总是基于校对文本
        strategy = renderer._get_optimal_rendering_strategy(capabilities)

        logger.info(f"校对文本专用渲染策略: {strategy}")

        # 强制使用校对文本内容进行渲染
        if strategy == 'structured':
            # 对于结构化数据，我们仍然使用结构化渲染，因为它包含了校对后的内容和时间信息
            logger.info("校对文本使用结构化渲染")
            return renderer._render_from_structured_data(cache_dir)
        elif strategy == 'mapped':
            # 对于映射渲染，我们也使用它，因为它基于校对文本和说话人映射
            logger.info("校对文本使用映射渲染")
            return renderer._render_from_speaker_mapping(cache_dir)
        elif strategy == 'capswriter_long_text':
            # CapsWriter转录的校对文本，使用长文本渲染
            logger.info("校对文本使用CapsWriter长文本渲染")
            return renderer._render_capswriter_long_text(cache_dir, None)
        else:
            # 对于其他情况，强制使用校对文本进行检测渲染
            logger.info("校对文本使用检测渲染")
            return renderer._render_calibrated_text_detection(cache_dir)

    except Exception as e:
        logger.error(f"智能渲染校对文本失败 {cache_dir}: {e}", exc_info=True)
        # 降级到基础文本渲染
        try:
            logger.warning("降级到基础校对文本渲染")
            with open(calibrated_file, 'r', encoding='utf-8') as f:
                calibrated_text = f.read().strip()
            if calibrated_text:
                renderer = DialogRenderer()
                return renderer.render_dialog_html(calibrated_text)
        except Exception as fallback_e:
            logger.error(f"降级渲染校对文本也失败 {cache_dir}: {fallback_e}", exc_info=True)
        return None
