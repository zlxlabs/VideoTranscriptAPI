"""Test SummaryProcessor unit tests"""

import unittest
from unittest.mock import Mock, patch

from video_transcript_api.llm.processors import summary_processor as summary_processor_module
from video_transcript_api.llm.processors.summary_processor import SummaryProcessor
from video_transcript_api.llm.core.config import LLMConfig
from video_transcript_api.llm.core.llm_client import LLMResponse, LLMUsage
from video_transcript_api.utils.llm_status import SummaryStatus

processor_module_logger = summary_processor_module.logger


def _long_text() -> str:
  return "This is a very long text segment. " * 60  # ~2160 chars


def _make_processor(*, reasoning_effort=None) -> SummaryProcessor:
  config = LLMConfig(
    api_key="test_key",
    base_url="http://test.api.com",
    calibrate_model="test-model",
    summary_model="test-summary-model",
    min_summary_threshold=500,
    summary_reasoning_effort=reasoning_effort,
  )
  return SummaryProcessor(llm_client=Mock(), config=config)


def _usage(completion_tokens: int) -> LLMUsage:
  return LLMUsage(completion_tokens=completion_tokens, usage_missing=False)


class TestSummaryProcessor(unittest.TestCase):
  def setUp(self):
    self.processor_module_logger = processor_module_logger
    self.language_patcher = patch.object(
      summary_processor_module, "get_language", return_value="en"
    )
    self.language_patcher.start()
    self.addCleanup(self.language_patcher.stop)
    self.processor = _make_processor()
    self.long_text = _long_text()
    self.mock_call = self.processor.llm_client.call

  def test_short_text_returns_none(self):
    result = self.processor.process(
      text="This is a very short text.",
      title="Test Title",
    )
    self.assertIsNone(result.text)
    self.assertEqual(result.status, SummaryStatus.SKIPPED_SHORT)

  def test_single_speaker_prompt_selection(self):
    single_prompt = self.processor._select_system_prompt(speaker_count=0)
    multi_prompt = self.processor._select_system_prompt(speaker_count=2)
    self.assertTrue(len(single_prompt) > 100)
    self.assertNotEqual(single_prompt, multi_prompt)

  def test_english_language_selects_native_english_prompt(self):
    from video_transcript_api.llm.prompts import en as en_prompts
    from video_transcript_api.utils.logging.logger import load_config

    config = load_config()
    had_key = "language" in config
    original = config.get("language")
    config["language"] = "en"
    try:
      single_prompt = self.processor._select_system_prompt(speaker_count=0)
      self.assertEqual(single_prompt, en_prompts.SUMMARY_SYSTEM_PROMPT_SINGLE_SPEAKER)
      self.assertIn("Write in English only", single_prompt)
      self.assertNotIn("只能使用中文书写", single_prompt)
    finally:
      if had_key:
        config["language"] = original
      else:
        config.pop("language", None)

  def test_within_hard_cap_single_call(self):
    self.mock_call.return_value = LLMResponse(text="generated summary text. " * 20)

    result = self.processor.process(text=self.long_text, title="Test Title")

    self.assertEqual(result.status, SummaryStatus.GENERATED)
    self.assertEqual(self.mock_call.call_count, 1)
    self.assertIsNone(self.mock_call.call_args.kwargs.get("max_tokens"))

  def test_reasoning_model_omits_max_tokens(self):
    self.mock_call.return_value = LLMResponse(text="generated summary text. " * 20)

    self.processor.process(
      text=self.long_text,
      title="Test Title",
      selected_models={"summary_reasoning_effort": "high"},
    )

    self.assertIsNone(self.mock_call.call_args.kwargs.get("max_tokens"))

  def test_disabled_reasoning_effort_sends_max_tokens(self):
    processor = _make_processor(reasoning_effort="disabled")
    processor.llm_client.call.return_value = LLMResponse(
      text="generated summary text. " * 20
    )
    hard_cap = min(2 * len(self.long_text), 4500)

    processor.process(text=self.long_text, title="Test Title")

    self.assertEqual(
      processor.llm_client.call.call_args.kwargs.get("max_tokens"),
      int(hard_cap * 1.5),
    )

  def test_first_truncation_retry_success(self):
    processor = _make_processor(reasoning_effort="disabled")
    hard_cap = min(2 * len(self.long_text), 4500)
    max_tokens = int(hard_cap * 1.5)
    retry_text = "z" * (hard_cap - 100)
    processor.llm_client.call.side_effect = [
      LLMResponse(text="x" * 200, usage=_usage(max_tokens)),
      LLMResponse(text=retry_text, usage=_usage(1000)),
    ]

    result = processor.process(text=self.long_text, title="Test Title")

    self.assertEqual(result.status, SummaryStatus.GENERATED)
    self.assertEqual(result.text, retry_text)
    self.assertEqual(processor.llm_client.call.call_count, 2)

  def test_first_truncation_retry_still_truncated_fails(self):
    processor = _make_processor(reasoning_effort="disabled")
    hard_cap = min(2 * len(self.long_text), 4500)
    max_tokens = int(hard_cap * 1.5)
    processor.llm_client.call.side_effect = [
      LLMResponse(text="x" * 200, usage=_usage(max_tokens)),
      LLMResponse(text="y" * 200, usage=_usage(max_tokens)),
    ]

    result = processor.process(text=self.long_text, title="Test Title")

    self.assertIsNone(result.text)
    self.assertEqual(result.status, SummaryStatus.FAILED)

  def test_first_truncation_retry_exception_fails(self):
    processor = _make_processor(reasoning_effort="disabled")
    hard_cap = min(2 * len(self.long_text), 4500)
    max_tokens = int(hard_cap * 1.5)
    processor.llm_client.call.side_effect = [
      LLMResponse(text="x" * 200, usage=_usage(max_tokens)),
      Exception("retry failed"),
    ]

    result = processor.process(text=self.long_text, title="Test Title")

    self.assertIsNone(result.text)
    self.assertEqual(result.status, SummaryStatus.FAILED)

  def test_over_hard_cap_retry_success(self):
    hard_cap = min(2 * len(self.long_text), 4500)
    self.mock_call.side_effect = [
      LLMResponse(text="x" * (hard_cap + 100)),
      LLMResponse(text="y" * (hard_cap - 50)),
    ]

    with patch.object(self.processor_module_logger, "warning") as warning_mock:
      result = self.processor.process(text=self.long_text, title="Test Title")

    self.assertEqual(result.status, SummaryStatus.GENERATED)
    self.assertEqual(self.mock_call.call_count, 2)
    self.assertEqual(len(result.text), hard_cap - 50)
    joined = " ".join(str(call.args[0]) for call in warning_mock.call_args_list)
    self.assertNotIn("summary_over_budget_accepted", joined)

  def test_over_hard_cap_retry_too_short_falls_back_to_first(self):
    hard_cap = min(2 * len(self.long_text), 4500)
    first = "d" * (hard_cap + 100)
    self.mock_call.side_effect = [
      LLMResponse(text=first),
      LLMResponse(text="tiny"),
    ]

    with patch.object(self.processor_module_logger, "warning") as warning_mock:
      result = self.processor.process(text=self.long_text, title="Test Title")

    self.assertEqual(result.status, SummaryStatus.GENERATED)
    self.assertEqual(result.text, first)
    joined = " ".join(str(call.args[0]) for call in warning_mock.call_args_list)
    self.assertIn("summary_over_budget_accepted", joined)

  def test_over_hard_cap_accept_shortest_with_warning(self):
    hard_cap = min(2 * len(self.long_text), 4500)
    first = "a" * (hard_cap + 200)
    retry = "b" * (hard_cap + 50)
    self.mock_call.side_effect = [
      LLMResponse(text=first),
      LLMResponse(text=retry),
    ]

    with patch.object(self.processor_module_logger, "warning") as warning_mock:
      result = self.processor.process(text=self.long_text, title="Test Title")

    self.assertEqual(result.status, SummaryStatus.GENERATED)
    self.assertEqual(len(result.text), len(retry))
    joined = " ".join(str(call.args[0]) for call in warning_mock.call_args_list)
    self.assertIn("summary_over_budget_accepted", joined)

  def test_over_hard_cap_retry_failure_keeps_first(self):
    hard_cap = min(2 * len(self.long_text), 4500)
    first = "c" * (hard_cap + 300)
    self.mock_call.side_effect = [
      LLMResponse(text=first),
      Exception("retry failed"),
    ]

    with patch.object(self.processor_module_logger, "warning") as warning_mock:
      result = self.processor.process(text=self.long_text, title="Test Title")

    self.assertEqual(result.status, SummaryStatus.GENERATED)
    self.assertEqual(result.text, first)
    joined = " ".join(str(call.args[0]) for call in warning_mock.call_args_list)
    self.assertIn("summary_over_budget_accepted", joined)

  def test_over_hard_cap_compression_retry_truncated_falls_back_to_first(self):
    cases = (
      {
        "name": "disabled_first_complete_retry_truncated",
        "first_over_cap": 100,
        "retry_over_cap": 50,
        "first_completion_tokens_delta": -100,
      },
    )
    hard_cap = min(2 * len(self.long_text), 4500)
    max_tokens = int(hard_cap * 1.5)

    for case in cases:
      with self.subTest(case=case["name"]):
        processor = _make_processor(reasoning_effort="disabled")
        first = "f" * (hard_cap + case["first_over_cap"])
        truncated_retry = "r" * (hard_cap + case["retry_over_cap"])
        first_usage = _usage(max_tokens + case["first_completion_tokens_delta"])
        processor.llm_client.call.side_effect = [
          LLMResponse(text=first, usage=first_usage),
          LLMResponse(text=truncated_retry, usage=_usage(max_tokens)),
        ]

        with patch.object(processor_module_logger, "warning") as warning_mock:
          result = processor.process(text=self.long_text, title="Test Title")

        self.assertEqual(result.status, SummaryStatus.GENERATED)
        self.assertEqual(result.text, first)
        joined = " ".join(str(call.args[0]) for call in warning_mock.call_args_list)
        self.assertIn("summary_over_budget_accepted", joined)
        self.assertNotIn("summary_truncated_failed", joined)

  def test_summary_too_short_returns_failed(self):
    self.mock_call.return_value = LLMResponse(text="Short")

    result = self.processor.process(text=self.long_text, title="Test Title")

    self.assertIsNone(result.text)
    self.assertEqual(result.status, SummaryStatus.FAILED)

  def test_exception_handling(self):
    self.mock_call.side_effect = Exception("Test error")

    result = self.processor.process(text=self.long_text, title="Test Title")

    self.assertIsNone(result.text)
    self.assertEqual(result.status, SummaryStatus.FAILED)

  def test_task_type_and_model_override(self):
    self.mock_call.return_value = LLMResponse(text="generated summary text. " * 20)
    selected_models = {
      "summary_model": "risk-model",
      "summary_reasoning_effort": "high",
    }

    self.processor.process(
      text=self.long_text,
      title="Test Title",
      selected_models=selected_models,
    )

    kwargs = self.mock_call.call_args.kwargs
    self.assertEqual(kwargs.get("task_type"), "summary")
    self.assertEqual(kwargs.get("model"), "risk-model")
    self.assertEqual(kwargs.get("reasoning_effort"), "high")


class TestSummaryBelowFloorGuard(unittest.TestCase):
  """Length floor guard: below-floor output is treated as truncation (issue #322).

  L band input (47466 chars) gives target_min=4000 and floor=2000 with the
  default Chinese coefficient (1.0). Reasoning effort is unset, so no
  max_tokens is sent and token-based truncation detection never fires; the
  floor is the only truncation signal under test.
  """

  FLOOR_INPUT = "a" * 47466  # L band: target_min=4000 -> floor=2000

  def setUp(self):
    self.language_patcher = patch.object(
      summary_processor_module, "get_language", return_value="en"
    )
    self.language_patcher.start()
    self.addCleanup(self.language_patcher.stop)
    self.processor = _make_processor()
    self.mock_call = self.processor.llm_client.call

  def test_below_floor_retry_success_returns_retry_text(self):
    self.mock_call.side_effect = [
      LLMResponse(text="x" * 1457),
      LLMResponse(text="y" * 6000),
    ]

    with patch.object(processor_module_logger, "warning") as warning_mock:
      result = self.processor.process(text=self.FLOOR_INPUT, title="Test Title")

    self.assertEqual(result.status, SummaryStatus.GENERATED)
    self.assertEqual(result.text, "y" * 6000)
    self.assertEqual(self.mock_call.call_count, 2)
    joined = " ".join(str(call.args[0]) for call in warning_mock.call_args_list)
    self.assertIn("summary_below_floor:", joined)
    self.assertIn("actual=1457", joined)
    self.assertIn("floor=2000", joined)
    self.assertIn("target_min=4000", joined)
    self.assertIn("attempt=1", joined)

  def test_below_floor_retry_still_short_fails(self):
    self.mock_call.side_effect = [
      LLMResponse(text="x" * 1457),
      LLMResponse(text="y" * 1457),
    ]

    with patch.object(processor_module_logger, "warning") as warning_mock:
      result = self.processor.process(text=self.FLOOR_INPUT, title="Test Title")

    self.assertIsNone(result.text)
    self.assertEqual(result.status, SummaryStatus.FAILED)
    # On the base implementation (no floor guard) the LLM is called only once,
    # so this count also locks the retry wiring, not just the FAILED status.
    self.assertEqual(self.mock_call.call_count, 2)
    joined = " ".join(str(call.args[0]) for call in warning_mock.call_args_list)
    self.assertIn("summary_below_floor:", joined)
    self.assertIn("attempt=2", joined)

  def test_below_floor_retry_exception_fails(self):
    self.mock_call.side_effect = [
      LLMResponse(text="x" * 1457),
      Exception("retry exploded"),
    ]

    result = self.processor.process(text=self.FLOOR_INPUT, title="Test Title")

    self.assertIsNone(result.text)
    self.assertEqual(result.status, SummaryStatus.FAILED)
    # Two calls prove the floor-triggered retry actually ran; a FAILED status
    # alone could come from the generic exception handler without any retry.
    self.assertEqual(self.mock_call.call_count, 2)

  def test_at_or_above_floor_single_call_no_retry(self):
    self.mock_call.return_value = LLMResponse(text="z" * 3000)

    with patch.object(processor_module_logger, "warning") as warning_mock:
      result = self.processor.process(text=self.FLOOR_INPUT, title="Test Title")

    self.assertEqual(result.status, SummaryStatus.GENERATED)
    self.assertEqual(result.text, "z" * 3000)
    self.assertEqual(self.mock_call.call_count, 1)
    joined = " ".join(str(call.args[0]) for call in warning_mock.call_args_list)
    self.assertNotIn("summary_below_floor:", joined)


class TestSummaryPromptContent(unittest.TestCase):
  def test_prompts_removed_expansion_language(self):
    from video_transcript_api.llm.prompts import (
      SUMMARY_SYSTEM_PROMPT_MULTI_SPEAKER,
      SUMMARY_SYSTEM_PROMPT_SINGLE_SPEAKER,
    )

    for prompt in (
      SUMMARY_SYSTEM_PROMPT_SINGLE_SPEAKER,
      SUMMARY_SYSTEM_PROMPT_MULTI_SPEAKER,
    ):
      self.assertNotIn("不少于500字", prompt)
      self.assertNotIn("150字以上", prompt)
      self.assertNotIn("永远不要高度浓缩", prompt)
      self.assertNotIn("框架与心智模型", prompt)
      self.assertIn("概述", prompt)
      self.assertIn("主题详述", prompt)
      self.assertIn("核心观点与洞察", prompt)
      self.assertIn("逻辑分析", prompt)
      self.assertIn("默认不生成", prompt)

  def test_user_prompt_injects_budget_line(self):
    from video_transcript_api.llm.prompts import build_summary_user_prompt

    prompt = build_summary_user_prompt(
      transcript="body",
      budget_target_min=500,
      budget_target_max=3000,
      budget_hard_cap=4500,
    )
    self.assertTrue(prompt.startswith("**篇幅预算**：总长 500–3000 字，不得超过 4500 字。"))


if __name__ == "__main__":
  unittest.main()
