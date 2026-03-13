"""统一质量验证 Prompt 模板"""

import json
from typing import Any, Dict, List


UNIFIED_VALIDATION_SYSTEM_PROMPT = """你是一位专业的文本质量评估专家。

评估维度（每项 0-10 分）：
1. 准确性（accuracy）：核心信息是否保留
2. 完整性（completeness）：删减是否合理
3. 流畅度（fluency）：语言是否通顺
4. 格式规范（format）：标点与段落是否合理

重点：对于直播、闲聊类内容，合理删减不应扣分。

输出要求：
- 必须输出 JSON 格式
- 字段必须包含 scores（accuracy/completeness/fluency/format）
- 可包含 issues、deleted_content_analysis、recommendation
- 不要输出整体分数与通过与否（由本地计算）
"""


def build_unified_validation_user_prompt(
    validation_input: Any,
    video_title: str = "",
    author: str = "",
    description: str = "",
) -> str:
    """构建统一质量验证的 User Prompt"""
    parts = []

    if video_title or author or description:
        parts.append("**辅助信息**（用于判断专有名词修正合理性）：")
        if video_title:
            parts.append(f"- 标题：{video_title}")
        if author:
            parts.append(f"- 作者：{author}")
        if description:
            desc_truncated = description[:500] + ("..." if len(description) > 500 else "")
            parts.append(f"- 描述：{desc_truncated}")
        parts.append("")

    length_info = validation_input.length_info
    parts.append("**长度信息**：")
    parts.append(json.dumps(length_info, ensure_ascii=False, indent=2))

    if validation_input.content_type == "text":
        original = validation_input.original
        calibrated = validation_input.calibrated
        sample_original = original[:2000]
        sample_calibrated = calibrated[:2000]

        parts.append("\n**原始文本（截取前 2000 字符）**：")
        parts.append(sample_original)
        parts.append("\n**校对文本（截取前 2000 字符）**：")
        parts.append(sample_calibrated)

    else:
        original_dialogs = validation_input.original
        calibrated_dialogs = validation_input.calibrated
        sampled_original, sampled_calibrated = _sample_dialogs(
            original_dialogs, calibrated_dialogs
        )
        parts.append("\n**对话样本（原始）**：")
        parts.append(json.dumps(sampled_original, ensure_ascii=False, indent=2))
        parts.append("\n**对话样本（校对后）**：")
        parts.append(json.dumps(sampled_calibrated, ensure_ascii=False, indent=2))
        parts.append("\n**说明**：说话人和时间信息已在本地检查，不需要再次验证。")

    return "\n".join(parts)


def _sample_dialogs(
    original: List[Dict[str, Any]],
    calibrated: List[Dict[str, Any]],
    max_samples: int = 50,
) -> tuple:
    """采样对话内容（头 40%、中 30%、尾 30%）"""
    total = min(len(original), len(calibrated))
    if total <= max_samples:
        return original[:total], calibrated[:total]

    sample_size = max_samples
    head_count = int(sample_size * 0.4)
    mid_count = int(sample_size * 0.3)
    tail_count = sample_size - head_count - mid_count

    head_end = head_count
    mid_start = max((total - mid_count) // 2, head_end)
    mid_end = min(mid_start + mid_count, total)
    tail_start = max(total - tail_count, mid_end)

    sampled_original = (
        original[:head_end]
        + original[mid_start:mid_end]
        + original[tail_start:total]
    )
    sampled_calibrated = (
        calibrated[:head_end]
        + calibrated[mid_start:mid_end]
        + calibrated[tail_start:total]
    )

    return sampled_original, sampled_calibrated
