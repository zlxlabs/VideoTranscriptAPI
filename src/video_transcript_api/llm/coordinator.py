"""协调器模块 - 场景路由和统一入口"""

from typing import Dict, List, Optional, Any, Union

from ..utils.logging import setup_logger
from ..utils.llm_status import SummaryStatus
from .core.config import LLMConfig
from .core.llm_client import LLMClient
from .core.cache_manager import CacheManager
from .core.key_info_extractor import KeyInfoExtractor
from .core.speaker_inferencer import SpeakerInferencer
from .core.usage_context import set_context
from .validators.unified_quality_validator import UnifiedQualityValidator
from .processors.plain_text_processor import PlainTextProcessor
from .processors.speaker_aware_processor import SpeakerAwareProcessor
from .processors.summary_processor import SummaryProcessor, SummaryResult

logger = setup_logger(__name__)


class LLMCoordinator:
    """LLM 处理协调器

    负责场景路由，统一入口接口，集成两个处理器
    """

    def __init__(self, config_dict: dict, cache_dir: str):
        """初始化协调器

        Args:
            config_dict: 完整的配置字典
            cache_dir: 缓存目录路径
        """
        # 保存完整配置字典
        self.config_dict = config_dict

        # 创建配置对象
        self.config = LLMConfig.from_dict(config_dict)

        # 创建核心组件
        self.llm_client = LLMClient(
            api_key=self.config.api_key,
            base_url=self.config.base_url,
            max_retries=self.config.max_retries,
            retry_delay=self.config.retry_delay,
            config=config_dict,  # 传递完整配置，以便读取 JSON 输出模式等设置
        )

        self.cache_manager = CacheManager(cache_dir=cache_dir)

        self.key_info_extractor = KeyInfoExtractor(
            llm_client=self.llm_client,
            cache_manager=self.cache_manager,
            model=self.config.key_info_model or self.config.calibrate_model,
            reasoning_effort=self.config.key_info_reasoning_effort,
        )

        self.speaker_inferencer = SpeakerInferencer(
            llm_client=self.llm_client,
            cache_manager=self.cache_manager,
            model=self.config.speaker_model or self.config.calibrate_model,
            reasoning_effort=self.config.speaker_reasoning_effort,
            samples_per_speaker=self.config.speaker_samples_per_speaker,
            max_chars_per_speaker=self.config.speaker_max_chars_per_speaker,
            context_dialogs=self.config.speaker_context_dialogs,
            confidence_threshold=self.config.speaker_confidence_threshold,
        )

        self.quality_validator = UnifiedQualityValidator(
            llm_client=self.llm_client,
            model=self.config.validator_model or self.config.calibrate_model,
            reasoning_effort=self.config.validator_reasoning_effort,
            score_weights=self.config.quality_score_weights,
            overall_score_threshold=self.config.overall_score_threshold,
            minimum_single_score=self.config.minimum_single_score,
        )

        # 创建处理器
        self.plain_text_processor = PlainTextProcessor(
            config=self.config,
            llm_client=self.llm_client,
            key_info_extractor=self.key_info_extractor,
            quality_validator=self.quality_validator,
        )

        self.speaker_aware_processor = SpeakerAwareProcessor(
            config=self.config,
            llm_client=self.llm_client,
            key_info_extractor=self.key_info_extractor,
            speaker_inferencer=self.speaker_inferencer,
            quality_validator=self.quality_validator,
        )

        # 创建总结处理器
        self.summary_processor = SummaryProcessor(
            llm_client=self.llm_client,
            config=self.config,
        )

        logger.info("LLM Coordinator initialized successfully with summary support")

    def process(
        self,
        content: Union[str, List[Dict]],
        title: str,
        author: str = "",
        description: str = "",
        platform: str = "",
        media_id: str = "",
        skip_summary: bool = False,
    ) -> Dict:
        """处理文本（统一入口）

        Args:
            content: 文本内容（纯文本或对话列表）
            title: 视频标题
            author: 作者
            description: 描述
            platform: 平台标识
            media_id: 媒体 ID
            skip_summary: 是否跳过总结生成（重新校对场景使用）

        Returns:
            处理结果字典:
            {
                "calibrated_text": str,        # 校对后的文本
                "summary_text": Optional[str], # 总结文本（新增）
                "key_info": dict,              # 关键信息
                "stats": dict,                 # 统计信息
                "structured_data": dict,       # 结构化数据（仅有说话人）
            }
        """
        logger.info(f"Processing content for: {title} (skip_summary={skip_summary})")

        # 获取模型配置（敏感词降级由 llm-compat 自动处理）
        selected_models = self.config.get_models()

        # 步骤 2: 校对处理（路由到对应处理器）
        logger.info("Step 1/2: Calibration processing")

        # 用 contextvar 标记当前处理阶段为 calibration，供 LLM 调用链路末端的
        # token 用量审计记录使用（跨 ThreadPoolExecutor 需 processor 内部显式
        # copy_context 传播，见 plain_text_processor.py / speaker_aware_processor.py）
        with set_context(stage="calibration"):
            calibration_result = self._route_to_calibration_processor(
                content=content,
                title=title,
                author=author,
                description=description,
                platform=platform,
                media_id=media_id,
                selected_models=selected_models,
            )

        # 提取校对文本和说话人信息
        calibrated_text = calibration_result.get("calibrated_text", "")
        speaker_count = self._extract_speaker_count(content, calibration_result)

        # 步骤 3: 总结生成（基于校对文本，可跳过）
        # summary_status 为 None 表示"本轮未尝试生成"（仅 calibrate_only 重新校对场景，
        # 不补跑 summary 时出现）；下游（llm_ops/cache_manager）据此保留上一轮的 summary_status，
        # 不会用 None 误覆盖已有的 GENERATED/FAILED 等状态。
        summary_text = None
        summary_status: Optional[SummaryStatus] = None
        if skip_summary:
            logger.info("Step 2/2: Summary generation SKIPPED (skip_summary=True)")
        else:
            logger.info("Step 2/2: Summary generation")
            with set_context(stage="summary"):
                summary_result = self._generate_summary_if_needed(
                    text=calibrated_text,
                    title=title,
                    author=author,
                    description=description,
                    speaker_count=speaker_count,
                    transcription_data=self._extract_transcription_data(content),
                    selected_models=selected_models,
                )
            summary_text = summary_result.text
            summary_status = summary_result.status

        # 步骤 4: 合并结果
        # calibration_status/calibration_stats 统一提升到 stats 顶层：
        # - 纯文本路径：calibration_status 与统计字段(total_segments 等)本来就在 stats 顶层
        # - 结构化路径：calibration_status 与统计字段都嵌在 stats["calibration_stats"] 里
        # 这里做一次归一化，让下游（llm_ops/cache_manager/模板）只需读 stats["calibration_status"]
        # 和 stats["calibration_stats"] 两个统一位置，不用关心是哪条路径产出的。
        calibration_stats_raw = calibration_result.get("stats", {})
        calibration_status = calibration_stats_raw.get("calibration_status")
        calibration_stats_detail = calibration_stats_raw.get("calibration_stats")
        if calibration_status is None and calibration_stats_detail:
            calibration_status = calibration_stats_detail.get("calibration_status")

        if calibration_stats_detail is None and "total_segments" in calibration_stats_raw:
            # 纯文本路径的统计字段是扁平的，这里合成一份 nested 视图，
            # 使 llm_status.json / 通知警告 / 模板渲染可以统一从 calibration_stats 读取
            calibration_stats_detail = {
                "total_segments": calibration_stats_raw.get("total_segments"),
                "calibrated_segments": calibration_stats_raw.get("calibrated_segments"),
                "fallback_segments": calibration_stats_raw.get("fallback_segments"),
                "low_quality_segments": calibration_stats_raw.get("low_quality_segments"),
            }

        return {
            "calibrated_text": calibrated_text,
            "summary_text": summary_text,
            "key_info": calibration_result.get("key_info"),
            "stats": {
                **calibration_stats_raw,
                "summary_length": len(summary_text) if summary_text else 0,
                "calibration_status": calibration_status,
                "calibration_stats": calibration_stats_detail,
                "summary_status": summary_status,
            },
            "structured_data": calibration_result.get("structured_data"),
            "models_used": selected_models,
        }

    def _route_to_calibration_processor(
        self,
        content: Union[str, List[Dict]],
        title: str,
        author: str,
        description: str,
        platform: str,
        media_id: str,
        selected_models: Dict,
    ) -> Dict:
        """路由到对应的校对处理器

        Args:
            content: 文本内容（纯文本或对话列表）
            title: 视频标题
            author: 作者
            description: 描述
            platform: 平台标识
            media_id: 媒体 ID
            selected_models: 选定的模型

        Returns:
            校对结果字典
        """
        if isinstance(content, str):
            # 纯文本 - 使用 PlainTextProcessor
            logger.debug("Routing to PlainTextProcessor")
            return self.plain_text_processor.process(
                text=content,
                title=title,
                author=author,
                description=description,
                platform=platform,
                media_id=media_id,
                selected_models=selected_models,
            )
        elif isinstance(content, list):
            # 对话列表 - 使用 SpeakerAwareProcessor
            logger.debug(f"Routing to SpeakerAwareProcessor (dialog count: {len(content)})")
            return self.speaker_aware_processor.process(
                dialogs=content,
                title=title,
                author=author,
                description=description,
                platform=platform,
                media_id=media_id,
                selected_models=selected_models,
            )
        elif isinstance(content, dict):
            # 如果传入字典，尝试提取 segments 字段
            if "segments" in content:
                logger.warning(
                    "Received dict with 'segments' key. Extracting segments list. "
                    "Please pass segments directly instead of the full dict."
                )
                segments = content.get("segments", [])
                return self.speaker_aware_processor.process(
                    dialogs=segments,
                    title=title,
                    author=author,
                    description=description,
                    platform=platform,
                    media_id=media_id,
                    selected_models=selected_models,
                )
            else:
                raise ValueError(
                    f"Unsupported content type: dict without 'segments' key. "
                    f"Available keys: {list(content.keys())}"
                )
        else:
            raise ValueError(
                f"Unsupported content type: {type(content)}. "
                f"Expected str (plain text) or list (dialogs)."
            )

    def _extract_speaker_count(
        self,
        content: Union[str, List[Dict]],
        calibration_result: Dict,
    ) -> int:
        """提取说话人数量

        Args:
            content: 原始内容
            calibration_result: 校对结果

        Returns:
            说话人数量（0 表示单说话人，>= 2 表示多说话人）
        """
        # 纯文本 → 单说话人
        if isinstance(content, str):
            return 0

        # 有说话人 → 从结果中提取
        structured_data = calibration_result.get("structured_data", {})
        speaker_mapping = structured_data.get("speaker_mapping", {})
        speaker_count = len(speaker_mapping)

        logger.debug(f"Detected speaker count: {speaker_count}")
        return speaker_count

    def _extract_transcription_data(
        self,
        content: Union[str, List[Dict]],
    ) -> Optional[Dict]:
        """提取原始转录数据（用于辅助总结）

        Args:
            content: 原始内容

        Returns:
            转录数据字典（如果是有说话人文本）
        """
        if isinstance(content, list):
            # 有说话人 → 构建 transcription_data
            return {"segments": content}
        else:
            return None

    def _generate_summary_if_needed(
        self,
        text: str,
        title: str,
        author: str,
        description: str,
        speaker_count: int,
        transcription_data: Optional[Dict],
        selected_models: Dict,
    ) -> SummaryResult:
        """生成总结（如果需要）

        Args:
            text: 校对后的文本
            title: 视频标题
            author: 作者
            description: 描述
            speaker_count: 说话人数量
            transcription_data: 原始转录数据
            selected_models: 选定的模型

        Returns:
            SummaryResult: text 为 None 时通过 status 区分是"文本过短跳过"
            还是"生成失败"，不再用裸 None 二义（详见 SummaryResult 定义）。

        Note:
            这里的长度预检是对 SummaryProcessor 内部同一检查的前置优化
            （避免不必要的函数调用/日志），SummaryProcessor.process() 本身
            仍保留完整检查作为独立调用时的兜底，两者判定口径一致。
        """
        # 检查长度阈值（与 SummaryProcessor.process() 内部检查口径一致）
        if len(text) < self.config.min_summary_threshold:
            logger.info(
                f"Text too short for summary: {len(text)} < {self.config.min_summary_threshold}"
            )
            return SummaryResult(text=None, status=SummaryStatus.SKIPPED_SHORT)

        # 调用总结处理器
        logger.info(f"Generating summary (text length: {len(text)}, speaker_count: {speaker_count})")

        try:
            result = self.summary_processor.process(
                text=text,
                title=title,
                author=author,
                description=description,
                speaker_count=speaker_count,
                transcription_data=transcription_data,
                selected_models=selected_models,
            )

            if result.text:
                logger.info(f"Summary generated successfully (length: {len(result.text)})")
            else:
                logger.warning(f"Summary generation did not produce text (status={result.status})")

            return result

        except Exception as e:
            logger.error(f"Summary generation failed: {e}", exc_info=True)
            return SummaryResult(text=None, status=SummaryStatus.FAILED)
