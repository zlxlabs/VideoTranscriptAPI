import asyncio
import concurrent.futures
import importlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _minimal_llm_config() -> dict:
    """Smallest llm section that satisfies LLMConfig.from_dict's hard
    subscript keys (api_key/base_url/calibrate_model/summary_model -- see
    llm/core/config.py). base_url deliberately points at a closed local port
    so nothing in this test file can ever reach the network even if a code
    path accidentally tried to use it."""
    return {
        "api_key": "test-llm-key",
        "base_url": "http://127.0.0.1:1/v1",
        "calibrate_model": "test-calibrate-model",
        "summary_model": "test-summary-model",
    }


def _minimal_config(tmp_path: Path) -> dict:
    return {
        "api": {"host": "127.0.0.1", "port": 8000, "auth_token": "test-token"},
        "concurrent": {"max_workers": 1, "queue_size": 2, "llm_max_workers": 1},
        "storage": {
            "cache_dir": str(tmp_path / "cache"),
            "workspace_dir": str(tmp_path / "workspace"),
            "temp_dir": str(tmp_path / "temp"),
            # Explicit (rather than relying on AuditLogger's default of
            # <project_root>/data/audit.db) so any test that runs a real
            # RuntimeContext.start() never writes into this worktree's data
            # directory.
            "audit_db": str(tmp_path / "audit.db"),
        },
        "web": {"base_url": "http://localhost:8000"},
        "llm": _minimal_llm_config(),
        # Explicit log file (rather than setup_logger's default ./logs/app.log)
        # so real RuntimeContext.__init__ never writes into the worktree.
        "log": {"file": str(tmp_path / "app.log")},
    }


def test_import_server_does_not_require_config(tmp_path):
    script = "import video_transcript_api.api.server; print('import-ok')"
    env = {
        "PATH": str(Path(sys.executable).parent),
        "PYTHONPATH": str(PROJECT_ROOT / "src"),
        "VTAPI_CONFIG": str(tmp_path / "missing.jsonc"),
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "import-ok" in result.stdout
    assert not (tmp_path / "data").exists()


def test_check_config_is_side_effect_free(tmp_path):
    config_path = tmp_path / "config.jsonc"
    config_path.write_text(json.dumps(_minimal_config(tmp_path)), encoding="utf-8")
    before_threads = threading.active_count()
    result = subprocess.run(
        [sys.executable, "main.py", "--check-config", "--config", str(config_path)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Configuration OK" in result.stdout
    assert not (tmp_path / "cache").exists()
    assert not (tmp_path / "workspace").exists()
    assert threading.active_count() == before_threads


def test_start_server_passes_one_validated_config_to_app(monkeypatch, tmp_path):
    from video_transcript_api.api import server

    config = _minimal_config(tmp_path)
    captured = {}
    sentinel_app = object()

    def create_app(*, config_loader):
        captured["runtime_config"] = config_loader()
        return sentinel_app

    monkeypatch.setattr(server, "load_and_validate_config", lambda: config)
    monkeypatch.setattr(server, "create_app", create_app)
    monkeypatch.setattr(
        server.uvicorn,
        "run",
        lambda app, **kwargs: captured.update(app=app, **kwargs),
    )

    server.start_server()

    assert captured["runtime_config"] is config
    assert captured["app"] is sentinel_app
    assert captured["host"] == config["api"]["host"]
    assert captured["port"] == config["api"]["port"]


def test_two_app_lifespans_own_and_close_separate_contexts(tmp_path):
    from video_transcript_api.api.app import create_app
    from video_transcript_api.api.context import RuntimeContext

    created = []

    class FakeRuntimeContext(RuntimeContext):
        def start(self):
            self.started = True
            created.append(self)

        def close(self):
            self.closed = True

    config = _minimal_config(tmp_path)
    app_one = create_app(
        config_loader=lambda: config,
        context_factory=FakeRuntimeContext,
        start_background=False,
    )
    app_two = create_app(
        config_loader=lambda: config,
        context_factory=FakeRuntimeContext,
        start_background=False,
    )

    with TestClient(app_one):
        with TestClient(app_two):
            assert app_one.state.runtime is created[0]
            assert app_two.state.runtime is created[1]
            assert created[0] is not created[1]
            assert all(context.started for context in created)
        assert created[1].closed is True
        assert not getattr(created[0], "closed", False)
    assert created[0].closed is True


def test_lifespan_start_failure_aborts_app_startup(tmp_path):
    """A RuntimeContext.start() failure (e.g. CacheManager._migrate_database
    or AuditLogger._init_database re-raising during a bad migration/config)
    must abort the FastAPI lifespan instead of being swallowed into a
    half-started app.

    app.py's lifespan wraps `runtime.start()` in a bare `try/finally` (no
    `except`), so the exception propagates through `_close_runtime_in_order`
    cleanup and out of the lifespan context manager -- TestClient(app)
    entering the app must raise it. Nothing pinned this contract before; a
    future change that adds a swallowing `except` around `runtime.start()`
    would leave the process serving requests with none of its real
    dependencies wired up, silently. This test fails loudly if that happens
    (verified by temporarily adding such a try/except locally: this test
    goes red, confirming it actually exercises the no-catch contract).
    """
    from video_transcript_api.api.app import create_app
    from video_transcript_api.api.context import RuntimeContext

    created = []

    class BoomingRuntimeContext(RuntimeContext):
        """Stands in for a RuntimeContext whose start() hits a fatal
        migration/config error -- mirrors CacheManager._migrate_database
        and AuditLogger._init_database, which both re-raise on failure."""

        def __init__(self, config):
            super().__init__(config)
            self.aclose_called = False

        def start(self):
            raise RuntimeError("simulated startup failure (e.g. failed DB migration)")

        async def aclose(self, deadline=None):
            self.aclose_called = True
            return await super().aclose(deadline)

    def context_factory(config):
        runtime = BoomingRuntimeContext(config)
        created.append(runtime)
        return runtime

    app = create_app(
        config_loader=lambda: _minimal_config(tmp_path),
        context_factory=context_factory,
        start_background=False,
    )

    with pytest.raises(RuntimeError, match="simulated startup failure"):
        with TestClient(app):
            pass

    assert len(created) == 1
    runtime = created[0]
    # start() raised before the real body ever set self.started = True --
    # the app never treated this as a live, usable runtime.
    assert runtime.started is False
    # Cleanup still ran on the half-started runtime instead of leaking it
    # (thread pools / queues never got created since start() raised first,
    # so there is nothing else to assert was released -- see
    # RuntimeContext._stop_workers's getattr-guarded attribute access).
    assert runtime.aclose_called is True
    assert runtime.closed is True


def test_request_task_binds_its_app_runtime(tmp_path):
    from video_transcript_api.api.app import create_app
    from video_transcript_api.api.context import RuntimeContext, get_runtime

    class FakeRuntimeContext(RuntimeContext):
        def start(self):
            self.started = True

        def close(self):
            self.closed = True

    app = create_app(
        config_loader=lambda: _minimal_config(tmp_path),
        context_factory=FakeRuntimeContext,
        start_background=False,
    )

    @app.get("/_runtime_owner")
    async def runtime_owner():
        return {"owned": get_runtime() is app.state.runtime}

    with TestClient(app) as client:
        assert client.get("/_runtime_owner").json() == {"owned": True}


def test_runtime_close_guarantees_llm_stop_signal_when_queue_is_full(tmp_path):
    from video_transcript_api.api.context import RuntimeContext

    class FullQueue:
        def __init__(self):
            self.stop_sent = False

        def put_nowait(self, value):
            raise queue.Full

        def put(self, value, timeout=None):
            assert value is None
            self.stop_sent = True

    class ConsumerThread:
        def __init__(self, work_queue):
            self.work_queue = work_queue
            self.joined = False

        def is_alive(self):
            return not self.work_queue.stop_sent

        def join(self, timeout=None):
            self.joined = True

    runtime = RuntimeContext(_minimal_config(tmp_path))
    runtime.llm_queue = FullQueue()
    runtime.llm_thread = ConsumerThread(runtime.llm_queue)

    runtime.close()

    assert runtime.llm_stop_event.is_set()
    assert runtime.llm_thread.joined is True


def test_runtime_close_is_bounded_when_llm_consumer_does_not_stop(tmp_path):
    from video_transcript_api.api.context import RuntimeContext

    class FullQueue:
        def put_nowait(self, value):
            raise queue.Full

    class StuckThread:
        def __init__(self):
            self.join_timeout = None

        def is_alive(self):
            return True

        def join(self, timeout=None):
            self.join_timeout = timeout

    runtime = RuntimeContext(_minimal_config(tmp_path))
    runtime.llm_queue = FullQueue()
    runtime.llm_thread = StuckThread()

    runtime.close()

    assert runtime.llm_stop_event.is_set()
    # N1（本地 codex review 第 11 轮）：join timeout 不再是硬编码的字面量
    # 5，而是 close() 入口计算的单一 deadline 减去到这一步已经流逝的
    # 墙钟时间算出的剩余预算——不能再要求精确等于 5，允许极小的流逝误差
    # （其余阶段的 wait_for 在空 worker_futures 上都会立即返回，实测流逝
    # 远小于 0.1s，留了充足裕量）。
    assert runtime.llm_thread.join_timeout == pytest.approx(5, abs=0.5)
    assert runtime.llm_thread.join_timeout <= 5


def test_runtime_close_does_not_wait_forever_for_executor_jobs(tmp_path):
    from unittest.mock import MagicMock
    from video_transcript_api.api.context import RuntimeContext

    runtime = RuntimeContext(_minimal_config(tmp_path))
    runtime.executor = MagicMock()
    runtime.llm_executor = MagicMock()

    runtime.close()

    runtime.executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)
    runtime.llm_executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)


def test_runtime_aclose_awaits_background_task_cancellation(tmp_path):
    from video_transcript_api.api.context import RuntimeContext

    async def scenario():
        runtime = RuntimeContext(_minimal_config(tmp_path))
        cancelled = asyncio.Event()

        async def background():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        task = asyncio.create_task(background())
        runtime.background_tasks.append(task)
        await asyncio.sleep(0)
        await runtime.aclose()
        assert cancelled.is_set()
        assert task.done()

    asyncio.run(scenario())


def test_aclose_waits_for_in_flight_maintenance_work_before_draining(tmp_path):
    """Codex review worry (乙1): the periodic maintenance background task
    might still be executing a blocking DB call (e.g. repair_task_snapshots)
    at the exact moment aclose()'s "关闭清算" step
    (_drain_non_terminal_tasks_on_shutdown) runs, racing with it over shared
    task rows/locks.

    Verified TRUE via a reproduction (see the fix in run_maintenance's
    docstring): _periodic_maintenance previously ran its blocking calls
    through bare `asyncio.to_thread()`. Cancelling the Task awaiting that
    call marks the *wrapping* asyncio.Future as CANCELLED immediately
    (asyncio.Future.cancel() always succeeds while PENDING -- unlike
    concurrent.futures.Future.cancel(), it does not check whether the
    underlying callable already started running), so `await asyncio.gather(
    *self.background_tasks, ...)` inside aclose() returned while the
    blocking callable was still executing on its own thread, letting the
    drain step run concurrently with it.

    Fixed by routing maintenance work through RuntimeContext.run_maintenance,
    which submits to a dedicated executor and tracks the raw
    concurrent.futures.Future via the same track_future/worker_futures
    mechanism transcription/llm workers already use -- _stop_workers waits
    for that future's real completion (via its add_done_callback, immune to
    asyncio-level cancellation) before _finish_close's drain step runs. This
    test pins that ordering.
    """
    import concurrent.futures

    from video_transcript_api.api.context import RuntimeContext

    async def scenario():
        runtime = RuntimeContext(_minimal_config(tmp_path))
        runtime.maintenance_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="maintenance-test"
        )
        order = []
        started = threading.Event()
        release = threading.Event()

        def blocking_work():
            started.set()
            release.wait(timeout=5)
            order.append("blocking-work-finished")

        async def maintenance_like_task():
            await runtime.run_maintenance(blocking_work)

        task = asyncio.create_task(maintenance_like_task())
        runtime.background_tasks.append(task)

        # Wait until blocking_work has actually started running on the
        # executor thread (not merely been submitted) -- otherwise cancel()
        # could remove the not-yet-started callable from the executor queue
        # before it ever runs, which would not exercise the "already
        # running" branch this test targets.
        while not started.is_set():
            await asyncio.sleep(0)

        original_drain = runtime._drain_non_terminal_tasks_on_shutdown

        # N1（本地 codex review 第 11 轮）：_finish_close 现在显式传入
        # deadline 参数调用 _drain_non_terminal_tasks_on_shutdown，spy 必须
        # 接住并透传这个位置参数，否则会被 TypeError 拦下。
        def spy_drain(*args, **kwargs):
            order.append("drain-called")
            return original_drain(*args, **kwargs)

        runtime._drain_non_terminal_tasks_on_shutdown = spy_drain

        async def release_soon():
            await asyncio.sleep(0.05)
            release.set()

        resources_safe, _ = await asyncio.gather(runtime.aclose(), release_soon())

        assert order == ["blocking-work-finished", "drain-called"], order
        assert resources_safe is True

    asyncio.run(scenario())


def test_worker_runtime_binding_is_scoped_to_its_owner(tmp_path):
    from video_transcript_api.api.context import RuntimeContext, get_runtime, run_with_runtime

    runtime_one = RuntimeContext(_minimal_config(tmp_path / "one"))
    runtime_two = RuntimeContext(_minimal_config(tmp_path / "two"))
    observed = []

    threads = [
        threading.Thread(target=run_with_runtime, args=(runtime_one, lambda: observed.append(get_runtime()))),
        threading.Thread(target=run_with_runtime, args=(runtime_two, lambda: observed.append(get_runtime()))),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert set(observed) == {runtime_one, runtime_two}


def test_task_queue_normalizes_explicit_null_notification_webhooks(monkeypatch):
    from video_transcript_api.api.services import transcription

    observed = []
    work_queue = asyncio.Queue()

    class Cache:
        def update_task_status(self, *args, **kwargs):
            pass

    class Runtime:
        def track_future(self, future):
            pass

    class ImmediateExecutor:
        def submit(self, function, *args):
            future = concurrent.futures.Future()
            try:
                future.set_result(function(*args))
            except Exception as exc:
                future.set_exception(exc)
            return future

    monkeypatch.setattr(transcription, "task_queue", work_queue)
    monkeypatch.setattr(transcription, "cache_manager", Cache())
    monkeypatch.setattr(transcription, "executor", ImmediateExecutor())
    monkeypatch.setattr(transcription, "get_runtime", lambda: Runtime())
    monkeypatch.setattr(
        transcription,
        "process_transcription",
        lambda *args, **kwargs: observed.append(kwargs["notification_webhooks"]),
    )

    async def scenario():
        processor = asyncio.create_task(transcription.process_task_queue())
        await work_queue.put({
            "id": "task-null-webhooks",
            "url": "https://example.com/video",
            "notification_webhooks": None,
        })
        await work_queue.join()
        processor.cancel()
        with pytest.raises(asyncio.CancelledError):
            await processor

    asyncio.run(scenario())
    assert observed == [{}]


def test_runtime_workers_close_before_notification_clients(monkeypatch):
    app_module = importlib.import_module("video_transcript_api.api.app")

    events = []

    class Runtime:
        def new_shutdown_deadline(self):
            return time.monotonic() + 5.0

        async def aclose(self, deadline=None):
            events.append("workers-closed")

    async def stop_background_owners(deadline):
        events.append("background-stopped")

    monkeypatch.setattr(
        app_module,
        "shutdown_all_notifiers",
        lambda: events.append("notifiers-closed"),
    )

    asyncio.run(
        app_module._close_runtime_in_order(
            Runtime(),
            stop_background_owners,
            close_notifiers=True,
        )
    )

    assert events == [
        "background-stopped",
        "workers-closed",
        "notifiers-closed",
    ]


def test_notification_clients_remain_open_when_workers_time_out(monkeypatch):
    app_module = importlib.import_module("video_transcript_api.api.app")

    events = []

    class Runtime:
        def new_shutdown_deadline(self):
            return time.monotonic() + 5.0

        async def aclose(self, deadline=None):
            events.append("workers-timed-out")
            return False

    monkeypatch.setattr(
        app_module,
        "shutdown_all_notifiers",
        lambda: events.append("notifiers-closed"),
    )

    asyncio.run(
        app_module._close_runtime_in_order(Runtime(), close_notifiers=True)
    )

    assert events == ["workers-timed-out"]


def test_producer_timeout_keeps_llm_consumer_available(tmp_path):
    from unittest.mock import MagicMock
    from video_transcript_api.api.context import RuntimeContext

    class ProducerTimeoutCondition:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def wait_for(self, predicate, timeout=None):
            return False

    class LlmThread:
        def __init__(self):
            self.joined = False

        def is_alive(self):
            return True

        def join(self, timeout=None):
            self.joined = True

    runtime = RuntimeContext(_minimal_config(tmp_path))
    runtime.executor = MagicMock()
    runtime._worker_futures_condition = ProducerTimeoutCondition()
    runtime.worker_futures = {("transcription", object())}
    runtime.llm_thread = LlmThread()

    assert runtime._stop_workers() is False
    assert runtime.llm_stop_event.is_set() is False
    assert runtime.llm_thread.joined is False


class _ProducerStuckThenRealCondition:
    """Wraps a real threading.Condition so the *first* wait_for call (the
    producer/"transcription" kind check inside _stop_workers) returns False
    immediately without touching the real condition -- simulating a producer
    that never finishes within the timeout budget, without an actual 5s
    sleep. Every subsequent wait_for call (the maintenance/llm checks)
    delegates to the real condition, so genuine cross-thread synchronization
    (worker future completion notifications fired by track_future's
    add_done_callback) still works for those.
    """

    def __init__(self, real_condition):
        self._real = real_condition
        self._calls = 0

    def __enter__(self):
        return self._real.__enter__()

    def __exit__(self, *exc):
        return self._real.__exit__(*exc)

    def wait_for(self, predicate, timeout=None):
        self._calls += 1
        if self._calls == 1:
            return False
        return self._real.wait_for(predicate, timeout=timeout)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_producer_timeout_still_waits_for_in_flight_maintenance_before_drain(tmp_path):
    """K2 修复（本地 codex review 第 8 轮）：此前 producers_finished=False
    时 _stop_workers 直接 return False，跳过 maintenance executor 的
    shutdown + 有界等待——但 _finish_close 的关闭清算
    （_drain_non_terminal_tasks_on_shutdown）是无条件调用的（G2 修复），
    于是"清算必须等维护调用真正跑完再动手"这条不变式在 producer 超时路径
    上完全失效：清算可能与仍在线程里真实执行的维护调用并发，重新踩中
    run_maintenance 文档描述的竞态（test_aclose_waits_for_in_flight_
    maintenance_work_before_draining 覆盖的是 producer 正常结束的路径，
    这里补上 producer 也同时超时的路径）。

    用 _ProducerStuckThenRealCondition 同时模拟两件事：
    1. producer（"transcription" kind）"超时"——第一次 wait_for 调用立即
       返回 False，不做真实等待。
    2. maintenance 调用是真实提交到 maintenance_executor 的阻塞调用，在
       独立线程里运行，通过 threading.Event 手动控制何时"跑完"。

    断言：即便 producer 超时，maintenance 的 shutdown+wait_for 仍然被
    执行，真实等到 blocking_work 跑完；关闭清算因此在 blocking_work 完成
    之后才被调用，不与它并发——顺序被锁死为
    ["blocking-work-finished", "drain-called"]。
    """
    import concurrent.futures

    from video_transcript_api.api.context import RuntimeContext

    runtime = RuntimeContext(_minimal_config(tmp_path))
    runtime.maintenance_executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="maintenance-test"
    )
    real_condition = threading.Condition()
    runtime._worker_futures_condition = _ProducerStuckThenRealCondition(real_condition)
    # Simulate a producer that never finishes in time: nothing ever removes
    # this entry from worker_futures.
    runtime.worker_futures = {("transcription", object())}

    order = []
    started = threading.Event()
    release = threading.Event()

    def blocking_work():
        started.set()
        release.wait(timeout=5)
        order.append("blocking-work-finished")

    future = runtime.maintenance_executor.submit(blocking_work)
    runtime.track_future(future, kind="maintenance")
    assert started.wait(timeout=2)

    original_drain = runtime._drain_non_terminal_tasks_on_shutdown

    # N1（本地 codex review 第 11 轮）：_finish_close 现在显式传入 deadline
    # 参数调用 _drain_non_terminal_tasks_on_shutdown，spy 必须接住并透传
    # 这个位置参数，否则会被 TypeError 拦下。
    def spy_drain(*args, **kwargs):
        order.append("drain-called")
        return original_drain(*args, **kwargs)

    runtime._drain_non_terminal_tasks_on_shutdown = spy_drain

    def release_soon():
        time.sleep(0.05)
        release.set()

    releaser = threading.Thread(target=release_soon)
    releaser.start()
    try:
        resources_safe = runtime.close()
    finally:
        releaser.join()

    assert order == ["blocking-work-finished", "drain-called"], order
    # Producer 超时了，整体结果仍然不安全……
    assert resources_safe is False
    # ……但 maintenance 已经真正确认停止，所以清算没有被跳过。
    assert runtime._maintenance_confirmed_stopped is True


def test_repeated_close_preserves_unsafe_timeout_result(tmp_path, monkeypatch):
    from video_transcript_api.api.context import RuntimeContext

    runtime = RuntimeContext(_minimal_config(tmp_path))
    # N1（本地 codex review 第 11 轮）：close() 现在显式传入 deadline 位置
    # 参数调用 _stop_workers，monkeypatch 的替身必须接住这个参数。
    monkeypatch.setattr(runtime, "_stop_workers", lambda *args, **kwargs: False)

    assert runtime.close() is False
    assert runtime.close() is False


def test_task_lock_keeps_one_lock_while_waiters_exist():
    from video_transcript_api.api import context

    context._task_locks.clear()
    context._task_lock_refcounts.clear()
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    release_second = threading.Event()
    third_entered = threading.Event()

    def first():
        with context.task_lock("same-task"):
            first_entered.set()
            release_first.wait(timeout=2)

    def second():
        with context.task_lock("same-task"):
            second_entered.set()
            release_second.wait(timeout=2)

    def third():
        with context.task_lock("same-task"):
            third_entered.set()

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    threads[0].start()
    assert first_entered.wait(timeout=1)
    threads[1].start()
    deadline = time.monotonic() + 1
    while context._task_lock_refcounts.get("same-task") != 2:
        assert time.monotonic() < deadline
        time.sleep(0.005)

    release_first.set()
    assert second_entered.wait(timeout=1)
    third_thread = threading.Thread(target=third)
    third_thread.start()
    assert not third_entered.wait(timeout=0.05)
    release_second.set()

    for thread in [*threads, third_thread]:
        thread.join(timeout=1)
        assert not thread.is_alive()
    assert third_entered.is_set()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda config: config.pop("api"), "api"),
        (lambda config: config["api"].update(port=70000), "api.port"),
        (lambda config: config["concurrent"].update(max_workers=0), "concurrent.max_workers"),
        (lambda config: config["storage"].update(cache_dir=""), "storage.cache_dir"),
    ],
)
def test_validate_config_rejects_invalid_structure(tmp_path, mutation, message):
    from video_transcript_api.api.context import ConfigError, validate_config

    config = _minimal_config(tmp_path)
    mutation(config)
    with pytest.raises(ConfigError, match=message.replace(".", r"\.")):
        validate_config(config)


@pytest.mark.parametrize(
    ("storage_values", "message"),
    [
        ({"cache_retention_days": "7"}, "storage.cache_retention_days"),
        ({"task_status_retention_days": -1}, "storage.task_status_retention_days"),
        ({"audit_log_retention_days": True}, "storage.audit_log_retention_days"),
        ({"temp_retention_hours": "24"}, "storage.temp_retention_hours"),
    ],
)
def test_validate_config_rejects_invalid_maintenance_values(
    tmp_path, storage_values, message
):
    from video_transcript_api.api.context import ConfigError, validate_config

    config = _minimal_config(tmp_path)
    config["storage"].update(storage_values)
    with pytest.raises(ConfigError, match=message.replace(".", r"\.")):
        validate_config(config)


@pytest.mark.parametrize("enabled", [1, 0, "true", None])
def test_validate_config_requires_boolean_backend_enabled(tmp_path, enabled):
    from video_transcript_api.api.context import ConfigError, validate_config

    config = _minimal_config(tmp_path)
    config["funasr"] = {"enabled": enabled, "server_url": "http://asr"}
    with pytest.raises(ConfigError, match=r"funasr\.enabled"):
        validate_config(config)


@pytest.mark.parametrize(
    ("section", "extra_key"),
    [
        ("concurrent", "max_worker"),
        ("storage", "cache_retention_day"),
        ("tikhub", "api_keey"),
    ],
)
def test_validate_config_rejects_unknown_lifecycle_keys(
    tmp_path, section, extra_key
):
    from video_transcript_api.api.context import ConfigError, validate_config

    config = _minimal_config(tmp_path)
    config.setdefault(section, {})[extra_key] = "typo"
    with pytest.raises(ConfigError, match=rf"{section}\.{extra_key}"):
        validate_config(config)


def test_validate_config_requires_boolean_risk_control_enabled(tmp_path):
    from video_transcript_api.api.context import ConfigError, validate_config

    config = _minimal_config(tmp_path)
    config["risk_control"] = {"enabled": "false"}
    with pytest.raises(ConfigError, match=r"risk_control\.enabled"):
        validate_config(config)


@pytest.mark.parametrize(
    "auth_token",
    [
        "test token",  # 内部空格
        " test-token",  # 前导空格
        "test-token ",  # 尾随空格
        "test\ttoken",  # tab
        "test\ntoken",  # 换行
    ],
)
def test_validate_config_rejects_auth_token_with_whitespace(tmp_path, auth_token):
    """真实鉴权 transcription.py::verify_token 用
    `authorization.split()`（按任意空白切分）要求 `Bearer <token>` 恰好
    两段；token 本身含空白会让这个请求永远无法通过恰好两段的切分，legacy
    单 token 模式因此被永久锁死，且预检此前对此完全没有察觉（只查
    strip 后非空）。这里必须在 --check-config 阶段就拒绝。"""
    from video_transcript_api.api.context import ConfigError, validate_config

    config = _minimal_config(tmp_path)
    config["api"]["auth_token"] = auth_token
    with pytest.raises(ConfigError, match=r"api\.auth_token") as exc_info:
        validate_config(config)
    # 错误信息指名字段，但不得回显 token 值本身
    assert auth_token not in str(exc_info.value)


def test_validate_config_accepts_auth_token_without_whitespace(tmp_path):
    """健全性检查：不含空白的正常 token 不受影响。"""
    from video_transcript_api.api.context import validate_config

    config = _minimal_config(tmp_path)
    config["api"]["auth_token"] = "perfectly-normal-token_123"
    validated = validate_config(config)
    assert validated["api"]["auth_token"] == "perfectly-normal-token_123"


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("storage", "audit_db", []),
        ("storage", "max_download_size_mb", "large"),
        ("tikhub", "max_retries", "3"),
        ("tikhub", "timeout", []),
        ("capswriter", "server_url", 123),
        ("capswriter", "connection_timeout", []),
        ("capswriter", "enable_hot_words", "true"),
    ],
)
def test_validate_config_rejects_invalid_known_optional_values(
    tmp_path, section, field, value
):
    from video_transcript_api.api.context import ConfigError, validate_config

    config = _minimal_config(tmp_path)
    config.setdefault(section, {})[field] = value
    with pytest.raises(ConfigError, match=rf"{section}\.{field}"):
        validate_config(config)


def test_validate_config_rejects_missing_llm_section(tmp_path):
    """RuntimeContext.start() unconditionally constructs LLMCoordinator, and
    LLMConfig.from_dict() reads llm.api_key/base_url/calibrate_model/
    summary_model via hard dict subscripts with no `.get` default. Before
    this check, a config missing the llm section entirely passed
    --check-config clean and only blew up with a bare KeyError inside a
    real lifespan's RuntimeContext.start()."""
    from video_transcript_api.api.context import ConfigError, validate_config

    config = _minimal_config(tmp_path)
    del config["llm"]
    with pytest.raises(ConfigError, match=r"llm"):
        validate_config(config)


def test_validate_config_accepts_minimal_valid_llm_section(tmp_path):
    """Sanity check for the happy path: a config carrying exactly the four
    hard-required llm keys (nothing else) must pass validate_config."""
    from video_transcript_api.api.context import validate_config

    config = _minimal_config(tmp_path)
    validated = validate_config(config)
    assert validated["llm"] == _minimal_llm_config()


@pytest.mark.parametrize(
    "field", ["api_key", "base_url", "calibrate_model", "summary_model"]
)
def test_validate_config_rejects_llm_section_missing_required_field(
    tmp_path, field
):
    """Every key LLMConfig.from_dict reads via `llm_config["key"]` (no
    default) must be required by validate_config too -- otherwise
    --check-config stays green while a real boot KeyErrors on that exact
    key. This is the full set confirmed by reading llm/core/config.py
    from_dict end to end: all other from_dict keys go through `.get(...)`
    with a default (some default to calibrate_model itself, so they add no
    additional hard requirement beyond calibrate_model already being
    required here)."""
    from video_transcript_api.api.context import ConfigError, validate_config

    config = _minimal_config(tmp_path)
    del config["llm"][field]
    with pytest.raises(ConfigError, match=rf"llm\.{field}"):
        validate_config(config)


@pytest.mark.parametrize(
    "field", ["api_key", "base_url", "calibrate_model", "summary_model"]
)
@pytest.mark.parametrize("value", ["", "   ", 123, None, [], {}])
def test_validate_config_rejects_llm_section_invalid_required_field_values(
    tmp_path, field, value
):
    from video_transcript_api.api.context import ConfigError, validate_config

    config = _minimal_config(tmp_path)
    config["llm"][field] = value
    with pytest.raises(ConfigError, match=rf"llm\.{field}"):
        validate_config(config)


def test_validate_config_llm_section_agrees_with_llm_config_from_dict(tmp_path):
    """Consistency guard tying the two independent readers of the llm
    section together: whatever validate_config accepts, LLMConfig.from_dict
    must be able to consume without raising KeyError (and vice versa --
    anything from_dict would KeyError on, validate_config must reject
    first). This is the fallback invariant asked for even though the real
    create_app boot tests below already exercise this end to end."""
    from video_transcript_api.api.context import validate_config
    from video_transcript_api.llm.core.config import LLMConfig

    config = _minimal_config(tmp_path)
    validate_config(config)
    # Must not raise KeyError once validate_config has approved the config.
    llm_config = LLMConfig.from_dict(config)
    assert llm_config.api_key == config["llm"]["api_key"]
    assert llm_config.base_url == config["llm"]["base_url"]
    assert llm_config.calibrate_model == config["llm"]["calibrate_model"]
    assert llm_config.summary_model == config["llm"]["summary_model"]


# --- llm nested subsections: same "green preflight, boot-fatal" gap class --
#
# validate_config previously only checked the four hard llm.* string keys.
# LLMConfig.from_dict (llm/core/config.py) also does two more things that can
# raise on a config validate_config used to wave through:
#   1. `total_timeout=float(llm_config.get("total_timeout", 300.0))` -- a
#      non-numeric value (e.g. a string) raises ValueError.
#   2. `llm_config.get("segmentation", {})` / `"structured_calibration"` /
#      `"speaker_inference"` / `"quality_validation"` are immediately used as
#      dicts (`.get(...)` called on the result a few lines later). If the
#      config author put a string/list/int there instead of an object, that
#      `.get()` call raises AttributeError the moment a real lifespan calls
#      RuntimeContext.start() -- --check-config stayed green because nothing
#      there ever constructs LLMConfig.


def test_validate_config_rejects_non_numeric_llm_total_timeout(tmp_path):
    from video_transcript_api.api.context import ConfigError, validate_config

    config = _minimal_config(tmp_path)
    config["llm"]["total_timeout"] = "not-a-number"
    with pytest.raises(ConfigError, match=r"llm\.total_timeout"):
        validate_config(config)


@pytest.mark.parametrize("value", [120, 120.5])
def test_validate_config_accepts_numeric_llm_total_timeout(tmp_path, value):
    from video_transcript_api.api.context import validate_config

    config = _minimal_config(tmp_path)
    config["llm"]["total_timeout"] = value
    validated = validate_config(config)
    assert validated["llm"]["total_timeout"] == value


@pytest.mark.parametrize(
    "section",
    ["segmentation", "structured_calibration", "speaker_inference", "quality_validation"],
)
@pytest.mark.parametrize("bad_value", ["not-an-object", ["also", "not"], 42])
def test_validate_config_rejects_non_dict_llm_nested_section(tmp_path, section, bad_value):
    """Each of these sections is fed straight into `.get(...)` calls inside
    LLMConfig.from_dict without ever checking it is a dict first."""
    from video_transcript_api.api.context import ConfigError, validate_config

    config = _minimal_config(tmp_path)
    config["llm"][section] = bad_value
    with pytest.raises(ConfigError, match=rf"llm\.{section}"):
        validate_config(config)


@pytest.mark.parametrize(
    "section",
    ["segmentation", "structured_calibration", "speaker_inference", "quality_validation"],
)
def test_validate_config_accepts_empty_or_absent_llm_nested_section(tmp_path, section):
    """Absent nested sections must keep passing (from_dict's `.get(key, {})`
    default already covers this); an explicit empty object is equally
    valid."""
    from video_transcript_api.api.context import validate_config

    config = _minimal_config(tmp_path)
    config["llm"][section] = {}
    validate_config(config)

    config_without = _minimal_config(tmp_path)
    assert section not in config_without["llm"]
    validate_config(config_without)


def test_validate_config_llm_nested_sections_agree_with_llm_config_from_dict(tmp_path):
    """Same consistency invariant as
    test_validate_config_llm_section_agrees_with_llm_config_from_dict, now
    covering the nested subsections too: a handful of boundary configs that
    validate_config accepts must all be consumable by LLMConfig.from_dict
    without raising."""
    from video_transcript_api.api.context import validate_config
    from video_transcript_api.llm.core.config import LLMConfig

    variants = [
        {},
        {"total_timeout": 42},
        {"total_timeout": 42.5},
        {"segmentation": {}},
        {"segmentation": {"segment_size": 1000}},
        {"structured_calibration": {}},
        {"speaker_inference": {}},
        {"quality_validation": {}},
        {
            "segmentation": {"enable_threshold": 100},
            "structured_calibration": {"min_chunk_length": 10},
            "speaker_inference": {"samples_per_speaker": 5},
            "quality_validation": {},
            "total_timeout": 60,
        },
    ]
    for overrides in variants:
        config = _minimal_config(tmp_path)
        config["llm"].update(overrides)
        validate_config(config)
        # Must not raise AttributeError/ValueError once validate_config has
        # approved the config.
        LLMConfig.from_dict(config)


# --- rehearsal layer: validate_config's explicit checks above only look one
# level into llm's nested sections (is `segmentation` itself a dict? is
# `quality_validation` itself a dict?) and never read the `ytdlp` section at
# all. LLMConfig.from_dict / YtdlpConfigBuilder go one level deeper and crash
# on shapes validate_config waves through (llm.quality_validation
# .quality_threshold, llm.segmentation.quality_validation,
# llm.provider_patterns, ytdlp itself, ytdlp.youtube_cookie). Rather than
# keep adding hand-written field checks to validate_config for each new case
# Codex finds, load_and_validate_config now replays the real parsers
# (_rehearse_llm_config / _rehearse_ytdlp_config in api/context.py) so
# --check-config catches the whole class, including ones nobody has found
# yet, before a real lifespan does.


def _inject_quality_threshold_string(llm: dict) -> None:
    llm["quality_validation"] = {"quality_threshold": "not-an-object"}


def _inject_segmentation_quality_validation_non_object(llm: dict) -> None:
    llm["segmentation"] = {"quality_validation": ["not", "an", "object"]}


def _inject_provider_patterns_non_object(llm: dict) -> None:
    llm["provider_patterns"] = "not-an-object"


@pytest.mark.parametrize(
    "inject",
    [
        _inject_quality_threshold_string,
        _inject_segmentation_quality_validation_non_object,
        _inject_provider_patterns_non_object,
    ],
    ids=[
        "quality_validation.quality_threshold-string",
        "segmentation.quality_validation-list",
        "provider_patterns-string",
    ],
)
def test_rehearse_llm_config_rejects_deep_shape_errors_validate_config_misses(
    tmp_path, inject
):
    """Codex-reported gap: validate_config only checks that
    llm.segmentation/quality_validation/etc are themselves dicts, not what's
    inside them. All three cases here sail through validate_config unchanged
    -- only the from_dict/provider_patterns rehearsal catches them."""
    from video_transcript_api.api.context import (
        ConfigError,
        _rehearse_llm_config,
        validate_config,
    )

    config = _minimal_config(tmp_path)
    inject(config["llm"])

    # Confirms the gap is real: validate_config's own explicit checks accept
    # this config unchanged.
    validate_config(config)

    with pytest.raises(ConfigError):
        _rehearse_llm_config(config)


def test_rehearse_llm_config_accepts_the_minimal_valid_config(tmp_path):
    from video_transcript_api.api.context import _rehearse_llm_config

    _rehearse_llm_config(_minimal_config(tmp_path))  # must not raise


def test_rehearse_ytdlp_config_accepts_absent_or_valid_section(tmp_path):
    from video_transcript_api.api.context import _rehearse_ytdlp_config

    config = _minimal_config(tmp_path)
    _rehearse_ytdlp_config(config)  # no ytdlp section at all: must not raise

    config["ytdlp"] = {"youtube_cookie": {"enabled": False}}
    _rehearse_ytdlp_config(config)  # valid, disabled cookie: must not raise


@pytest.mark.parametrize(
    "inject",
    [
        lambda cfg: cfg.update({"ytdlp": "bad"}),
        lambda cfg: cfg.update({"ytdlp": {"youtube_cookie": ["not", "an", "object"]}}),
    ],
    ids=["ytdlp-string", "ytdlp.youtube_cookie-list"],
)
def test_rehearse_ytdlp_config_rejects_shapes_validate_config_never_looks_at(
    tmp_path, inject
):
    """Codex-reported gap: validate_config never reads the ytdlp section at
    all, so app.py's unconditional `YtdlpConfigBuilder(config)
    .validate_cookie_on_startup()` at real boot is the only thing that would
    have caught this -- until this rehearsal layer moved that check into
    --check-config too."""
    from video_transcript_api.api.context import (
        ConfigError,
        _rehearse_ytdlp_config,
        validate_config,
    )

    config = _minimal_config(tmp_path)
    inject(config)

    # Confirms the gap is real: validate_config doesn't look at ytdlp at all.
    validate_config(config)

    with pytest.raises(ConfigError):
        _rehearse_ytdlp_config(config)


def test_load_and_validate_config_runs_both_rehearsals_end_to_end(tmp_path):
    """Full --check-config path (load_and_validate_config, not the lower-
    level helpers directly): a malformed ytdlp section must fail
    --check-config, not just a real lifespan."""
    from video_transcript_api.api.context import ConfigError, load_and_validate_config

    config = _minimal_config(tmp_path)
    config["ytdlp"] = "bad"
    config_path = tmp_path / "config.jsonc"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(ConfigError, match="ytdlp"):
        load_and_validate_config(str(config_path))


# --- rehearsal layer, round 2 (local Codex review): three more startup-time
# parsers identified as still unrehearsed -- set_default_config's own
# `timeout` fallback numeric conversion (llm/llm.py), setup_logger's log
# section type coercions (utils/logging/logger.py), and the wechat/feishu
# shape reads inside init_all_notifiers() (utils/notifications). All three
# are invoked unconditionally at real boot with no --check-config coverage
# before this round.


def test_rehearse_set_default_config_types_rejects_non_numeric_timeout_fallback(tmp_path):
    """set_default_config computes total_timeout via `float(llm_cfg.get(
    "total_timeout", llm_cfg.get("timeout", DEFAULT_LLM_TIMEOUT)))` -- a
    second, independent total_timeout source (the legacy `timeout` key) that
    neither validate_config's explicit check nor _rehearse_llm_config's
    LLMConfig.from_dict replay ever reads (from_dict only looks at
    `total_timeout`). A config with only `llm.timeout` set to a non-numeric
    value sails through both unchanged and only crashes for real once
    set_default_config runs at boot."""
    from video_transcript_api.api.context import (
        ConfigError,
        _rehearse_llm_config,
        _rehearse_set_default_config_types,
        validate_config,
    )

    config = _minimal_config(tmp_path)
    config["llm"]["timeout"] = "not-a-number"

    # Confirms the gap is real: neither existing check rejects this.
    validate_config(config)
    _rehearse_llm_config(config)

    with pytest.raises(ConfigError):
        _rehearse_set_default_config_types(config)


def test_rehearse_set_default_config_types_accepts_the_minimal_valid_config(tmp_path):
    from video_transcript_api.api.context import _rehearse_set_default_config_types

    _rehearse_set_default_config_types(_minimal_config(tmp_path))  # must not raise

    config = _minimal_config(tmp_path)
    config["llm"]["timeout"] = 42
    _rehearse_set_default_config_types(config)  # numeric fallback: must not raise


# --- rehearsal layer, round 4 (local Codex review): _rehearse_set_default_
# config_types only replayed the total_timeout float() conversion, but
# set_default_config's SyncLLMClient(...) construction (llm/llm.py) also
# hands two more raw config values straight to llm_compat's BaseClient
# .__init__, which consumes them immediately (not lazily at chat() time):
#   - refusal_keywords_url: a truthy non-str/non-iterable value (e.g. an int
#     or bool accidentally left in config) makes `list(refusal_keywords_url)`
#     raise TypeError immediately in BaseClient.__init__.
#   - collector_url: a truthy non-str value makes CollectorClient.__init__'s
#     `url.rstrip("/")` raise AttributeError immediately.
# Both were previously invisible to --check-config (which never constructs
# SyncLLMClient to avoid the real network calls get_cached_keywords() makes)
# and only surfaced the first time a real lifespan called set_default_config.


def test_rehearse_set_default_config_types_rejects_non_iterable_refusal_keywords_url(
    tmp_path,
):
    """Confirms the gap is real: SyncLLMClient's BaseClient.__init__ does
    `list(refusal_keywords_url)` when the value is truthy and not a str,
    which raises TypeError immediately for a scalar like an int or bool --
    verified directly against llm_compat.SyncLLMClient (no network reached,
    since the TypeError fires before get_cached_keywords() is ever called)."""
    from video_transcript_api.api.context import (
        ConfigError,
        _rehearse_set_default_config_types,
    )

    config = _minimal_config(tmp_path)
    config["llm"]["refusal_keywords_url"] = 123

    with pytest.raises(ConfigError):
        _rehearse_set_default_config_types(config)

    config2 = _minimal_config(tmp_path)
    config2["llm"]["refusal_keywords_url"] = True

    with pytest.raises(ConfigError):
        _rehearse_set_default_config_types(config2)


def test_rehearse_set_default_config_types_rejects_non_string_list_items_in_refusal_keywords_url(
    tmp_path,
):
    from video_transcript_api.api.context import (
        ConfigError,
        _rehearse_set_default_config_types,
    )

    config = _minimal_config(tmp_path)
    config["llm"]["refusal_keywords_url"] = ["http://ok", 1]

    with pytest.raises(ConfigError):
        _rehearse_set_default_config_types(config)


def test_rehearse_set_default_config_types_accepts_valid_refusal_keywords_url_shapes(
    tmp_path,
):
    from video_transcript_api.api.context import _rehearse_set_default_config_types

    config = _minimal_config(tmp_path)
    config["llm"]["refusal_keywords_url"] = None
    _rehearse_set_default_config_types(config)  # must not raise

    config["llm"]["refusal_keywords_url"] = "http://example.invalid/words"
    _rehearse_set_default_config_types(config)  # must not raise

    config["llm"]["refusal_keywords_url"] = ["http://a.invalid", "http://b.invalid"]
    _rehearse_set_default_config_types(config)  # must not raise


def test_rehearse_set_default_config_types_rejects_non_string_collector_url(tmp_path):
    """Confirms the gap is real: SyncLLMClient constructs a CollectorClient
    (llm_compat) whose __init__ does `url.rstrip("/")` immediately when
    collector_url is truthy -- an int or other non-str value raises
    AttributeError right away, before any network call is attempted."""
    from video_transcript_api.api.context import (
        ConfigError,
        _rehearse_set_default_config_types,
    )

    config = _minimal_config(tmp_path)
    config["llm"]["collector_url"] = 123

    with pytest.raises(ConfigError):
        _rehearse_set_default_config_types(config)


def test_rehearse_set_default_config_types_accepts_valid_collector_url_shapes(tmp_path):
    from video_transcript_api.api.context import _rehearse_set_default_config_types

    config = _minimal_config(tmp_path)
    config["llm"]["collector_url"] = None
    _rehearse_set_default_config_types(config)  # must not raise

    config["llm"]["collector_url"] = "http://collector.invalid"
    _rehearse_set_default_config_types(config)  # must not raise


def test_rehearse_log_config_rejects_non_string_level(tmp_path):
    """validate_config never reads the log section at all (accepted debt,
    see docs/sessions/260716-pr3x-gate/REVIEW-LOG.md); only the real
    setup_logger() call at boot -- or this rehearsal replay of its pure
    parsing slice -- catches a garbage-typed log.level."""
    from video_transcript_api.api.context import (
        ConfigError,
        _rehearse_log_config,
        validate_config,
    )

    config = _minimal_config(tmp_path)
    config["log"]["level"] = 123

    validate_config(config)  # confirms the gap: validate_config accepts it

    with pytest.raises(ConfigError):
        _rehearse_log_config(config)


def test_rehearse_log_config_accepts_the_minimal_valid_config(tmp_path):
    from video_transcript_api.api.context import _rehearse_log_config

    _rehearse_log_config(_minimal_config(tmp_path))  # must not raise


# --- rehearsal layer, round 4 (local Codex review): _parse_log_settings only
# checked that log.max_size/backup_count were *typed* as int|str, never that
# a string value was something loguru's own rotation/retention parser could
# actually make sense of. `logger.add(rotation=..., retention=...)` at real
# boot feeds string values through loguru's private `_string_parsers` module
# (parse_size for rotation, parse_duration for retention) and raises
# ValueError("Cannot parse rotation/retention from: ...") for garbage -- a
# gap this rehearsal replay didn't reproduce, so a typo'd max_size/
# backup_count string sailed through --check-config and only crashed the
# first time a real lifespan called setup_logger().


def test_loguru_string_parsers_private_api_is_importable():
    """Canary for the private-API dependency: _parse_log_settings degrades
    to lenient pass-through (see its docstring) if this import ever breaks
    on a future loguru upgrade -- silently weakening validation instead of
    failing loudly. This test pins the assumption so a broken import is
    caught by CI here first, not discovered by config validation quietly
    doing less than intended."""
    from loguru._string_parsers import parse_duration, parse_size

    assert callable(parse_size)
    assert callable(parse_duration)


def test_rehearse_log_config_rejects_unparseable_rotation_string(tmp_path):
    """Confirms the gap is real: a max_size string loguru's rotation parser
    cannot make sense of (no valid size/duration shape) passes the old
    isinstance-only check but crashes real logger.add()."""
    from video_transcript_api.api.context import (
        ConfigError,
        _rehearse_log_config,
        validate_config,
    )

    config = _minimal_config(tmp_path)
    config["log"]["max_size"] = "not-a-rotation-value"

    validate_config(config)  # confirms the gap: validate_config never looks

    with pytest.raises(ConfigError):
        _rehearse_log_config(config)


def test_rehearse_log_config_rejects_unparseable_retention_string(tmp_path):
    """backup_count as a bare numeric string (a natural typo for the int
    5, e.g. "5") is not a loguru retention duration -- real retention
    parsing only tries parse_duration for strings (no parse_size fallback,
    unlike rotation), so "5" has no unit and loguru raises. The old
    isinstance-only check let it through."""
    from video_transcript_api.api.context import (
        ConfigError,
        _rehearse_log_config,
        validate_config,
    )

    config = _minimal_config(tmp_path)
    config["log"]["backup_count"] = "5"

    validate_config(config)  # confirms the gap

    with pytest.raises(ConfigError):
        _rehearse_log_config(config)


def test_rehearse_log_config_rejects_size_string_as_retention(tmp_path):
    """A size-shaped string like "10 MB" is a valid *rotation* value but not
    a valid *retention* value -- real loguru retention only accepts a
    duration string, never a size string, for backup_count."""
    from video_transcript_api.api.context import ConfigError, _rehearse_log_config

    config = _minimal_config(tmp_path)
    config["log"]["backup_count"] = "10 MB"

    with pytest.raises(ConfigError):
        _rehearse_log_config(config)


def test_rehearse_log_config_accepts_valid_rotation_and_retention_strings(tmp_path):
    from video_transcript_api.api.context import _rehearse_log_config

    config = _minimal_config(tmp_path)
    config["log"]["max_size"] = "10 MB"
    config["log"]["backup_count"] = "10 days"
    _rehearse_log_config(config)  # must not raise

    config["log"]["max_size"] = "1 day"  # duration-shaped rotation is valid too
    _rehearse_log_config(config)  # must not raise


def test_setup_logger_accepts_a_bare_filename_with_no_directory_component(
    tmp_path, monkeypatch
):
    """Real startup bug, independent of the --check-config rehearsal above:
    setup_logger's production path (utils/logging/logger.py) does
    `os.path.dirname(log_file)` then unconditionally `ensure_dir(log_dir)`.
    For a log.file with no directory component at all -- e.g. "app.log", a
    perfectly ordinary relative filename meaning "write next to the working
    directory" -- os.path.dirname returns "" and ensure_dir("") crashes on
    os.makedirs("") with FileNotFoundError. Verified directly against the
    real setup_logger() (not the rehearsal replay), so this is provably a
    real startup crash, not just a --check-config blind spot."""
    from video_transcript_api.utils.logging.logger import setup_logger, shutdown_logger

    monkeypatch.chdir(tmp_path)
    config = {"log": {"file": "app.log", "level": "INFO"}}
    try:
        setup_logger("test-bare-log-filename", config=config, bootstrap=False)
    finally:
        shutdown_logger()


def test_rehearse_notification_config_rejects_non_object_sections(tmp_path):
    """init_all_notifiers() (app.py startup_event, unconditional, no
    try/except) crashes with an uncaught AttributeError at real boot if
    wechat/feishu are configured as non-objects -- validate_config never
    reads either section."""
    from video_transcript_api.api.context import (
        ConfigError,
        _rehearse_notification_config,
        validate_config,
    )

    for section in ("wechat", "feishu"):
        config = _minimal_config(tmp_path)
        config[section] = "not-an-object"

        validate_config(config)  # confirms the gap

        with pytest.raises(ConfigError, match=section):
            _rehearse_notification_config(config)


def test_rehearse_notification_config_accepts_absent_or_valid_sections(tmp_path):
    from video_transcript_api.api.context import _rehearse_notification_config

    config = _minimal_config(tmp_path)
    _rehearse_notification_config(config)  # neither section present: must not raise

    config["wechat"] = {"webhook": "https://example.invalid/hook"}
    config["feishu"] = {"webhook": "https://example.invalid/hook", "secret": "s"}
    _rehearse_notification_config(config)  # valid objects: must not raise


def test_load_and_validate_config_runs_all_five_rehearsals_end_to_end(tmp_path):
    """Full --check-config path covers this round's three new rehearsals
    too, not just the pre-existing llm/ytdlp/users.json ones."""
    from video_transcript_api.api.context import ConfigError, load_and_validate_config

    config = _minimal_config(tmp_path)
    config["feishu"] = "bad"
    config_path = tmp_path / "config.jsonc"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(ConfigError, match="feishu"):
        load_and_validate_config(str(config_path))


def test_real_create_app_boots_serves_livez_and_shuts_down_cleanly(tmp_path):
    """Closes the "FakeRuntimeContext 互相掩护" blind spot: every other
    lifespan test in this file swaps in a Fake/BoomingRuntimeContext whose
    start() never touches the real LLMCoordinator/LLMConfig.from_dict code
    path, so a validate_config <-> from_dict mismatch (e.g. a required
    from_dict key validate_config forgot to check) would sail through every
    one of them and only detonate in a real deployment.

    This test uses the real create_app + real RuntimeContext +
    start_background=True (default), so the LLM consumer thread and task
    queue processor actually start -- while staying fully closed to the
    network: every optional ASR/risk-control backend is absent/disabled,
    YouTube cookie validation is disabled (no cookie config), and
    llm.base_url points at a closed local port that nothing here ever
    dials (LLMCoordinator only builds an HTTP client lazily; it never
    connects at construction time).
    """
    from video_transcript_api.api.app import create_app
    from video_transcript_api.utils.notifications import init_all_notifiers

    config = _minimal_config(tmp_path)
    app = create_app(config_loader=lambda: config)

    try:
        with TestClient(app) as client:
            runtime = app.state.runtime
            assert runtime.started is True
            assert runtime.llm_coordinator is not None
            assert (
                runtime.llm_thread is not None and runtime.llm_thread.is_alive()
            ), "real LLM consumer thread did not start"
            # queue processor + periodic maintenance background tasks.
            assert len(runtime.background_tasks) >= 2

            response = client.get("/livez")
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}

        # Lifespan exit ran a graceful shutdown with no unhandled exception
        # and no leaked LLM consumer thread.
        assert runtime.closed is True
        assert runtime.resources_safe is True
        deadline = time.monotonic() + 5
        while runtime.llm_thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert runtime.llm_thread.is_alive() is False
        for task in runtime.background_tasks:
            assert task.cancelled() or task.done()
    finally:
        # start_background=True's shutdown tears down the session-scoped
        # notifier singletons (see conftest.setup_global_notifiers /
        # app._close_runtime_in_order's close_notifiers=True branch);
        # restore them so later tests in this process still see an
        # initialized singleton instead of a one-off warning-logged
        # lazy re-init.
        init_all_notifiers()


def test_real_create_app_rejects_config_missing_llm_section_with_readable_error(
    tmp_path,
):
    """Same fully real create_app/RuntimeContext wiring as the success test
    above, but the config is missing the llm section entirely. Startup
    must fail with a readable ConfigError raised by validate_config
    (RuntimeContext.__init__ calls it before start() ever runs), not a bare
    KeyError from deep inside LLMConfig.from_dict -- proving the rejection
    actually reaches the lifespan boundary and aborts app startup, not just
    a unit test calling validate_config directly.
    """
    from video_transcript_api.api.app import create_app
    from video_transcript_api.api.context import ConfigError

    config = _minimal_config(tmp_path)
    del config["llm"]
    app = create_app(config_loader=lambda: config, start_background=False)

    with pytest.raises(ConfigError, match=r"llm"):
        with TestClient(app):
            pass


def test_real_shutdown_drains_stuck_task_to_failed_with_terminal_snapshot(tmp_path):
    """Task 4 fix: aclose() used to just cancel the queue consumer and the
    executor's not-yet-started futures (ThreadPoolExecutor.shutdown(...,
    cancel_futures=True)), silently leaving any task already accepted
    (queued/processing/calibrating) stuck in that non-terminal state --
    discoverable only by the next startup's orphan recovery sweep, with
    clients polling to timeout in the meantime.

    Real create_app + real RuntimeContext (not a Fake/BoomingRuntimeContext)
    so the fix under test actually runs end to end: RuntimeContext.
    _finish_close -> _drain_non_terminal_tasks_on_shutdown ->
    CacheManager.drain_non_terminal_tasks_on_shutdown (shares its per-task
    CAS terminal-write loop with recover_orphaned_tasks, reason swapped to
    "shutdown_drain").
    """
    from video_transcript_api.api.app import create_app
    from video_transcript_api.utils.notifications import init_all_notifiers
    from video_transcript_api.utils.task_status import TaskStatus

    config = _minimal_config(tmp_path)
    app = create_app(config_loader=lambda: config)

    try:
        with TestClient(app):
            runtime = app.state.runtime
            cache_manager = runtime.cache_manager

            # Inserted directly via cache_manager, bypassing the real
            # asyncio task queue entirely -- it is therefore never picked up
            # by the real queue processor and stays at its default
            # 'queued' status for the lifetime of this test, exactly the
            # "already accepted but not yet started" case a shutdown
            # mid-flight leaves behind.
            stuck_task_id = cache_manager.create_task(
                url="https://example.com/stuck"
            )["task_id"]

            # A second, already-terminal task the drain must leave alone.
            done_task_id = cache_manager.create_task(
                url="https://example.com/done"
            )["task_id"]
            cache_manager.update_task_status(
                done_task_id, TaskStatus.SUCCESS, title="Already finished"
            )

        # TestClient's context manager exit just ran the real graceful
        # shutdown (RuntimeContext.aclose() via the app's lifespan).
        assert runtime.closed is True
        assert runtime.resources_safe is True

        stuck_task = cache_manager.get_task_by_id(stuck_task_id)
        assert stuck_task["status"] == "failed"
        snapshot = stuck_task["terminal_snapshot"]
        assert snapshot is not None, "shutdown-drained task has no terminal_snapshot"
        assert snapshot.get("recovered") is True
        assert snapshot.get("reason") == "shutdown_drain"

        done_task = cache_manager.get_task_by_id(done_task_id)
        assert done_task["status"] == "success"
        assert done_task["title"] == "Already finished"
    finally:
        # See test_real_create_app_boots_serves_livez_and_shuts_down_cleanly
        # for why this restore is needed after a real (non-Fake) shutdown.
        init_all_notifiers()


def test_terminal_write_pending_register_and_drain_round_trip(tmp_path):
    """Unit-level lock-down of RuntimeContext.terminal_write_pending's two
    methods (K1 bucket b, CI review round 3, major) -- independent of the
    pump/maintenance wiring covered elsewhere: register is idempotent,
    drain returns a snapshot and clears the set."""
    from video_transcript_api.api.context import RuntimeContext

    config = _minimal_config(tmp_path)
    runtime = RuntimeContext(config)

    assert runtime.terminal_write_pending == set()

    runtime.register_terminal_write_pending("task-a")
    runtime.register_terminal_write_pending("task-b")
    runtime.register_terminal_write_pending("task-a")  # idempotent

    assert runtime.terminal_write_pending == {"task-a", "task-b"}

    drained = runtime.drain_terminal_write_pending()
    assert drained == {"task-a", "task-b"}
    assert runtime.terminal_write_pending == set(), (
        "drain must clear the live set, not just return a copy"
    )

    # Draining an already-empty set is a safe no-op, not an error.
    assert runtime.drain_terminal_write_pending() == set()


def test_real_shutdown_drains_terminal_write_pending_to_failed(tmp_path):
    """K1 bucket b (CI review round 3, major): a task_id registered into
    RuntimeContext.terminal_write_pending (simulating process_llm_queue's
    submit-failure double-failure path -- see llm_ops.process_llm_queue and
    tests/integration/test_llm_stage_terminal_state.py for the pump-side
    registration coverage) still lands as 'failed' by the time graceful
    shutdown completes -- but no longer via a dedicated
    terminal_write_pending drain step in the close path.

    PR3 review hardening (major reliability): the close path's own
    synchronous drain of terminal_write_pending (+ _retry_terminal_write_
    pending) was removed as redundant and dangerous. It ran *after* the
    bounded drain_non_terminal_tasks_on_shutdown() phase, calling
    update_task_status() directly with no deadline of its own against a
    connection whose busy_timeout had just been reset to SQLite's ~5s
    default -- able to block aclose()/close() well past
    WORKER_STOP_TIMEOUT_SECONDS (see
    test_aclose_bounded_despite_held_sqlite_lock_and_pending_terminal_write
    below for that failure mode locked down directly). Any task_id
    registered here has, by construction, a DB row still in a
    non-terminal state (its FAILED write attempt is what failed) -- so it
    is already caught by the preceding bounded
    drain_non_terminal_tasks_on_shutdown() sweep, which enumerates *all*
    non-terminal rows and writes them to failed within the shared close
    budget. What changes here: the in-memory terminal_write_pending entry
    itself is no longer cleared by shutdown -- it is simply left in a set
    that is discarded with the process; DB-level correctness does not
    depend on it (recover_orphaned_tasks covers any row the bounded
    sweep itself could not reach in time).

    Real create_app + real RuntimeContext, mirroring
    test_real_shutdown_drains_stuck_task_to_failed_with_terminal_snapshot
    above."""
    from video_transcript_api.api.app import create_app
    from video_transcript_api.utils.notifications import init_all_notifiers
    from video_transcript_api.utils.task_status import TaskStatus

    config = _minimal_config(tmp_path)
    app = create_app(config_loader=lambda: config)

    try:
        with TestClient(app):
            runtime = app.state.runtime
            cache_manager = runtime.cache_manager

            # Reproduces the pump's double-failure precondition directly:
            # a task stuck in a non-terminal state (calibrating) whose
            # earlier FAILED write attempt already failed, hence its
            # task_id sitting in terminal_write_pending with no further
            # pump-side retry coming.
            pending_task_id = cache_manager.create_task(
                url="https://example.com/pending"
            )["task_id"]
            cache_manager.update_task_status(pending_task_id, TaskStatus.CALIBRATING)
            runtime.register_terminal_write_pending(pending_task_id)

        # TestClient's context manager exit just ran the real graceful
        # shutdown (RuntimeContext.aclose() via the app's lifespan).
        assert runtime.closed is True

        pending_task = cache_manager.get_task_by_id(pending_task_id)
        assert pending_task["status"] == "failed"
        snapshot = pending_task["terminal_snapshot"]
        assert snapshot is not None
        # Written by the general non-terminal-task drain now, not the
        # removed terminal_write_pending retry step -- the "reason" tag
        # proves which code path actually did the write.
        assert snapshot.get("reason") == "shutdown_drain"
        assert runtime.terminal_write_pending == {pending_task_id}, (
            "shutdown no longer synchronously drains this set -- the DB "
            "write already happened via the general non-terminal-task "
            "drain above; the stray in-memory entry is harmless since the "
            "process is exiting (see this test's docstring)"
        )
    finally:
        init_all_notifiers()


def test_aclose_bounded_despite_held_sqlite_lock_and_tiny_budget(tmp_path, monkeypatch):
    """P2 end-to-end（本地 codex review 第 12 轮，发现 e，覆盖 review 明确
    要求的组合场景）：一个真实的 SQLite 写锁（第二条原生连接 BEGIN
    IMMEDIATE，一直不提交）叠加一个极小的关闭预算，验证完整的 aclose()
    仍然在 WORKER_STOP_TIMEOUT_SECONDS 允许误差内返回，被锁挡住写不进去
    的任务保持非终态（留给后续运行期对账收敛，闭环语义已在
    TestReconcileRuntimeOrphanedTasks 验证）。

    旧行为下限：关闭清算的 UPDATE 会针对这把锁按连接默认 ~5s busy_timeout
    反复重试，即使 WORKER_STOP_TIMEOUT_SECONDS 远小于 5s。新行为：busy_
    timeout 收窄到剩余预算量级，aclose() 应当接近预算返回，而不是接近
    5s。
    """
    import sqlite3

    from video_transcript_api.api import context as context_module
    from video_transcript_api.api.context import RuntimeContext
    from video_transcript_api.cache.cache_manager import CacheManager

    small_budget = 0.3
    monkeypatch.setattr(context_module, "WORKER_STOP_TIMEOUT_SECONDS", small_budget)

    async def scenario():
        runtime = RuntimeContext(_minimal_config(tmp_path))
        runtime.start()
        cache_manager = runtime.cache_manager

        stuck_task_id = cache_manager.create_task(
            url="https://example.com/lock-held-at-shutdown"
        )["task_id"]
        db_path = str(cache_manager.db_path)

        blocker = sqlite3.connect(db_path)
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute(
            "UPDATE task_status SET title = 'lock-holder' WHERE task_id = ?",
            (stuck_task_id,),
        )
        try:
            start = time.monotonic()
            await runtime.aclose()
            elapsed = time.monotonic() - start
        finally:
            blocker.rollback()
            blocker.close()

        assert elapsed < small_budget + 2.0, (
            f"elapsed={elapsed:.2f}s, expected close to the {small_budget}s "
            f"budget, not the ~5s SQLite default busy_timeout"
        )

        # _finish_close() closes the *calling thread's* thread-local
        # cache_manager connection once resources_safe is True (this
        # scenario has no real worker_futures, so it is) -- that's the same
        # thread this coroutine runs on, so cache_manager's own connection
        # is no longer usable here. A fresh CacheManager instance against
        # the same on-disk db avoids touching the closed connection.
        verifier = CacheManager(cache_dir=str(cache_manager.cache_dir), db_path=db_path)
        try:
            row = verifier.get_task_by_id(stuck_task_id)
            # The row could not be written while the lock was held -- it
            # must remain non-terminal, ready to be picked up by the next
            # reconcile/recovery pass (closed-loop semantics proven
            # separately in TestReconcileRuntimeOrphanedTasks).
            assert row["status"] not in ("success", "failed")
        finally:
            verifier.close()

    asyncio.run(scenario())


def test_aclose_bounded_despite_held_sqlite_lock_and_pending_terminal_write(
    tmp_path, monkeypatch
):
    """PR3 review hardening (major reliability): a task_id sitting in
    RuntimeContext.terminal_write_pending used to get an extra,
    unbounded compensation attempt during shutdown -- on top of (and
    *after*) the already-bounded drain_non_terminal_tasks_on_shutdown()
    sweep this test's sibling above (test_aclose_bounded_despite_held_
    sqlite_lock_and_tiny_budget) locks down.

    That extra step (RuntimeContext._drain_non_terminal_tasks_on_shutdown
    draining terminal_write_pending and calling
    context._retry_terminal_write_pending synchronously) called
    cache_manager.update_task_status() directly -- not through
    CacheManager._fail_non_terminal_tasks, so it carried no deadline of
    its own and ran against a connection whose busy_timeout the
    preceding bounded sweep's own `finally` had *just* reset to SQLite's
    ~5s default (see CacheManager._fail_non_terminal_tasks). Under a real
    held write lock, that single extra update_task_status() call could
    block for the full ~5s default busy_timeout, on top of the already
    tiny WORKER_STOP_TIMEOUT_SECONDS budget -- breaking the "aclose()/
    close() returns within one shared budget" invariant.

    Red under the pre-fix code: elapsed would approach small_budget +
    ~5s (the extra unbounded terminal_write_pending retry), failing the
    bound asserted below. Green after the fix: that extra call was
    deleted as redundant (the task_id's DB row -- still non-terminal by
    construction, see register_terminal_write_pending's only call site
    in llm_ops.process_llm_queue -- is already covered by the bounded
    drain_non_terminal_tasks_on_shutdown() sweep above it), so aclose()
    returns close to the budget and the locked row is simply left
    non-terminal for the next startup's orphan recovery, exactly like
    its sibling test.
    """
    import sqlite3

    from video_transcript_api.api import context as context_module
    from video_transcript_api.api.context import RuntimeContext
    from video_transcript_api.cache.cache_manager import CacheManager

    small_budget = 0.3
    monkeypatch.setattr(context_module, "WORKER_STOP_TIMEOUT_SECONDS", small_budget)

    async def scenario():
        runtime = RuntimeContext(_minimal_config(tmp_path))
        runtime.start()
        cache_manager = runtime.cache_manager

        pending_task_id = cache_manager.create_task(
            url="https://example.com/pending-lock-held"
        )["task_id"]
        db_path = str(cache_manager.db_path)

        # Simulates the pump's double-failure precondition: the task is
        # stuck non-terminal, and its task_id is separately registered as
        # a terminal-write-pending compensation candidate (in real
        # operation this happens when process_llm_queue's own FAILED
        # write attempt also raises -- see llm_ops.process_llm_queue).
        runtime.register_terminal_write_pending(pending_task_id)

        blocker = sqlite3.connect(db_path)
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute(
            "UPDATE task_status SET title = 'lock-holder' WHERE task_id = ?",
            (pending_task_id,),
        )
        try:
            start = time.monotonic()
            await runtime.aclose()
            elapsed = time.monotonic() - start
        finally:
            blocker.rollback()
            blocker.close()

        assert elapsed < small_budget + 2.0, (
            f"elapsed={elapsed:.2f}s, expected aclose() to stay close to "
            f"the {small_budget}s budget, not padded by an extra, "
            f"unbounded terminal_write_pending retry riding SQLite's "
            f"~5s default busy_timeout"
        )

        # _finish_close() closes the calling thread's cache_manager
        # connection once resources_safe is True -- a fresh CacheManager
        # against the same on-disk db avoids touching the closed one.
        verifier = CacheManager(cache_dir=str(cache_manager.cache_dir), db_path=db_path)
        try:
            row = verifier.get_task_by_id(pending_task_id)
            # Blocked by the held lock for the whole call -- stays
            # non-terminal, left for the next startup's orphan recovery,
            # exactly like the non-pending sibling scenario.
            assert row["status"] not in ("success", "failed")
        finally:
            verifier.close()

    asyncio.run(scenario())


def test_lifespan_shutdown_stays_within_budget_when_temp_cleanup_blocks(
    tmp_path, monkeypatch
):
    """本地 codex review 第 16 轮 Q4 红灯测试：故障注入让 shutdown_event()
    里的临时目录清扫（原本是同步调用、完全无界，且发生在 aclose() 的
    deadline 创建之前）远超关闭预算，验证整个 lifespan 关闭（覆盖
    shutdown_event 的清扫 + runtime.aclose() 两段）仍然在总预算内有界
    返回——清扫本身不必真的跑完，只是不能拖累总关闭耗时突破预算。
    """
    from video_transcript_api.api import context as context_module
    from video_transcript_api.api.app import create_app

    small_budget = 0.3
    monkeypatch.setattr(context_module, "WORKER_STOP_TIMEOUT_SECONDS", small_budget)

    config = _minimal_config(tmp_path)
    app = create_app(config_loader=lambda: config)

    blocking_seconds = small_budget + 3.0

    client = TestClient(app)
    client.__enter__()
    try:
        runtime = app.state.runtime
        original_clean_up = runtime.temp_manager.clean_up_old_files

        def blocking_clean_up(*args, **kwargs):
            time.sleep(blocking_seconds)
            return original_clean_up(*args, **kwargs)

        monkeypatch.setattr(
            runtime.temp_manager, "clean_up_old_files", blocking_clean_up
        )

        response = client.get("/livez")
        assert response.status_code == 200
    finally:
        start = time.monotonic()
        client.__exit__(None, None, None)
        elapsed = time.monotonic() - start

    assert elapsed < small_budget + 2.0, (
        f"elapsed={elapsed:.2f}s, expected lifespan shutdown to stay close "
        f"to the {small_budget}s budget despite the blocked temp cleanup "
        f"(blocked for {blocking_seconds:.2f}s)"
    )


def test_aclose_bounded_and_marks_unsafe_when_default_executor_is_saturated(
    tmp_path, monkeypatch
):
    """本地 codex review 第 16 轮 Q5 红灯测试。

    此前 aclose() 用 `asyncio.to_thread(self._stop_workers, deadline)` 提交
    ——`asyncio.to_thread`/`loop.run_in_executor(None, ...)` 都借道进程级
    共享的默认执行器。一旦该执行器被业务侧其它阻塞调用占满，
    _stop_workers 的提交会在共享池内部排队等待空闲线程——deadline 只在它
    真正开始执行之后才生效，排队等待期完全不受约束，aclose() 会跟着共享
    池的占用时长一起被拖慢，WORKER_STOP_TIMEOUT_SECONDS 形同虚设。

    这里把默认执行器整个替换成 max_workers=1 的池并提前占满（占用时长远
    超预算），同时构造一个真实卡住的 "transcription" worker（永不完成的
    future）让 _stop_workers 自身也合理地判定为不安全——验证修复后
    aclose() 仍在总预算内有界返回，且 resources_safe 如实标记 False（不是
    被共享池排队拖累出的假象，而是卡住的 worker 本来就没能在预算内确认
    停止——这才是"标记为不安全"真正应该成立的理由）。
    """
    import concurrent.futures

    from video_transcript_api.api import context as context_module
    from video_transcript_api.api.context import RuntimeContext

    small_budget = 0.3
    monkeypatch.setattr(context_module, "WORKER_STOP_TIMEOUT_SECONDS", small_budget)

    async def scenario():
        runtime = RuntimeContext(_minimal_config(tmp_path))
        runtime.start()

        # 卡住一个 "transcription" worker：永不完成的 future，让
        # _stop_workers 自身的第一段 wait_for 合理地超时、返回 False——
        # 与共享池是否饱和无关，只是为了让"标记为不安全"这件事本身有
        # 真实依据，不被共享池排队问题掩盖了真正的判定逻辑。
        stuck_future = concurrent.futures.Future()
        runtime.track_future(stuck_future, kind="transcription")

        loop = asyncio.get_running_loop()
        saturating_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        loop.set_default_executor(saturating_executor)
        release = threading.Event()

        def occupy_the_only_shared_worker():
            release.wait(timeout=small_budget + 5.0)

        occupying_future = loop.run_in_executor(None, occupy_the_only_shared_worker)
        # 给占用任务一点时间真正开始执行，确保共享池此刻确实已经饱和
        # （而不是恰好还没被调度到）。
        await asyncio.sleep(0.05)

        try:
            start = time.monotonic()
            resources_safe = await runtime.aclose()
            elapsed = time.monotonic() - start
        finally:
            release.set()
            stuck_future.cancel()
            await occupying_future
            saturating_executor.shutdown(wait=True)

        assert elapsed < small_budget + 2.0, (
            f"elapsed={elapsed:.2f}s, expected aclose() to stay close to "
            f"the {small_budget}s budget despite the saturated shared "
            f"default executor"
        )
        assert resources_safe is False

    asyncio.run(scenario())


def test_drain_shutdown_task_passes_worker_stop_timeout_as_deadline(tmp_path):
    """H3 (local codex review round 7): shutdown clean-up must be bounded by
    design, not just "if the caller happens to remember to pass a budget" --
    RuntimeContext._drain_non_terminal_tasks_on_shutdown (the real call site
    _finish_close always goes through) must explicitly pass
    deadline_seconds=WORKER_STOP_TIMEOUT_SECONDS, the same budget
    _stop_workers' three bounded wait_for calls already use, so the
    "aclose returns within a bound" invariant holds end to end and not only
    when CacheManager.drain_non_terminal_tasks_on_shutdown happens to be
    called directly with an explicit deadline (see
    tests/unit/test_cache_task_recovery.py for that mechanism itself)."""
    from video_transcript_api.api.context import RuntimeContext, WORKER_STOP_TIMEOUT_SECONDS

    runtime = RuntimeContext(_minimal_config(tmp_path))

    calls = []

    class FakeCacheManager:
        def drain_non_terminal_tasks_on_shutdown(self, *, deadline_seconds=None):
            calls.append(deadline_seconds)
            return 0

    runtime.cache_manager = FakeCacheManager()

    runtime._drain_non_terminal_tasks_on_shutdown()

    # N1（本地 codex review 第 11 轮）：deadline_seconds 不再是硬编码的
    # 字面量 WORKER_STOP_TIMEOUT_SECONDS 本身，而是"这次调用现算的
    # deadline 减去随后计算剩余预算时已经流逝的墙钟时间"——deadline 参数
    # 未显式传入时在方法体内现算，两次 time.monotonic() 调用之间只隔几行
    # 纯计算，流逝时间是纳秒/微秒级，因此仍然极接近常量本身，但不再要求
    # 精确相等（见 _drain_non_terminal_tasks_on_shutdown 的 deadline 参数
    # 说明）。
    assert len(calls) == 1
    assert calls[0] == pytest.approx(WORKER_STOP_TIMEOUT_SECONDS, abs=0.05)
    assert calls[0] <= WORKER_STOP_TIMEOUT_SECONDS


def test_aclose_total_time_is_bounded_by_a_single_shared_budget(tmp_path, monkeypatch):
    """N1（本地 codex review 第 11 轮）：此前 _stop_workers 的 producers/
    maintenance/llm 三段 wait_for、_shutdown_llm_owner 的 join、以及关闭
    清算，各自独立拿满完整的 WORKER_STOP_TIMEOUT_SECONDS——多段都真的超时
    时，总耗时会累加成"阶段数 x 预算"，违反"aclose 在单份预算内有界返回"
    这条对外承诺的不变式。

    这里把常量 monkeypatch 成很小的值（1.0s——W6 修复，PR3 review hardening
    二轮，从 0.2s 放大：本机并行跑其它 review/测试进程时，0.2s 预算 /
    <0.3s 断言的裕量太薄，进程调度抖动就能把"共享一份预算"的新行为挤到
    断言线以上，制造 flake。放大量级不改变测试验证的行为区别，只是让
    "一份预算"和"两份预算"这两个数量级在高负载下依然有清楚的间隔——
    见下面新阈值的选取依据），构造 producers 与 maintenance 两个 kind 都
    永远不会被清空 worker_futures 的场景——两段 wait_for 的谓词永远为
    False，各自都会真实等到传入的 timeout 用尽才返回，正是复现"两个连续
    阶段是否共享同一份预算"所需要的最小场景（不依赖真正的线程/执行器，
    只操纵 worker_futures 集合本身）。

    旧行为下限：producers（~1.0s，真实等到超时）+ maintenance（~1.0s，
    重新拿满一份独立预算、同样真实等到超时）= 总耗时 >= ~2.0s（2x 预算）。
    新行为：producers 真实耗尽整份共享预算（~1.0s）后，maintenance 阶段
    的剩余预算已经是 0，wait_for(timeout=0) 只做一次即时谓词检查就返回，
    不再真实等待——总耗时应约等于*一份*预算，而不是两份的总和。

    负载容忍设计（W6）：断言上界固定写死 1.6s，而不是 `small_budget * 1.5`
    这种随预算等比例缩放的写法——目的是在“一份预算”（约 1.0s，允许到
    1.6s 有 0.6s 的调度抖动裕量）与“两份预算”（旧 bug 行为，>= 2.0s）
    之间保留足够宽的区分带，即使本机同时有其它并行进程（如 codex/review
    抢占 CPU）拖慢调度，正常路径也几乎不可能被抖动到 1.6s 以上；而旧 bug
    行为的下限（2.0s）距离这条断言线还有 0.4s 的安全边际，不会被误判为
    "通过"。
    """
    from video_transcript_api.api import context as context_module
    from video_transcript_api.api.context import RuntimeContext

    small_budget = 1.0
    monkeypatch.setattr(context_module, "WORKER_STOP_TIMEOUT_SECONDS", small_budget)

    async def scenario():
        runtime = RuntimeContext(_minimal_config(tmp_path))
        # 永远不会被移除的两个 worker_futures 条目：没有真实的 executor/
        # 线程会调用 track_future 的 add_done_callback 去 discard 它们，
        # 两段 wait_for 的谓词因此永远评估为 False。
        runtime.worker_futures = {
            ("transcription", object()),
            ("maintenance", object()),
        }

        start = time.monotonic()
        resources_safe = await runtime.aclose()
        elapsed = time.monotonic() - start

        assert resources_safe is False
        # 严格小于旧行为下限的 2x 预算（~2.0s），同时留出裕量应对高负载下
        # 的调度抖动（W6：本机并行跑其它 review/测试进程时曾经翻车过，见
        # 上方 docstring"负载容忍设计"一节）——1.6s 这个固定上界，距离
        # 期望值（~1.0s）有 0.6s 裕量，距离旧 bug 行为下限（~2.0s）仍有
        # 0.4s 安全边际，足以区分"共享同一 deadline"（新）与"各自独立
        # 计满"（旧，必然 >= 2x）两种行为。
        assert elapsed < 1.6, (
            f"elapsed={elapsed:.3f}s, expected close to a single "
            f"budget({small_budget}s), not the sum of two"
        )

        # 状态标记必须如实反映真实检查结果（本地 codex review 第 11 轮
        # N1）：maintenance 的 worker_futures 条目确实还在，即便 wait_for
        # 因为预算耗尽而没有真的等待，如实检查后仍应得到 False，不能被
        # 硬编码成别的值。
        assert runtime._maintenance_confirmed_stopped is False

    asyncio.run(scenario())


def test_aclose_timeout_waiting_for_stop_workers_marks_maintenance_unconfirmed(
    tmp_path, monkeypatch,
):
    """L4 (CI review round 5, P1 correctness): _stop_workers_off_shared_pool
    submits self._stop_workers to a dedicated one-shot executor and waits on
    it via `asyncio.wait(timeout=...)`. When that outer wait itself times
    out with the future still pending (the background thread has not
    finished -- e.g. it never got scheduled promptly, or is stuck on
    something upstream of its own internal bookkeeping), the method logs an
    error and returns False *without ever touching
    `_maintenance_confirmed_stopped`* -- that attribute still holds
    whatever it was before this call (the `__init__` default of True, since
    only `_stop_workers` itself ever sets it, and it never got that far).

    aclose() immediately calls `_finish_close(resources_safe=False, ...)`
    next, which reads `_maintenance_confirmed_stopped` via
    `getattr(..., True)` as the sole gate for whether the shutdown drain
    (`_drain_non_terminal_tasks_on_shutdown`) is safe to run. Reading the
    stale True here lets the drain proceed concurrently with whatever the
    still-running background `_stop_workers` thread is doing -- exactly the
    maintenance/drain race K2 and G2 exist to prevent, just reached through
    this one remaining unguarded path: the *caller* gave up waiting, not
    `_stop_workers` itself.

    Reproduced by replacing `runtime._stop_workers` with a stand-in that
    blocks on a `threading.Event` the test controls, well past the tiny
    monkeypatched shutdown budget -- it deliberately never reaches (and
    never will, until released) the point where the real `_stop_workers`
    would set `_maintenance_confirmed_stopped`, forcing the outer
    `asyncio.wait` in `_stop_workers_off_shared_pool` to observe a
    non-empty `pending` set and give up early. Asserts both halves of the
    fix: the flag must be set to False before `_finish_close` runs, and the
    drain must consequently be skipped."""
    from video_transcript_api.api import context as context_module
    from video_transcript_api.api.context import RuntimeContext

    small_budget = 0.2
    monkeypatch.setattr(context_module, "WORKER_STOP_TIMEOUT_SECONDS", small_budget)

    async def scenario():
        runtime = RuntimeContext(_minimal_config(tmp_path))

        release_event = threading.Event()

        def blocking_stop_workers(deadline=None):
            # Blocks far longer than small_budget -- simulates the
            # dedicated executor's submission/scheduling (or the function
            # itself) not completing before the outer asyncio.wait's
            # deadline, independent of whatever _stop_workers' own three
            # internal wait_for calls would eventually decide.
            release_event.wait(timeout=5.0)
            return False

        runtime._stop_workers = blocking_stop_workers

        drain_calls = []
        runtime._drain_non_terminal_tasks_on_shutdown = (
            lambda *a, **k: drain_calls.append((a, k))
        )

        try:
            resources_safe = await runtime.aclose()

            assert resources_safe is False
            assert runtime._maintenance_confirmed_stopped is False, (
                "外层 asyncio.wait 放弃等待 _stop_workers 时必须显式把 "
                "_maintenance_confirmed_stopped 置 False -- 不能让它保留 "
                "__init__ 预置的默认值 True（红：旧代码在这里仍是 True，"
                "因为只有 _stop_workers 自己才会设置这个标志，而它此刻还"
                "没跑到那一步）"
            )
            assert drain_calls == [], (
                "_finish_close 读到未确认停止的标志时必须整体跳过关闭清算"
                "（_drain_non_terminal_tasks_on_shutdown）-- 不能与仍在后台"
                "线程里运行的 _stop_workers 并发访问共享的 DB 连接"
            )
        finally:
            release_event.set()

    asyncio.run(scenario())


def test_aclose_bounded_when_background_task_is_slow_to_cancel(tmp_path, monkeypatch):
    """P2（本地 codex review 第 12 轮，发现 d）：此前 aclose() 对
    background_tasks 的 cancel+gather 完全没有超时保护，deadline 也要等
    这一步跑完才计算——一个响应取消较慢的后台任务（如捕获 CancelledError
    后还要做一段耗时清理才会真正退出）会让这一步的耗时等于该任务实际
    停下所需的时间，而不是关闭预算允许的时间。

    构造一个"取消后需要比预算更久才能真正停下"的后台任务（catch 住
    CancelledError 后再 sleep 一段明显长于预算的时间才退出），验证
    aclose() 仍然在 WORKER_STOP_TIMEOUT_SECONDS 量级内有界返回，且如实
    把结果标记为不安全——一个尚未确认完成取消的后台任务本身就是不安全
    条件，即使 _stop_workers 自己（这里没有真实 worker）会报告"安全"。

    本地实测排除了一个更看似直观的写法（asyncio.wait_for 包一层
    asyncio.gather）：wait_for 的内部清理逻辑在超时后会继续 await 被
    取消的 gather()，直到它真正 resolve——对于"取消后还要慢慢清理"的
    任务，wait_for 実际耗时等于任务真正停下所需的时间，而不是传入的
    timeout，并不能提供这里需要的有界保证；只有 asyncio.wait(tasks,
    timeout=...) 会在 timeout 到达时如实返回 (done, pending)，不等待
    pending 任务真正完成——这正是 aclose() 现在采用的写法。
    """
    from video_transcript_api.api import context as context_module
    from video_transcript_api.api.context import RuntimeContext

    small_budget = 0.2
    monkeypatch.setattr(context_module, "WORKER_STOP_TIMEOUT_SECONDS", small_budget)

    async def scenario():
        runtime = RuntimeContext(_minimal_config(tmp_path))

        async def slow_to_cancel_background():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # 明显长于 small_budget 的"清理"耗时 -- 任务最终确实会
                # 停下，只是远比关闭预算允许的时间慢。
                await asyncio.sleep(small_budget * 10)
                raise

        task = asyncio.create_task(slow_to_cancel_background())
        runtime.background_tasks.append(task)
        await asyncio.sleep(0)

        start = time.monotonic()
        resources_safe = await runtime.aclose()
        elapsed = time.monotonic() - start

        # 旧行为下限：gather 完全无超时保护，耗时会等于该任务真正响应
        # 取消所需的时间（这里是 small_budget * 10）。新行为：asyncio.wait
        # 在 timeout 到达时如实返回，不等待 pending 任务，elapsed 应当
        # 接近 small_budget 这个量级，而不是 small_budget * 10。
        assert elapsed < small_budget * 5, (
            f"elapsed={elapsed:.3f}s, expected bounded close to the "
            f"{small_budget}s budget, not the task's actual ~"
            f"{small_budget * 10}s cancellation-response time"
        )
        assert resources_safe is False, (
            "an unconfirmed-cancelled background task is an unsafe "
            "condition even when _stop_workers itself would report safe "
            "(no real worker_futures exist in this scenario)"
        )

        # 测试自己收尾：等后台任务真正停下，避免留下悬挂的 asyncio 任务。
        try:
            await asyncio.wait_for(task, timeout=small_budget * 20)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    asyncio.run(scenario())


def test_aclose_deadline_created_before_cancelling_background_tasks(tmp_path):
    """P2（本地 codex review 第 12 轮，发现 d）：deadline 必须在取消动作
    之前创建，而不是等 cancel+gather 跑完才计算——否则 gather 阶段本身
    消耗的时间不计入预算，"aclose 在单份预算内有界返回"这条承诺就出现
    了一个不受 deadline 覆盖的空档。

    直接断言真实的时间顺序：deadline 的计算（_new_shutdown_deadline）必
    须先于后台任务*观测到*自己被取消（task.cancel() 调用本身在源码里已
    经晚于 deadline 计算，任务真正处理 CancelledError 又要再晚一个事件
    循环轮次，是比"cancel() 调用"更保守、更容易在测试里观测到的信号）。
    """
    from video_transcript_api.api.context import RuntimeContext

    async def scenario():
        runtime = RuntimeContext(_minimal_config(tmp_path))

        events = []
        real_new_shutdown_deadline = runtime._new_shutdown_deadline

        def spy_new_shutdown_deadline():
            events.append("deadline_created")
            return real_new_shutdown_deadline()

        runtime._new_shutdown_deadline = spy_new_shutdown_deadline

        async def background():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                events.append("task_cancellation_observed")
                raise

        task = asyncio.create_task(background())
        runtime.background_tasks.append(task)
        await asyncio.sleep(0)

        await runtime.aclose()

        assert "deadline_created" in events
        assert "task_cancellation_observed" in events
        assert events.index("deadline_created") < events.index(
            "task_cancellation_observed"
        ), f"expected deadline creation before cancellation, got: {events}"

    asyncio.run(scenario())


def test_maintenance_confirmed_stopped_is_true_when_already_stopped_despite_zero_remaining_budget(
    tmp_path,
):
    """N1：wait_for(timeout=0) 在预算已耗尽时仍然会先做一次真实的谓词
    检查——如果 maintenance 其实已经真正停止（worker_futures 里没有
    "maintenance" kind 的残留条目），_maintenance_confirmed_stopped 必须
    如实置为 True，不能因为预算已耗尽就被硬编码为 False（那会连带让
    _finish_close 不必要地跳过本可以安全执行的关闭清算）。"""
    from video_transcript_api.api.context import RuntimeContext

    runtime = RuntimeContext(_minimal_config(tmp_path))
    runtime.worker_futures = set()  # 没有任何 kind 残留，两段谓词天然为真

    already_expired_deadline = time.monotonic() - 1.0
    runtime._stop_workers(already_expired_deadline)

    assert runtime._maintenance_confirmed_stopped is True


def test_drain_skipped_when_shared_budget_already_exhausted(tmp_path):
    """N1：关闭清算的预算并入 aclose()/close() 入口计算的同一个
    deadline——deadline 已经过期（剩余预算 <= 0）时必须整体跳过，连
    CacheManager.drain_non_terminal_tasks_on_shutdown 的第一步 SELECT 都
    不能发起，不能像此前那样重新给满一份独立预算继续跑。未清算的任务留给
    下一次启动的孤儿恢复兜底（该兜底语义已存在，非本次改动新增）。"""
    from video_transcript_api.api.context import RuntimeContext

    runtime = RuntimeContext(_minimal_config(tmp_path))

    calls = []

    class FakeCacheManager:
        def drain_non_terminal_tasks_on_shutdown(self, *, deadline_seconds=None):
            calls.append(deadline_seconds)
            return 0

    runtime.cache_manager = FakeCacheManager()

    already_expired_deadline = time.monotonic() - 1.0
    runtime._drain_non_terminal_tasks_on_shutdown(already_expired_deadline)

    assert calls == []


class _PredicateOnlyCondition:
    """Stand-in for threading.Condition used by tests that need to force one
    of _stop_workers' three bounded wait_for() calls to time out without
    actually burning the real 5-second timeout. Evaluates the predicate
    exactly once and returns its result immediately -- this reproduces
    Condition.wait_for's fast path (predicate already true -> return
    immediately, no waiting) but skips the real bounded retry loop when the
    predicate is false, since these tests want a deterministic, instant
    "still stuck" outcome rather than genuinely waiting out the timeout."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def wait_for(self, predicate, timeout=None):
        return predicate()


def test_shutdown_drains_llm_backlog_task_when_llm_drain_times_out(tmp_path):
    """Local codex review round 6, G2: _stop_workers' llm_drained wait_for
    can time out with LLM tasks still backlogged (queued faster than
    llm_max_workers can process them). When it does, _shutdown_llm_owner()
    calls llm_executor.shutdown(wait=False, cancel_futures=True) -- which
    discards any future still sitting in the executor's internal queue
    without ever running it. Since llm_ops._handle_llm_task is the only
    place that both calls cache_manager.update_task_status(..., FAILED) on
    error *and* llm_task_queue.task_done() (in its `finally`), a cancelled-
    before-it-ran future leaves its task_id stuck in the DB at whatever
    non-terminal status it already had (processing/calibrating) forever --
    the container is shutting down, so no other worker will ever pick it up
    again.

    _stop_workers hard-codes resources_safe=False on this exact timeout
    branch, and the old _finish_close only called
    _drain_non_terminal_tasks_on_shutdown() when resources_safe was True --
    skipping the one cleanup step that could still rescue this task, on
    precisely the path that produces it. The fix runs the drain
    unconditionally (as long as cache_manager exists), before the
    conditional DB-connection close.

    Reproduced directly against a real RuntimeContext + real CacheManager
    (no create_app/TestClient needed -- this is a _stop_workers-level
    concern): a task is parked at PROCESSING (modeling an in-flight LLM
    task), and one item is left sitting in llm_queue without a matching
    task_done() (modeling the cancelled-before-it-ran future) so the
    llm_drained predicate is false. _worker_futures_condition is replaced
    with _PredicateOnlyCondition so the test does not have to wait out the
    real 5-second timeout to observe the same outcome.
    """
    from video_transcript_api.api.context import RuntimeContext
    from video_transcript_api.utils.task_status import TaskStatus

    runtime = RuntimeContext(_minimal_config(tmp_path))
    runtime.start()
    cache_manager = runtime.cache_manager

    try:
        backlog_task_id = cache_manager.create_task(
            url="https://example.com/llm-backlog"
        )["task_id"]
        cache_manager.update_task_status(backlog_task_id, TaskStatus.PROCESSING)

        # Models the cancelled-before-it-ran LLM future: an item was pulled
        # off llm_queue and handed to the executor, but never reached
        # _handle_llm_task's `finally: llm_task_queue.task_done()`.
        runtime.llm_queue.put(object())

        # Skip the real bounded wait -- the predicate is evaluated once and
        # is already false (unfinished_tasks == 1), so this reproduces a
        # genuine wait_for(..., timeout=5) timeout instantly.
        runtime._worker_futures_condition = _PredicateOnlyCondition()

        resources_safe = runtime.close()

        assert resources_safe is False, (
            "test setup did not reproduce the llm_drained timeout branch"
        )

        backlog_task = cache_manager.get_task_by_id(backlog_task_id)
        assert backlog_task["status"] == "failed", (
            "LLM backlog task must not be left stuck in a non-terminal "
            "status when the shutdown timeout path is hit"
        )
        snapshot = backlog_task["terminal_snapshot"]
        assert snapshot is not None
        assert snapshot.get("reason") == "shutdown_drain"
    finally:
        cache_manager.close()
        runtime.audit_logger.close()


def test_startup_recovery_failure_still_boots_and_sets_recovery_pending(tmp_path, monkeypatch):
    """Local codex review round 6, G3: startup_event()'s one-shot
    recover_orphaned_tasks() call is wrapped in try/except that only logs on
    failure and lets the service boot anyway (a locked audit.db/cache.db at
    the exact moment of boot is transient, and refusing to start the whole
    service over it would be worse) -- but before this fix there was no
    retry mechanism at all, so any non-terminal task left over from a
    previous crashed process would hang around, invisible and unrecoverable,
    for the entire uptime of the new process.

    The fix: RuntimeContext.recovery_pending gets set True in that except
    branch, to be picked up and retried by _periodic_maintenance (see
    tests/cache/test_periodic_maintenance.py::
    TestPeriodicMaintenanceOrphanRecoveryRetry for the retry-and-clear
    behavior itself). This test only exercises the startup half: a real
    create_app + real RuntimeContext, with CacheManager.recover_orphaned_tasks
    monkeypatched to raise on its first call (modeling a transient DB lock at
    boot), asserting the app still boots successfully and the flag is set.
    """
    from video_transcript_api.api.app import create_app
    from video_transcript_api.cache.cache_manager import CacheManager
    from video_transcript_api.utils.notifications import init_all_notifiers

    config = _minimal_config(tmp_path)

    def _boom(self, *args, **kwargs):
        raise RuntimeError("simulated audit.db lock at startup")

    monkeypatch.setattr(CacheManager, "recover_orphaned_tasks", _boom)

    app = create_app(config_loader=lambda: config)

    try:
        with TestClient(app) as client:
            runtime = app.state.runtime
            assert runtime.started is True, "startup must not abort on this failure"
            assert runtime.recovery_pending is True

            response = client.get("/livez")
            assert response.status_code == 200
    finally:
        init_all_notifiers()


# --- --check-config must validate the users.json a real boot will load ----
#
# UserManager has no config-driven override for where users.json lives: both
# RuntimeContext.start() (src/video_transcript_api/api/context.py) and the
# legacy get_user_manager() singleton (utils/accounts/user_manager.py)
# construct UserManager(...) without a users_config_path, so it always
# resolves to the hardcoded default `<project_root>/config/users.json`
# (see UserManager.__init__). These tests exercise the real --check-config
# subprocess (to pin down real exit codes / stderr text), which means the
# only way to control which users.json content a run actually sees is a
# path override UserManager itself understands.
#
# V4 fix (PR3 review hardening): the old fixture (_real_users_json) achieved
# this by directly unlinking/overwriting the REAL
# <project_root>/config/users.json (gitignored, may hold real API tokens),
# saving/restoring a backup around each run. A killed test process (before
# the `finally` restore ran) meant real credentials lost; two of these
# tests running concurrently (e.g. pytest-xdist) meant they'd stomp on each
# other's temp content mid-run. UserManager.__init__ now also honors the
# VTAPI_USERS_JSON env var (see USERS_CONFIG_PATH_ENV_OVERRIDE in
# utils/accounts/user_manager.py) purely as a test-injection seam -- unset
# in every real boot path, so --check-config <-> real-boot parity is
# unaffected. _isolated_users_json below writes content to a pytest
# tmp_path file and _run_check_config forwards it to the subprocess via
# that env var; the real config/users.json is never read, written, or
# deleted by any test in this module.


@contextmanager
def _isolated_users_json(tmp_path: Path, content: str | None):
    """Write `content` to an isolated tmp file the --check-config subprocess
    will be pointed at via VTAPI_USERS_JSON, instead of ever touching the
    real <project_root>/config/users.json.

    content=None deliberately leaves the path non-existent -- this mirrors
    UserManager._read_validated_users(allow_missing=True), which treats a
    missing file as the legacy single-token fallback (empty user map), the
    exact case test_check_config_allows_missing_users_json exercises.
    """
    path = tmp_path / "isolated_users.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    yield path


def _run_check_config(tmp_path: Path, *, users_json_path: Path):
    config_path = tmp_path / "config.jsonc"
    config_path.write_text(json.dumps(_minimal_config(tmp_path)), encoding="utf-8")
    env = dict(os.environ)
    env["VTAPI_USERS_JSON"] = str(users_json_path)
    return subprocess.run(
        [sys.executable, "main.py", "--check-config", "--config", str(config_path)],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_check_config_allows_missing_users_json(tmp_path):
    """No users.json at all is the one legacy-fallback case: single-token
    mode via config.api.auth_token, not an error."""
    with _isolated_users_json(tmp_path, None) as users_json_path:
        result = _run_check_config(tmp_path, users_json_path=users_json_path)
    assert result.returncode == 0, result.stderr
    assert "Configuration OK" in result.stdout


def test_check_config_accepts_valid_users_json(tmp_path):
    valid = json.dumps(
        {
            "users": {
                "token-alice": {
                    "user_id": "alice",
                    "name": "Alice",
                    "permissions": ["recalibrate"],
                }
            }
        }
    )
    with _isolated_users_json(tmp_path, valid) as users_json_path:
        result = _run_check_config(tmp_path, users_json_path=users_json_path)
    assert result.returncode == 0, result.stderr
    assert "Configuration OK" in result.stdout


def test_check_config_rejects_empty_users_json(tmp_path):
    """An existing-but-empty file is a real deployment mistake (e.g. a
    truncated write), not the legacy-fallback case -- only a fully absent
    file gets that pass."""
    with _isolated_users_json(tmp_path, "") as users_json_path:
        result = _run_check_config(tmp_path, users_json_path=users_json_path)
    assert result.returncode != 0
    assert "users" in result.stderr


def test_check_config_rejects_malformed_users_json(tmp_path):
    with _isolated_users_json(tmp_path, "{not valid json") as users_json_path:
        result = _run_check_config(tmp_path, users_json_path=users_json_path)
    assert result.returncode != 0
    assert "users" in result.stderr


def test_check_config_rejects_duplicate_user_id_in_users_json(tmp_path):
    duplicated = json.dumps(
        {
            "users": {
                "token-one": {"user_id": "same", "name": "One", "permissions": []},
                "token-two": {"user_id": "same", "name": "Two", "permissions": []},
            }
        }
    )
    with _isolated_users_json(tmp_path, duplicated) as users_json_path:
        result = _run_check_config(tmp_path, users_json_path=users_json_path)
    assert result.returncode != 0
    assert "duplicate user_id" in result.stderr


def test_check_config_rejects_invalid_permission_in_users_json(tmp_path):
    bad_permission = json.dumps(
        {
            "users": {
                "token-one": {
                    "user_id": "carol",
                    "name": "Carol",
                    "permissions": ["root"],
                }
            }
        }
    )
    with _isolated_users_json(tmp_path, bad_permission) as users_json_path:
        result = _run_check_config(tmp_path, users_json_path=users_json_path)
    assert result.returncode != 0
    assert "permission" in result.stderr


def test_check_config_users_json_validation_is_side_effect_free(tmp_path):
    """Same invariant as test_check_config_is_side_effect_free, specifically
    for the added users.json read: validating a valid users.json during
    --check-config must not create a database, spawn a thread, or touch
    cache/workspace directories."""
    valid = json.dumps(
        {"users": {"token-a": {"user_id": "a", "name": "A", "permissions": []}}}
    )
    before_threads = threading.active_count()
    with _isolated_users_json(tmp_path, valid) as users_json_path:
        result = _run_check_config(tmp_path, users_json_path=users_json_path)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "cache").exists()
    assert not (tmp_path / "workspace").exists()
    assert threading.active_count() == before_threads


# --- _LazyResource must not shadow the proxied object's own methods --------
#
# _LazyResource previously subclassed collections.abc.Mapping purely to make
# the dict-shaped `config = lazy_resource(get_config)` proxy support
# `config.get(...)`. But Mapping injects get/keys/items/values as real class
# attributes, and Python's normal attribute lookup finds those *before* ever
# falling back to __getattr__. Every lazy_resource() proxy shares this one
# class -- including `task_queue`/`llm_task_queue` in
# api/services/transcription.py and api/services/llm_ops.py, which wrap
# queue.Queue. queue.Queue.get(timeout=...) takes a timeout kwarg;
# Mapping.get(self, key, default=None) does not, so the consumer loop's
# `llm_task_queue.get(timeout=0.2)` (llm_ops.py) resolved to the wrong method
# and raised TypeError on every call. That TypeError was swallowed by the
# consumer loop's own `except Exception: sleep(1)` and retried forever, so
# the LLM queue was silently never drained in production while every
# lifespan/startup test stayed green (they never call .get() on a live
# queue proxy). This is the regression these two tests pin down.


def test_lazy_resource_proxy_delegates_queue_get_instead_of_shadowing_it(tmp_path):
    from video_transcript_api.api.context import lazy_resource

    proxy = lazy_resource(lambda: queue.Queue())

    # A Mapping-based proxy would resolve this to Mapping.get(key, default),
    # which requires a `key` positional argument and raises TypeError here.
    # The real queue.Queue.get(timeout=...) call must run instead and raise
    # queue.Empty, the normal "nothing to consume" signal the consumer loops
    # already handle.
    with pytest.raises(queue.Empty):
        proxy.get(timeout=0.01)


def test_lazy_resource_proxy_still_supports_dict_like_access(tmp_path):
    from video_transcript_api.api.context import lazy_resource

    backing = {"llm": {"api_key": "secret"}, "storage": {"cache_dir": "/tmp/cache"}}
    proxy = lazy_resource(lambda: backing)

    # Item access, iteration and len go through __getitem__/__iter__/__len__,
    # which _LazyResource always implemented directly (not via Mapping).
    assert proxy["llm"] == {"api_key": "secret"}
    assert set(iter(proxy)) == set(backing)
    assert len(proxy) == len(backing)

    # .get()/.keys() on a dict-shaped proxy must still work: without the
    # Mapping mixin they now reach the real dict via __getattr__ instead.
    assert proxy.get("storage") == {"cache_dir": "/tmp/cache"}
    assert proxy.get("missing", "default") == "default"
    assert set(proxy.keys()) == set(backing.keys())
    assert dict(proxy.items()) == backing
