# C-r1 独立审查结论

failure-visibility: p2-only

## 结论

固定审查范围：`c7b38563eb2f06f5ca36c06c6c7da7de9c5e9995..99780aff6b8dc567146b6a61e402007423d89db8`。Git 汇总为 18 个文件、1476 行新增、81 行删除。按审查卡要求，未读取 `docs/sessions/261008-local-upload/progress/C-progress.md` 的内容，且将该文件从人工审查和 OCR 输入中排除；其余 17 个文件已逐项审查。

发现 1 项 P2，未发现 P1。交付阻断：否（按 personal 风险档；建议在本地上传投入使用前修正明确的清理顺序不变量）。没有据此改写应用或测试。

## Finding P2：LLM 消费者可在源媒体删除前开始

- **不变式**：设计不变式 2 要求转录段结束时真实删除源媒体、音轨和 ASR 中间文件，并在 LLM 持有正文继续前完成删除。
- **触发与代码**：上传分支在 `src/video_transcript_api/api/services/transcription.py:2557-2582` 调用 `_handoff_to_llm_stage`。该 helper 在 `779` 行后将已转录任务放入共享 LLM 队列；消费者可立即运行。源文件只在同一函数外层 `finally` 的 `2690-2706` 行清理。因此真实并发消费者开始 LLM 阶段时，`upload-source.bin` 仍可存在。清理会等整个转录 worker 返回；而这之前已经把任务交给 LLM。
- **实际影响**：启用本地上传时，用户媒体在预期清理点后继续留在临时目录，至少覆盖 LLM 阶段启动与转录 worker 收尾之间的窗口；LLM 慢或阻塞时窗口也随之变长。这违反已批准的数据生命周期承诺。属于个人上传内容延长暂存，判 P2；不具备 P1 的广泛或高影响条件，当前证据也不支持按 P1 阻断交付。
- **测试缺口**：`tests/integration/test_local_upload_lifecycle.py:463-645` 的真实 Transcriber 测试先等待 dispatcher future 完成，再断言文件不存在（619-623），随后才手动调用 `_handle_llm_task`（629-631）。因此它验证了 eventually cleanup，却没有并发观察“LLM 开始时文件是否已清理”。下面的隔离探针使用真实 dispatcher、`Transcriber`、队列消费者和 `_handle_llm_task`，断言当消费者进入实际 LLM `process` 方法时源路径仍存在；通过。
- **建议修复判据**：将上传源及 ASR 临时文件的清理完成放到 LLM handoff 之前，并保留异常时的 finally 清理；增加并发队列消费者断言：消费者进入 LLM 前源文件和任务临时目录已不存在。不要仅把当前串行测试中的手动 LLM 调用提前或延后作为时序证明。

### P1 两问

1. 是否存在无交互即可触发的高影响安全/数据完整性损害？否；证据仅表明本地暂存清理晚于承诺时点，未发现媒体被送入 LLM 或暴露给其他用户。
2. 是否会影响广泛用户、造成不可恢复数据损失或绕过主要访问边界？否；范围为启用本地上传的任务，临时文件最终删除，未发现持久化或公开读取旁路。

## 不变式与代码→测试核对

| 范围 | 实际代码 | 锁定的真实测试 / 结论 |
|---|---|---|
| 上传 admission、文件字节、处理选项、ASR 与 CapsWriter 结果 | `routes/uploads.py`；`transcription.py`；`cache_manager.py` | `test_upload_worker.py`；`test_local_upload_lifecycle.py` 使用真实 Transcriber 和 CapsWriter fixture，核对实际源字节、时长、选项、结果文本；未走 URL parser/downloader。join(tokens)/时间戳契约见 `test_capswriter_contract.py`。 |
| 临时空间预算、异常清理及 URL 调用者 | `context.py:reserve_upload_temp`；`transcription.py`；`utils/tempfile_manager.py` | `test_local_upload_policy.py`、`test_tempfile_manager.py`、`test_temp_cleanup_integration.py`。清理函数新抛错的生产调用点已逐一核对；URL 转录分支仍在原有 warning 边界内处理清理异常。 |
| 终态固定期限、never、启动孤儿、子任务和重复写入 | `terminal_status.py`；`cache_manager.py` | `test_local_upload_policy.py` 覆盖成功/失败终态、孤儿、never/revocation 和 child；`test_task_status_cleanup.py`、`test_cache_cleanup.py` 覆盖清理。新增 `terminal_at` 写者为迁移回填及终态更新；用来锁定一次性终态时钟（包括 never），未发现第三个镜像状态机写者。 |
| 空旧 token、Resolver 读/导出/历史、owner 先过滤 | `view_token_resolver.py`；`routes/audit.py`；`cache_manager.py:get_task_by_view_token` | `test_view_token_resolver.py`、生命周期测试中的 summary/export/history/close 序列、`test_upload_history_filters_owner_before_page_limit`。旧列为空、upload capability 独立；关闭/过期不返回正文。 |
| 三种 reprocess 与关闭并发排序 | `routes/tasks.py` 的 recalibrate/resummarize/generate_notes | `test_upload_reprocess_routes_use_owner_gate_and_never_persist_legacy_token`、`test_reprocess_admission_holds_sqlite_order_against_concurrent_close`；owner/share gate 与 child INSERT 在同一 SQLite 写事务内。 |
| 通知实际 producer 到最终 HTTP payload | `completion_share.py`；`task_formatter.py`；终态通知路径 | `test_notification_e2e_delivery.py` 实际捕获最终 HTTP payload，断言 upload 独立 URL、标题、来源标签、无本地路径/凭据，以及总结先于短完成回执。未新增 prompt/outbox/通知重试。 |
| A/B admission、队列和恢复合同 | `uploads.py`；`context.py`；`cache_manager.py` | 上传及 policy/lifecycle producer 测试核对 SQLite 真实结果和队列；未发现环境关闭时破坏已有 cache 保护或队列一次性 admission。 |

## 验证记录

- 当前审查树 `git diff --check c7b38563eb2f06f5ca36c06c6c7da7de9c5e9995..99780aff6b8dc567146b6a61e402007423d89db8`：退出 0。
- 在隔离 scratch worktree、网络 guard 生效下，以下窄测通过（退出 0）：`tests/unit/test_upload_worker.py`、`tests/integration/test_local_upload_lifecycle.py`、`tests/unit/test_notification_e2e_delivery.py`、`tests/unit/test_capswriter_contract.py`、`tests/unit/test_local_upload_policy.py`、`tests/unit/test_tempfile_manager.py`、`tests/integration/test_temp_cleanup_integration.py`、`tests/unit/test_view_token_resolver.py`、`tests/api/test_history_routes.py`、`tests/unit/test_task_status_cleanup.py`、`tests/unit/test_cache_cleanup.py`。
- 精确 base 红测：在 `c7b38563eb2f06f5ca36c06c6c7da7de9c5e9995` scratch worktree 拷入 `test_dispatcher_persists_named_local_upload_processing_failure` 后运行 `uv run pytest -q tests/unit/test_upload_worker.py::test_dispatcher_persists_named_local_upload_processing_failure`，预期退出 1：仅断言失败，实际 `error_message == '转录任务失败: ASR fixture failure'`，缺少 `UPLOAD_PROCESSING_FAILED`。不是导入或 schema 错误。
- 裸 shell 缺省环境与真实子进程 producer fixture 已验证：`VTA_UPLOADS_ENABLED` 未设置时拒绝且响应正文无；显式 true 时成功且有正文。临时 systemd unit 使用 `--expand-environment=no` 和 false，子进程读到 false；producer 的真实子进程环境/SQLite 测试通过，且在子进程内断言缺省/true 两态。未改生产 unit。
- OCR envelope：`status=skipped`、`findings=[]`；这不是 clean 结论。一次独立并发时序探针修正版通过；第一次无效尝试因探针重复调用 `queue.task_done()` 而失败，已修复探针并重跑。scratch 结束后由 helper 清除其临时 worktree；目标审查树和主树均未运行 runtime 测试、未改运行时配置或数据。

## 并发清理探针的可复制源码生成与运行命令

在隔离 scratch checkout 的固定 head `99780aff6b8dc567146b6a61e402007423d89db8` 执行。该命令从该 head 的现有真实生命周期测试生成临时 probe，仅把串行手动 LLM 调用替换为一个同步队列消费者；不改目标 checkout：

```bash
set -euo pipefail
FIXED=99780aff6b8dc567146b6a61e402007423d89db8
git show "$FIXED:tests/integration/test_local_upload_lifecycle.py" > tests/integration/test_local_upload_order_probe.py
python3 - <<'PY'
from pathlib import Path
p = Path("tests/integration/test_local_upload_order_probe.py")
s = p.read_text()
changes = [
("    loop_state = {}\n    from loguru import logger as test_logger\n",
"""    loop_state = {}
    llm_observation = {}
    llm_started = threading.Event()
    llm_done = threading.Event()
    original_llm_put = llm_queue.put
    def put_before_cleanup(item, *args, **kwargs):
        original_llm_put(item, *args, **kwargs)
        if not llm_started.wait(timeout=5):
            raise TimeoutError("LLM consumer did not start before cleanup")
    llm_queue.put = put_before_cleanup
    from loguru import logger as test_logger
"""),
("        def process(self, **kwargs):\n            assert kwargs[\"skip_calibration\"] is True\n",
 "        def process(self, **kwargs):\n"
 "            llm_observation[\"media_exists_during_llm\"] = source_path.exists()\n"
 "            llm_started.set()\n"
 "            assert kwargs[\"skip_calibration\"] is True\n"),
("    async def dispatch_and_wait():\n",
"""    def consume_llm_task():
        task = llm_queue.get()
        llm_observation["task"] = task
        try:
            from video_transcript_api.api.context import run_with_runtime
            run_with_runtime(runtime, llm_ops._handle_llm_task, task)
        finally:
            llm_done.set()
    llm_consumer = threading.Thread(target=consume_llm_task, name="upload-llm-probe")
    llm_consumer.start()
    async def dispatch_and_wait():
"""),
("""    assert llm_queue.qsize() == 1
    llm_task = llm_queue.get_nowait()
    assert llm_task["task_id"] == task_id
    assert llm_task["media_id"] == upload["media_id"]
    assert llm_task["processing_options"] == options

    from video_transcript_api.api.context import run_with_runtime

    run_with_runtime(runtime, llm_ops._handle_llm_task, llm_task)
""",
"""    assert llm_done.wait(timeout=3)
    llm_consumer.join(timeout=3)
    assert not llm_consumer.is_alive()
    assert llm_queue.empty()
    llm_task = llm_observation["task"]
    assert llm_task["task_id"] == task_id
    assert llm_task["media_id"] == upload["media_id"]
    assert llm_task["processing_options"] == options
    assert llm_observation["media_exists_during_llm"] is True
"""),
]
for old, new in changes:
    assert s.count(old) == 1, (old[:50], s.count(old))
    s = s.replace(old, new, 1)
p.write_text(s)
PY
uv sync --frozen
PYTHONPATH="$PWD/src" uv run --frozen pytest -vv tests/integration/test_local_upload_order_probe.py::test_dispatcher_uses_real_transcriber_then_cleans_owned_media_before_llm
```

实际结果：1 passed；输出断言 `media exists when actual LLM task consumer starts; removal follows transcription finally`。
