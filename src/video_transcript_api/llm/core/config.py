"""LLM 统一配置类"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Union


@dataclass
class LLMConfig:
    """LLM 统一配置类"""

    # 必需参数（无默认值）
    api_key: str
    base_url: str
    calibrate_model: str
    summary_model: str

    # 可选参数（有默认值）
    calibrate_reasoning_effort: Optional[str] = None
    summary_reasoning_effort: Optional[str] = None

    # 关键信息提取模型
    key_info_model: Optional[str] = None  # 默认使用 calibrate_model
    key_info_reasoning_effort: Optional[str] = None

    # 说话人推断模型
    speaker_model: Optional[str] = None  # 默认使用 calibrate_model
    speaker_reasoning_effort: Optional[str] = None

    # 说话人推断采样配置（按说话人采样，而非全局前 N 字符截断）
    speaker_samples_per_speaker: int = 3       # 每个说话人采样的发言条数上限
    speaker_max_chars_per_speaker: int = 400   # 每个说话人采样文本的总字符上限
    speaker_context_dialogs: int = 2           # 首次出场前，采集他人发言作为上下文的条数
    speaker_confidence_threshold: float = 0.6  # 置信度阈值，低于此值不采用推断姓名
    # 所有说话人采样文本合计的全局字符上限，防止 diarization 切分错误产生大量
    # 虚假说话人标签时，单人上限仍因人数膨胀导致 prompt 总量失控
    speaker_max_total_sample_chars: int = 8000

    # 质量验证模型
    validator_model: Optional[str] = None  # 默认使用 calibrate_model
    validator_reasoning_effort: Optional[str] = None

    # 重试配置
    max_retries: int = 3
    retry_delay: int = 5

    # 质量配置
    min_calibrate_ratio: float = 0.80
    min_summary_threshold: int = 500

    # 统一质量验证配置
    quality_score_weights: Dict[str, float] = field(
        default_factory=lambda: {
            "accuracy": 0.40,
            "completeness": 0.30,
            "fluency": 0.20,
            "format": 0.10,
        }
    )

    # 分段配置
    enable_threshold: int = 5000
    segment_size: int = 2000
    max_segment_size: int = 3000
    # 分段质量验证配置（纯文本）
    segmentation_validation_enabled: bool = False
    segmentation_pass_ratio: float = 0.7
    segmentation_force_retry_ratio: float = 0.5
    segmentation_fallback_strategy: str = "best_quality"

    # 并发配置
    concurrent_workers: int = 10

    # 结构化校对配置
    min_chunk_length: int = 300
    max_chunk_length: int = 1500
    preferred_chunk_length: int = 800
    max_calibration_retries: int = 2
    calibration_concurrent_limit: int = 3
    chunk_time_budget: int = 300  # 单个 chunk 校对的时间预算（秒），超时直接 fallback
    # ID 锚点校对：chunk 中被返回修正的对话占比低于此阈值时，视为低覆盖（疑似截断/偷懒）触发重试
    min_correction_coverage: float = 0.5
    enable_validation: bool = False  # 是否启用分段质量验证（每个chunk独立打分，不再进行整体验证）
    # 结构化校对质量验证配置（对话流）
    structured_validation_enabled: bool = False
    structured_fallback_strategy: str = "best_quality"

    # 质量阈值
    overall_score_threshold: float = 8.0
    minimum_single_score: float = 7.0

    # llm-compat 集成配置
    content_fallbacks: Optional[Dict[str, List[str]]] = None
    collector_url: Optional[str] = None
    collector_project: str = ""
    collector_api_key: str = ""
    refusal_keywords_url: Optional[Union[str, List[str]]] = None
    total_timeout: float = 300.0

    # 章节梗概生成模型（默认使用 calibrate_model）
    # 注：本段落字段（含以下 3 个）追加于 dataclass 字段列表末尾，而非插在中间——
    # 插在中间会移位既有的可选位置参数，老代码若按位置传参（例如 speaker 相关
    # 配置）会静默写进这里，新增字段一律追加到最后以保持位置参数兼容。
    chapters_model: Optional[str] = None
    chapters_reasoning_effort: Optional[str] = None
    min_chapters_threshold: int = 10000  # 原文字符数低于此值不生成章节（正常跳过，非失败）
    # 章节生成输入的字符数上限：超过此值直接判为 FAILED，而不是把超大输入硬塞给模型。
    # 须与 chapters_model 的上下文窗口能力匹配——换用上下文更小/更大的模型时需同步调整。
    max_chapters_input_chars: int = 500000

    @classmethod
    def from_dict(cls, config_dict: dict) -> "LLMConfig":
        """从配置字典创建 LLMConfig 实例

        Args:
            config_dict: 完整的配置字典

        Returns:
            LLMConfig 实例
        """
        llm_config = config_dict.get("llm", {})
        segmentation_config = llm_config.get("segmentation", {})
        calibration_config = llm_config.get("structured_calibration", {})
        speaker_inference_config = llm_config.get("speaker_inference", {})
        quality_validation_config = llm_config.get("quality_validation", {})
        quality_config = quality_validation_config.get(
            "quality_threshold", calibration_config.get("quality_threshold", {})
        )
        score_weights = quality_validation_config.get("score_weights")

        segmentation_validation = segmentation_config.get("quality_validation", {})
        structured_validation = calibration_config.get("quality_validation", {})

        # 导入 normalize_reasoning_effort 函数
        from .. import normalize_reasoning_effort

        # 统一质量验证权重（默认值）
        if not score_weights:
            score_weights = {
                "accuracy": 0.40,
                "completeness": 0.30,
                "fluency": 0.20,
                "format": 0.10,
            }

        # 纯文本质量验证配置（兼容旧版：若缺失则默认关闭）
        segmentation_validation_enabled = segmentation_validation.get("enabled")
        if segmentation_validation_enabled is None:
            segmentation_validation_enabled = False

        # 对话流质量验证配置（兼容旧字段）
        structured_validation_enabled = structured_validation.get("enabled")
        if structured_validation_enabled is None:
            structured_validation_enabled = calibration_config.get("enable_validation", False)

        structured_fallback_strategy = structured_validation.get("fallback_strategy")
        if not structured_fallback_strategy:
            fallback_to_original = calibration_config.get("fallback_to_original", True)
            structured_fallback_strategy = (
                "formatted_original" if fallback_to_original else "best_quality"
            )

        return cls(
            # API 配置
            api_key=llm_config["api_key"],
            base_url=llm_config["base_url"],

            # 校对模型
            calibrate_model=llm_config["calibrate_model"],
            calibrate_reasoning_effort=normalize_reasoning_effort(
                llm_config.get("calibrate_reasoning_effort")
            ),

            # 总结模型
            summary_model=llm_config["summary_model"],
            summary_reasoning_effort=normalize_reasoning_effort(
                llm_config.get("summary_reasoning_effort")
            ),

            # 关键信息提取模型（默认使用校对模型）
            key_info_model=llm_config.get("key_info_model", llm_config["calibrate_model"]),
            key_info_reasoning_effort=normalize_reasoning_effort(
                llm_config.get("key_info_reasoning_effort")
            ),

            # 说话人推断模型（默认使用校对模型）
            speaker_model=llm_config.get("speaker_model", llm_config["calibrate_model"]),
            speaker_reasoning_effort=normalize_reasoning_effort(
                llm_config.get("speaker_reasoning_effort")
            ),

            # 章节梗概生成模型（默认使用校对模型）
            chapters_model=llm_config.get("chapters_model", llm_config["calibrate_model"]),
            chapters_reasoning_effort=normalize_reasoning_effort(
                llm_config.get("chapters_reasoning_effort")
            ),

            # 说话人推断采样配置
            speaker_samples_per_speaker=speaker_inference_config.get("samples_per_speaker", 3),
            speaker_max_chars_per_speaker=speaker_inference_config.get(
                "max_chars_per_speaker", 400
            ),
            speaker_context_dialogs=speaker_inference_config.get("context_dialogs", 2),
            speaker_confidence_threshold=speaker_inference_config.get(
                "confidence_threshold", 0.6
            ),
            speaker_max_total_sample_chars=speaker_inference_config.get(
                "max_total_sample_chars", 8000
            ),

            # 质量验证模型
            validator_model=calibration_config.get(
                "validator_model", llm_config["calibrate_model"]
            ),
            validator_reasoning_effort=normalize_reasoning_effort(
                calibration_config.get("validator_reasoning_effort")
            ),

            # 重试配置
            max_retries=llm_config.get("max_retries", 3),
            retry_delay=llm_config.get("retry_delay", 5),

            # 质量配置
            min_calibrate_ratio=llm_config.get("min_calibrate_ratio", 0.80),
            min_summary_threshold=llm_config.get("min_summary_threshold", 500),
            min_chapters_threshold=llm_config.get("min_chapters_threshold", 10000),
            max_chapters_input_chars=llm_config.get("max_chapters_input_chars", 500000),
            quality_score_weights=score_weights,

            # 分段配置
            enable_threshold=segmentation_config.get("enable_threshold", 5000),
            segment_size=segmentation_config.get("segment_size", 2000),
            max_segment_size=segmentation_config.get("max_segment_size", 3000),
            concurrent_workers=segmentation_config.get("concurrent_workers", 10),
            segmentation_validation_enabled=segmentation_validation_enabled,
            segmentation_pass_ratio=segmentation_validation.get("pass_ratio", 0.7),
            segmentation_force_retry_ratio=segmentation_validation.get("force_retry_ratio", 0.5),
            segmentation_fallback_strategy=segmentation_validation.get(
                "fallback_strategy", "best_quality"
            ),

            # 结构化校对配置
            min_chunk_length=calibration_config.get("min_chunk_length", 300),
            max_chunk_length=calibration_config.get("max_chunk_length", 1500),
            preferred_chunk_length=calibration_config.get("preferred_chunk_length", 800),
            max_calibration_retries=calibration_config.get("max_calibration_retries", 2),
            calibration_concurrent_limit=calibration_config.get(
                "calibration_concurrent_limit", 3
            ),
            chunk_time_budget=calibration_config.get("chunk_time_budget", 300),
            min_correction_coverage=calibration_config.get("min_correction_coverage", 0.5),
            # enable_validation 保持向后兼容（指向结构化校对质量验证开关）
            enable_validation=structured_validation_enabled,
            structured_validation_enabled=structured_validation_enabled,
            structured_fallback_strategy=structured_fallback_strategy,

            # 质量阈值
            overall_score_threshold=quality_config.get("overall_score", 8.0),
            minimum_single_score=quality_config.get("minimum_single_score", 7.0),

            # llm-compat 集成
            content_fallbacks=llm_config.get("content_fallbacks"),
            collector_url=llm_config.get("collector_url"),
            collector_project=llm_config.get("collector_project", ""),
            collector_api_key=llm_config.get("collector_api_key", ""),
            refusal_keywords_url=llm_config.get("refusal_keywords_url"),
            total_timeout=float(llm_config.get("total_timeout", 300.0)),
        )

    def get_models(self) -> dict:
        """获取当前模型配置

        Returns:
            包含所有模型的字典
        """
        return {
            "calibrate_model": self.calibrate_model,
            "calibrate_reasoning_effort": self.calibrate_reasoning_effort,
            "summary_model": self.summary_model,
            "summary_reasoning_effort": self.summary_reasoning_effort,
            "validator_model": self.validator_model,
            "validator_reasoning_effort": self.validator_reasoning_effort,
        }
