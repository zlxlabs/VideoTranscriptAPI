"""
结构化校对处理器
专门处理带说话人识别的转录数据校对
"""
import json
import asyncio
import threading
from typing import Dict, List, Any, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor
import concurrent.futures

from ..logging import setup_logger
from .llm import call_llm_api, StructuredResult
from .schemas import CALIBRATION_RESULT_SCHEMA, VALIDATION_RESULT_SCHEMA

logger = setup_logger(__name__)


class StructuredCalibrator:
    """结构化校对处理器"""
    
    def __init__(self, config: Dict[str, Any]):
        """
        初始化结构化校对处理器

        Args:
            config: 配置字典
        """
        self.config = config
        self.llm_config = config.get('llm', {})
        self.calibration_config = self.llm_config.get('structured_calibration', {})

        # LLM API 配置
        self.api_key = self.llm_config['api_key']
        self.base_url = self.llm_config['base_url']
        self.calibrate_model = self.llm_config['calibrate_model']
        self.calibrate_reasoning_effort = self.llm_config.get('calibrate_reasoning_effort', None)
        self.validator_model = self.calibration_config.get('validator_model', self.calibrate_model)
        self.validator_reasoning_effort = self.calibration_config.get('validator_reasoning_effort', None)
        # 风险校验模型配置
        self.risk_validator_model = self.calibration_config.get('risk_validator_model')
        self.risk_validator_reasoning_effort = self.calibration_config.get('risk_validator_reasoning_effort', None)
        self.max_retries = self.llm_config['max_retries']
        self.retry_delay = self.llm_config['retry_delay']
        
        # 校对配置
        self.min_chunk_length = self.calibration_config.get('min_chunk_length', 300)
        self.max_chunk_length = self.calibration_config.get('max_chunk_length', 1500)
        self.preferred_chunk_length = self.calibration_config.get('preferred_chunk_length', 800)
        self.max_calibration_retries = self.calibration_config.get('max_calibration_retries', 2)
        self.calibration_concurrent_limit = self.calibration_config.get('calibration_concurrent_limit', 3)
        self.enable_validation = self.calibration_config.get('enable_validation', True)
        self.fallback_to_original = self.calibration_config.get('fallback_to_original', True)
        
        # 质量阈值
        quality_config = self.calibration_config.get('quality_threshold', {})
        self.overall_score_threshold = quality_config.get('overall_score', 8.0)
        self.minimum_single_score = quality_config.get('minimum_single_score', 7.0)
        
        logger.info(f"结构化校对器初始化完成，配置: chunk长度[{self.min_chunk_length}-{self.max_chunk_length}], 并发限制: {self.calibration_concurrent_limit}")
    
    def calibrate_structured_dialogs(
        self, dialogs_with_time: List[Dict[str, Any]], video_metadata: Dict[str, str],
        selected_calibrate_model: str = None, selected_calibrate_effort: str = None,
        selected_validator_model: str = None, selected_validator_effort: str = None
    ) -> List[Dict[str, Any]]:
        """
        校对结构化对话数据

        Args:
            dialogs_with_time: 包含时间信息的对话列表
            video_metadata: 视频元数据
            selected_calibrate_model: 选定的校对模型（可选，默认使用配置的模型）
            selected_calibrate_effort: 选定的校对 reasoning_effort（可选）
            selected_validator_model: 选定的校验模型（可选，默认使用配置的模型）
            selected_validator_effort: 选定的校验 reasoning_effort（可选）

        Returns:
            List[Dict]: 校对后的对话数据
        """
        # 如果指定了模型，临时覆盖实例变量（用于本次调用）
        original_calibrate_model = self.calibrate_model
        original_calibrate_effort = self.calibrate_reasoning_effort
        original_validator_model = self.validator_model
        original_validator_effort = self.validator_reasoning_effort

        if selected_calibrate_model is not None:
            self.calibrate_model = selected_calibrate_model
            logger.info(f"使用指定的校对模型: {selected_calibrate_model}")
        if selected_calibrate_effort is not None:
            self.calibrate_reasoning_effort = selected_calibrate_effort
        if selected_validator_model is not None:
            self.validator_model = selected_validator_model
            logger.info(f"使用指定的校验模型: {selected_validator_model}")
        if selected_validator_effort is not None:
            self.validator_reasoning_effort = selected_validator_effort

        logger.info(f"开始结构化校对，对话数量: {len(dialogs_with_time)}, 模型: {self.calibrate_model}, 校验模型: {self.validator_model}")

        try:
            # 1. 智能分块
            chunks = self._intelligent_chunking(dialogs_with_time)
            logger.info(f"分块完成，共 {len(chunks)} 个chunk")

            # 2. 并发校对处理
            calibrated_chunks = self._process_chunks_concurrent(chunks, video_metadata)

            # 3. 合并结果
            calibrated_dialogs = self._merge_calibrated_chunks(calibrated_chunks)

            logger.info(f"结构化校对完成，输出对话数量: {len(calibrated_dialogs)}")
            return calibrated_dialogs

        except Exception as e:
            logger.error(f"结构化校对失败: {e}")
            # 降级处理：返回原始数据
            if self.fallback_to_original:
                logger.warning("降级使用原始对话数据")
                return dialogs_with_time
            else:
                raise
        finally:
            # 恢复原始配置
            self.calibrate_model = original_calibrate_model
            self.calibrate_reasoning_effort = original_calibrate_effort
            self.validator_model = original_validator_model
            self.validator_reasoning_effort = original_validator_effort
    
    def _intelligent_chunking(self, dialogs: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        """
        智能分块策略
        
        Args:
            dialogs: 对话列表
            
        Returns:
            List[List[Dict]]: 分块后的对话组
        """
        chunks = []
        current_chunk = []
        current_length = 0
        
        for dialog in dialogs:
            dialog_length = len(dialog.get('text', ''))
            
            # 策略1：单个对话过长，需要拆分
            if dialog_length > self.max_chunk_length:
                # 保存当前chunk
                if current_chunk:
                    chunks.append(current_chunk)
                    current_chunk = []
                    current_length = 0
                
                # 拆分长对话
                sub_dialogs = self._split_long_dialog(dialog)
                for sub_dialog in sub_dialogs:
                    chunks.append([sub_dialog])
                continue
            
            # 策略2：加入当前对话会超长
            if current_length + dialog_length > self.max_chunk_length:
                if current_chunk:  # 当前chunk不为空
                    chunks.append(current_chunk)
                current_chunk = [dialog]
                current_length = dialog_length
            else:
                current_chunk.append(dialog)
                current_length += dialog_length
                
                # 策略3：达到理想长度，可以结束当前chunk
                if current_length >= self.preferred_chunk_length:
                    chunks.append(current_chunk)
                    current_chunk = []
                    current_length = 0
        
        # 处理剩余的chunk
        if current_chunk:
            # 如果最后一个chunk太短，考虑与前一个合并
            if len(chunks) > 0 and current_length < self.min_chunk_length:
                chunks[-1].extend(current_chunk)
            else:
                chunks.append(current_chunk)
        
        # 过滤空chunk
        chunks = [chunk for chunk in chunks if chunk]
        
        logger.debug(f"分块结果: {len(chunks)} 个chunk，长度分布: {[sum(len(d.get('text', '')) for d in chunk) for chunk in chunks]}")
        return chunks
    
    def _split_long_dialog(self, dialog: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        拆分过长的单个对话
        
        Args:
            dialog: 单个对话
            
        Returns:
            List[Dict]: 拆分后的对话片段
        """
        text = dialog.get('text', '')
        if len(text) <= self.max_chunk_length:
            return [dialog]
        
        # 按句子分割
        sentences = self._split_by_sentences(text)
        sub_dialogs = []
        current_text = ""
        
        for sentence in sentences:
            if len(current_text + sentence) > self.max_chunk_length and current_text:
                # 创建子对话
                sub_dialog = dialog.copy()
                sub_dialog['text'] = current_text.strip()
                sub_dialogs.append(sub_dialog)
                current_text = sentence
            else:
                current_text += sentence
        
        # 处理剩余文本
        if current_text.strip():
            sub_dialog = dialog.copy()
            sub_dialog['text'] = current_text.strip()
            sub_dialogs.append(sub_dialog)
        
        logger.debug(f"长对话拆分: 原长度{len(text)} -> {len(sub_dialogs)}个片段")
        return sub_dialogs
    
    def _split_by_sentences(self, text: str) -> List[str]:
        """按句子分割文本"""
        import re
        # 按中文句号、问号、感叹号分割，保留标点
        sentences = re.split(r'([。！？])', text)
        
        # 重组句子（将标点符号合并回前一个句子）
        result = []
        for i in range(0, len(sentences), 2):
            sentence = sentences[i]
            if i + 1 < len(sentences):
                sentence += sentences[i + 1]
            if sentence.strip():
                result.append(sentence)
        
        return result
    
    def _process_chunks_concurrent(self, chunks: List[List[Dict[str, Any]]], video_metadata: Dict[str, str]) -> List[List[Dict[str, Any]]]:
        """
        并发处理chunks
        
        Args:
            chunks: 分块数据
            video_metadata: 视频元数据
            
        Returns:
            List[List[Dict]]: 校对后的chunks
        """
        calibrated_chunks = [None] * len(chunks)  # 保持顺序
        
        def process_single_chunk(index: int, chunk: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            """处理单个chunk"""
            logger.info(f"开始处理第 {index+1}/{len(chunks)} 个chunk，包含 {len(chunk)} 个对话")
            
            for attempt in range(self.max_calibration_retries + 1):
                try:
                    # 校对
                    calibrated_result = self._calibrate_chunk(chunk, video_metadata)
                    
                    # 验证
                    if self.enable_validation:
                        validation_result = self._validate_calibration(chunk, calibrated_result, video_metadata)
                        
                        if validation_result['pass']:
                            calibrated_chunks[index] = calibrated_result
                            logger.info(f"第 {index+1} 个chunk校对成功，质量分数: {validation_result['overall_score']}")
                            return calibrated_result
                        else:
                            logger.warning(f"第 {index+1} 个chunk校对质量不合格，第{attempt+1}次重试: {validation_result['issues']}")
                            if attempt == self.max_calibration_retries:
                                if self.fallback_to_original:
                                    logger.error(f"第 {index+1} 个chunk校对失败，使用原始数据")
                                    calibrated_chunks[index] = self._format_as_calibrated(chunk)
                                    return chunk
                                else:
                                    raise Exception(f"校对质量验证失败: {validation_result['issues']}")
                    else:
                        # 不启用验证，直接返回校对结果
                        calibrated_chunks[index] = calibrated_result
                        logger.info(f"第 {index+1} 个chunk校对完成（未验证）")
                        return calibrated_result
                
                except Exception as e:
                    logger.error(f"第 {index+1} 个chunk校对异常，第{attempt+1}次重试: {e}")
                    if attempt == self.max_calibration_retries:
                        if self.fallback_to_original:
                            logger.error(f"第 {index+1} 个chunk校对异常，使用原始数据: {e}")
                            calibrated_chunks[index] = self._format_as_calibrated(chunk)
                            return chunk
                        else:
                            raise
        
        # 使用ThreadPoolExecutor进行并发处理
        max_workers = min(len(chunks), self.calibration_concurrent_limit)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # 提交所有任务
            futures = [
                executor.submit(process_single_chunk, i, chunk) 
                for i, chunk in enumerate(chunks)
            ]
            
            # 等待所有任务完成
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()  # 获取结果，如果有异常会抛出
                except Exception as e:
                    logger.error(f"并发校对过程中出现错误: {e}")
        
        return calibrated_chunks
    
    def _calibrate_chunk(self, chunk: List[Dict[str, Any]], video_metadata: Dict[str, str]) -> List[Dict[str, Any]]:
        """
        校对单个chunk
        
        Args:
            chunk: 对话chunk
            video_metadata: 视频元数据
            
        Returns:
            List[Dict]: 校对后的对话
        """
        # 构建输入数据
        input_data = {
            "dialogs": [
                {
                    "start_time": dialog.get('start_time', '00:00:00'),
                    "speaker": dialog.get('speaker', 'unknown'),
                    "text": dialog.get('text', '')
                }
                for dialog in chunk
            ]
        }
        
        # 生成校对prompt
        prompt = self._generate_calibration_prompt(input_data, video_metadata)

        # 保存prompt到文件进行分析
        # self._save_calibration_prompt_to_file(prompt, len(chunk))

        # 调用LLM（使用结构化输出）
        result: StructuredResult = call_llm_api(
            model=self.calibrate_model,
            prompt=prompt,
            api_key=self.api_key,
            base_url=self.base_url,
            max_retries=self.max_retries,
            retry_delay=self.retry_delay,
            reasoning_effort=self.calibrate_reasoning_effort,
            task_type="calibrate_chunk",
            response_schema=CALIBRATION_RESULT_SCHEMA
        )

        # 处理结构化输出结果
        if not result.success:
            raise Exception(f"Calibration failed: {result.error}")

        calibrated_data = result.data
        
        # 将结果转换为内部格式
        calibrated_dialogs = []
        calibrated_data_list = calibrated_data.get('calibrated_dialogs', [])
        
        # 处理对话数量不匹配的情况
        if len(calibrated_data_list) != len(chunk):
            logger.warning(f"LLM输出对话数量({len(calibrated_data_list)})与原始数量({len(chunk)})不匹配")
        
        for i, dialog_data in enumerate(calibrated_data_list):
            # 尝试找到对应的原始对话
            original_dialog = self._find_matching_original_dialog(dialog_data, chunk, i)
            
            calibrated_dialog = {
                'start_time': dialog_data.get('start_time', original_dialog.get('start_time', '00:00:00')),
                'end_time': original_dialog.get('end_time', '00:00:00'),
                'duration': original_dialog.get('duration', 0),
                'speaker': dialog_data.get('speaker', original_dialog.get('speaker', 'unknown')),
                'text': dialog_data.get('text', original_dialog.get('text', '')),
                'original_text': original_dialog.get('text', '')  # 保留原始文本
            }
            calibrated_dialogs.append(calibrated_dialog)
        
        return calibrated_dialogs
    
    def _find_matching_original_dialog(self, calibrated_dialog: Dict[str, Any], original_chunk: List[Dict[str, Any]], index: int) -> Dict[str, Any]:
        """找到与校对对话匹配的原始对话"""
        # 首先尝试按索引匹配
        if index < len(original_chunk):
            return original_chunk[index]
        
        # 如果索引超出范围，尝试按说话人和文本相似性匹配
        calibrated_speaker = calibrated_dialog.get('speaker', '')
        calibrated_text = calibrated_dialog.get('text', '')
        
        best_match = {}
        best_score = 0
        
        for orig_dialog in original_chunk:
            orig_speaker = orig_dialog.get('speaker', '')
            orig_text = orig_dialog.get('text', '')
            
            # 计算匹配分数
            score = 0
            if orig_speaker == calibrated_speaker:
                score += 2
            
            # 简单的文本相似性
            if orig_text and calibrated_text:
                common_chars = len(set(orig_text) & set(calibrated_text))
                total_chars = len(set(orig_text) | set(calibrated_text))
                if total_chars > 0:
                    score += common_chars / total_chars
            
            if score > best_score:
                best_score = score
                best_match = orig_dialog
        
        return best_match if best_match else {}
    
    def _generate_calibration_prompt(self, input_data: Dict[str, Any], video_metadata: Dict[str, str]) -> str:
        """
        生成校对prompt
        
        Args:
            input_data: 输入的对话数据
            video_metadata: 视频元数据
            
        Returns:
            str: 校对prompt
        """
        # 构建辅助信息（复用原有逻辑）
        context_info = ""
        video_title = video_metadata.get('video_title', '')
        author = video_metadata.get('author', '')
        description = video_metadata.get('description', '')
        
        if video_title or author or description:
            context_info = "\n以下是视频的辅助信息，可以帮助你更准确地校对文本中的专有名词和拼写错误：\n"
            if video_title:
                context_info += f"- 视频标题：{video_title}\n"
            if author:
                context_info += f"- 作者/频道：{author}\n"
            if description:
                context_info += f"- 视频描述：{description[:500]}{'...' if len(description) > 500 else ''}\n"
            context_info += "\n"
        
        input_dialog_count = len(input_data.get('dialogs', []))
        
        prompt = f"""你将收到一段音频的转录文本JSON数据。你的任务是对这段文本进行校对，提高其可读性，但不改变原意。

{context_info}**核心要求（必须遵守）：**
- **对话数量必须保持不变：输入有{input_dialog_count}个对话，输出也必须有{input_dialog_count}个对话**
- **禁止合并、拆分或增删对话**
- **每个对话的说话人和时间信息必须保持不变**

请按照以下指示进行校对:
1. **只能在单个对话内部进行修改**，不得跨对话操作
2. 修正明显的错别字和语法错误
3. 调整标点符号的使用，确保其正确性和一致性
4. 如有必要，可以轻微调整词序以提高可读性
5. 保留原文中的口语化表达和说话者的语气特点
6. 不要添加或删除任何实质性内容
7. 不要解释或评论文本内容

输入格式示例：
```json
{{
  "dialogs": [
    {{
      "start_time": "00:01:23",
      "speaker": "知白",
      "text": "那个呃今天我们来聊一下产品设计呃我觉得这个很重要的"
    }},
    {{
      "start_time": "00:01:45", 
      "speaker": "少楠",
      "text": "对对对我也这么认为呃我觉得用户体验是最核心的"
    }}
  ]
}}
```

输出格式要求（必须严格遵守）：
- **必须输出恰好{input_dialog_count}个对话**
- **每个对话的start_time和speaker必须与原始数据一致**
```json
{{
  "calibrated_dialogs": [
    {{
      "start_time": "00:01:23",
      "speaker": "知白", 
      "text": "今天我们来聊一下产品设计，我觉得这个很重要。"
    }},
    {{
      "start_time": "00:01:45",
      "speaker": "少楠",
      "text": "对，我也这么认为。我觉得用户体验是最核心的。"
    }}
  ]
}}
```

待校对的JSON数据：
{json.dumps(input_data, ensure_ascii=False, indent=2)}

只返回校对后的JSON，不要包含任何其他解释或评论。"""
        
        return prompt
    
    def _validate_calibration(self, original_chunk: List[Dict[str, Any]], calibrated_chunk: List[Dict[str, Any]], video_metadata: Dict[str, str]) -> Dict[str, Any]:
        """
        验证校对质量

        Args:
            original_chunk: 原始chunk
            calibrated_chunk: 校对后的chunk
            video_metadata: 视频元数据（用于提供上下文信息）

        Returns:
            Dict: 验证结果
        """
        try:
            # 构建验证数据
            original_data = {
                "dialogs": [
                    {
                        "start_time": dialog.get('start_time', '00:00:00'),
                        "speaker": dialog.get('speaker', 'unknown'),
                        "text": dialog.get('text', '')
                    }
                    for dialog in original_chunk
                ]
            }
            
            calibrated_data = {
                "dialogs": [
                    {
                        "start_time": dialog.get('start_time', '00:00:00'),
                        "speaker": dialog.get('speaker', 'unknown'),
                        "text": dialog.get('text', '')
                    }
                    for dialog in calibrated_chunk
                ]
            }
            
            # 生成验证prompt
            prompt = self._generate_validation_prompt(original_data, calibrated_data, video_metadata)

            # 调用LLM验证（使用结构化输出）
            result: StructuredResult = call_llm_api(
                model=self.validator_model,
                prompt=prompt,
                api_key=self.api_key,
                base_url=self.base_url,
                max_retries=self.max_retries,
                retry_delay=self.retry_delay,
                reasoning_effort=self.validator_reasoning_effort,
                task_type="validate",
                response_schema=VALIDATION_RESULT_SCHEMA
            )

            # 处理结构化输出结果
            if not result.success:
                logger.warning(f"Validation structured output failed: {result.error}")
                # 验证失败时返回默认通过结果
                return {
                    'overall_score': self.overall_score_threshold,
                    'pass': True,
                    'issues': [f"Validation failed: {result.error}"],
                    'recommendation': 'Validation failed, assumed pass'
                }

            validation_result = result.data
            
            # 检查是否通过验证
            overall_score = validation_result.get('overall_score', 0)
            scores = validation_result.get('scores', {})
            
            # 检查总分和单项分数
            pass_overall = overall_score >= self.overall_score_threshold
            pass_individual = all(score >= self.minimum_single_score for score in scores.values())
            
            validation_result['pass'] = pass_overall and pass_individual
            
            return validation_result
            
        except Exception as e:
            logger.error(f"校对验证失败: {e}")
            # 验证失败时，假设通过（避免阻塞流程）
            return {
                'overall_score': self.overall_score_threshold,
                'pass': True,
                'issues': [f"验证过程异常: {e}"],
                'recommendation': '验证异常，假设通过'
            }
    
    def _generate_validation_prompt(self, original_data: Dict[str, Any], calibrated_data: Dict[str, Any], video_metadata: Dict[str, str]) -> str:
        """生成验证prompt"""
        # 构建辅助信息（与校对prompt保持一致）
        context_info = ""
        video_title = video_metadata.get('video_title', '')
        author = video_metadata.get('author', '')
        description = video_metadata.get('description', '')

        if video_title or author or description:
            context_info = "\n以下是视频的辅助信息，可以帮助你更准确地评估校对质量（特别是专有名词、人名等）：\n"
            if video_title:
                context_info += f"- 视频标题：{video_title}\n"
            if author:
                context_info += f"- 作者/频道：{author}\n"
            if description:
                context_info += f"- 视频描述：{description[:500]}{'...' if len(description) > 500 else ''}\n"
            context_info += "\n"

        prompt = f"""你是一个专业的文本校对质量评估专家。请评估以下校对结果的质量。

{context_info}原始文本：
{json.dumps(original_data, ensure_ascii=False, indent=2)}

校对后文本：
{json.dumps(calibrated_data, ensure_ascii=False, indent=2)}

请从以下维度评估校对质量（每项0-10分）：

1. 格式正确性：JSON格式是否正确，字段是否完整
2. 内容保真度：是否保持了原始内容的意思，没有添加或删除实质信息。**注意：结合视频辅助信息，某些专有名词、人名的修正是合理的**
3. 文本质量：错别字、语法、标点是否得到改善
4. 说话人一致性：说话人标识是否保持不变
5. 时间信息一致性：时间戳是否保持不变

请按以下JSON格式返回评估结果：
```json
{{
  "overall_score": 8.5,
  "scores": {{
    "format_correctness": 10,
    "content_fidelity": 9,
    "text_quality": 8,
    "speaker_consistency": 10,
    "time_consistency": 10
  }},
  "pass": true,
  "issues": ["发现轻微的标点问题"],
  "recommendation": "建议重新校对标点符号部分"
}}
```

评估标准：
- overall_score >= {self.overall_score_threshold} 且所有单项 >= {self.minimum_single_score} 才算通过
- 格式错误直接不通过
- 内容增删超过10%不通过
- 说话人或时间信息改变直接不通过
- **参考视频辅助信息评估专有名词修正的合理性**"""

        return prompt

    def _format_as_calibrated(self, chunk: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """将原始chunk格式化为校对格式"""
        return [
            {
                'start_time': dialog.get('start_time', '00:00:00'),
                'end_time': dialog.get('end_time', '00:00:00'),
                'duration': dialog.get('duration', 0),
                'speaker': dialog.get('speaker', 'unknown'),
                'text': dialog.get('text', ''),
                'original_text': dialog.get('text', '')
            }
            for dialog in chunk
        ]
    
    def _merge_calibrated_chunks(self, calibrated_chunks: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        """合并校对后的chunks"""
        merged_dialogs = []
        for chunk in calibrated_chunks:
            if chunk:
                merged_dialogs.extend(chunk)
        return merged_dialogs
    
    @staticmethod
    def seconds_to_timestamp(seconds: float) -> str:
        """将秒数转换为 HH:MM:SS 格式"""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    
    @staticmethod
    def extract_time_enhanced_dialogs_from_funasr(funasr_data: Dict, speaker_mapping: Dict[str, str]) -> List[Dict[str, Any]]:
        """
        从FunASR数据提取带时间信息的对话
        
        Args:
            funasr_data: FunASR数据
            speaker_mapping: 说话人映射关系
            
        Returns:
            List[Dict]: 带时间信息的对话列表
        """
        time_enhanced_dialogs = []
        
        # 提取转录段落
        segments = []
        if isinstance(funasr_data, list):
            segments = funasr_data
        elif isinstance(funasr_data, dict):
            for key in ['segments', 'result', 'data']:
                if key in funasr_data:
                    segments = funasr_data[key]
                    break
        
        # 重构对话，合并连续同说话人
        current_speaker = None
        current_content = []
        current_start_time = None
        current_end_time = None
        
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
            
            # 提取时间信息
            start_time = segment.get('start_time', segment.get('start', 0))
            end_time = segment.get('end_time', segment.get('end', start_time))
            
            if not original_speaker or not text_content:
                continue
            
            # 映射到实际人名
            actual_speaker = speaker_mapping.get(original_speaker, original_speaker)
            
            # 合并连续同一说话人的内容
            if current_speaker == actual_speaker:
                current_content.append(text_content)
                current_end_time = end_time  # 更新结束时间
            else:
                # 保存前一个说话人的内容
                if current_speaker and current_content:
                    time_enhanced_dialogs.append({
                        'start_time': StructuredCalibrator.seconds_to_timestamp(current_start_time),
                        'end_time': StructuredCalibrator.seconds_to_timestamp(current_end_time),
                        'duration': current_end_time - current_start_time,
                        'speaker': current_speaker,
                        'text': ' '.join(current_content)
                    })
                
                # 开始新的说话人
                current_speaker = actual_speaker
                current_content = [text_content]
                current_start_time = start_time
                current_end_time = end_time
        
        # 保存最后一个说话人的内容
        if current_speaker and current_content:
            time_enhanced_dialogs.append({
                'start_time': StructuredCalibrator.seconds_to_timestamp(current_start_time),
                'end_time': StructuredCalibrator.seconds_to_timestamp(current_end_time),
                'duration': current_end_time - current_start_time,
                'speaker': current_speaker,
                'text': ' '.join(current_content)
            })
        
        logger.info(f"从 FunASR数据提取 {len(time_enhanced_dialogs)} 个时间增强对话")
        return time_enhanced_dialogs
    
    def _save_calibration_prompt_to_file(self, prompt: str, chunk_size: int) -> None:
        """保存校对prompt到文件进行分析"""
        import os
        from datetime import datetime
        from video_transcript_api.utils import get_llm_debug_dir

        # 从配置获取调试目录
        debug_dir = get_llm_debug_dir()

        # 添加时间戳和大小信息
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:17]  # 包含微秒避免重名
        filename = f"{timestamp}_calibration_prompt_chunk{chunk_size}.txt"
        file_path = os.path.join(debug_dir, filename)

        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(prompt)
            logger.debug(f"校对prompt已保存到: {file_path}")
        except Exception as e:
            logger.warning(f"保存校对prompt失败: {e}")
