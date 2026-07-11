"""内容总结处理器"""

from dataclasses import dataclass
from typing import Dict, Optional

from ...utils.logging import setup_logger
from ...utils.llm_status import SummaryStatus
from ..core.config import LLMConfig
from ..core.llm_client import LLMClient
from ..prompts import (
    SUMMARY_SYSTEM_PROMPT_SINGLE_SPEAKER,
    SUMMARY_SYSTEM_PROMPT_MULTI_SPEAKER,
    build_summary_user_prompt,
)

logger = setup_logger(__name__)


@dataclass(frozen=True)
class SummaryResult:
    """总结生成结果："诚实状态模型"的载体。

    取代过去裸 Optional[str] 返回值：过去"文本过短跳过"和"LLM 异常/输出过短失败"
    都返回 None，下游完全无法区分，最终表现为"总结处理中..."永久占位。
    现在通过 status 显式区分三种终态，text 仅在 status == GENERATED 时非空。
    """

    text: Optional[str]
    status: SummaryStatus


class SummaryProcessor:
    """内容总结处理器

    职责：
    - 生成视频内容的文本总结
    - 根据说话人数量选择合适的 System Prompt
    - 处理长度检查和降级
    """

    def __init__(
        self,
        llm_client: LLMClient,
        config: LLMConfig,
    ):
        """初始化总结处理器

        Args:
            llm_client: LLM 客户端（含智能重试）
            config: LLM 配置对象
        """
        self.llm_client = llm_client
        self.config = config

        logger.info("SummaryProcessor initialized")

    def process(
        self,
        text: str,
        title: str,
        author: str = "",
        description: str = "",
        speaker_count: int = 0,
        transcription_data: Optional[Dict] = None,
        selected_models: Optional[Dict] = None,
    ) -> SummaryResult:
        """生成文本总结

        Args:
            text: 待总结的文本（通常是校对后的文本）
            title: 视频标题
            author: 作者/频道
            description: 视频描述
            speaker_count: 说话人数量（0 或 1 表示单说话人，>= 2 表示多说话人）
            transcription_data: 原始转录数据（可选，用于辅助分析）
            selected_models: 选定的模型配置（可选，来自风险检测）

        Returns:
            SummaryResult: text 与 status 的组合。
                - 原文过短: text=None, status=SKIPPED_SHORT（正常路径，非失败）
                - LLM 异常或输出过短/为空: text=None, status=FAILED
                - 成功: text=摘要文本, status=GENERATED

        Raises:
            不抛出异常，出错时返回 status=FAILED 的 SummaryResult
        """
        # 步骤 1: 长度检查
        if len(text) < self.config.min_summary_threshold:
            logger.info(
                f"Text too short for summary: {len(text)} < {self.config.min_summary_threshold}"
            )
            return SummaryResult(text=None, status=SummaryStatus.SKIPPED_SHORT)

        logger.info(f"Generating summary for text (length: {len(text)}, speaker_count: {speaker_count})")

        try:
            # 步骤 2: 选择模型
            if selected_models:
                model = selected_models.get("summary_model", self.config.summary_model)
                reasoning_effort = selected_models.get(
                    "summary_reasoning_effort",
                    self.config.summary_reasoning_effort
                )
            else:
                model = self.config.summary_model
                reasoning_effort = self.config.summary_reasoning_effort

            # 步骤 3: 选择 System Prompt
            system_prompt = self._select_system_prompt(speaker_count)

            # 步骤 4: 构建 User Prompt
            user_prompt = build_summary_user_prompt(
                transcript=text,
                video_title=title,
                author=author,
                description=description,
            )

            # 步骤 5: 调用 LLM
            response = self.llm_client.call(
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                reasoning_effort=reasoning_effort,
                task_type="summary",  # 标识为总结任务（用于日志追踪和监控）
            )

            summary_text = response.text

            # 步骤 6: 验证结果
            if not summary_text or len(summary_text) < 50:
                logger.warning(
                    f"Summary too short or empty: {len(summary_text) if summary_text else 0} chars"
                )
                return SummaryResult(text=None, status=SummaryStatus.FAILED)

            logger.info(f"Summary generated successfully (length: {len(summary_text)})")
            return SummaryResult(text=summary_text, status=SummaryStatus.GENERATED)

        except Exception as e:
            logger.error(f"Summary generation failed: {e}", exc_info=True)
            return SummaryResult(text=None, status=SummaryStatus.FAILED)

    def _select_system_prompt(self, speaker_count: int) -> str:
        """根据说话人数量选择 System Prompt

        Args:
            speaker_count: 说话人数量

        Returns:
            System Prompt 字符串
        """
        if speaker_count >= 2:
            # 多说话人：强调对话动态、观点碰撞
            logger.debug("Using multi-speaker summary prompt")
            return SUMMARY_SYSTEM_PROMPT_MULTI_SPEAKER
        else:
            # 单说话人：强调论点提取、逻辑结构
            logger.debug("Using single-speaker summary prompt")
            return SUMMARY_SYSTEM_PROMPT_SINGLE_SPEAKER
