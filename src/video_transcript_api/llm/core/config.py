"""LLM 统一配置类"""

from dataclasses import dataclass, field
from typing import Optional, Dict


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

    # 质量验证模型
    validator_model: Optional[str] = None  # 默认使用 calibrate_model
    validator_reasoning_effort: Optional[str] = None

    # 风险模型配置
    risk_calibrate_model: Optional[str] = None
    risk_calibrate_reasoning_effort: Optional[str] = None
    risk_summary_model: Optional[str] = None
    risk_summary_reasoning_effort: Optional[str] = None
    risk_validator_model: Optional[str] = None
    risk_validator_reasoning_effort: Optional[str] = None

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
    enable_validation: bool = False  # 是否启用分段质量验证（每个chunk独立打分，不再进行整体验证）
    # 结构化校对质量验证配置（对话流）
    structured_validation_enabled: bool = False
    structured_fallback_strategy: str = "best_quality"

    # 质量阈值
    overall_score_threshold: float = 8.0
    minimum_single_score: float = 7.0

    # 风控配置
    enable_risk_model_selection: bool = False

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

            # 质量验证模型
            validator_model=calibration_config.get(
                "validator_model", llm_config["calibrate_model"]
            ),
            validator_reasoning_effort=normalize_reasoning_effort(
                calibration_config.get("validator_reasoning_effort")
            ),

            # 风险模型
            risk_calibrate_model=llm_config.get("risk_calibrate_model"),
            risk_calibrate_reasoning_effort=normalize_reasoning_effort(
                llm_config.get("risk_calibrate_reasoning_effort")
            ),
            risk_summary_model=llm_config.get("risk_summary_model"),
            risk_summary_reasoning_effort=normalize_reasoning_effort(
                llm_config.get("risk_summary_reasoning_effort")
            ),
            risk_validator_model=calibration_config.get("risk_validator_model"),
            risk_validator_reasoning_effort=normalize_reasoning_effort(
                calibration_config.get("risk_validator_reasoning_effort")
            ),

            # 重试配置
            max_retries=llm_config.get("max_retries", 3),
            retry_delay=llm_config.get("retry_delay", 5),

            # 质量配置
            min_calibrate_ratio=llm_config.get("min_calibrate_ratio", 0.80),
            min_summary_threshold=llm_config.get("min_summary_threshold", 500),
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
            # enable_validation 保持向后兼容（指向结构化校对质量验证开关）
            enable_validation=structured_validation_enabled,
            structured_validation_enabled=structured_validation_enabled,
            structured_fallback_strategy=structured_fallback_strategy,

            # 质量阈值
            overall_score_threshold=quality_config.get("overall_score", 8.0),
            minimum_single_score=quality_config.get("minimum_single_score", 7.0),

            # 风控配置
            enable_risk_model_selection=llm_config.get(
                "enable_risk_model_selection", False
            ),
        )

    def select_models_for_task(self, has_risk: bool) -> dict:
        """根据风险情况选择模型

        Args:
            has_risk: 是否检测到风险

        Returns:
            包含所选模型的字典
        """
        if has_risk and self.enable_risk_model_selection:
            return {
                "calibrate_model": self.risk_calibrate_model or self.calibrate_model,
                "calibrate_reasoning_effort": self.risk_calibrate_reasoning_effort or self.calibrate_reasoning_effort,
                "summary_model": self.risk_summary_model or self.summary_model,
                "summary_reasoning_effort": self.risk_summary_reasoning_effort or self.summary_reasoning_effort,
                "validator_model": self.risk_validator_model or self.validator_model,
                "validator_reasoning_effort": self.risk_validator_reasoning_effort or self.validator_reasoning_effort,
                "has_risk": True,
            }
        else:
            return {
                "calibrate_model": self.calibrate_model,
                "calibrate_reasoning_effort": self.calibrate_reasoning_effort,
                "summary_model": self.summary_model,
                "summary_reasoning_effort": self.summary_reasoning_effort,
                "validator_model": self.validator_model,
                "validator_reasoning_effort": self.validator_reasoning_effort,
                "has_risk": False,
            }
