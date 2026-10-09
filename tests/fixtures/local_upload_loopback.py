"""Loopback-only ASR, LLM, and notification consumers for the real API process."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import socket
import threading
import time
from typing import Any

import uvicorn
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse


CAPSWRITER_TEXT = (
    "这段转录由真实 CapsWriter SDK 从浏览器上传的 WAV 文件经 ffmpeg 转码后生成。"
    "本地上传会写入独立 SQLite 记录，经过实际任务队列处理，并通过公开只读链接提供正文。"
    "这个受控 loopback 服务只替代外部识别、模型和通知接收端，产品路由与序列化仍真实运行。"
)
LLM_TEXT = (
    CAPSWRITER_TEXT
    + "共享模型客户端已读取完整转录正文并完成校对；这段追加说明用于确认真实 API、"
    "SDK 输出与模型 HTTP 请求之间的内容链路完整。"
)


class LoopbackUpstreams:
    """Real HTTP/WebSocket server implementing the external protocol boundaries."""

    def __init__(self) -> None:
        self.app = FastAPI()
        self._lock = threading.Lock()
        self._asr_started = threading.Event()
        self._asr_release = threading.Event()
        self._asr_frames: list[str] = []
        self._asr_payload_bytes = 0
        self._asr_reply: dict[str, Any] | None = None
        self._llm_requests: list[dict[str, Any]] = []
        self._notifications: list[dict[str, Any]] = []
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(128)
        self.port = self._socket.getsockname()[1]
        self._server = uvicorn.Server(
            uvicorn.Config(self.app, log_level="error", access_log=False)
        )
        self._thread = threading.Thread(
            target=self._server.run,
            kwargs={"sockets": [self._socket]},
            name="upload-loopback-upstreams",
            daemon=True,
        )

        self.app.add_api_route("/health", self.health, methods=["GET"])
        self.app.add_api_route(
            "/v1/chat/completions", self.chat_completions, methods=["POST"]
        )
        self.app.add_api_route("/hooks/feishu", self.notification, methods=["POST"])
        self.app.add_api_route("/__e2e__/events", self.events, methods=["GET"])
        self.app.add_api_route(
            "/__e2e__/release-asr", self.release_asr, methods=["POST"]
        )
        self.app.add_api_websocket_route("/", self.capswriter)

    def start(self) -> None:
        self._thread.start()
        deadline = time.monotonic() + 15
        while not self._server.started:
            if not self._thread.is_alive():
                raise RuntimeError("loopback upstream server exited before startup")
            if time.monotonic() >= deadline:
                raise TimeoutError("loopback upstream server did not start")
            time.sleep(0.01)

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)
        if self._thread.is_alive():
            raise TimeoutError("loopback upstream server did not stop")

    async def health(self) -> dict[str, Any]:
        return {"protocol_version": 2, "encodings": ["flac"]}

    async def capswriter(self, websocket: WebSocket) -> None:
        await websocket.accept()
        final_frame: dict[str, Any] | None = None
        async for raw_frame in websocket.iter_text():
            frame = json.loads(raw_frame)
            decoded = base64.b64decode(frame["data"])
            with self._lock:
                self._asr_frames.append(raw_frame)
                self._asr_payload_bytes += len(decoded)
            if frame.get("is_final"):
                final_frame = frame
                self._asr_started.set()
                released = await asyncio.to_thread(self._asr_release.wait, 30)
                if not released:
                    raise TimeoutError("e2e did not release the controlled ASR response")
                tokens = list(CAPSWRITER_TEXT)
                timestamps = [index * 0.02 for index in range(len(tokens))]
                result = {
                    "type": "result",
                    "is_final": True,
                    "task_id": frame["task_id"],
                    "text": CAPSWRITER_TEXT,
                    "text_accu": CAPSWRITER_TEXT,
                    "tokens": tokens,
                    "timestamps": timestamps,
                    "duration": 3.0,
                    "time_start": frame["time_start"],
                    "time_submit": frame["time_start"],
                    "time_complete": frame["time_start"] + 1,
                }
                await websocket.send_json(result)
                with self._lock:
                    self._asr_reply = {
                        "task_id": result["task_id"],
                        "text_accu": result["text_accu"],
                        "token_count": len(tokens),
                        "timestamp_count": len(timestamps),
                        "duration": result["duration"],
                        "source": final_frame["source"],
                        "encoding": final_frame["encoding"],
                        "samples_total": final_frame.get("samples_total"),
                    }
                return

    async def chat_completions(self, request: Request) -> JSONResponse:
        raw = await request.body()
        body = json.loads(raw)
        with self._lock:
            self._llm_requests.append(
                {
                    "content_type": request.headers.get("content-type"),
                    "model": body.get("model"),
                    "messages": body.get("messages"),
                    "request_sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
        return JSONResponse(
            {
                "id": "chatcmpl-local-upload-e2e",
                "object": "chat.completion",
                "created": 1,
                "model": body.get("model", "local-e2e"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": LLM_TEXT},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 80,
                    "total_tokens": 180,
                },
            }
        )

    async def notification(self, request: Request) -> JSONResponse:
        raw = await request.body()
        with self._lock:
            self._notifications.append(
                {
                    "content_type": request.headers.get("content-type"),
                    "payload": json.loads(raw),
                    "raw_sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
        return JSONResponse({"code": 0, "msg": "success"})

    async def events(self) -> JSONResponse:
        with self._lock:
            body = {
                "asr_started": self._asr_started.is_set(),
                "asr_frames": list(self._asr_frames),
                "asr_payload_bytes": self._asr_payload_bytes,
                "asr_reply": self._asr_reply,
                "llm_requests": list(self._llm_requests),
                "notifications": list(self._notifications),
            }
        return JSONResponse(body)

    async def release_asr(self) -> JSONResponse:
        self._asr_release.set()
        return JSONResponse({"released": True})
