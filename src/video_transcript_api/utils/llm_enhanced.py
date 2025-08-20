"""
增强的LLM处理模块
集成分段处理逻辑，自动判断是否需要分段
"""
import os
import threading
from typing import Dict, Any, Optional
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
        处理LLM任务，自动判断是否需要分段
        
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
        
        logger.info(f"开始处理LLM任务: {task_id}, 标题: {video_title}")
        
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