"""
增强的LLM处理模块
集成分段处理逻辑，自动判断是否需要分段
"""
import os
import threading
from typing import Dict, Any, Optional, List
from .logger import setup_logger
from .llm import call_llm_api
from .llm_segmented import SegmentedLLMProcessor
from .text_segmentation import TextSegmentationProcessor

logger = setup_logger(__name__)


class EnhancedLLMProcessor:
    """增强的LLM处理器，支持自动分段判断"""
    
    def __init__(self, config: Dict[str, Any]):
        """
        初始化增强LLM处理器
        
        Args:
            config: 配置字典
        """
        self.config = config
        self.llm_config = config.get('llm', {})
        
        # LLM API 配置 - 检查必需配置项
        required_llm_keys = ['api_key', 'base_url', 'calibrate_model', 'summary_model', 'max_retries', 'retry_delay']
        for key in required_llm_keys:
            if key not in self.llm_config:
                raise ValueError(f"配置文件中缺少 llm.{key} 配置项")
        
        self.api_key = self.llm_config['api_key']
        self.base_url = self.llm_config['base_url']
        self.calibrate_model = self.llm_config['calibrate_model']
        self.summary_model = self.llm_config['summary_model']
        self.max_retries = self.llm_config['max_retries']
        self.retry_delay = self.llm_config['retry_delay']
        
        # 初始化分段处理器
        self.segmentation_processor = TextSegmentationProcessor(config)
        self.segmented_llm_processor = SegmentedLLMProcessor(config)
        
        logger.info("增强LLM处理器初始化完成")
    
    def process_llm_task(self, llm_task: Dict[str, Any]) -> Dict[str, Any]:
        """
        处理LLM任务，自动判断是否需要分段和结构化处理
        
        Args:
            llm_task: LLM任务字典，包含所有必要信息
            
        Returns:
            包含校对文本、总结文本和是否分段标识的字典
        """
        task_id = llm_task["task_id"]
        transcript = llm_task["transcript"]
        use_speaker_recognition = llm_task.get("use_speaker_recognition", False)
        video_title = llm_task["video_title"]
        author = llm_task["author"]
        description = llm_task.get("description", "")
        transcription_data = llm_task.get("transcription_data")
        platform = llm_task.get("platform", "")
        media_id = llm_task.get("media_id", "")
        
        logger.info(f"开始处理LLM任务: {task_id}, 标题: {video_title}")
        
        # 优先使用结构化处理（仅限说话人识别场景）
        if use_speaker_recognition and transcription_data and platform and media_id:
            logger.info(f"检测到说话人识别场景，使用结构化处理: {task_id}, 平台: {platform}, 媒体ID: {media_id}")
            return self._process_with_structured_output(llm_task)
        
        # 根据transcription_data判断文件类型和处理方式
        if use_speaker_recognition and transcription_data:
            # FunASR JSON格式，需要创建临时文件进行处理
            file_type = 'json'
            temp_file_needed = True
        else:
            # CapsWriter TXT格式或纯文本
            file_type = 'txt'
            temp_file_needed = False
        
        # 判断是否需要分段处理
        text_length = len(transcript)
        need_segmentation = text_length > self.segmentation_processor.enable_threshold
        
        logger.info(f"文本长度: {text_length}, 需要分段: {need_segmentation}")
        
        if need_segmentation:
            if temp_file_needed:
                # 创建临时JSON文件进行分段处理
                return self._process_json_segmented(llm_task)
            else:
                # 直接对文本进行分段处理
                return self._process_txt_segmented(llm_task)
        else:
            # 使用原有逻辑处理
            return self._process_original_logic(llm_task)
    
    def _process_with_structured_output(self, llm_task: Dict[str, Any]) -> Dict[str, Any]:
        """
        使用结构化输出处理说话人识别任务
        
        Args:
            llm_task: LLM任务字典
            
        Returns:
            包含校对文本、总结文本和结构化数据的字典
        """
        try:
            # 构建缓存目录路径
            cache_dir = self._build_cache_dir_from_task(llm_task)
            
            # 构建视频元数据
            video_metadata = {
                'video_title': llm_task["video_title"],
                'author': llm_task["author"],
                'description': llm_task.get("description", "")
            }
            
            # 调用结构化处理方法
            result = self.process_llm_task_with_structure(
                cache_dir=cache_dir,
                funasr_data=llm_task.get("transcription_data"),
                video_metadata=video_metadata
            )
            
            logger.info(f"结构化处理完成: {llm_task['task_id']}")
            return result
            
        except Exception as e:
            logger.error(f"结构化处理失败，降级到传统处理: {e}")
            return self._process_original_logic(llm_task)
    
    def _build_cache_dir_from_task(self, llm_task: Dict[str, Any]) -> str:
        """从任务信息构建缓存目录路径"""
        import os
        from .cache_manager import CacheManager
        
        # 获取配置中的缓存目录
        cache_base_dir = self.config.get("storage", {}).get("cache_dir", "./data/cache")
        
        platform = llm_task.get("platform", "")
        media_id = llm_task.get("media_id", "")
        
        if not platform or not media_id:
            raise ValueError("缺少平台或媒体ID信息")
        
        # 构建缓存路径：cache_dir/platform/YYYY/YYYYMM/media_id
        import datetime
        now = datetime.datetime.now()
        year = now.strftime("%Y")
        year_month = now.strftime("%Y%m")
        
        cache_dir = os.path.join(cache_base_dir, platform, year, year_month, media_id)
        
        # 确保目录存在
        os.makedirs(cache_dir, exist_ok=True)
        
        return cache_dir
    
    def _process_txt_segmented(self, llm_task: Dict[str, Any]) -> Dict[str, str]:
        """处理TXT格式的分段校对"""
        import tempfile
        import os
        
        transcript = llm_task["transcript"]
        video_title = llm_task["video_title"]
        description = llm_task.get("description", "")
        
        # 创建临时文件
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.txt', delete=False) as temp_file:
            temp_file.write(transcript)
            temp_file_path = temp_file.name
        
        try:
            # 使用分段处理器进行校对
            calibrated_text = self.segmented_llm_processor.calibrate_text_segmented(
                temp_file_path, 'txt', video_title, description
            )
            
            # 进行总结
            summary_text = self.segmented_llm_processor.summarize_text_segmented(
                calibrated_text, video_title, description
            )
            
            return {
                '校对文本': calibrated_text,
                '内容总结': summary_text
            }
        finally:
            # 清理临时文件
            if os.path.exists(temp_file_path):
                os.unlink(temp_file_path)
    
    def _process_json_segmented(self, llm_task: Dict[str, Any]) -> Dict[str, str]:
        """处理JSON格式的分段校对"""
        import tempfile
        import json
        import os
        
        transcription_data = llm_task.get("transcription_data")
        video_title = llm_task["video_title"]
        description = llm_task.get("description", "")
        
        # 创建临时JSON文件
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.json', delete=False) as temp_file:
            json.dump(transcription_data, temp_file, ensure_ascii=False, indent=2)
            temp_file_path = temp_file.name
        
        try:
            # 使用分段处理器进行校对
            calibrated_text = self.segmented_llm_processor.calibrate_text_segmented(
                temp_file_path, 'json', video_title, description
            )
            
            # 进行总结
            summary_text = self.segmented_llm_processor.summarize_text_segmented(
                calibrated_text, video_title, description
            )
            
            return {
                '校对文本': calibrated_text,
                '内容总结': summary_text
            }
        finally:
            # 清理临时文件
            if os.path.exists(temp_file_path):
                os.unlink(temp_file_path)
    
    def _process_original_logic(self, llm_task: Dict[str, Any]) -> Dict[str, str]:
        """使用原有逻辑处理短文本"""
        task_id = llm_task["task_id"]
        transcript = llm_task["transcript"]
        use_speaker_recognition = llm_task.get("use_speaker_recognition", False)
        video_title = llm_task["video_title"]
        author = llm_task["author"]
        description = llm_task.get("description", "")
        transcription_data = llm_task.get("transcription_data")
        
        logger.info(f"使用原有逻辑处理短文本: {task_id}")
        
        # 生成校对提示词
        calibrate_prompt = self._generate_original_calibrate_prompt(
            transcript, video_title, author, description, use_speaker_recognition
        )
        
        # 生成总结提示词
        summary_prompt = self._generate_original_summary_prompt(
            transcript, video_title, author, description, use_speaker_recognition, transcription_data
        )
        
        # 并发调用LLM API
        result_dict = {}
        
        def run_calibrate():
            result_dict['校对文本'] = call_llm_api(
                self.calibrate_model, calibrate_prompt, self.api_key, 
                self.base_url, self.max_retries, self.retry_delay
            )
        
        def run_summary():
            result_dict['内容总结'] = call_llm_api(
                self.summary_model, summary_prompt, self.api_key, 
                self.base_url, self.max_retries, self.retry_delay
            )
        
        # 启动并发线程
        t1 = threading.Thread(target=run_calibrate)
        t2 = threading.Thread(target=run_summary)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        
        return result_dict
    
    def _generate_original_calibrate_prompt(self, transcript: str, video_title: str, 
                                          author: str, description: str, 
                                          use_speaker_recognition: bool) -> str:
        """生成原始校对提示词"""
        # 根据是否有说话人识别调整提示词
        speaker_prompt = ""
        if use_speaker_recognition:
            speaker_prompt = (
                "8. 文本中的 Speaker1、Speaker2 等是说话人标识。请尝试根据对话内容推测每个 Speaker "
                "的实际姓名或身份，并在文本中用推测的姓名替换 Speaker[x]。如果无法推测，则保留 "
                "Speaker[x] 的格式。例如，如果 Speaker1 自我介绍为'我是李明'，则将后续的 Speaker1 "
                "都替换为'李明'。 "
            )
        
        # 构建辅助信息
        context_info = ""
        if video_title or author or description:
            context_info = "\n以下是视频的辅助信息，可以帮助你更准确地校对文本中的专有名词和拼写错误：\n"
            if video_title:
                context_info += f"- 视频标题：{video_title}\n"
            if author:
                context_info += f"- 作者/频道：{author}\n"
            if description:
                context_info += f"- 视频描述：{description[:500]}{'...' if len(description) > 500 else ''}\n"
            context_info += "\n"
        
        calibrate_prompt = (
            "你将收到一段音频的转录文本。你的任务是对这段文本进行校对,提高其可读性,但不改变原意。 "
            + context_info +
            "请按照以下指示进行校对: "
            "1. 适当分段,使文本结构更清晰。每个自然段落应该是一个完整的思想单元。 "
            "2. 修正明显的错别字和语法错误。特别注意根据上述辅助信息修正专有名词的拼写。 "
            "3. 调整标点符号的使用,确保其正确性和一致性。 "
            "4. 如有必要,可以轻微调整词序以提高可读性,但不要改变原意。 "
            "5. 保留原文中的口语化表达和说话者的语气特点。 "
            "6. 不要添加或删除任何实质性内容。 "
            "7. 不要解释或评论文本内容。 "
            + speaker_prompt +
            "只返回校对后的文本,不要包含任何其他解释或评论。 "
            "以下是需要校对的转录文本: <transcript>  " + transcript + "  </transcript>"
        )
        
        return calibrate_prompt
    
    def _generate_original_summary_prompt(self, transcript: str, video_title: str, 
                                        author: str, description: str, 
                                        use_speaker_recognition: bool, 
                                        transcription_data: Optional[Dict[str, Any]]) -> str:
        """生成原始总结提示词"""
        # 检测说话人数量，决定总结策略
        speaker_count = 1  # 默认单说话人
        if use_speaker_recognition and transcription_data:
            # 从转录数据中获取说话人数量
            speakers = transcription_data.get("speakers", [])
            speaker_count = len(speakers) if speakers else 1
        elif use_speaker_recognition:
            # 如果没有转录数据，从文本中检测说话人标识
            import re
            speaker_pattern = r'Speaker\d+'
            unique_speakers = set(re.findall(speaker_pattern, transcript))
            speaker_count = len(unique_speakers) if unique_speakers else 1
        
        logger.info(f"检测到说话人数量: {speaker_count}，选择相应的总结策略")
        
        # 构建辅助信息
        context_info = ""
        if video_title or author or description:
            context_info = "\n以下是视频的辅助信息：\n"
            if video_title:
                context_info += f"- 视频标题：{video_title}\n"
            if author:
                context_info += f"- 作者/频道：{author}\n"
            if description:
                context_info += f"- 视频描述：{description[:500]}{'...' if len(description) > 500 else ''}\n"
            context_info += "\n"
        
        # 根据说话人数量选择不同的总结策略
        if speaker_count > 1:
            # 多说话人：使用结构化深度总结
            summary_prompt = (
                "这是一段多人对话的转录文本。请按以下结构进行详细总结：\n"
                + context_info +
                "\n## 1. 概述（Overview）\n"
                "用一段话（100-150字）点明对话的核心主题、参与者和关键结论。\n"
                "\n## 2. 主题详述\n"
                "按照对话中的主要话题进行梳理，每个主题都需要：\n"
                "- 详细展开讨论内容，包含各方观点、论据和细节（每个主题不少于300字）\n"
                "- 保留关键数字、定义、重要原话（用引号标注）\n"
                "- 说明不同说话人的立场和贡献\n"
                "- 如果能推测出Speaker的真实姓名或身份，请使用推测的姓名，无法推测则保留Speaker[x]\n"
                "\n## 3. 核心观点与洞察\n"
                "- 提炼对话中的核心观点和重要结论（每点150字以上）\n"
                "- 识别对话中达成的共识或分歧点\n"
                "- 总结各方的主要论点和支撑论据\n"
                "\n## 4. 框架与思维模型（如适用）\n"
                "如果对话中涉及方法论、流程或思维框架：\n"
                "- 将其重写为条理清晰的步骤或要点（不少于300字）\n"
                "- 说明该框架的应用场景和价值\n"
                "\n风格要求：\n"
                "- 永远不要高度浓缩，要充分展开细节\n"
                "- 使用分层的bullet points组织长段落，提高可读性\n"
                "- 专有名词保留原文，必要时括号内给出解释\n"
                "- 多使用emoji增加可读性\n"
                "- 专注于总结，要求类的指令禁止体现出来（例如 不少于300字、多使用 emoji，分层 bullet points）\n"
                "- 不新增事实，含混表述请保持原意并注明不确定性\n"
                "\n对话内容：\n" + transcript
            )
        else:
            # 单说话人或无说话人识别：使用结构化深度总结
            summary_speaker_instruction = ""
            if use_speaker_recognition:
                summary_speaker_instruction = (
                    "注意：如果文本中有 Speaker1 等说话人标识，请尝试根据内容推测具体姓名或身份，"
                    "无法推测则保留 Speaker[x] 的格式。"
                )
            
            summary_prompt = (
                "这是一段视频/音频的转录文本。请按以下结构进行详细总结：\n"
                + context_info
                + ("\n" + summary_speaker_instruction + "\n" if summary_speaker_instruction else "\n") +
                "\n## 1. 概述（Overview）\n"
                "用一段话（100-150字）点明视频的核心论题与结论。\n"
                "\n## 2. 按主题梳理\n"
                "识别并详细展开视频中的各个主题，要求：\n"
                "- 每个主题作为一个小节，详细展开内容（每个小节不少于500字）\n"
                "- 让读者不需要二次查看视频就能了解详情\n"
                "- 若出现方法/框架/流程，将其重写为条理清晰的步骤或段落\n"
                "- 若有关键数字、定义、原话，请如实保留核心词，并在括号内补充注释\n"
                "- 使用分层的bullet points组织内容，避免单个段落过长\n"
                "\n## 3. 框架与心智模型（Framework & Mindset）\n"
                "从视频中抽象出的framework & mindset，要求：\n"
                "- 将其重写为条理清晰的步骤或段落\n"
                "- 每个framework & mindset不少于500字\n"
                "- 说明其应用场景和核心价值\n"
                "- 如果视频中没有明显的框架或模型，可省略此部分\n"
                "\n风格与限制：\n"
                "- 永远不要高度浓缩！要充分展开所有细节\n"
                "- 不新增事实；若出现含混表述，请保持原意并注明不确定性\n"
                "- 专有名词保留原文，并在括号给出中文释义（若能直译）\n"
                "- 避免一个段落的内容过多，可以拆解成多个逻辑段落（使用bullet points）\n"
                "- 多使用emoji增加可读性\n"
                "- 专注于总结，要求类的指令禁止体现出来（例如 不少于300字、多使用 emoji，分层 bullet points）\n"
                "\n转录文本：\n" + transcript
            )
        
        return summary_prompt
    
    def process_llm_task_with_structure(self, cache_dir: str, funasr_data: Dict, video_metadata: Dict[str, Any]) -> Dict[str, Any]:
        """
        处理LLM任务并生成结构化输出（新格式）
        
        Args:
            cache_dir: 缓存目录路径
            funasr_data: FunASR原始转录数据
            video_metadata: 视频元数据信息
            
        Returns:
            Dict: 包含结构化对话数据和映射关系
        """
        try:
            # 导入说话人映射推断器
            from .speaker_mapping import SpeakerMappingInference
            
            # 提取基本信息
            video_title = video_metadata.get('video_title', '未知标题')
            author = video_metadata.get('author', '未知作者')
            description = video_metadata.get('description', '')
            
            logger.info(f"开始结构化LLM处理: {video_title}")
            
            # 1. 提取原始说话人信息
            mapping_inference = SpeakerMappingInference()
            original_speakers = mapping_inference.extract_speakers_from_funasr(funasr_data)
            
            if not original_speakers:
                logger.warning("FunASR数据中未找到说话人信息，降级到文本处理")
                return self._process_without_speakers(funasr_data, video_metadata, cache_dir)
            
            # 2. 生成说话人推断提示词
            speaker_inference_prompt = self._generate_speaker_inference_prompt(funasr_data, original_speakers, video_metadata)
            
            # 3. 调用LLM进行说话人推断
            logger.info("执行说话人推断")
            speaker_mapping_result = call_llm_api(
                prompt=speaker_inference_prompt,
                model=self.calibrate_model,
                api_key=self.api_key,
                base_url=self.base_url,
                max_retries=self.max_retries,
                retry_delay=self.retry_delay
            )
            
            # 4. 解析说话人映射关系
            speaker_mapping = self._parse_speaker_mapping_result(speaker_mapping_result, original_speakers)
            
            # 5. 生成结构化对话内容
            structured_dialogs = self._generate_structured_dialogs(funasr_data, speaker_mapping)
            
            # 6. 生成文本版本（兼容性）
            calibrated_text = self._generate_text_from_structured_dialogs(structured_dialogs)
            
            # 7. 生成总结
            summary_text = self._generate_summary_from_structured_content(calibrated_text, video_metadata)
            
            # 8. 构建结构化结果
            structured_result = {
                'format_version': 'v2',
                'video_metadata': video_metadata,
                'original_speakers': original_speakers,
                'speaker_mapping': speaker_mapping,
                'dialogs': structured_dialogs,
                'summary': summary_text,
                'generated_at': self._get_current_timestamp()
            }
            
            # 9. 保存结果到缓存
            self._save_structured_result(cache_dir, structured_result, calibrated_text, summary_text)
            
            logger.info(f"结构化LLM处理完成，已保存llm_processed.json到: {cache_dir}")
            return {
                '校对文本': calibrated_text,
                '内容总结': summary_text,
                '结构化数据': structured_result
            }
            
        except Exception as e:
            logger.error(f"结构化LLM处理失败: {e}")
            # 降级到传统处理方式
            return self._fallback_to_traditional_processing(funasr_data, video_metadata, cache_dir)
    
    def _generate_speaker_inference_prompt(self, funasr_data: Dict, original_speakers: List[str], video_metadata: Dict) -> str:
        """生成说话人推断提示词"""
        video_title = video_metadata.get('video_title', '未知标题')
        author = video_metadata.get('author', '未知作者')
        description = video_metadata.get('description', '')
        
        # 提取部分转录内容作为上下文
        context_snippets = self._extract_context_snippets(funasr_data, original_speakers)
        
        prompt = f"""你是一个专业的说话人识别专家。请基于以下转录内容，推断出每个说话人的真实姓名或身份。

视频信息：
- 标题：{video_title}
- 作者：{author}
- 描述：{description}

原始说话人标识：{', '.join(original_speakers)}

转录内容片段：
{context_snippets}

请按照以下JSON格式返回说话人映射关系：

```json
{{
    "speaker_mapping": {{
        "{original_speakers[0] if original_speakers else 'speaker1'}": "推断的真实姓名或身份",
        "{original_speakers[1] if len(original_speakers) > 1 else 'speaker2'}": "推断的真实姓名或身份"
    }},
    "confidence": {{
        "{original_speakers[0] if original_speakers else 'speaker1'}": 0.8,
        "{original_speakers[1] if len(original_speakers) > 1 else 'speaker2'}": 0.9
    }},
    "reasoning": "简要说明推断依据"
}}
```

推断规则：
1. 优先根据内容中的自我介绍、称呼等信息推断
2. 结合视频标题、作者信息进行合理推测
3. 如果无法确定，使用描述性身份（如"主持人"、"嘉宾"等）
4. 确信度请如实评估（0-1之间）
5. 姓名长度应合理（通常2-4个字符）
"""
        
        return prompt
    
    def _extract_context_snippets(self, funasr_data: Dict, speakers: List[str], max_snippets: int = 10) -> str:
        """提取关键的转录片段作为推断上下文"""
        snippets = []
        
        # 提取转录段落
        segments = []
        if isinstance(funasr_data, list):
            segments = funasr_data
        elif isinstance(funasr_data, dict):
            for key in ['segments', 'result', 'data']:
                if key in funasr_data:
                    segments = funasr_data[key]
                    break
        
        # 选择有代表性的片段
        speaker_samples = {speaker: [] for speaker in speakers}
        
        for segment in segments[:50]:  # 只看前50个段落
            if not isinstance(segment, dict):
                continue
            
            # 提取说话人和文本
            speaker = None
            text = ""
            
            for field in ['spk', 'speaker', 'speaker_id']:
                if field in segment:
                    speaker = str(segment[field])
                    break
            
            for field in ['text', 'content', 'transcript']:
                if field in segment:
                    text = str(segment[field]).strip()
                    break
            
            if speaker in speaker_samples and text and len(text) > 10:
                speaker_samples[speaker].append(text)
        
        # 构建上下文片段
        for speaker in speakers:
            samples = speaker_samples.get(speaker, [])[:3]  # 每个说话人最多3个样本
            if samples:
                snippets.append(f"\n{speaker}:")
                for i, sample in enumerate(samples, 1):
                    snippets.append(f"  {i}. {sample}")
        
        return '\n'.join(snippets) if snippets else "无足够的转录内容"
    
    def _parse_speaker_mapping_result(self, llm_result: str, original_speakers: List[str]) -> Dict[str, str]:
        """解析LLM返回的说话人映射结果"""
        try:
            import json
            import re
            
            # 尝试提取JSON部分
            json_match = re.search(r'```json\s*(.*?)\s*```', llm_result, re.DOTALL)
            if json_match:
                json_str = json_match.group(1)
            else:
                # 如果没找到代码块，尝试直接解析
                json_str = llm_result
            
            parsed = json.loads(json_str)
            speaker_mapping = parsed.get('speaker_mapping', {})
            
            # 验证映射关系
            validated_mapping = {}
            for original_speaker in original_speakers:
                if original_speaker in speaker_mapping:
                    mapped_name = speaker_mapping[original_speaker].strip()
                    if mapped_name and len(mapped_name) <= 20:  # 合理的名字长度
                        validated_mapping[original_speaker] = mapped_name
                    else:
                        validated_mapping[original_speaker] = original_speaker
                else:
                    validated_mapping[original_speaker] = original_speaker
            
            logger.info(f"成功解析说话人映射: {validated_mapping}")
            return validated_mapping
            
        except Exception as e:
            logger.error(f"解析说话人映射失败: {e}")
            # 降级：使用原始标识
            return {speaker: speaker for speaker in original_speakers}
    
    def _generate_structured_dialogs(self, funasr_data: Dict, speaker_mapping: Dict[str, str]) -> List[Dict[str, str]]:
        """基于映射关系生成结构化对话"""
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
    
    def _generate_text_from_structured_dialogs(self, dialogs: List[Dict[str, str]]) -> str:
        """从结构化对话生成文本版本（兼容性）"""
        text_lines = []
        
        for dialog in dialogs:
            speaker = dialog['speaker']
            content = dialog['content']
            text_lines.append(f"{speaker}：{content}")
        
        return '\n\n'.join(text_lines)
    
    def _generate_summary_from_structured_content(self, calibrated_text: str, video_metadata: Dict) -> str:
        """基于结构化内容生成总结"""
        # 复用现有的总结生成逻辑
        video_title = video_metadata.get('video_title', '')
        author = video_metadata.get('author', '')
        description = video_metadata.get('description', '')
        
        summary_prompt = self._generate_original_summary_prompt(
            calibrated_text, video_title, author, description, 
            use_speaker_recognition=True, transcription_data=None
        )
        
        return call_llm_api(
            prompt=summary_prompt,
            model=self.summary_model,
            api_key=self.api_key,
            base_url=self.base_url,
            max_retries=self.max_retries,
            retry_delay=self.retry_delay
        )
    
    def _save_structured_result(self, cache_dir: str, structured_result: Dict, calibrated_text: str, summary_text: str):
        """保存结构化结果到缓存"""
        import json
        import os
        
        # 保存结构化JSON
        structured_file = os.path.join(cache_dir, 'llm_processed.json')
        with open(structured_file, 'w', encoding='utf-8') as f:
            json.dump(structured_result, f, ensure_ascii=False, indent=2)
        
        # 保存兼容性文本文件
        calibrated_file = os.path.join(cache_dir, 'llm_calibrated.txt')
        with open(calibrated_file, 'w', encoding='utf-8') as f:
            f.write(calibrated_text)
        
        summary_file = os.path.join(cache_dir, 'llm_summary.txt')
        with open(summary_file, 'w', encoding='utf-8') as f:
            f.write(summary_text)
        
        # 保存版本标识
        version_file = os.path.join(cache_dir, '.format_version')
        with open(version_file, 'w', encoding='utf-8') as f:
            f.write('v2')
        
        logger.info(f"结构化结果已保存到: {cache_dir}")
    
    def _process_without_speakers(self, funasr_data: Dict, video_metadata: Dict, cache_dir: str) -> Dict[str, Any]:
        """处理无说话人数据的情况"""
        # 提取纯文本
        text_content = self._extract_text_from_funasr(funasr_data)
        
        # 使用传统LLM处理
        llm_task = {
            'task_id': 'structured_fallback',
            'transcript': text_content,
            'use_speaker_recognition': False,
            'video_title': video_metadata.get('video_title', ''),
            'author': video_metadata.get('author', ''),
            'description': video_metadata.get('description', ''),
            'transcription_data': funasr_data
        }
        
        return self._process_original_logic(llm_task)
    
    def _extract_text_from_funasr(self, funasr_data: Dict) -> str:
        """从FunASR数据中提取纯文本"""
        text_parts = []
        
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
                for field in ['text', 'content', 'transcript']:
                    if field in segment:
                        text_parts.append(str(segment[field]).strip())
                        break
        
        return ' '.join(text_parts)
    
    def _fallback_to_traditional_processing(self, funasr_data: Dict, video_metadata: Dict, cache_dir: str) -> Dict[str, Any]:
        """降级到传统处理方式"""
        logger.warning("降级到传统LLM处理方式")
        return self._process_without_speakers(funasr_data, video_metadata, cache_dir)
    
    def _get_current_timestamp(self) -> str:
        """获取当前时间戳"""
        import datetime
        return datetime.datetime.now().isoformat()
    
    def should_use_structured_processing(self, cache_dir: str) -> bool:
        """判断是否应该使用结构化处理"""
        # 检查是否已有结构化数据
        import os
        structured_file = os.path.join(cache_dir, 'llm_processed.json')
        if os.path.exists(structured_file):
            return False  # 已有结构化数据，无需重复处理
        
        # 检查是否有FunASR数据
        funasr_file = os.path.join(cache_dir, 'transcript_funasr.json')
        return os.path.exists(funasr_file)