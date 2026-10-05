"""SDK 传输层回归：参数透传与「final 已送达就必须返回」。

第二条用例是 #166 的回归锁：上游 `b0818dc` 之前，`client.py` 的 `idle_watch`
在 CPython ≤3.11 上会被 `asyncio.wait_for` 吞掉取消，于是 `_transcribe_connected`
的 `finally: gather` 永久挂起——服务端早就按真实 UUID 回完 final，调用方仍然等到
预算到点才拿到 `AsrError(code=timeout)`（现场 120.152s，见
`docs/sessions/triage-261004/wire-probe.md`）。

回归必须在真实解释器上判定，因此它跑一个**子进程**：进程内起真实
`websockets.serve`（随机 loopback 端口 + 真实 `/health`），用真实 SDK 序列化帧
回 final 并**保持连接打开**，然后走本仓同步适配入口
`CapsWriterClient.transcribe_file`。子进程有父进程硬超时 kill，不会留下挂死进程。

该缺陷只在 ≤3.11 存在（3.12 用 `asyncio.timeout` 重写了 `wait_for`），因此
≥3.12 显式 skip；skip 不等于守住回归，见 design.md I6。
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from capswriter_asr import client as sdk_client
from capswriter_asr import Transcript

from video_transcript_api.transcriber.capswriter_client import CapsWriterClient, Config

#: 子进程内假服务端回的正语；产物正文必须逐字等于它。
_FINAL_TEXT = "sync entry body"

#: 0.2s / 16 kHz / 单声道 s16le = 16000 samples，SDK 应把这个数写进 final 帧。
_EXPECTED_SAMPLES_TOTAL = 16_000

#: 子进程硬截止（秒）。旧 pin 的红在 ~6s 内由断言给出，不靠这个超时收场。
_CHILD_HARD_DEADLINE_S = 45

#: 成功路径的硬边界：远小于 SDK 默认 120s 预算。
_SUCCESS_BUDGET_S = 10.0

_CHILD_SCRIPT = r'''
"""真实同步入口 + 真实 websockets 服务端（final 后保持连接打开）。"""
import asyncio, hashlib, importlib.metadata as md, json, os, struct, sys, threading, time, wave
from pathlib import Path

RESULT_TEXT = sys.argv[3]


def build_media(path):
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"".join(
            struct.pack("<h", int(3000 * ((i % 200) / 100 - 1))) for i in range(16000)
        ))


def main():
    from capswriter_asr import client as sdk_module
    from video_transcript_api.transcriber import capswriter_client as cc

    media_path, output_dir = Path(sys.argv[1]), Path(sys.argv[2])
    build_media(media_path)
    state = {"frames": [], "logs": [], "kept_open_after_final": False}
    ready = threading.Event()

    async def health(connection, request):
        if request.path != "/health":
            return None
        from websockets.datastructures import Headers
        from websockets.http11 import Response
        body = json.dumps({"protocol_version": 2, "encodings": ["flac"]}).encode()
        return Response(200, "OK", Headers([
            ("Content-Type", "application/json"), ("Content-Length", str(len(body)))]), body)

    async def handler(ws):
        try:
            while True:
                message = await ws.recv()
                raw = message.encode("utf-8") if isinstance(message, str) else bytes(message)
                frame = json.loads(raw)
                state["frames"].append({k: frame.get(k) for k in (
                    "task_id", "source", "encoding", "is_final",
                    "samples_total", "seg_duration", "seg_overlap")})
                if frame.get("is_final"):
                    state["task_id"] = frame["task_id"]
                    state["final_sent_at"] = time.monotonic()
                    await ws.send(json.dumps({
                        "type": "result", "is_final": True, "task_id": frame["task_id"],
                        "text": RESULT_TEXT, "tokens": list(RESULT_TEXT),
                        "timestamps": [round(i * 0.1, 3) for i in range(len(RESULT_TEXT))],
                        "duration": round(len(RESULT_TEXT) * 0.1, 3),
                        "text_accu": RESULT_TEXT, "time_start": 1.0, "time_complete": 2.0,
                    }))
                    # 这正是让旧 pin 挂死的形态：final 之后不关闭连接。
                    state["kept_open_after_final"] = True
                    await ws.wait_closed()
                    return
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            state["server_error"] = repr(exc)

    def serve():
        from websockets.asyncio.server import serve
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def start():
            server = await serve(handler, "127.0.0.1", 0, process_request=health,
                                 ping_interval=None, max_size=None)
            return server, server.sockets[0].getsockname()[1]

        server, port = loop.run_until_complete(start())
        state.update(loop=loop, server=server, port=port)
        ready.set()
        loop.run_forever()

    threading.Thread(target=serve, daemon=True).start()
    ready.wait(20)
    port = state["port"]

    client = cc.CapsWriterClient.__new__(cc.CapsWriterClient)
    client.output_dir = str(output_dir)
    client.max_retries, client.retry_delay = 1, 0
    client.log = lambda *a, **k: state["logs"].append(a[0] if a else "")
    cc.Config.server_addr, cc.Config.server_port = "127.0.0.1", port
    cc.Config.generate_txt, cc.Config.generate_merge_txt = True, True
    cc.Config.generate_json, cc.Config.generate_funasr_compat = False, True
    cc.Config.file_seg_duration, cc.Config.file_seg_overlap = 25, 2
    # 只缩小本测试自己这一次的 deadline，让旧 pin 的红以秒级出现而不是等满
    # 120s 自动预算；预算公式本身由 test_capswriter_deadline_budget.py 锁住。
    cc.DEADLINE_REALTIME_FACTOR, cc.DEADLINE_OVERHEAD_SECONDS = 0.1, 6.0

    started = time.monotonic()
    success, files = client.transcribe_file(str(media_path), media_duration=0.2)
    elapsed = time.monotonic() - started

    state["server"].close()  # sync in websockets.asyncio.server
    asyncio.run_coroutine_threadsafe(
        state["server"].wait_closed(), state["loop"]).result(10)
    state["loop"].call_soon_threadsafe(state["loop"].stop)

    print("PROBE_JSON=" + json.dumps({
        "returned": True, "success": success, "elapsed": round(elapsed, 3),
        "files": [str(f) for f in files],
        "file_bytes": {f.name: f.stat().st_size for f in files if f.is_file()},
        "frames": state["frames"], "task_id": state.get("task_id"),
        "final_sent_at": round(state.get("final_sent_at", 0.0) - started, 3),
        "kept_open_after_final": state["kept_open_after_final"],
        "logs": state["logs"][-6:],
        "python": sys.version.split()[0],
        "websockets": md.version("websockets"),
        "client_sha256": hashlib.sha256(open(sdk_module.__file__, "rb").read()).hexdigest(),
    }, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - surfaced as payload, not swallowed
        print("PROBE_JSON=" + json.dumps({"returned": False, "error": repr(exc)}), flush=True)
        os._exit(9)
    os._exit(0)
'''


#: T1 子进程硬截止（秒）。旧 pin 的红要真等满 120s 自动预算才以 timeout 收场，
#: 因此这里必须给到 120s 以上——提前 kill 会把红变成 subprocess.TimeoutExpired，
#: 证明不了「旧 pin 挂到预算到点」这件事。
_REFUSED_CHILD_HARD_DEADLINE_S = 200

#: T1 的行为阈值：从调用到拿到错误码的上限（秒）。
_REFUSED_FAST_FAIL_S = 5.0

#: T1 子进程：/health 通过、随后端口关闭，让 WebSocket 连接真正被拒。
_REFUSED_CHILD_SCRIPT = r'''
"""默认预算路径 + 真实「/health 通过但 WS 连接被拒」场景（issue #169 / 上游 #67）。"""
import hashlib, json, os, socket, struct, sys, threading, time, wave
from pathlib import Path


def build_media(path):
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"".join(
            struct.pack("<h", int(3000 * ((i % 200) / 100 - 1))) for i in range(16000)
        ))


def serve_health_once(host):
    """裸 socket 只回答一次 GET /health，随后关掉监听套接字 → 端口此后确定拒绝连接。

    必须用裸 socket 而不是 websockets.serve：SDK 的 _operation 先 _check_server 再
    转码再 connect，只有「/health 通过、WS 连接被拒」这一组合才会走进
    deadline_watch 的取消路径（上游 #67 的真实触发形态）。
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((host, 0))
    listener.listen(1)
    listener.settimeout(20)
    port = listener.getsockname()[1]
    state = {"health_served": False, "request_line": "", "accept_error": None}

    def run():
        try:
            conn, _ = listener.accept()
        except OSError as exc:
            state["accept_error"] = repr(exc)
            listener.close()
            return
        # 先关监听再回包：urllib 的连接已被 accept，关闭监听不影响它，
        # 但后续任何 connect 立刻被拒——避免「回包后、转码完成前」那个窗口里的竞争。
        listener.close()
        with conn:
            conn.settimeout(20)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
            body = json.dumps({"protocol_version": 2, "encodings": ["flac"]}).encode()
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         b"Content-Length: " + str(len(body)).encode() +
                         b"\r\nConnection: close\r\n\r\n" + body)
            state["request_line"] = buf.split(b"\r\n", 1)[0].decode("latin-1")
            state["health_served"] = True

    threading.Thread(target=run, daemon=True).start()
    return port, state


def port_is_closed(host, port):
    try:
        probe = socket.create_connection((host, port), timeout=5)
    except OSError as exc:
        return True, type(exc).__name__
    probe.close()
    return False, "connected"


def main():
    import capswriter_asr
    from capswriter_asr import client as sdk_module

    media_path = Path(sys.argv[1])
    host = "127.0.0.1"
    build_media(media_path)

    port, health = serve_health_once(host)

    started = time.monotonic()
    payload = {"port": port}
    try:
        capswriter_asr.transcribe_file_sync(
            str(media_path), f"ws://{host}:{port}", encoding="flac")
        payload.update(returned=True, code=None, message=None)
    except capswriter_asr.AsrError as exc:
        payload.update(returned=True, code=exc.code, message=exc.message)
    except BaseException as exc:  # noqa: BLE001 - surfaced as payload, not swallowed
        payload.update(returned=False, code=None, message=repr(exc))
    payload["elapsed"] = round(time.monotonic() - started, 3)

    # 端口已关闭的自查放在调用之后：监听套接字一旦关闭就再也不会 accept，
    # 此刻的连接必然被内核拒绝，所以这里探多少次都不会污染 /health 的那次 accept。
    closed, how = port_is_closed(host, port)
    payload.update(port_closed=closed, refusal=how,
                   health_served=health.get("health_served"),
                   request_line=health.get("request_line"),
                   accept_error=health.get("accept_error"))
    payload["python"] = sys.version.split()[0]
    payload["client_sha256"] = hashlib.sha256(
        open(sdk_module.__file__, "rb").read()).hexdigest()
    print("PROBE_JSON=" + json.dumps(payload, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - surfaced as payload, not swallowed
        print("PROBE_JSON=" + json.dumps({"returned": False, "error": repr(exc)}), flush=True)
        os._exit(9)
    os._exit(0)
'''


#: T3 子进程硬截止（秒）。用例本身应在秒级收场，这里只是防挂死的硬闸。
_TIMEOUT_CHILD_HARD_DEADLINE_S = 60

#: T3 的显式预算（秒）。必须小到能真触发超时，又大到本地准备（两次 ffmpeg）能走完。
_TIMEOUT_BUDGET_S = 1.0

#: T3 的音频时长（秒）。3200 samples / 16 kHz；SDK 在超时消息里回显的就是这个数。
_TIMEOUT_DURATION_S = 0.2

#: T3 子进程：连上、收帧、永不回 final。
_TIMEOUT_CHILD_SCRIPT = r'''
"""真实 websockets 服务端（连上但不回 final）+ 显式 deadline_total 触发真超时。"""
import asyncio, hashlib, importlib.metadata as md, json, os, struct, sys, threading, time, wave
from pathlib import Path


def build_media(path, samples):
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"".join(
            struct.pack("<h", int(3000 * ((i % 200) / 100 - 1))) for i in range(samples)
        ))


def main():
    import capswriter_asr
    from capswriter_asr import client as sdk_module

    budget = float(sys.argv[2])
    media_path = Path(sys.argv[1])
    build_media(media_path, int(sys.argv[3]))
    state = {"frames": 0, "final_seen": False, "connected": False}
    ready = threading.Event()

    async def health(connection, request):
        if request.path != "/health":
            return None
        from websockets.datastructures import Headers
        from websockets.http11 import Response
        body = json.dumps({"protocol_version": 2, "encodings": ["flac"]}).encode()
        return Response(200, "OK", Headers([
            ("Content-Type", "application/json"), ("Content-Length", str(len(body)))]), body)

    async def handler(ws):
        state["connected"] = True
        try:
            while True:
                message = await ws.recv()
                raw = message.encode("utf-8") if isinstance(message, str) else bytes(message)
                frame = json.loads(raw)
                state["frames"] += 1
                if frame.get("is_final"):
                    state["final_seen"] = True
                    # 收完最后一帧就是不回：让调用方只能被预算救回来。
                    await ws.wait_closed()
                    return
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            state["server_error"] = repr(exc)

    def serve():
        from websockets.asyncio.server import serve
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def start():
            server = await serve(handler, "127.0.0.1", 0, process_request=health,
                                 ping_interval=None, max_size=None)
            return server, server.sockets[0].getsockname()[1]

        server, port = loop.run_until_complete(start())
        state.update(loop=loop, server=server, port=port)
        ready.set()
        loop.run_forever()

    threading.Thread(target=serve, daemon=True).start()
    ready.wait(20)
    port = state["port"]

    started = time.monotonic()
    payload = {"port": port, "budget": budget}
    try:
        capswriter_asr.transcribe_file_sync(
            str(media_path), f"ws://127.0.0.1:{port}", encoding="flac",
            deadline_total=budget)
        payload.update(returned=True, code=None, message=None)
    except capswriter_asr.AsrError as exc:
        payload.update(returned=True, code=exc.code, message=exc.message)
    except BaseException as exc:  # noqa: BLE001 - surfaced as payload, not swallowed
        payload.update(returned=False, code=None, message=repr(exc))
    payload["elapsed"] = round(time.monotonic() - started, 3)
    payload.update(frames=state["frames"], final_seen=state["final_seen"],
                   connected=state["connected"], server_error=state.get("server_error"))
    payload["python"] = sys.version.split()[0]
    payload["websockets"] = md.version("websockets")
    payload["client_sha256"] = hashlib.sha256(
        open(sdk_module.__file__, "rb").read()).hexdigest()

    state["server"].close()  # sync in websockets.asyncio.server
    asyncio.run_coroutine_threadsafe(
        state["server"].wait_closed(), state["loop"]).result(10)
    state["loop"].call_soon_threadsafe(state["loop"].stop)

    print("PROBE_JSON=" + json.dumps(payload, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - surfaced as payload, not swallowed
        print("PROBE_JSON=" + json.dumps({"returned": False, "error": repr(exc)}), flush=True)
        os._exit(9)
    os._exit(0)
'''


def _run_child(script: str, tmp_path: Path, argv: list[str], timeout: int) -> dict:
    """在真实解释器的子进程里跑一段探针脚本，返回其 PROBE_JSON 负载。"""
    child = tmp_path / "child_probe.py"
    child.write_text(script, encoding="utf-8")
    child_env = dict(os.environ)
    child_env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"

    completed = subprocess.run(
        [sys.executable, str(child), *argv],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(Path(__file__).resolve().parents[2]),
        env=child_env,
    )
    assert completed.returncode == 0, (
        f"child exit={completed.returncode} stderr={completed.stderr[-800:]}"
    )
    marker = "PROBE_JSON="
    assert marker in completed.stdout, f"no probe payload: {completed.stdout[-800:]}"
    return json.loads(completed.stdout.split(marker, 1)[1].splitlines()[0])


def _make_client(output_dir: Path) -> CapsWriterClient:
    """Build a client without loading project configuration."""
    with patch.object(CapsWriterClient, "__init__", lambda self: None):
        client = CapsWriterClient()
    client.output_dir = str(output_dir)
    client.max_retries = 1
    client.retry_delay = 0
    client.log = MagicMock()
    return client


def test_sdk_transport_passes_config_and_writes_transcript_sidecars(
    tmp_path, monkeypatch
):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    media_path = tmp_path / "audio.mp4"
    media_path.write_bytes(b"fixture")
    client = _make_client(output_dir)

    monkeypatch.setattr(Config, "server_addr", "test-server")
    monkeypatch.setattr(Config, "server_port", 6010)
    monkeypatch.setattr(Config, "file_seg_duration", 37)
    monkeypatch.setattr(Config, "file_seg_overlap", 5)
    monkeypatch.setattr(Config, "generate_txt", True)
    monkeypatch.setattr(Config, "generate_merge_txt", False)
    monkeypatch.setattr(Config, "generate_json", False)
    monkeypatch.setattr(Config, "generate_funasr_compat", True)

    # text_accu is the body; the echo draft in transcript.text deliberately
    # differs from it, and the products must follow text_accu.
    transcript = Transcript(
        text="hallo",
        tokens=list("hello!"),
        timestamps=[0.0, 0.1, 0.2, 0.3, 0.4, 0.5],
        duration=1.5,
        raw={
            "task_id": "task-1",
            "time_start": 10.0,
            "time_complete": 12.5,
            "text_accu": "hello!",
        },
    )
    with patch(
        "video_transcript_api.transcriber.capswriter_client.transcribe_file_sync",
        return_value=transcript,
    ) as sdk_call:
        success, generated_files = client.transcribe_file(str(media_path))

    assert success is True
    assert set(generated_files) == {
        output_dir / "audio.txt",
        output_dir / "audio_funasr.json",
    }

    path, url = sdk_call.call_args.args
    keywords = sdk_call.call_args.kwargs
    assert path == media_path
    assert url.startswith("ws://")
    assert keywords["encoding"] == "flac"
    assert keywords["seg_duration"] == 37
    assert keywords["seg_overlap"] == 5
    assert "model" not in keywords

    assert (output_dir / "audio.txt").read_text(encoding="utf-8") == "hello!"
    sidecar = json.loads(
        (output_dir / "audio_funasr.json").read_text(encoding="utf-8")
    )
    assert sidecar["task_id"] == "task-1"
    assert sidecar["duration"] == 1.5
    assert sidecar["processing_time"] == 2.5
    assert sidecar["segments"] == [
        {"start_time": 0.0, "end_time": 0.5, "text": "hello!"}
    ]


@pytest.mark.skipif(
    sys.version_info >= (3, 12),
    reason=(
        "SDK #65 regression only observable on CPython <=3.11; on 3.12 "
        "asyncio.wait_for no longer swallows the cancel, so this test "
        "cannot observe the defect (design.md I6)"
    ),
)
def test_final_result_returns_and_writes_products_on_python311(tmp_path):
    """A delivered final must return products, not a budget timeout (issue #166)."""
    assert shutil.which("ffmpeg") is not None, "ffmpeg drives the real SDK transcode"

    media_path = tmp_path / "tone.wav"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    child = tmp_path / "child.py"
    child.write_text(_CHILD_SCRIPT, encoding="utf-8")

    child_env = dict(os.environ)
    child_env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"

    completed = subprocess.run(
        [sys.executable, str(child), str(media_path), str(output_dir), _FINAL_TEXT],
        capture_output=True,
        text=True,
        timeout=_CHILD_HARD_DEADLINE_S,
        cwd=str(Path(__file__).resolve().parents[2]),
        env=child_env,
    )
    assert completed.returncode == 0, (
        f"child exit={completed.returncode} stderr={completed.stderr[-800:]}"
    )
    marker = "PROBE_JSON="
    assert marker in completed.stdout, f"no probe payload: {completed.stdout[-800:]}"
    payload = json.loads(completed.stdout.split(marker, 1)[1].splitlines()[0])

    # The child ran the same installed SDK as this test session.
    assert payload["returned"] is True, payload.get("error")
    assert payload["client_sha256"] == hashlib.sha256(
        Path(sdk_client.__file__).read_bytes()
    ).hexdigest(), "child tested a different capswriter_asr than the test session"
    assert tuple(int(p) for p in payload["python"].split(".")) < (3, 12), payload["python"]

    # The fake server consumed the SDK's real serialized frame and answered the
    # real UUID, then deliberately kept the connection open.
    assert len(payload["frames"]) == 1, payload["frames"]
    frame = payload["frames"][0]
    assert frame["is_final"] is True and frame["encoding"] == "flac"
    assert frame["samples_total"] == _EXPECTED_SAMPLES_TOTAL
    assert frame["task_id"] == payload["task_id"]
    assert payload["kept_open_after_final"] is True
    assert 0 < payload["final_sent_at"] < payload["elapsed"], (
        f"final delivered at {payload['final_sent_at']}s, call returned at "
        f"{payload['elapsed']}s"
    )

    assert payload["success"] is True, (
        "sync entry did not return a transcript even though final was delivered: "
        f"elapsed={payload['elapsed']}s logs={payload['logs']}"
    )
    assert payload["elapsed"] < _SUCCESS_BUDGET_S, payload["elapsed"]

    produced = {Path(p).name: Path(p) for p in payload["files"]}
    assert set(produced) == {"tone.txt", "tone.merge.txt", "tone_funasr.json"}, sorted(produced)
    assert all(payload["file_bytes"].values()), "empty transcript product"
    assert produced["tone.txt"].read_text(encoding="utf-8").strip() == _FINAL_TEXT
    assert produced["tone.merge.txt"].read_text(encoding="utf-8").strip() == _FINAL_TEXT
    sidecar = json.loads(produced["tone_funasr.json"].read_text(encoding="utf-8"))
    assert sidecar["task_id"] == payload["task_id"]
    assert [segment["text"] for segment in sidecar["segments"]] == [_FINAL_TEXT]


@pytest.mark.skipif(
    sys.version_info >= (3, 12),
    reason=(
        "upstream #67 (wait_for swallowing the cancel) is only observable on "
        "CPython <=3.11; on 3.12 asyncio.wait_for no longer swallows it, so this "
        "test cannot observe the regression (design.md I1 / blind spot)"
    ),
)
def test_connection_refused_fails_fast_with_connection_lost(tmp_path):
    """A refused WebSocket must surface connection_lost in seconds, not at budget end (issue #169)."""
    assert shutil.which("ffmpeg") is not None, "ffmpeg drives the real SDK transcode"

    payload = _run_child(
        _REFUSED_CHILD_SCRIPT,
        tmp_path,
        [str(tmp_path / "tone.wav")],
        _REFUSED_CHILD_HARD_DEADLINE_S,
    )

    assert payload["returned"] is True, payload.get("message")
    # The child ran the same installed SDK as this test session.
    assert payload["client_sha256"] == hashlib.sha256(
        Path(sdk_client.__file__).read_bytes()
    ).hexdigest(), "child tested a different capswriter_asr than the test session"
    assert tuple(int(p) for p in payload["python"].split(".")) < (3, 12), payload["python"]

    # Precondition, self-checked inside the child: /health answered over real TCP,
    # then the listening socket closed so the SDK's connect is genuinely refused.
    assert payload["port_closed"] is True, payload
    assert payload["refusal"] == "ConnectionRefusedError", payload
    assert payload["health_served"] is True, payload
    assert payload["request_line"].startswith("GET /health HTTP/1.1"), payload["request_line"]

    assert payload["code"] == "connection_lost", (
        f"refused connect ended as {payload['code']!r} after {payload['elapsed']}s: "
        f"{payload['message']}"
    )
    assert payload["elapsed"] < _REFUSED_FAST_FAIL_S, (
        f"connection_lost took {payload['elapsed']}s (>= {_REFUSED_FAST_FAIL_S}s); "
        f"the call hung until the auto budget expired: {payload['message']}"
    )


def test_timeout_message_names_the_budget_and_its_seconds(tmp_path):
    """A timeout must say which budget ran out, how many seconds it had, and how long the audio was."""
    assert shutil.which("ffmpeg") is not None, "ffmpeg drives the real SDK transcode"

    payload = _run_child(
        _TIMEOUT_CHILD_SCRIPT,
        tmp_path,
        [
            str(tmp_path / "tone.wav"),
            str(_TIMEOUT_BUDGET_S),
            str(int(_TIMEOUT_DURATION_S * 16_000)),
        ],
        _TIMEOUT_CHILD_HARD_DEADLINE_S,
    )

    assert payload["returned"] is True, payload.get("message")
    assert payload["client_sha256"] == hashlib.sha256(
        Path(sdk_client.__file__).read_bytes()
    ).hexdigest(), "child tested a different capswriter_asr than the test session"

    # The real server saw a real WebSocket upgrade and consumed the SDK's real
    # final frame, then withheld the reply — that is what forces the budget out.
    assert payload["connected"] is True, payload
    assert payload["frames"] >= 1, payload
    assert payload["final_seen"] is True, payload
    assert payload["server_error"] is None, payload

    assert payload["code"] == "timeout", (
        f"silent server ended as {payload['code']!r}: {payload['message']}"
    )
    # It was the budget that ended the call, not some incidental hang.
    assert payload["elapsed"] < 30.0, payload["elapsed"]

    message = payload["message"]
    assert "deadline_total" in message, message
    assert f"{_TIMEOUT_BUDGET_S:.0f} 秒" in message, message
    assert f"音频 {_TIMEOUT_DURATION_S:.1f} 秒" in message, message
