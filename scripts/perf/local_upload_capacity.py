#!/usr/bin/env python3
"""Run a bounded local-only upload/download overlap experiment in a fresh temp tree.

This records a synthetic local observation, not a production limit or enablement
credential. The real API worker is allowed to reach only a loopback ASR rejector.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import http.server
import json
import os
from pathlib import Path
import platform
import socket
import socketserver
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timezone
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))


class ExperimentFailure(RuntimeError):
    """A sandbox measurement did not produce the promised evidence."""


class QuietMediaHandler(http.server.SimpleHTTPRequestHandler):
    requests_seen = 0
    request_intervals: list[tuple[float, float]] = []
    requests_guard = threading.Lock()

    def do_GET(self):
        started = time.monotonic()
        time.sleep(0.5)
        super().do_GET()
        with self.requests_guard:
            type(self).requests_seen += 1
            type(self).request_intervals.append((started, time.monotonic()))

    def log_message(self, _format, *args):
        return


class LoopbackRejectHandler(socketserver.BaseRequestHandler):
    connections_seen = 0
    requests_guard = threading.Lock()

    def handle(self):
        with self.requests_guard:
            type(self).connections_seen += 1
        self.request.recv(1024)
        self.request.sendall(b"HTTP/1.1 503 Controlled local ASR fixture\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")


class LoopbackThreadingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _make_wav(path: Path, duration_seconds: int) -> bytes:
    sample_rate = 48_000
    frames = duration_seconds * sample_rate
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(b"\x00\x00" * frames)
    return path.read_bytes()


def _tool_version(name: str) -> str:
    result = subprocess.run(
        [name, "-version"], check=False, capture_output=True, text=True, timeout=10
    )
    if result.returncode != 0:
        raise ExperimentFailure(f"{name}_version_probe_failed_rc_{result.returncode}")
    return result.stdout.splitlines()[0].replace(" ", "_")


def _write_synthetic_config(root: Path, *, api_port: int, asr_port: int) -> tuple[Path, Path]:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    from video_transcript_api.utils.logging import load_config

    config = load_config(PROJECT_ROOT / "config/config.example.jsonc", use_cache=False)
    data = root / "data"
    storage = config["storage"]
    storage["cache_dir"] = str(data / "cache")
    storage["workspace_dir"] = str(data / "workspace")
    storage["temp_dir"] = str(data / "temp")
    storage["audit_db"] = str(data / "audit.db")
    storage["upload_limits"] = {
        "max_file_mib": 4,
        "max_media_hours": 0.02,
        "receive_concurrency": 2,
        "upload_temp_budget_mib": 12,
    }
    config["api"]["host"] = "127.0.0.1"
    config["api"]["port"] = api_port
    config["api"]["auth_token"] = "sandbox-token-never-use-outside-this-run"
    config["web"]["base_url"] = f"http://127.0.0.1:{api_port}"
    config["capswriter"]["server_url"] = f"ws://127.0.0.1:{asr_port}"
    config["capswriter"]["max_retries"] = 1
    config["capswriter"]["retry_delay"] = 0
    config["capswriter"]["connection_timeout"] = 1
    config["funasr_spk_server"].pop("send_terms", None)
    config["security"]["download_url_allowlist"] = ["127.0.0.1/32"]
    config["wechat"]["webhook"] = ""
    config["feishu"]["webhook"] = ""
    config["log"]["file"] = str(data / "logs" / "sandbox-api.log")
    config_path = root / "sandbox-config.json"
    config_path.write_text(json.dumps(config, ensure_ascii=True), encoding="utf-8")
    users_path = root / "sandbox-users.json"
    users_path.write_text(
        json.dumps({"users": {
            config["api"]["auth_token"]: {"user_id": "sandbox-owner", "name": "Sandbox"}
        }}),
        encoding="utf-8",
    )
    return config_path, users_path


def _raw_upload(base_url: str, token: str, payload: bytes, starts: threading.Barrier) -> dict:
    key = f"{int(datetime.now(timezone.utc).timestamp() * 1000)}-{uuid.uuid4()}"
    metadata = {
        "filename": "bounded-synthetic.wav",
        "byte_size": len(payload),
        "title": "local capacity fixture",
        "source_url": None,
        "retention": "never",
        "processing_options": {"calibrate": False, "summarize": False},
    }
    metadata_header = base64.urlsafe_b64encode(
        json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    parsed = urlparse(base_url)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=20)
    path = "/api/uploads"
    connection.putrequest("POST", path)
    connection.putheader("Authorization", f"Bearer {token}")
    connection.putheader("Content-Type", "application/octet-stream")
    connection.putheader("Content-Length", str(len(payload)))
    connection.putheader("Idempotency-Key", key)
    connection.putheader("X-Upload-Metadata", metadata_header)
    connection.endheaders()
    starts.wait(timeout=5)
    started = time.monotonic()
    chunk_bytes = 64 * 1024
    for offset in range(0, len(payload), chunk_bytes):
        connection.send(payload[offset : offset + chunk_bytes])
        time.sleep(0.003)
    response = connection.getresponse()
    body = response.read()
    elapsed = time.monotonic() - started
    connection.close()
    if response.status != 202:
        detail = json.loads(body).get("detail", "no_detail")
        raise ExperimentFailure(f"upload_http_status_{response.status}_detail={detail}")
    receipt = json.loads(body)
    if not receipt.get("upload_id") or not receipt.get("task_id"):
        raise ExperimentFailure("upload_receipt_missing_identity")
    return {
        "upload_id": receipt["upload_id"],
        "task_id": receipt["task_id"],
        "elapsed_seconds": elapsed,
        "started_at": started,
        "ended_at": time.monotonic(),
    }


def _submit_controlled_url(base_url: str, token: str, url: str, starts: threading.Barrier) -> str:
    parsed = urlparse(base_url)
    body = json.dumps({
        "url": url,
        "use_speaker_recognition": False,
        "processing_options": {"calibrate": False, "summarize": False},
    }).encode("utf-8")
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=20)
    starts.wait(timeout=5)
    connection.request(
        "POST",
        "/api/transcribe",
        body=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    response = connection.getresponse()
    result = json.loads(response.read())
    connection.close()
    task_id = result.get("data", {}).get("task_id")
    if response.status != 200 or not task_id:
        raise ExperimentFailure(f"controlled_url_http_status_{response.status}")
    return task_id


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
    process.wait(timeout=10)


def _file_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def _wait_for_terminal(db_path: Path, task_ids: list[str], process: subprocess.Popen, timeout_seconds: int = 30) -> dict[str, str]:
    deadline = time.monotonic() + timeout_seconds
    states: dict[str, str] = {}
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ExperimentFailure("sandbox_api_exited_during_measurement")
        with sqlite3.connect(db_path) as connection:
            rows = connection.execute(
                "SELECT task_id, status FROM task_status WHERE task_id IN (%s)" % ",".join("?" for _ in task_ids),
                task_ids,
            ).fetchall()
        states = dict(rows)
        if len(states) == len(task_ids) and all(status in {"success", "failed"} for status in states.values()):
            return states
        time.sleep(0.1)
    raise ExperimentFailure("sandbox_tasks_did_not_reach_terminal_state")


def run_experiment(duration_seconds: int) -> int:
    if duration_seconds < 1 or duration_seconds > 30:
        raise ExperimentFailure("duration_seconds_must_be_between_1_and_30")

    with tempfile.TemporaryDirectory(prefix="local-upload-capacity-") as temporary:
        root = Path(temporary).resolve()
        if root == PROJECT_ROOT or PROJECT_ROOT in root.parents:
            raise ExperimentFailure("sandbox_must_be_outside_project_tree")
        wav_path = root / "bounded-synthetic.wav"
        payload = _make_wav(wav_path, duration_seconds)
        if len(payload) > 4 * 1024 * 1024:
            raise ExperimentFailure("synthetic_sample_exceeded_4_mib")

        from scripts.ops.local_upload_check import inspect_sample

        sample_info = inspect_sample(wav_path)
        if abs(sample_info["duration_seconds"] - duration_seconds) > 0.1:
            raise ExperimentFailure("ffprobe_duration_did_not_match_synthetic_source")

        with ExitStack() as cleanup:
            asr_fixture = LoopbackThreadingServer(("127.0.0.1", 0), LoopbackRejectHandler)
            asr_thread = threading.Thread(target=asr_fixture.serve_forever, daemon=True)
            cleanup.callback(asr_thread.join, 5)
            cleanup.callback(asr_fixture.server_close)
            cleanup.callback(asr_fixture.shutdown)
            asr_thread.start()

            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as port_socket:
                port_socket.bind(("127.0.0.1", 0))
                api_port = port_socket.getsockname()[1]

            config_path, users_path = _write_synthetic_config(
                root, api_port=api_port, asr_port=asr_fixture.server_address[1]
            )

            class SandboxMediaHandler(QuietMediaHandler):
                requests_seen = 0
                request_intervals: list[tuple[float, float]] = []

                def __init__(self, *args, **kwargs):
                    super().__init__(*args, directory=str(root), **kwargs)

            media_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SandboxMediaHandler)
            media_thread = threading.Thread(target=media_server.serve_forever, daemon=True)
            cleanup.callback(media_thread.join, 5)
            cleanup.callback(media_server.server_close)
            cleanup.callback(media_server.shutdown)
            media_thread.start()
            controlled_url = f"http://127.0.0.1:{media_server.server_address[1]}/{wav_path.name}"

            clean_env = {
                "PATH": os.environ["PATH"],
                "HOME": os.environ.get("HOME", str(root)),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "TZ": "UTC",
                "PYTHONPATH": str(PROJECT_ROOT / "src"),
                "VTAPI_USERS_JSON": str(users_path),
                "VTA_UPLOADS_ENABLED": "true",
            }
            service_log_path = root / "sandbox-api.log"
            with service_log_path.open("wb") as service_log:
                process = subprocess.Popen(
                    [sys.executable, str(PROJECT_ROOT / "main.py"), "--start", "--config", str(config_path)],
                    cwd=PROJECT_ROOT,
                    env=clean_env,
                    stdout=service_log,
                    stderr=subprocess.STDOUT,
                )
                cleanup.callback(_stop_process, process)
                time.sleep(2)
                if process.poll() is not None:
                    raise ExperimentFailure("sandbox_api_failed_to_start")

                base_url = f"http://127.0.0.1:{api_port}"
                parsed = urlparse(base_url)
                connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
                connection.request(
                    "GET",
                    "/api/uploads/capabilities",
                    headers={"Authorization": "Bearer sandbox-token-never-use-outside-this-run"},
                )
                readiness = connection.getresponse()
                capabilities = json.loads(readiness.read())
                connection.close()
                if readiness.status != 200 or capabilities.get("enabled") is not True:
                    raise ExperimentFailure("sandbox_upload_receiver_not_ready")

                starts = threading.Barrier(3)
                data_temp = root / "data" / "temp"
                max_temp_bytes = 0
                max_data_bytes = 0
                minimum_free_bytes = shutil.disk_usage(root).free
                with ThreadPoolExecutor(max_workers=3) as pool:
                    upload_futures = [
                        pool.submit(
                            _raw_upload,
                            base_url,
                            "sandbox-token-never-use-outside-this-run",
                            payload,
                            starts,
                        )
                        for _ in range(2)
                    ]
                    url_future = pool.submit(
                        _submit_controlled_url,
                        base_url,
                        "sandbox-token-never-use-outside-this-run",
                        controlled_url,
                        starts,
                    )
                    futures = upload_futures + [url_future]
                    while any(not future.done() for future in futures):
                        max_temp_bytes = max(max_temp_bytes, _file_bytes(data_temp))
                        max_data_bytes = max(max_data_bytes, _file_bytes(root / "data"))
                        minimum_free_bytes = min(minimum_free_bytes, shutil.disk_usage(root).free)
                        time.sleep(0.01)
                    uploads = [future.result(timeout=1) for future in upload_futures]
                    url_task_id = url_future.result(timeout=1)
                    max_temp_bytes = max(max_temp_bytes, _file_bytes(data_temp))
                    max_data_bytes = max(max_data_bytes, _file_bytes(root / "data"))

                task_ids = [item["task_id"] for item in uploads] + [url_task_id]
                terminal_states = _wait_for_terminal(
                    root / "data/cache/cache.db", task_ids, process
                )
                if any(status != "failed" for status in terminal_states.values()):
                    raise ExperimentFailure("unexpected_non_failure_in_loopback_asr_experiment")

                with sqlite3.connect(root / "data/cache/cache.db") as connection:
                    received = connection.execute(
                        "SELECT upload_id, byte_size, sha256 FROM local_uploads ORDER BY upload_id"
                    ).fetchall()
                expected_hash = hashlib.sha256(payload).hexdigest()
                if len(received) != 2 or any(
                    size != len(payload) or digest != expected_hash
                    for _, size, digest in received
                ):
                    raise ExperimentFailure("producer_bytes_do_not_match_persisted_receiver_record")
                intervals = SandboxMediaHandler.request_intervals
                if SandboxMediaHandler.requests_seen < 1:
                    raise ExperimentFailure("controlled_url_download_fixture_not_consumed")
                if not any(
                    download_start < upload["ended_at"]
                    and download_end > upload["started_at"]
                    for download_start, download_end in intervals
                    for upload in uploads
                ):
                    raise ExperimentFailure("controlled_url_download_did_not_overlap_upload_receiving")
                if LoopbackRejectHandler.connections_seen < 1:
                    raise ExperimentFailure("loopback_asr_fixture_not_consumed")

                source_sha = subprocess.run(
                    ["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                source_status = subprocess.run(
                    ["git", "-C", str(PROJECT_ROOT), "status", "--porcelain"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
                source_tree_state = "clean" if not source_status else "dirty"
                latency_values = ",".join(f"{item['elapsed_seconds']:.3f}" for item in uploads)
                print(f"sandbox_root=mkdtemp:{root.name}")
                print(f"source_sha={source_sha}")
                print(f"source_worktree={source_tree_state}")
                print(f"host={platform.machine()}_cpu_count={os.cpu_count()}_python={platform.python_version()}")
                print(f"ffmpeg={_tool_version('ffmpeg')}")
                print(f"ffprobe={_tool_version('ffprobe')}")
                print(f"synthetic_codec=pcm_s16le_mono_48000Hz_duration_seconds={duration_seconds}")
                print(f"sample_bytes={len(payload)}_ffprobe_duration_seconds={sample_info['duration_seconds']:.3f}")
                print("raw_http_receipts=2_of_2")
                print(f"received_payload_sha256={expected_hash}")
                print("sandbox_limits=max_file_mib:4_max_media_hours:0.02_receive_concurrency:2_upload_temp_budget_mib:12")
                print("receiver_concurrency_configured=2_upload_clients=2")
                print(f"upload_latency_seconds={latency_values}")
                print("controlled_url_upload_overlap=confirmed")
                print(f"controlled_loopback_url_download_requests={SandboxMediaHandler.requests_seen}")
                print(f"loopback_asr_rejector_connections={LoopbackRejectHandler.connections_seen}")
                print(f"worker_terminal_states={','.join(sorted(terminal_states.values()))}")
                print(f"temp_directory_peak_observed_bytes={max_temp_bytes}_sample_interval_ms=10")
                print(f"data_tree_peak_observed_bytes={max_data_bytes}_sample_interval_ms=10")
                print(f"sandbox_free_bytes_min={minimum_free_bytes}")
                print("asr_capacity=unknown_external_model_not_called")
                print("production_capacity=unknown_production_ingress_and_disk_quota_not_measured")
                print("UPLOAD_ENABLE_BLOCKED: sandbox observations are not production capacity, restore-domain, or deployment authorization")

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-seconds", type=int, default=20)
    args = parser.parse_args(argv)
    return run_experiment(args.duration_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
