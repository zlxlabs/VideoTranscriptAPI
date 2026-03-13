"""统一质量验证器"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union
import json

from ...utils.logging import setup_logger
from ..core.llm_client import LLMClient
from ..prompts.unified_validation_prompts import (
    UNIFIED_VALIDATION_SYSTEM_PROMPT,
    build_unified_validation_user_prompt,
)
from ..schemas import UNIFIED_VALIDATION_SCHEMA

logger = setup_logger(__name__)


@dataclass
class ValidationInput:
    """验证输入的标准化数据类"""

    content_type: str  # "text" or "dialog"
    original: Union[str, List[Dict[str, Any]]]
    calibrated: Union[str, List[Dict[str, Any]]]
    length_info: Dict[str, Any]

    @classmethod
    def from_inputs(
        cls,
        original: Union[str, List[Dict[str, Any]]],
        calibrated: Union[str, List[Dict[str, Any]]],
    ) -> "ValidationInput":
        """自动识别类型并标准化"""
        if isinstance(original, str) and isinstance(calibrated, str):
            original_len = len(original)
            calibrated_len = len(calibrated)
            ratio = calibrated_len / original_len if original_len > 0 else 0.0
            return cls(
                content_type="text",
                original=original,
                calibrated=calibrated,
                length_info={
                    "original_length": original_len,
                    "calibrated_length": calibrated_len,
                    "ratio": ratio,
                },
            )

        if isinstance(original, list) and isinstance(calibrated, list):
            original_len = sum(len(d.get("text", "")) for d in original)
            calibrated_len = sum(len(d.get("text", "")) for d in calibrated)
            ratio = calibrated_len / original_len if original_len > 0 else 0.0
            return cls(
                content_type="dialog",
                original=original,
                calibrated=calibrated,
                length_info={
                    "original_count": len(original),
                    "calibrated_count": len(calibrated),
                    "count_ratio": (len(calibrated) / len(original)) if len(original) > 0 else 0.0,
                    "original_length": original_len,
                    "calibrated_length": calibrated_len,
                    "ratio": ratio,
                },
            )

        raise ValueError("ValidationInput expects both inputs as str or both as list")


class ScoreCalculator:
    """本地计算加权平均"""

    def __init__(self, weights: Dict[str, float], thresholds: Dict[str, float]):
        self.weights = weights
        self.thresholds = thresholds

    def calculate_overall_score(self, scores: Dict[str, float]) -> float:
        overall = sum(scores.get(dim, 0.0) * weight for dim, weight in self.weights.items())
        return round(overall, 2)

    def check_passed(self, overall_score: float, scores: Dict[str, float]) -> bool:
        if overall_score < self.thresholds.get("overall_score", 8.0):
            return False
        minimum = self.thresholds.get("minimum_single_score", 7.0)
        for dimension, score in scores.items():
            if score < minimum:
                return False
        return True


class UnifiedQualityValidator:
    """统一质量验证器"""

    def __init__(
        self,
        llm_client: LLMClient,
        model: str,
        reasoning_effort: Optional[str],
        score_weights: Dict[str, float],
        overall_score_threshold: float,
        minimum_single_score: float,
    ):
        self.llm_client = llm_client
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.score_weights = score_weights
        self.calculator = ScoreCalculator(
            weights=score_weights,
            thresholds={
                "overall_score": overall_score_threshold,
                "minimum_single_score": minimum_single_score,
            },
        )

    def validate(
        self,
        original: Union[str, List[Dict[str, Any]]],
        calibrated: Union[str, List[Dict[str, Any]]],
        context: Optional[Dict[str, Any]] = None,
        selected_models: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """统一验证入口"""
        context = context or {}
        validation_input = ValidationInput.from_inputs(original, calibrated)

        structure_check = None
        if validation_input.content_type == "dialog":
            structure_check = self._check_dialog_structure(
                validation_input.original, validation_input.calibrated
            )
            if not structure_check["passed"]:
                return {
                    "scores": {},
                    "overall_score": 0.0,
                    "passed": False,
                    "issues": structure_check["issues"],
                    "deleted_content_analysis": "",
                    "recommendation": "Dialog structure mismatch",
                    "length_info": validation_input.length_info,
                    "structure_check": structure_check,
                }

        model = self.model
        reasoning_effort = self.reasoning_effort
        if selected_models:
            model = selected_models.get("validator_model") or model
            reasoning_effort = selected_models.get("validator_reasoning_effort") or reasoning_effort

        user_prompt = build_unified_validation_user_prompt(
            validation_input=validation_input,
            video_title=context.get("title", ""),
            author=context.get("author", ""),
            description=context.get("description", ""),
        )

        try:
            result = self.llm_client.call(
                model=model,
                system_prompt=UNIFIED_VALIDATION_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                response_schema=UNIFIED_VALIDATION_SCHEMA,
                reasoning_effort=reasoning_effort,
                task_type="unified_quality_validation",
            )

            validation_data = result.structured_output or {}
            scores = self._normalize_scores(validation_data.get("scores", {}))
            if not scores:
                raise ValueError("Validation scores missing or invalid")

            overall_score = self.calculator.calculate_overall_score(scores)
            passed = self.calculator.check_passed(overall_score, scores)

            issues = validation_data.get("issues", [])
            if not isinstance(issues, list):
                issues = [str(issues)]

            return {
                "scores": scores,
                "overall_score": overall_score,
                "passed": passed,
                "issues": issues,
                "deleted_content_analysis": validation_data.get("deleted_content_analysis", ""),
                "recommendation": validation_data.get("recommendation", ""),
                "length_info": validation_input.length_info,
                "structure_check": structure_check,
            }

        except Exception as e:
            logger.error(f"Unified quality validation failed: {e}")
            return {
                "scores": {},
                "overall_score": 0.0,
                "passed": False,
                "issues": [f"Validation error: {str(e)}"],
                "deleted_content_analysis": "",
                "recommendation": "Validation failed, fallback to original",
                "length_info": validation_input.length_info,
                "structure_check": structure_check,
            }

    def _normalize_scores(self, scores: Dict[str, Any]) -> Dict[str, float]:
        """确保评分字段完整且为数值"""
        normalized = {}
        for dim in self.score_weights.keys():
            value = scores.get(dim)
            try:
                normalized[dim] = float(value)
            except (TypeError, ValueError):
                normalized[dim] = None

        if any(v is None for v in normalized.values()):
            return {}

        return normalized

    def _check_dialog_structure(
        self, original: List[Dict[str, Any]], calibrated: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """检查对话结构一致性"""
        issues = []
        speaker_mismatches = []

        count_match = len(original) == len(calibrated)
        if not count_match:
            issues.append(
                f"Dialog count mismatch: {len(original)} != {len(calibrated)}"
            )

        min_count = min(len(original), len(calibrated))
        for i in range(min_count):
            original_speaker = original[i].get("speaker")
            calibrated_speaker = calibrated[i].get("speaker")
            if original_speaker != calibrated_speaker:
                speaker_mismatches.append(
                    {
                        "index": i,
                        "original_speaker": original_speaker,
                        "calibrated_speaker": calibrated_speaker,
                    }
                )

        if speaker_mismatches:
            issues.append(f"Speaker mismatch count: {len(speaker_mismatches)}")

        passed = count_match and not speaker_mismatches
        return {
            "passed": passed,
            "issues": issues,
            "count_match": count_match,
            "speaker_mismatches": speaker_mismatches,
        }


def _format_dialogs_for_prompt(dialogs: List[Dict[str, Any]]) -> str:
    """将对话列表格式化为 JSON 字符串（用于日志或调试）"""
    return json.dumps(dialogs, ensure_ascii=False, indent=2)
