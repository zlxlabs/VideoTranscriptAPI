"""
Regression test for the global usage_context reset fixture itself.

Root cause this guards against: video_transcript_api.llm.core.usage_context
.bind_task_id() is a one-shot bind by design -- it deliberately has no
paired reset (see its docstring), which is correct for production because
ThreadPoolExecutor worker threads re-call it at every task entry point,
overwriting whatever value the thread previously held.

Several integration tests (tests/integration/test_llm_stage_terminal_state.py,
test_layered_cache.py, test_llm_ops_status_backfill.py) call the production
entry point api/services/llm_ops.py::_handle_llm_task() directly and
synchronously in the pytest main thread instead of through a real worker
thread. That leaves a real task_id sitting in the module-level ContextVar
after those tests finish, which used to leak into whichever test pytest
happened to run next in the same process/thread -- a collection-order
dependent flake (see tests/unit/test_usage_context_propagation.py, which
expects a pristine 'unknown' default).

The fix is the autouse fixture `_reset_usage_context` in tests/conftest.py,
which resets usage_context's contextvar state before and after every test.

This file proves that fixture actually works, rather than relying on
"the previously-flaky tests happen to pass now" as indirect evidence. It
does so by deliberately reproducing the pollution scenario in test_a (mirrors
what _handle_llm_task()/bind_task_id() does) without any manual cleanup, then
asserting in test_b that the context is clean again. Since this project has
no pytest-randomly/xdist plugin installed, test execution order within a
single file is guaranteed to follow declaration order, so test_b reliably
runs immediately after test_a within the same pytest process/thread.

All console output must be in English only (no emoji, no Chinese).
"""

from video_transcript_api.llm.core import usage_context


def test_a_pollutes_context_like_handle_llm_task_does():
    """Simulates what llm_ops._handle_llm_task() does when called directly
    (synchronously, in the pytest main thread) by the integration tests:
    binds a real task_id via bind_task_id() and deliberately does NOT clean
    up afterwards -- exactly mirroring the real pollution scenario, since
    bind_task_id() has no paired reset by design."""
    usage_context.bind_task_id("task_deadbeefdeadbeefdeadbeefdeadbeef")

    # Sanity check: pollution actually happened (otherwise test_b passing
    # would prove nothing).
    assert usage_context.get_context() == {
        "task_id": "task_deadbeefdeadbeefdeadbeefdeadbeef",
        "stage": "unknown",
    }


def test_b_sees_clean_default_context_despite_prior_pollution():
    """Runs immediately after test_a in the same pytest process/thread. If
    the autouse fixture in tests/conftest.py were missing or broken, this
    would fail exactly the way the real bug did: get_context() would still
    return test_a's leftover task_id instead of the 'unknown' default."""
    assert usage_context.get_context() == {"task_id": "unknown", "stage": "unknown"}
