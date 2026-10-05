"""质量验证器"""

from typing import Dict, List, Optional, Any

from ...utils.logging import setup_logger
from .llm_client import LLMClient
from ..prompts import (
    VALIDATION_SYSTEM_PROMPT,
    build_validation_user_prompt,
)
from ..prompts.schemas.validation import VALIDATION_RESULT_SCHEMA

logger = setup_logger(__name__)


class QualityValidator:
    """质量验证器"""

    def __init__(
        self,
        llm_client: LLMClient,
        model: str = "claude-3-5-sonnet",
        reasoning_effort: Optional[str] = None,
        overall_score_threshold: float = 8.0,
        minimum_single_score: float = 7.0,
    ):
        """初始化质量验证器

        Args:
            llm_client: LLM 客户端
            model: 使用的模型
            reasoning_effort: reasoning effort 参数
            overall_score_threshold: 整体质量分数阈值
            minimum_single_score: 单项最小分数阈值
        """
        self.llm_client = llm_client
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.overall_score_threshold = overall_score_threshold
        self.minimum_single_score = minimum_single_score

    def validate_by_length(
        self,
        original: str,
        calibrated: str,
        min_ratio: float = 0.80,
    ) -> str:
        """通过长度检查验证校对质量（快速验证）

        Args:
            original: 原始文本
            calibrated: 校对后文本
            min_ratio: 最小长度比例

        Returns:
            校对后文本（如果通过）或原始文本（如果不通过）
        """
        original_len = len(original)
        calibrated_len = len(calibrated)

        if calibrated_len < original_len * min_ratio:
            logger.warning(
                f"Calibrated text too short: {calibrated_len} < {original_len * min_ratio:.0f}, "
                f"falling back to original"
            )
            return original

        logger.info(f"Length validation passed: {calibrated_len} >= {original_len * min_ratio:.0f}")
        return calibrated

    def validate_by_score(
        self,
        original: List[Dict[str, Any]],
        calibrated: List[Dict[str, Any]],
        video_metadata: Optional[Dict] = None,
        selected_models: Optional[Dict] = None,
    ) -> Dict:
        """通过 LLM 打分验证校对质量（全量验证）

        Args:
            original: 原始对话列表
            calibrated: 校对后对话列表
            video_metadata: 视频元数据
            selected_models: 选定的模型

        Returns:
            验证结果字典
        """
        if not video_metadata:
            video_metadata = {}

        logger.info("Starting quality validation by score")

        # 构建验证数据
        original_data = {
            "dialogs": original,
            "total_count": len(original)
        }

        calibrated_data = {
            "dialogs": calibrated,
            "total_count": len(calibrated)
        }

        # 构建 prompt
        user_prompt = build_validation_user_prompt(
            original_data=original_data,
            calibrated_data=calibrated_data,
            video_title=video_metadata.get('title', ''),
            author=video_metadata.get('author', ''),
            description=video_metadata.get('description', '')
        )

        try:
            model = self.model
            reasoning_effort = self.reasoning_effort
            if selected_models:
                model = selected_models.get("validator_model") or model
                reasoning_effort = selected_models.get("validator_reasoning_effort") or reasoning_effort

            result = self.llm_client.call(
                model=model,
                system_prompt=VALIDATION_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                response_schema=VALIDATION_RESULT_SCHEMA,
                reasoning_effort=reasoning_effort,
                task_type="quality_validation",
            )

            # 解析结果
            validation_data = result.structured_output

            overall_score = validation_data.get("overall_score", 0)
            scores = validation_data.get("scores", {})
            passed = validation_data.get("pass", False)

            # 双重验证：LLM 判断 + 阈值检查
            threshold_passed = self._check_threshold(overall_score, scores)

            # 最终判定
            final_passed = passed and threshold_passed

            logger.info(
                f"Quality validation completed: "
                f"overall_score={overall_score:.2f}, "
                f"passed={final_passed}"
            )

            return {
                "passed": final_passed,
                "overall_score": overall_score,
                "scores": scores,
                "issues": validation_data.get("issues", []),
                "recommendation": validation_data.get("recommendation", ""),
            }

        except Exception as e:
            logger.error(f"Quality validation failed: {e}")
            # 验证失败时，默认不通过
            return {
                "passed": False,
                "overall_score": 0,
                "scores": {},
                "issues": [f"Validation error: {str(e)}"],
                "recommendation": "Validation failed, fallback to original",
            }

    def _check_threshold(self, overall_score: float, scores: Dict[str, float]) -> bool:
        """检查分数是否达到阈值

        Args:
            overall_score: 整体分数
            scores: 各维度分数

        Returns:
            是否通过阈值检查
        """
        # 检查整体分数
        if overall_score < self.overall_score_threshold:
            logger.warning(
                f"Overall score too low: {overall_score:.2f} < {self.overall_score_threshold}"
            )
            return False

        # 检查各维度分数
        for dimension, score in scores.items():
            if score < self.minimum_single_score:
                logger.warning(
                    f"Score for {dimension} too low: {score:.2f} < {self.minimum_single_score}"
                )
                return False

        return True
