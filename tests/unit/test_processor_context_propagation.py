"""
Processor-level contextvars propagation tests (real executor.submit call sites).

Covers the actual fix applied to:
- PlainTextProcessor._calibrate_segments (plain_text_processor.py)
- SpeakerAwareProcessor._calibrate_chunks (speaker_aware_processor.py)

Both use a ThreadPoolExecutor internally to calibrate segments/chunks
concurrently. Before the fix, worker threads would not see the task_id/stage
set on the calling thread via usage_context.set_context(); after the fix
(executor.submit(contextvars.copy_context().run, fn, ...)), each worker
thread must observe the propagated context when it invokes llm_client.call().

These tests use a fake LLMClient that records usage_context.get_context()
at call time instead of hitting a real/mocked llm-compat client, keeping
the test focused purely on the contextvars propagation concern.

All console output must be in English only (no emoji, no Chinese).
"""

import threading
from typing import Dict, Optional

from video_transcript_api.llm.core import usage_context
from video_transcript_api.llm.core.config import LLMConfig
from video_transcript_api.llm.core.llm_client import LLMResponse
from video_transcript_api.llm.processors.plain_text_processor import PlainTextProcessor
from video_transcript_api.llm.processors.speaker_aware_processor import SpeakerAwareProcessor


class _ContextRecordingLLMClient:
    """Fake LLMClient.call() that records the calling thread's usage_context."""

    def __init__(self, structured: bool = False):
        self.structured = structured
        self.observed_contexts = []
        self._lock = threading.Lock()

    def call(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        response_schema: Optional[Dict] = None,
        reasoning_effort: Optional[str] = None,
        task_type: str = "unknown",
    ) -> LLMResponse:
        with self._lock:
            self.observed_contexts.append(usage_context.get_context())

        if self.structured:
            # One correction per dialog id=0 -> full coverage for single-dialog chunks
            return LLMResponse(text="", structured_output={"corrections": [{"id": 0, "text": "calibrated"}]})

        # Long calibrated text -> passes the length-ratio "green zone" check
        # immediately (no retry / no validation call needed)
        return LLMResponse(text="calibrated segment text " * 20)


class _NoOpKeyInfo:
    def format_for_prompt(self) -> str:
        return ""


class TestPlainTextProcessorPropagation:
    """PlainTextProcessor._calibrate_segments must propagate task_id/stage
    into each ThreadPoolExecutor worker thread."""

    def test_worker_threads_see_propagated_task_id_and_stage(self):
        fake_client = _ContextRecordingLLMClient()
        config = LLMConfig(
            api_key="test-key", base_url="https://api.test.com",
            calibrate_model="test-model", summary_model="test-model",
            concurrent_workers=4,
        )
        processor = PlainTextProcessor(
            config=config,
            llm_client=fake_client,
            key_info_extractor=None,
            quality_validator=None,
        )

        segments = [f"segment number {i} original text" for i in range(6)]

        with usage_context.set_context(task_id="task-plain", stage="calibration"):
            processor._calibrate_segments(
                segments=segments,
                key_info=_NoOpKeyInfo(),
                title="t",
                description="d",
                selected_models=None,
            )

        assert len(fake_client.observed_contexts) == len(segments)
        for ctx in fake_client.observed_contexts:
            assert ctx == {"task_id": "task-plain", "stage": "calibration"}

    def test_naive_baseline_would_lose_context(self):
        """Sanity check: without task_id/stage set at all, the fallback context
        used inside worker threads is 'unknown' -- confirms the assertions
        above are meaningfully distinguishing propagated vs. not-propagated
        state, not just always-default values."""
        fake_client = _ContextRecordingLLMClient()
        config = LLMConfig(
            api_key="test-key", base_url="https://api.test.com",
            calibrate_model="test-model", summary_model="test-model",
            concurrent_workers=2,
        )
        processor = PlainTextProcessor(
            config=config,
            llm_client=fake_client,
            key_info_extractor=None,
            quality_validator=None,
        )

        segments = ["only segment original text"]
        # No usage_context.set_context() wrapping this call
        processor._calibrate_segments(
            segments=segments,
            key_info=_NoOpKeyInfo(),
            title="t",
            description="d",
            selected_models=None,
        )

        assert fake_client.observed_contexts == [{"task_id": "unknown", "stage": "unknown"}]


class TestSpeakerAwareProcessorPropagation:
    """SpeakerAwareProcessor._calibrate_chunks must propagate task_id/stage
    into each ThreadPoolExecutor worker thread."""

    def test_worker_threads_see_propagated_task_id_and_stage(self):
        fake_client = _ContextRecordingLLMClient(structured=True)
        config = LLMConfig(
            api_key="test-key", base_url="https://api.test.com",
            calibrate_model="test-model", summary_model="test-model",
            calibration_concurrent_limit=4, structured_validation_enabled=False,
        )
        processor = SpeakerAwareProcessor(
            config=config,
            llm_client=fake_client,
            key_info_extractor=None,
            speaker_inferencer=None,
            quality_validator=None,
        )

        # 5 chunks, each with exactly one dialog so a single correction (id=0)
        # yields full coverage and the closure returns without retrying.
        chunks = [
            [{"speaker": "S0", "text": f"dialog {i}", "start_time": "00:00:00"}]
            for i in range(5)
        ]

        with usage_context.set_context(task_id="task-speaker", stage="calibration"):
            processor._calibrate_chunks(
                chunks=chunks,
                original_chunks=chunks,
                key_info=_NoOpKeyInfo(),
                speaker_mapping={},
                title="t",
                description="d",
                selected_models=None,
            )

        assert len(fake_client.observed_contexts) == len(chunks)
        for ctx in fake_client.observed_contexts:
            assert ctx == {"task_id": "task-speaker", "stage": "calibration"}
