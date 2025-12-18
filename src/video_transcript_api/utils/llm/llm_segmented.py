"""
分段LLM处理模块
处理超长文本的分段校对和总结
"""
import json
import os
from typing import List, Dict, Any, Optional, Tuple
from ..logging import setup_logger
from .llm import call_llm_api
from .text_segmentation import TextSegmentationProcessor

logger = setup_logger(__name__)


class SegmentedLLMProcessor:
    """分段LLM处理器"""
    
    def __init__(self, config: Dict[str, Any]):
        """
        初始化分段LLM处理器
        
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
        self.calibrate_reasoning_effort = self.llm_config.get('calibrate_reasoning_effort', None)
        self.summary_model = self.llm_config['summary_model']
        self.summary_reasoning_effort = self.llm_config.get('summary_reasoning_effort', None)
        self.max_retries = self.llm_config['max_retries']
        self.retry_delay = self.llm_config['retry_delay']
        
        # 初始化分段处理器
        self.segmentation_processor = TextSegmentationProcessor(config)
        
        # 并发配置
        segmentation_config = self.llm_config.get('segmentation', {})
        if 'concurrent_workers' not in segmentation_config:
            raise ValueError("配置文件中缺少 llm.segmentation.concurrent_workers 配置项")
        self.concurrent_workers = segmentation_config['concurrent_workers']
        
        logger.info(f"分段LLM处理器初始化完成，并发数: {self.concurrent_workers}")
    
    def calibrate_text_segmented(
        self,
        file_path: str,
        file_type: str,
        title: str = "",
        description: str = "",
        speaker_mapping: Optional[Dict[str, str]] = None,
        selected_calibrate_model: str = None,
        selected_calibrate_effort: str = None,
    ) -> str:
        """
        对文本进行分段校对

        Args:
            file_path: 文件路径
            file_type: 文件类型 ('txt' 或 'json')
            title: 视频标题
            description: 视频描述
            speaker_mapping: 说话人映射（可选）
            selected_calibrate_model: 选定的校对模型（可选，默认使用配置的模型）
            selected_calibrate_effort: 选定的校对 reasoning_effort（可选）

        Returns:
            校对后的完整文本
        """
        # 如果未指定模型，使用默认配置
        if selected_calibrate_model is None:
            selected_calibrate_model = self.calibrate_model
        if selected_calibrate_effort is None:
            selected_calibrate_effort = self.calibrate_reasoning_effort
        logger.info(f"开始分段校对: {os.path.basename(file_path)} (类型: {file_type}), 模型: {selected_calibrate_model}")

        try:
            if file_type == 'txt':
                return self._calibrate_txt_segmented(
                    file_path, title, description,
                    selected_calibrate_model, selected_calibrate_effort
                )
            elif file_type == 'json':
                return self._calibrate_json_segmented(
                    file_path, title, description,
                    speaker_mapping=speaker_mapping,
                    selected_calibrate_model=selected_calibrate_model,
                    selected_calibrate_effort=selected_calibrate_effort,
                )
            else:
                raise ValueError(f"不支持的文件类型: {file_type}")
        except Exception as e:
            logger.error(f"分段校对失败 {file_path}: {e}")
            return f"【分段校对失败】{e}"
    
    def _calibrate_txt_segmented(
        self, file_path: str, title: str = "", description: str = "",
        selected_calibrate_model: str = None, selected_calibrate_effort: str = None
    ) -> str:
        """
        对TXT文件进行并发分段校对

        Args:
            file_path: TXT文件路径
            title: 视频标题
            description: 视频描述
            selected_calibrate_model: 选定的校对模型
            selected_calibrate_effort: 选定的校对 reasoning_effort

        Returns:
            校对后的完整文本
        """
        # 如果未指定模型，使用默认配置
        if selected_calibrate_model is None:
            selected_calibrate_model = self.calibrate_model
        if selected_calibrate_effort is None:
            selected_calibrate_effort = self.calibrate_reasoning_effort
        import threading
        import concurrent.futures
        
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # 分段处理
        segments = self.segmentation_processor.segment_txt_content(content)
        total_segments = len(segments)
        logger.info(f"开始并发分段校对，共 {total_segments} 个段落")
        
        # 使用线程池进行并发校对
        calibrated_segments = [None] * total_segments  # 保持原始顺序
        
        def calibrate_segment(index, segment):
            """校对单个段落"""
            logger.info(f"开始校对第 {index+1}/{total_segments} 段 (长度: {len(segment)} 字符)")

            def run_calibration(retry_idx: int):
                prompt = self._generate_calibrate_prompt(
                    segment,
                    False,
                    title,
                    "",
                    description,
                    length_retry_level=retry_idx,
                )
                return call_llm_api(
                    model=selected_calibrate_model,
                    prompt=prompt,
                    api_key=self.api_key,
                    base_url=self.base_url,
                    max_retries=self.max_retries,
                    retry_delay=self.retry_delay,
                    reasoning_effort=selected_calibrate_effort,
                    task_type="calibrate_segment",
                )

            max_attempts = self.llm_config.get("segmentation", {}).get("length_retry_attempts", 3)
            calibrated_text = run_calibration(0)
            calibrated_text = self._enforce_segment_length(
                segment,
                calibrated_text,
                index,
                total_segments,
                retry_fn=run_calibration,
                max_attempts=max_attempts,
            )
            calibrated_segments[index] = calibrated_text
            logger.info(f"第 {index+1} 段校对完成（原始 {len(segment)} 字，校对 {len(calibrated_text)} 字）")
            return calibrated_text
        
        # 使用ThreadPoolExecutor进行并发处理
        max_workers = min(total_segments, self.concurrent_workers)  # 使用配置的并发数
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            # 提交所有任务
            futures = [
                executor.submit(calibrate_segment, i, segment) 
                for i, segment in enumerate(segments)
            ]
            
            # 等待所有任务完成
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()  # 获取结果，如果有异常会抛出
                except Exception as e:
                    logger.error(f"并发校对过程中出现错误: {e}")
        
        # 合并所有段落（按原始顺序）
        final_result = self.segmentation_processor.merge_txt_segments(calibrated_segments)
        
        logger.info(f"TXT并发分段校对完成，最终长度: {len(final_result)} 字符")
        return final_result
    
    def _calibrate_json_segmented(
        self,
        file_path: str,
        title: str,
        description: str,
        speaker_mapping: Optional[Dict[str, str]] = None,
        selected_calibrate_model: str = None,
        selected_calibrate_effort: str = None,
    ) -> str:
        """
        对JSON文件进行并发分段校对

        Args:
            file_path: JSON文件路径
            title: 视频标题
            description: 视频描述
            speaker_mapping: 说话人映射（可选）
            selected_calibrate_model: 选定的校对模型
            selected_calibrate_effort: 选定的校对 reasoning_effort

        Returns:
            校对后的完整文本
        """
        # 如果未指定模型，使用默认配置
        if selected_calibrate_model is None:
            selected_calibrate_model = self.calibrate_model
        if selected_calibrate_effort is None:
            selected_calibrate_effort = self.calibrate_reasoning_effort
        import concurrent.futures
        
        # 首先生成说话人映射
        if speaker_mapping is None:
            logger.info("生成全局说话人映射")
            speaker_mapping = self.segmentation_processor.extract_speaker_mapping_from_json(
                file_path, title, description
            )
        
        # 应用说话人映射并分段
        segments = self.segmentation_processor.segment_json_content(file_path, speaker_mapping)
        total_segments = len(segments)
        logger.info(f"开始并发分段校对，共 {total_segments} 个段落")
        
        # 使用线程池进行并发校对
        calibrated_segments = [None] * total_segments  # 保持原始顺序
        
        def calibrate_json_segment(index, segment_data):
            """校对单个JSON段落"""
            segment_text = self._json_segment_to_text(segment_data)
            text_length = len(segment_text)

            logger.info(f"开始校对第 {index+1}/{total_segments} 段 (长度: {text_length} 字符)")

            def run_calibration(retry_idx: int):
                prompt = self._generate_calibrate_prompt(
                    segment_text,
                    True,
                    title,
                    "",
                    description,
                    length_retry_level=retry_idx,
                )
                return call_llm_api(
                    model=selected_calibrate_model,
                    prompt=prompt,
                    api_key=self.api_key,
                    base_url=self.base_url,
                    max_retries=self.max_retries,
                    retry_delay=self.retry_delay,
                    reasoning_effort=selected_calibrate_effort,
                    task_type="calibrate_segment",
                )

            max_attempts = self.llm_config.get("segmentation", {}).get("length_retry_attempts", 3)
            calibrated_text = run_calibration(0)
            calibrated_text = self._enforce_segment_length(
                segment_text,
                calibrated_text,
                index,
                total_segments,
                retry_fn=run_calibration,
                max_attempts=max_attempts,
            )
            calibrated_segments[index] = calibrated_text
            logger.info(f"第 {index+1} 段校对完成（原始 {text_length} 字，校对 {len(calibrated_text)} 字）")
            return calibrated_text
        
        # 使用ThreadPoolExecutor进行并发处理
        max_workers = min(total_segments, self.concurrent_workers)  # 使用配置的并发数
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            # 提交所有任务
            futures = [
                executor.submit(calibrate_json_segment, i, segment_data) 
                for i, segment_data in enumerate(segments)
            ]
            
            # 等待所有任务完成
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()  # 获取结果，如果有异常会抛出
                except Exception as e:
                    logger.error(f"并发校对过程中出现错误: {e}")
        
        # 合并所有段落（按原始顺序）
        final_result = self.segmentation_processor.merge_json_segments(calibrated_segments)
        
        logger.info(f"JSON并发分段校对完成，最终长度: {len(final_result)} 字符")
        return final_result
    
    def _json_segment_to_text(self, segment_data: Dict[str, Any]) -> str:
        """
        将JSON段落数据转换为文本
        
        Args:
            segment_data: JSON段落数据
            
        Returns:
            格式化的文本
        """
        segments = segment_data.get('segments', [])
        text_parts = []
        
        for segment in segments:
            speaker = segment.get('speaker', '')
            text = segment.get('text', '')
            
            if speaker and text:
                text_parts.append(f"{speaker}：{text}")
            elif text:
                text_parts.append(text)
        
        return "\n\n".join(text_parts)
    
    def _generate_calibrate_prompt(
        self,
        text: str,
        use_speaker_recognition: bool = False,
        title: str = "",
        author: str = "",
        description: str = "",
        length_retry_level: int = 0,
    ) -> str:
        """
        生成校对提示词（与原始server.py保持一致）
        
        Args:
            text: 需要校对的文本
            use_speaker_recognition: 是否使用说话人识别
            title: 视频标题
            author: 作者信息
            description: 视频描述
            
        Returns:
            校对提示词
        """
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
        if title or author or description:
            context_info = "\n以下是视频的辅助信息，可以帮助你更准确地校对文本中的专有名词和拼写错误：\n"
            if title:
                context_info += f"- 视频标题：{title}\n"
            if author:
                context_info += f"- 作者/频道：{author}\n"
            if description:
                context_info += f"- 视频描述：{description[:500]}{'...' if len(description) > 500 else ''}\n"
            context_info += "\n"
        
        min_ratio = self.llm_config.get("segmentation", {}).get(
            "min_segment_ratio", self.llm_config.get("min_calibrate_ratio", 0.80)
        )

        length_requirement = (
            f"⚠️ **绝对限制：不得删减内容，校对后的文本长度必须保持在原文的 {int(min_ratio * 100)}% 以上**。"
        )

        if length_retry_level > 0:
            length_requirement += (
                f"\n‼️ 第 {length_retry_level + 1} 次尝试：上一次校对结果长度不足，请严格按照原文篇幅输出，"
                "必要时完整保留原文内容，只修正标点和错别字。"
            )

        calibrate_prompt = (
            "你将收到一段音频的转录文本。你的任务是对这段文本进行校对,提高其可读性,但**保持原文长度和信息完整性**。 "
            + context_info
            + length_requirement
            + "\n请按照以下指示进行校对: "
            "1. **适当分段,使文本结构更清晰**。每当话题转换、时间跳跃或逻辑转折时应该分段。每个自然段落应该是一个完整的思想单元。 "
            "2. **不要删减或概括内容**，保留所有原始信息和细节。分段不等于删减,只是在合适的地方添加换行。 "
            "3. 修正明显的错别字和语法错误。特别注意根据上述辅助信息修正专有名词的拼写。 "
            "4. 调整标点符号的使用,确保其正确性和一致性。 "
            "5. 适当合并短句，使文本更流畅，但**不要改变原意或删除信息**。 "
            "6. 保留原文中的口语化表达和说话者的语气特点。 "
            "7. **禁止添加或删除任何实质性内容**。 "
            "8. 不要解释或评论文本内容。 "
            + speaker_prompt +
            "**重要**：\n"
            "- 必须适当分段以提高可读性,在话题转换、逻辑转折或时间跳跃处换行。\n"
            "- 只进行文字校对、标点调整和分段，不要进行内容压缩或概括。\n"
            "- 校对后的文本长度必须保持在原文的95%以上，除修正明显错误外禁止缩短篇幅。\n"
            "只返回校对后的文本,不要包含任何其他解释或评论。 "
            "以下是需要校对的转录文本: <transcript>  " + text + "  </transcript>"
        )
        
        return calibrate_prompt

    def _enforce_segment_length(
        self,
        original: str,
        calibrated: str,
        index: int,
        total_segments: int,
        retry_fn=None,
        max_attempts: int = 1,
    ) -> str:
        """确保单个分段校对结果不短于原文阈值，必要时重试"""
        if not original:
            return calibrated

        min_ratio = self.llm_config.get("segmentation", {}).get(
            "min_segment_ratio", self.llm_config.get("min_calibrate_ratio", 0.80)
        )
        min_length = int(len(original) * min_ratio)
        attempt = 1

        while True:
            calibrated_length = len(calibrated or "")
            ratio = (calibrated_length / len(original)) if original else 0
            if calibrated_length >= min_length:
                logger.info(
                    f"第 {index + 1}/{total_segments} 段校对长度满足要求：原始 {len(original)} 字，校对 {calibrated_length} 字，"
                    f"占比 {ratio * 100:.2f}%（第 {attempt} 次尝试）"
                )
                return calibrated

            if not retry_fn or attempt >= max_attempts:
                logger.warning(
                    f"第 {index + 1}/{total_segments} 段校对后长度 {ratio * 100:.2f}% 小于阈值 {min_ratio * 100:.2f}%，"
                    f"原始 {len(original)} 字，校对 {calibrated_length} 字，重试次数 {attempt}/{max_attempts}，回退原段"
                )
                return original

            logger.warning(
                f"第 {index + 1}/{total_segments} 段校对后长度 {ratio * 100:.2f}% 小于阈值 {min_ratio * 100:.2f}%，"
                f"准备进行第 {attempt + 1}/{max_attempts} 次重试"
            )
            attempt += 1
            calibrated = retry_fn(attempt - 1)
    
    def summarize_text_segmented(self, text_for_summary: str, title: str = "", description: str = "", selected_summary_model: str = None, selected_reasoning_effort: str = None) -> str:
        """
        对文本进行单次总结（不分段，文本可以是原始或校对结果）

        Args:
            text_for_summary: 用于总结的文本
            title: 视频标题
            description: 视频描述
            selected_summary_model: 选定的总结模型（如果为None则使用默认模型）
            selected_reasoning_effort: 选定的 reasoning_effort（如果为None则使用默认值）

        Returns:
            总结文本
        """
        logger.info(f"开始文本总结，长度: {len(text_for_summary)} 字符")

        # 如果未指定模型，使用默认模型
        if selected_summary_model is None:
            selected_summary_model = self.summary_model

        # 如果未指定 reasoning_effort，使用默认值
        if selected_reasoning_effort is None:
            selected_reasoning_effort = self.summary_reasoning_effort

        # 不再分段，直接对全文进行总结，让LLM有全局理解
        return self._summarize_single_text(text_for_summary, title, description, selected_summary_model, selected_reasoning_effort)
    
    def _summarize_single_text(self, text: str, title: str, description: str, selected_summary_model: str, selected_reasoning_effort: str) -> str:
        """
        对单个文本进行总结

        Args:
            text: 文本内容
            title: 视频标题
            description: 视频描述
            selected_summary_model: 选定的总结模型
            selected_reasoning_effort: 选定的 reasoning_effort

        Returns:
            总结文本
        """
        # 检测说话人数量，决定总结策略
        import re
        speaker_pattern = r'Speaker\d+'
        unique_speakers = set(re.findall(speaker_pattern, text))
        speaker_count = len(unique_speakers) if unique_speakers else 1
        use_speaker_recognition = len(unique_speakers) > 0

        logger.info(f"检测到说话人数量: {speaker_count}，选择相应的总结策略")

        prompt = self._generate_summary_prompt(text, title, description, use_speaker_recognition, speaker_count)

        # 使用选定的总结模型和 reasoning_effort
        summary = call_llm_api(
            model=selected_summary_model,
            prompt=prompt,
            api_key=self.api_key,
            base_url=self.base_url,
            max_retries=self.max_retries,
            retry_delay=self.retry_delay,
            reasoning_effort=selected_reasoning_effort,
            task_type="summary"
        )

        logger.info("文本总结完成")
        return summary
    
    def _summarize_segmented_text(self, text: str, title: str, description: str) -> str:
        """
        对超长文本进行分段总结
        
        Args:
            text: 文本内容
            title: 视频标题
            description: 视频描述
            
        Returns:
            总结文本
        """
        # 分段处理
        segments = self.segmentation_processor.segment_txt_content(text)
        segment_summaries = []
        
        total_segments = len(segments)
        logger.info(f"开始分段总结，共 {total_segments} 个段落")
        
        for i, segment in enumerate(segments):
            logger.info(f"正在总结第 {i+1}/{total_segments} 段")
            
            # 对每个段落进行总结
            prompt = self._generate_segment_summary_prompt(segment, i+1, total_segments)

            segment_summary = call_llm_api(
                model=self.summary_model,
                prompt=prompt,
                api_key=self.api_key,
                base_url=self.base_url,
                max_retries=self.max_retries,
                retry_delay=self.retry_delay,
                reasoning_effort=self.summary_reasoning_effort,
                task_type="segment_summary"
            )
            
            segment_summaries.append(segment_summary)
            logger.info(f"第 {i+1} 段总结完成")
        
        # 将各段总结合并为最终总结
        combined_summaries = "\n\n".join(segment_summaries)
        final_summary_prompt = self._generate_final_summary_prompt(combined_summaries, title, description)
        
        logger.info("开始生成最终总结")
        final_summary = call_llm_api(
            model=self.summary_model,
            prompt=final_summary_prompt,
            api_key=self.api_key,
            base_url=self.base_url,
            max_retries=self.max_retries,
            retry_delay=self.retry_delay,
            reasoning_effort=self.summary_reasoning_effort,
            task_type="final_summary"
        )
        
        logger.info("分段总结完成")
        return final_summary
    
    def _generate_summary_prompt(self, text: str, title: str, description: str, 
                               use_speaker_recognition: bool = False, speaker_count: int = 1) -> str:
        """生成总结提示词（与原始server.py保持一致）"""
        # 构建辅助信息
        context_info = ""
        if title or description:
            context_info = "\n以下是视频的辅助信息：\n"
            if title:
                context_info += f"- 视频标题：{title}\n"
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
                "\n对话内容：\n" + text
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
                "\n转录文本：\n" + text
            )
        
        return summary_prompt
    
    def _generate_segment_summary_prompt(self, segment: str, segment_num: int, total_segments: int) -> str:
        """生成段落总结提示词"""
        prompt = f"""请总结以下内容片段的要点（这是第{segment_num}段，共{total_segments}段）：

{segment}

要求：
1. 提取本段的核心要点（2-3句话）
2. 保留重要的细节信息
3. 如果是对话，注明主要讨论的话题

本段要点："""
        
        return prompt
    
    def _generate_final_summary_prompt(self, combined_summaries: str, title: str, description: str) -> str:
        """生成最终总结提示词"""
        context_info = ""
        if title:
            context_info += f"标题：{title}\n"
        if description:
            context_info += f"描述：{description}\n"
        
        prompt = f"""以下是对长内容各个部分的分段总结，请基于这些总结生成一个完整的、结构化的最终摘要：

{context_info}
分段总结：
{combined_summaries}

请按以下格式生成最终总结：

## 内容摘要
[用2-3段话概括整个内容的主要内容]

## 主要观点
[列出3-5个核心观点，每个观点用一句话概括]

## 重要信息
[提取关键信息、数据、结论等]

## 讨论话题
[如果是对话或讨论，列出主要话题]

最终总结："""
        
        return prompt
