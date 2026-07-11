"""
usage_context contextvars propagation tests.

Covers:
- set_context()/get_context() basic nesting and restore-on-exit semantics.
- bind_task_id() one-shot thread-entry binding (used by llm_ops._handle_llm_task).
- ChatResult usage bridge (record_chat_result_usage/pop_chat_result_usage):
  write-then-read-once, and "never called" -> None.
- ThreadPoolExecutor propagation: contextvars do NOT automatically cross into
  worker threads (naive executor.submit control case), and DO propagate when
  the submitting code explicitly captures contextvars.copy_context() and runs
  the worker function through it -- this is the exact pattern required at the
  processors' executor.submit call sites (plain_text_processor.py,
  speaker_aware_processor.py).

All console output must be in English only (no emoji, no Chinese).
"""

import contextvars
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from video_transcript_api.llm.core import usage_context


@pytest.fixture(autouse=True)
def _clean_context():
    """Each test starts from a pristine default context."""
    yield
    # best-effort cleanup: nothing to reset explicitly since ContextVar defaults
    # apply per-thread/per-Context and tests run in the main thread each time.


class TestSetContextGetContext:
    def test_default_context_is_unknown(self):
        assert usage_context.get_context() == {"task_id": "unknown", "stage": "unknown"}

    def test_set_context_task_id_only(self):
        with usage_context.set_context(task_id="task-1"):
            assert usage_context.get_context() == {"task_id": "task-1", "stage": "unknown"}
        assert usage_context.get_context() == {"task_id": "unknown", "stage": "unknown"}

    def test_nested_set_context_preserves_outer_task_id(self):
        with usage_context.set_context(task_id="task-1"):
            with usage_context.set_context(stage="calibration"):
                assert usage_context.get_context() == {
                    "task_id": "task-1", "stage": "calibration",
                }
            # stage reverts, task_id still set by outer scope
            assert usage_context.get_context() == {"task_id": "task-1", "stage": "unknown"}
        assert usage_context.get_context() == {"task_id": "unknown", "stage": "unknown"}

    def test_set_context_restores_on_exception(self):
        with usage_context.set_context(task_id="task-1"):
            try:
                with usage_context.set_context(stage="calibration"):
                    raise ValueError("boom")
            except ValueError:
                pass
            assert usage_context.get_context() == {"task_id": "task-1", "stage": "unknown"}


class TestBindTaskId:
    def test_bind_task_id_sets_fresh_context(self):
        with usage_context.set_context(stage="leftover-stage"):
            usage_context.bind_task_id("task-fresh")
            # bind_task_id resets stage back to 'unknown', establishing a clean
            # per-task context at the thread entry point
            assert usage_context.get_context() == {
                "task_id": "task-fresh", "stage": "unknown",
            }

    def test_bind_task_id_none_defaults_to_unknown(self):
        usage_context.bind_task_id(None)
        assert usage_context.get_context()["task_id"] == "unknown"


class TestChatResultUsageBridge:
    def test_pop_without_record_returns_none(self):
        assert usage_context.pop_chat_result_usage() is None

    def test_record_then_pop_returns_snapshot(self):
        usage_context.record_chat_result_usage(
            model="m1", usage=_FakeUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        )
        snapshot = usage_context.pop_chat_result_usage()
        assert snapshot.model == "m1"
        assert snapshot.prompt_tokens == 10
        assert snapshot.completion_tokens == 5
        assert snapshot.total_tokens == 15
        assert snapshot.usage_missing is False

    def test_pop_clears_the_slot(self):
        usage_context.record_chat_result_usage(
            model="m1", usage=_FakeUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2)
        )
        usage_context.pop_chat_result_usage()
        assert usage_context.pop_chat_result_usage() is None

    def test_record_with_none_usage_flags_missing(self):
        usage_context.record_chat_result_usage(model="m2", usage=None)
        snapshot = usage_context.pop_chat_result_usage()
        assert snapshot.usage_missing is True
        assert snapshot.prompt_tokens is None
        assert snapshot.completion_tokens is None
        assert snapshot.total_tokens is None

    def test_last_write_wins_across_multiple_records(self):
        usage_context.record_chat_result_usage(
            model="attempt-1", usage=_FakeUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2)
        )
        usage_context.record_chat_result_usage(
            model="attempt-2", usage=_FakeUsage(prompt_tokens=9, completion_tokens=9, total_tokens=18)
        )
        snapshot = usage_context.pop_chat_result_usage()
        assert snapshot.model == "attempt-2"
        assert snapshot.total_tokens == 18


class _FakeUsage:
    """Stand-in for llm_compat.TokenUsage (duck-typed via getattr in usage_context)."""

    def __init__(self, prompt_tokens, completion_tokens, total_tokens):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens


class TestThreadPoolExecutorPropagation:
    """Demonstrates why executor.submit() call sites must use copy_context().run.

    This mirrors the exact pattern used (after the fix) in
    plain_text_processor.py::_calibrate_segments and
    speaker_aware_processor.py::_calibrate_chunks: a ThreadPoolExecutor
    processes several closures concurrently, each of which needs to see the
    task_id/stage set on the submitting (main) thread.
    """

    def test_naive_submit_does_not_propagate_context(self):
        """Control case: plain executor.submit(fn, ...) leaves worker threads
        with the default context -- this is the bug the fix addresses."""

        observed = []

        def worker():
            observed.append(usage_context.get_context())

        with usage_context.set_context(task_id="main-task", stage="calibration"):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(worker) for _ in range(3)]
                for f in as_completed(futures):
                    f.result()

        assert len(observed) == 3
        for ctx in observed:
            # worker threads never saw the main thread's context
            assert ctx == {"task_id": "unknown", "stage": "unknown"}

    def test_copy_context_run_propagates_context_to_worker_threads(self):
        """Fix pattern: executor.submit(contextvars.copy_context().run, fn, ...)
        correctly propagates task_id/stage into each worker thread."""

        observed = []

        def worker(index):
            observed.append((index, usage_context.get_context()))

        with usage_context.set_context(task_id="main-task", stage="calibration"):
            with ThreadPoolExecutor(max_workers=3) as executor:
                futures = [
                    executor.submit(contextvars.copy_context().run, worker, i)
                    for i in range(5)
                ]
                for f in as_completed(futures):
                    f.result()

        assert len(observed) == 5
        for _, ctx in observed:
            assert ctx == {"task_id": "main-task", "stage": "calibration"}

    def test_per_worker_stage_override_does_not_leak_across_threads(self):
        """Each worker thread narrowing its own stage (e.g. entering a
        validation sub-step) must not affect sibling worker threads or the
        main thread's context -- contextvars.Context copies are independent."""

        observed = {}

        def worker(index):
            with usage_context.set_context(stage=f"validation-{index}"):
                observed[index] = usage_context.get_context()["stage"]

        with usage_context.set_context(task_id="main-task", stage="calibration"):
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = [
                    executor.submit(contextvars.copy_context().run, worker, i)
                    for i in range(4)
                ]
                for f in as_completed(futures):
                    f.result()

            # main thread's own context is untouched by worker-thread overrides
            assert usage_context.get_context() == {
                "task_id": "main-task", "stage": "calibration",
            }

        assert observed == {0: "validation-0", 1: "validation-1", 2: "validation-2", 3: "validation-3"}
