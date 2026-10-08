# C 卡复审结论（冻结 H1）

failure-visibility: p2-only

## 范围与结论

- 增量：99780aff6b8dc567146b6a61e402007423d89db8..c216f6f07f90ab34322606ad604919a65e3d78f5。四问均通过：只补 ASR 产物归属及 LLM 前清理；无新抽象、无无据状态/回退、URL 与本地上传仍各走原分支。
- 完整复验：c7b38563eb2f06f5ca36c06c6c7da7de9c5e9995..c216f6f07f90ab34322606ad604919a65e3d78f5，18 文件、1558 增/81 删。未读 progress/C-progress.md、实现报告或旧 review 正文。
- 结论：4 项 P2，未确认 P1。交付阻断：是，因临时预算、URL 历史过滤及受理通知违反本卡明确合同；P2 严重度与阻断判断分开。新增异常捕获也违反明确边界。
- OCR 前置扫描：skipped（primary 与 DeepSeek 均 leg_timeout），不算 clean。主干基线不可用：gh api request failed。
- 配置缺少或关闭时上传保持关闭；生产 systemd、真实 ASR 容量、部署额度、hosted CI 和恢复域仍未验证。

## Findings

1. P2 — 临时盘预算只在完整 ASR 之后核对。R7 要求 upload_temp_budget 覆盖源文件、音轨、中间产物及预留。uploads.py:274-284 先按声明大小两倍预留；transcription.py:2544-2564 才在音轨及 ASR 产物都写完后重新计数并拒绝超限。合成探针在预留 200 字节后写入 360 字节，最终检查才拒绝；所以处理期间实际占用可以超过已预留预算。影响是并行任务可先消耗超额临时盘。个人环境无真实容量数据，定 P2；不得据此宣称生产有硬上限。
2. P2 — source=url 没有 URL 专属过滤。audit.py:253 声明来源过滤支持 upload/url，:266-268 只处理 upload；source=url 落入 :418-464 的通用 CTE，没排除 local_upload。真实 HTTP/SQLite 探针用 source=url 查询，结果仍含 platform=local_upload 的 root 及 URL 任务。自己的历史筛选会混入上传记录；不泄露正文或越权。
3. P2 — 有 source_url 的上传受理通知不含“本地上传”。uploads.py:355-363 在 source_url 存在时只传该 URL；NotificationRouter.send_view_link (router.py:238-250) 只渲染该值、标题及 upload_ 分享链接。实际上传路由到企业微信 HTTP JSON 的探针确认标题、来源 URL、分享 URL 均在 payload，而“本地上传”不在。违反卡面通知字段合同，且永久源 URL 不代表来源类型。
4. P2 — uploads.py:354-366 新增宽泛 except Exception，记录日志后仍返回 202。卡面明确禁止新增 catch；路由/通知异常会被吸收，调用方只看见已接受而看不到受理通知异常。日志可查，故不列 P1；仍属未经授权的异常处理边界。

## 不变式到实现/测试

1. raw bytes、服务端路径、持久 admission/dispatcher 与处理选项：uploads.py、CacheManager.accept_local_upload、transcription.process_task_queue；test_upload_intake.py、test_local_upload_lifecycle.py。CapsWriter token 契约由 test_capswriter_contract.py 锁；额外探针实际调用 Transcriber/CapsWriterClient 文件 producer，仅替换最终 ASR SDK 边界。
2. LLM 前清理：transcription.py:841-845 与 2703-2719、TempFileManager.clean_up_task。并发真实队列消费者测试断言源、任务目录和 ASR 产物在处理入口前不存在；清理异常探针断言异常传播且 LLM 队列为空。H0 红验在同一用例上实际 AssertionError：source_exists 为 True。
3. 首终态与期限：cache_manager.py:3002-3013；unit_local_upload_policy.py 与 test_local_upload_lifecycle.py。terminal_at 是本地上传首终态标记，completed_at 仍为 root 时钟真源；terminal_at IS NULL 与 write-once trigger 锁住首个 expires_at。它区分 never 的 expires_at=NULL 与尚未终态，服务迁移回填及重复终态写，不是第二套任务状态机。
4. 空旧 token、Resolver、只读 history：view_token_resolver.py、views.py、audit.py；test_view_token_resolver.py、test_history_routes.py、test_local_upload_lifecycle.py。source=url 漏过滤见 Finding 2。
5. 三个 reprocess owner/share 检查及 child 插入事务：tasks.py、cache_manager.py；test_local_upload_lifecycle.py 中三条实际 route 用例锁定，选定回归集通过。
6. 通知：uploads.py、task_formatter.py、terminal_status.py、completion_share.py；test_notification_e2e_delivery.py 的完成通知真实 HTTP payload 通过；受理通知真实 HTTP payload 的缺标签由 Finding 3 捕获。
7. URL 旧清理/状态路径：transcription.py、tempfile_manager.py；test_temp_cleanup_integration.py、test_tempfile_manager.py、test_task_status_cleanup.py、test_cache_cleanup.py 通过；代码核对 URL finally 分支仍按原捕获语义处理。

## 验证与限制

- H1 通过：test_upload_worker.py、test_local_upload_lifecycle.py、test_upload_intake.py、test_notification_e2e_delivery.py、test_capswriter_contract.py、test_cache_cleanup.py、test_task_status_cleanup.py、test_view_token_resolver.py、test_history_routes.py、test_temp_cleanup_integration.py、test_tempfile_manager.py、test_task_status_lifecycle.py、test_failure_status_persistence.py、test_upload_routes.py、test_task_status.py。
- 裸子进程环境 + 合成 SQLite：test_local_upload_policy.py::test_resolver_uses_real_subprocess_environment_and_sqlite 通过；缺 VTA_UPLOADS_ENABLED 拒绝并输出 UPLOAD_DISABLED，无正文；true 返回成功正文。
- H0 红验：scratch-worktree.sh 在 99780... 树复制 H1 改动后的生命周期测试并运行 test_dispatcher_uses_real_transcriber_then_cleans_owned_media_before_llm；实际断言 source_exists 为 False 时观察到 True，非导入/schema 错误；scratch 已自动删除。
- 所有 pytest 的 tests/conftest.py 均报告 outbound network guard active。真实源媒体、生产配置、systemd、ASR/LLM 外部服务均未触碰。
- git diff --check 通过；只新增本 verdict。scratch 初始无 data/logs/.venv；pytest 后有空 data/temp、4 个 .venv 链接、163 个 pytest-tmp 临时目录链接及 base-red.log.tmp 的 1 个链接。config/config.jsonc、logs 仍缺，普通硬链接为 0；这些本轮 scratch 产物留存，未读内容或清理。

## 附录 A：历史筛选与受理通知 HTTP 探针源码

以下测试临时放在 tests/integration/test_review_probes.py；命令为 PYTHONPATH=src:tests/integration python -m pytest -q tests/integration/test_review_probes.py。仅最终企业微信 HTTP POST 被替换；SQLite、上传路由、NotificationRouter 和消息 producer 均真实。

~~~python
import asyncio
import base64
import datetime
import json
import threading
import uuid
from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_local_upload_lifecycle import OWNER, _make_upload, lifecycle_client
from video_transcript_api.api.routes import audit as audit_routes
from video_transcript_api.api.routes import uploads as uploads_routes
from video_transcript_api.api.services import transcription
from video_transcript_api.api.services.transcription import verify_token
from video_transcript_api.cache.cache_manager import CacheManager
from video_transcript_api.utils.logging.audit_logger import AuditLogger
from video_transcript_api.utils.notifications import channel as channel_module
from video_transcript_api.utils.notifications import router as router_module
from video_transcript_api.utils.notifications import wechat as wechat_module
from video_transcript_api.utils.notifications.router import NotificationRouter
from video_transcript_api.utils.tempfile_manager import TempFileManager
from wecom_notifier import WeComNotifier
from wecom_notifier.platforms.wecom import sender as wecom_sender
def test_source_url_filter_includes_upload_snapshot(lifecycle_client, tmp_path, monkeypatch):
    client, cache, _, _ = lifecycle_client
    audit = AuditLogger(str(tmp_path / "audit.db"))
    cache.audit_logger = audit
    monkeypatch.setattr(audit_routes, "audit_logger", audit)
    upload = _make_upload(cache, title="synthetic upload")
    url_task = cache.create_task(url="https://url.example.test/item", platform="youtube", submitted_by="alice")
    cache.update_task_status(url_task["task_id"], "success", platform="youtube", title="synthetic URL")
    response = client.get("/api/audit/history?source=url&status=all&limit=100")
    assert response.status_code == 200
    items = {item["task_id"]: item for item in response.json()["data"]["items"]}
    assert items[upload["root_task_id"]]["platform"] == "local_upload"
    assert url_task["task_id"] in items
def test_accepted_upload_final_http_payload_has_no_local_label(tmp_path, monkeypatch):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=synthetic"
    config = {"storage": {"temp_dir": str(tmp_path / "temp"), "upload_limits": {
        "max_file_mib": 1, "max_media_hours": 1, "receive_concurrency": 1,
        "upload_temp_budget_mib": 4}}, "wechat": {"webhook": webhook}}
    records, condition = [], threading.Condition()
    class Response:
        def json(self): return {"errcode": 0, "errmsg": "ok"}
    def post(url, *, json, **_kwargs):
        with condition:
            records.append(json)
            condition.notify_all()
        return Response()
    monkeypatch.setattr(wecom_sender.requests, "post", post)
    monkeypatch.setattr(router_module, "load_config", lambda: config)
    monkeypatch.setattr(channel_module, "load_config", lambda: config)
    monkeypatch.setattr(wechat_module, "load_config", lambda: config)
    monkeypatch.setattr("video_transcript_api.utils.rendering.get_base_url", lambda: "https://share.example.test")
    notifier = WeComNotifier(max_retries=0)
    monkeypatch.setattr(wechat_module, "_get_global_notifier", lambda: notifier)
    router = NotificationRouter()
    cache = CacheManager(str(tmp_path / "cache"))
    temp_manager = TempFileManager(config["storage"]["temp_dir"])
    q = asyncio.Queue(maxsize=2)
    runtime = SimpleNamespace(started=True, upload_receiver_slots=threading.BoundedSemaphore(1),
        reserve_upload_temp=lambda _task, size, budget, _dir: size <= budget,
        release_upload_temp=lambda _task: None)
    inflight = SimpleNamespace(try_register=lambda *_args: True, release=lambda *_args: None)
    class ReadyProcessor:
        done = lambda _self: False
        get_coro = staticmethod(lambda: transcription.process_task_queue)
    user = {**OWNER, "wechat_webhook": webhook}
    async def current_user(): return dict(user)
    monkeypatch.setenv("VTA_UPLOADS_ENABLED", "true")
    for name, value in (("get_cache_manager", lambda: cache), ("get_config", lambda: config),
        ("get_task_queue", lambda: q), ("get_temp_manager", lambda: temp_manager),
        ("get_inflight_registry", lambda: inflight), ("get_notification_router", lambda: router)):
        monkeypatch.setattr(uploads_routes, name, value)
    app = FastAPI()
    app.state.runtime, app.state.queue_processor = runtime, ReadyProcessor()
    app.include_router(uploads_routes.router)
    app.dependency_overrides[verify_token] = current_user
    raw = b"synthetic upload"
    metadata = {"filename": "synthetic.mp4", "byte_size": len(raw), "title": "Synthetic title",
        "source_url": "https://source.example.test/item", "retention": "30d",
        "processing_options": {"calibrate": False, "summarize": False}}
    key = f"{int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)}-{uuid.uuid4()}"
    try:
        with TestClient(app) as client:
            response = client.post("/api/uploads", content=raw, headers={
                "Authorization": "Bearer synthetic", "Content-Type": "application/octet-stream",
                "Idempotency-Key": key, "X-Upload-Metadata": base64.urlsafe_b64encode(
                    json.dumps(metadata, separators=(",", ":")).encode()).decode().rstrip("=")})
        assert response.status_code == 202
        with condition:
            assert condition.wait_for(lambda: len(records) == 1, timeout=5)
        content = records[0]["markdown_v2"]["content"]
        assert "/view/upload_" in content and "Synthetic title" in content
        assert "https://source.example.test/item" in content
        assert "本地上传" not in content
    finally:
        for manager in notifier.webhook_managers.values():
            manager._stop_flag.set()
            manager.worker_thread.join(timeout=5)
        cache.close()
~~~

## 附录 B：ASR 产物、预算、清理失败探针源码

以 scratch H1 的真实代码运行，mock 最后一个 CapsWriter SDK/网络边界；命令：PYTHONPATH=src python runtime-probe.py。断言通过，输出仅含合成值。

~~~python
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import queue
import shutil
import threading
from video_transcript_api.api.context import RuntimeContext
from video_transcript_api.api.services import transcription
from video_transcript_api.transcriber import capswriter_client as cw
from video_transcript_api.transcriber import transcriber as tm
from video_transcript_api.transcriber.transcriber import Transcriber
with TemporaryDirectory() as root:
    root = Path(root)
    taskdir = root / "owned-task"
    taskdir.mkdir()
    audio = taskdir / "input.wav"
    audio.write_bytes(b"synthetic audio")
    cw.Config.load_from_project_config = classmethod(lambda cls: None)
    cw.load_config = lambda: {"storage": {"temp_dir": str(root / "workspace")}}
    tm.load_config = lambda: {"capswriter": {"server_url": "ws://127.0.0.1:6006", "max_retries": 1}}
    tm.get_workspace_dir = lambda: str(root / "workspace")
    old_sdk = cw.transcribe_file_sync
    cw.transcribe_file_sync = lambda *_a, **_k: SimpleNamespace(
        raw={"text_accu": "producer text", "task_id": "synthetic", "time_start": 1., "time_complete": 2.},
        tokens=["producer text"], timestamps=[1.], duration=1.)
    try:
        tr = Transcriber({"capswriter": {"server_url": "ws://127.0.0.1:6006", "max_retries": 1}})
        tr.output_dir = tr.capswriter_client.output_dir = str(taskdir)
        cw.Config.generate_txt = True
        cw.Config.generate_json = cw.Config.generate_merge_txt = cw.Config.generate_srt = False
        cw.Config.generate_lrc = cw.Config.generate_funasr_compat = False
        out = Path(tr.transcribe(str(audio), "probe", media_duration=1.)["txt_path"])
        assert out == taskdir / "input.txt" and out.read_text() == "producer text"
        shutil.rmtree(taskdir)
        assert not out.exists()
    finally:
        cw.transcribe_file_sync = old_sdk
with TemporaryDirectory() as root:
    rt = RuntimeContext.__new__(RuntimeContext)
    rt._upload_budget_guard, rt._upload_reserved_bytes = threading.Lock(), {}
    assert rt.reserve_upload_temp("t", 200, 200, root)
    td = Path(root) / "t"
    td.mkdir()
    for name, size in (("source", 100), ("audio", 240), ("asr", 20)):
        (td / name).write_bytes(b"x" * size)
    actual = sum(p.stat().st_size for p in td.iterdir())
    assert actual == 360 and actual > 200
    assert not rt.reserve_upload_temp("t", actual, 200, root)
q = queue.Queue()
old_manager, old_queue = transcription.get_temp_manager, transcription.llm_task_queue
class BrokenTemp:
    def clean_up_task(self, _task_id): raise OSError("synthetic cleanup failure")
transcription.get_temp_manager = lambda: BrokenTemp()
transcription.llm_task_queue = q
try:
    try:
        transcription._handoff_to_llm_stage(
            "synthetic-task", {"platform": "local_upload"}, calibrating_status_kwargs={},
            task_notifier=None, log_context="probe", observability={})
    except OSError as exc:
        assert str(exc) == "synthetic cleanup failure"
    else:
        raise AssertionError("cleanup did not propagate")
    assert q.empty()
finally:
    transcription.get_temp_manager, transcription.llm_task_queue = old_manager, old_queue
print("producer path/cleanup, over-budget post-check, and cleanup-failure queue assertions passed")
~~~
