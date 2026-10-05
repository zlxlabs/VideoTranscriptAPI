"""转录可观测性：上游 task_id、进度日志与失败原因透传。

对应生产事故（4.9 小时直播录制判 failed，事后既不知道卡在哪个子阶段，也不知道
真因）。本文件锁住三条不变式：``capswriter_task_id=`` 出现在进度与终态日志里、
``on_progress`` 确实交给 SDK 且进度按间隔节流（回调内异常不打断转录）、失败原因
随异常消息走到通知用的那条 error_msg。

SDK 一律用 mock 造假载荷（本机装的 SDK 与生产 pin 可能不同步）；也不依赖 ``/tmp``
下的预置 fixture——那种测试在 CI 里恒 skip，等于没有守卫。
"""

from unittest.mock import MagicMock, patch

import pytest
from capswriter_asr import AsrError, Transcript
from loguru import logger as loguru_logger

from video_transcript_api.transcriber import capswriter_client as cc
from video_transcript_api.transcriber.capswriter_client import CapsWriterClient, Config
from video_transcript_api.transcriber.transcriber import Transcriber

#: 上游真实 task_id 的形状（SDK 客户端侧 uuid4）。
TASK_ID = "3f6b1c9e-6d0a-4a1e-9f2b-8c7d5e4f3a21"
#: 4.9 小时现场：媒体总时长（秒）。
MEDIA_DURATION = 17501.67


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / "audio.mp4"
    path.write_bytes(b"fixture")
    return path


@pytest.fixture
def quiet_config(monkeypatch):
    monkeypatch.setattr(Config, "server_addr", "test-server")
    monkeypatch.setattr(Config, "server_port", 6010)
    monkeypatch.setattr(Config, "generate_funasr_compat", False)


def _make_client(tmp_path, max_retries=1):
    client = CapsWriterClient.__new__(CapsWriterClient)
    client.output_dir = str(tmp_path)
    client.max_retries = max_retries
    client.retry_delay = 0
    client.last_failure_detail = ""
    records: list = []
    client.log = lambda message, level="info": records.append(message)
    return client, records


def _final_transcript(task_id=TASK_ID):
    body = "今天超绝"
    return Transcript(
        text=body,
        tokens=list(body),
        timestamps=[0.0, 0.5, 1.0, 1.5],
        duration=2.0,
        raw={
            "task_id": task_id,
            "time_start": 10.0,
            "time_complete": 12.5,
            "text_accu": body,
        },
    )


def _sdk(final=None, progress=(), side_effect=None):
    """模拟 SDK：先回调若干进度帧，再返回 final（或抛出）。"""

    def _transcribe_file_sync(*args, **kwargs):
        for payload in progress:
            kwargs["on_progress"](payload)
        if side_effect is not None:
            raise side_effect
        return final

    return patch.object(cc, "transcribe_file_sync", side_effect=_transcribe_file_sync)


def _hits(records, literal):
    return [line for line in records if literal in line]


def test_sdk_gets_on_progress_and_task_id_reaches_progress_and_terminal_logs(
    tmp_path, quiet_config, audio
):
    """开始（首个进度帧）与终态日志都必须带 SDK 给的真实 task_id。"""
    client, records = _make_client(tmp_path)
    progress = [
        {"type": "result", "is_final": False, "task_id": TASK_ID, "duration": 600.0},
        {"type": "result", "is_final": False, "task_id": TASK_ID, "duration": 900.0},
    ]
    with _sdk(final=_final_transcript(), progress=progress) as sdk_call:
        success, _ = client.transcribe_file(str(audio), media_duration=MEDIA_DURATION)

    assert success is True
    assert callable(sdk_call.call_args.kwargs["on_progress"])
    assert _hits(records, f"capswriter_task_id={TASK_ID}")
    assert _hits(records, "capswriter_progress=")
    assert _hits(records, f"capswriter_done capswriter_task_id={TASK_ID}")
    # 开始行：本次尝试的媒体与预算，进度行：已处理时长与百分比。
    start = _hits(records, "capswriter_start ")
    assert start and "attempt=1/1" in start[0] and "deadline_total=" in start[0]
    first = _hits(records, "capswriter_progress=")[0]
    assert "processed=600.0s" in first and "percent=3.4%" in first


def test_task_id_comes_from_sdk_payload_never_fabricated(tmp_path, quiet_config, audio):
    """SDK 没给 task_id 时只能诚实写 unknown，不许编一个占位 id。"""
    client, records = _make_client(tmp_path)
    with _sdk(final=_final_transcript(task_id=None)):
        success, _ = client.transcribe_file(str(audio), media_duration=10.0)

    assert success is True
    task_id_lines = [line for line in records if "capswriter_task_id=" in line]
    assert task_id_lines
    assert all("capswriter_task_id=unknown" in line for line in task_id_lines)


def test_progress_log_is_throttled_but_first_event_always_logged(
    tmp_path, quiet_config, audio
):
    """长任务每秒多条事件，只按间隔打一行；首个事件必打（它带真实 task_id）。"""
    client, records = _make_client(tmp_path)
    progress = [
        {"type": "result", "is_final": False, "task_id": TASK_ID, "duration": i}
        for i in range(200)
    ]
    with _sdk(final=_final_transcript(), progress=progress):
        success, _ = client.transcribe_file(str(audio), media_duration=MEDIA_DURATION)

    assert success is True
    lines = _hits(records, "capswriter_progress=")
    assert len(lines) == 1, "200 条进度事件打了两行以上，节流失效"
    assert lines[0].startswith("capswriter_progress=1 ")


def test_throttle_reopens_after_interval():
    """节流窗口走完后，下一个事件必须重新打一行（否则长任务会再次静默）。"""
    records: list = []
    observer = cc._TranscriptionObserver(records.append, MEDIA_DURATION)
    payload = {"task_id": TASK_ID, "duration": 120.0}
    observer.on_progress(payload)
    observer.on_progress(payload)  # 窗口内：静默
    observer.last_logged_at -= cc.PROGRESS_LOG_INTERVAL_SECONDS
    observer.on_progress(payload)  # 窗口外：再打一行
    assert [line.split(" ", 1)[0] for line in records] == [
        "capswriter_progress=1",
        "capswriter_progress=3",
    ]


def test_progress_without_known_total_omits_percent(tmp_path, quiet_config, audio):
    """媒体总时长未知时只写已处理秒数，绝不编百分比。"""
    client, records = _make_client(tmp_path)
    with _sdk(final=_final_transcript(), progress=[{"task_id": TASK_ID, "timestamps": [0.0, 42.5]}]):
        client.transcribe_file(str(audio))

    line = _hits(records, "capswriter_progress=")[0]
    assert "processed=42.5s" in line and "percent=" not in line


def test_progress_callback_exception_does_not_break_transcription(
    tmp_path, quiet_config, audio
):
    """回调是第三方边界：观测代码抛异常只记 warning，转录照常成功。"""
    client, records = _make_client(tmp_path)
    warnings: list = []
    sink_id = loguru_logger.add(lambda m: warnings.append(str(m)), level="WARNING")
    try:
        with _sdk(
            final=_final_transcript(),
            progress=[object()],  # 非 dict 载荷：payload.get 直接抛异常
        ):
            success, generated = client.transcribe_file(str(audio), media_duration=10.0)
    finally:
        loguru_logger.remove(sink_id)

    assert success is True and generated
    assert any("capswriter_progress" in line and "已忽略" in line for line in warnings)
    assert _hits(records, f"capswriter_done capswriter_task_id={TASK_ID}")


def test_failure_reason_travels_into_notification_message(tmp_path, quiet_config, audio):
    """AsrError 的 code 与原因必须出现在供通知使用的异常消息里。"""
    client, records = _make_client(tmp_path)
    error = AsrError("timeout", "转录超过自动预算：远端转录阶段超时", retryable=False)

    transcriber = Transcriber.__new__(Transcriber)
    transcriber.output_dir = str(tmp_path)
    transcriber.capswriter_client = client
    with _sdk(side_effect=error):
        with pytest.raises(RuntimeError) as excinfo:
            transcriber.transcribe(str(audio), "out_base")

    message = str(excinfo.value)
    assert message.startswith(f"转录文件失败: {audio}")
    assert "code=timeout" in message
    assert "原因: 转录超过自动预算：远端转录阶段超时" in message
    # 失败终态单独一行，且此时上游一个结果帧都没回来，只能诚实写 unknown。
    failed = _hits(records, "capswriter_failed")
    assert failed and "capswriter_task_id=unknown" in failed[0]
    assert any("code=timeout" in line for line in _hits(records, "转录文件失败"))


def test_process_transcription_notifies_with_failure_reason(
    tmp_path, quiet_config, monkeypatch
):
    """端到端一格：真实客户端 + 真实 Transcriber 的失败原因进 notify_error。

    只把 SDK 边界（transcribe_file_sync）换成 mock——这条链上其余每一格都是
    生产代码，包括 process_transcription 的异常收口。
    """
    from video_transcript_api.api.services import transcription as tx

    downloader = MagicMock()
    downloader.last_media_duration = MEDIA_DURATION
    downloader.get_download_info.return_value = MagicMock(
        downloaded=False,
        local_file=None,
        download_url="http://x/v.mp4",
        filename="v.mp4",
    )
    downloader.get_subtitle.return_value = None
    media = tmp_path / "v.mp4"
    media.write_bytes(b"fixture")
    downloader.download_file.return_value = str(media)

    client, records = _make_client(tmp_path)

    def _real_transcriber(*args, **kwargs):
        transcriber = Transcriber.__new__(Transcriber)
        transcriber.output_dir = str(tmp_path)
        transcriber.capswriter_client = client
        return transcriber

    finalize = MagicMock(return_value=True)
    monkeypatch.setattr(tx, "_ensure_audio_track", lambda path: None)
    monkeypatch.setattr(tx, "get_temp_manager", lambda: MagicMock())
    monkeypatch.setattr(tx, "create_downloader", lambda url: downloader)
    monkeypatch.setattr(tx, "Transcriber", _real_transcriber)
    monkeypatch.setattr(tx, "finalize_terminal_status_and_notify", finalize)
    monkeypatch.setattr(tx, "cache_manager", MagicMock())
    monkeypatch.setattr(tx, "get_notification_router", lambda: MagicMock())

    error = AsrError("timeout", "转录超过自动预算：远端转录阶段超时", retryable=False)
    with _sdk(side_effect=error):
        result = tx.process_transcription("t1", "https://example.com/v.mp4")

    notify_error = finalize.call_args.kwargs["notify_error"]
    assert result["status"] == "failed"
    assert "code=timeout" in notify_error
    assert "原因: 转录超过自动预算：远端转录阶段超时" in notify_error
    # 既有形态保留：本地临时路径仍在通知文案里。
    assert str(media) in notify_error
    assert _hits(records, "capswriter_failed capswriter_task_id=unknown")


def test_successful_run_clears_stale_failure_detail(tmp_path, quiet_config, audio):
    """上一次失败的细节不能串进下一次调用（否则通知会报错的 code）。"""
    client, _ = _make_client(tmp_path)
    with _sdk(side_effect=AsrError("overloaded", "try again", retryable=False)):
        client.transcribe_file(str(audio), media_duration=10.0)
    assert "code=overloaded" in client.last_failure_detail
    with _sdk(final=_final_transcript()):
        success, _ = client.transcribe_file(str(audio), media_duration=10.0)
    assert success is True and client.last_failure_detail == ""


def test_progress_observability_adds_no_new_media_io(tmp_path, quiet_config, audio):
    """进度只能来自已有字段或 media_duration：观测代码不得自己去探测媒体。"""
    client, records = _make_client(tmp_path)
    with patch("subprocess.run", side_effect=AssertionError("进度观测不得探测媒体")):
        with _sdk(final=_final_transcript(), progress=[{"task_id": TASK_ID, "duration": 3.0}]):
            success, _ = client.transcribe_file(str(audio), media_duration=30.0)
    assert success is True
    assert "percent=10.0%" in _hits(records, "capswriter_progress=")[0]