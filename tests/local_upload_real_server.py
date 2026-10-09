"""Start the production FastAPI app with isolated loopback dependencies."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import shutil
import socket
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any

import commentjson
import uvicorn
from fastapi import Request
from fastapi.responses import JSONResponse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from local_upload_loopback import LoopbackUpstreams  # noqa: E402


def _temporary_config(run_dir: Path, api_port: int, upstream_port: int) -> Path:
    with (ROOT / "config" / "config.example.jsonc").open(encoding="utf-8") as source:
        config = commentjson.load(source)

    storage = config["storage"]
    storage["cache_dir"] = str(run_dir / "cache")
    storage["workspace_dir"] = str(run_dir / "workspace")
    storage["temp_dir"] = str(run_dir / "temp")
    storage["audit_db"] = str(run_dir / "audit.sqlite3")
    storage["upload_limits"] = {
        "max_file_mib": 2,
        "max_media_hours": 1,
        "receive_concurrency": 1,
        "upload_temp_budget_mib": 8,
    }
    storage["cache_retention_days"] = 30
    storage["task_status_retention_days"] = 30
    storage["audit_log_retention_days"] = 30
    storage["temp_retention_hours"] = 24

    config["api"].update(host="127.0.0.1", port=api_port, auth_token="unused-local-e2e-token")
    config["concurrent"].update(max_workers=1, queue_size=2, llm_max_workers=1)
    config["capswriter"].update(
        server_url=f"ws://127.0.0.1:{upstream_port}",
        max_retries=1,
        retry_delay=0,
    )
    config["llm"].update(
        api_key="local-e2e-key",
        base_url=f"http://127.0.0.1:{upstream_port}/v1",
        calibrate_model="local-e2e-model",
        calibrate_reasoning_effort="disabled",
        summary_model="local-e2e-model",
        summary_reasoning_effort="disabled",
        max_retries=0,
        total_timeout=15,
        structured_calibration_for_plain=False,
    )
    config["llm"]["min_calibrate_ratio"] = 0.5
    config["web"].update(
        base_url=f"http://127.0.0.1:{api_port}",
        enable_view_links=True,
        timezone="UTC+8",
    )
    config["wechat"]["webhook"] = ""
    config["feishu"].update(
        webhook=f"http://127.0.0.1:{upstream_port}/hooks/feishu",
        secret="",
    )
    config["log"].update(
        level="WARNING",
        file=str(run_dir / "logs" / "app.log"),
        debug_dir=str(run_dir / "logs" / "debug"),
        llm_debug_dir=str(run_dir / "logs" / "llm_debug"),
    )
    config_path = run_dir / "config.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return config_path


def _write_ffprobe_observer(run_dir: Path) -> None:
    real_ffprobe = shutil.which("ffprobe")
    if real_ffprobe is None:
        raise FileNotFoundError("ffprobe is required for the real local-upload path")
    shim_dir = run_dir / "bin"
    shim_dir.mkdir(parents=True)
    log_path = run_dir / "ffprobe-argv.jsonl"
    shim = shim_dir / "ffprobe"
    shim.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['VTA_E2E_FFPROBE_LOG'], 'a', encoding='utf-8') as log:\n"
        "    log.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
        f"real = {json.dumps(real_ffprobe)}\n"
        "os.execv(real, [real, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    os.environ["PATH"] = f"{shim_dir}{os.pathsep}{os.environ['PATH']}"
    os.environ["VTA_E2E_FFPROBE_LOG"] = str(log_path)


def _register_state_inspector(app, run_dir: Path, upstreams: LoopbackUpstreams) -> None:
    @app.get("/__e2e__/state")
    async def inspect_real_state(request: Request) -> JSONResponse:
        runtime = request.app.state.runtime
        with runtime.cache_manager._get_cursor() as cursor:
            cursor.execute(
                """SELECT u.upload_id, u.owner_user_id, u.idempotency_key, u.state,
                          u.root_task_id, u.media_id, u.view_token, u.filename,
                          t.title AS title, u.source_url, u.retention, u.expires_at,
                          u.revoked_at, u.media_path, u.byte_size, u.sha256,
                          u.request_metadata,
                          t.processing_options AS task_processing_options,
                          t.status AS root_status, t.platform AS task_platform,
                          t.view_token AS legacy_task_token, t.completed_at
                   FROM local_uploads u JOIN task_status t ON t.task_id = u.root_task_id
                   ORDER BY u.rowid DESC"""
            )
            rows = [dict(row) for row in cursor.fetchall()]

        for row in rows:
            row["request_metadata"] = json.loads(row["request_metadata"])
            row["task_processing_options"] = json.loads(
                row["task_processing_options"]
            )
            media_path = Path(row["media_path"]) if row["media_path"] else None
            row["media_exists"] = bool(media_path and media_path.is_file())
            if row["media_exists"]:
                with media_path.open("rb") as media_file:
                    row["media_sha256"] = hashlib.file_digest(media_file, "sha256").hexdigest()
            else:
                row["media_sha256"] = None

        ffprobe_log = run_dir / "ffprobe-argv.jsonl"
        ffprobe_calls = (
            [json.loads(line) for line in ffprobe_log.read_text(encoding="utf-8").splitlines()]
            if ffprobe_log.exists()
            else []
        )
        return JSONResponse(
            {
                "uploads": rows,
                "ffprobe_calls": ffprobe_calls,
                "queued_upload_items": runtime.task_queue.qsize(),
            }
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)

    upstreams = LoopbackUpstreams()
    upstreams.start()

    api_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    api_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    api_socket.bind(("127.0.0.1", 0))
    api_socket.listen(128)
    api_port = api_socket.getsockname()[1]
    config_path = _temporary_config(run_dir, api_port, upstreams.port)

    os.environ["VTAPI_CONFIG"] = str(config_path)
    os.environ["VTAPI_USERS_JSON"] = str(run_dir / "users.json")
    os.environ["VTA_UPLOADS_ENABLED"] = "true"
    os.environ["SENTRY_DSN"] = ""
    user_config = {
        "users": {
            "browser-fixture-token": {
                "user_id": "browser_owner",
                "name": "Browser owner",
                "permissions": ["recalibrate", "delete"],
                "enabled": True,
                "feishu_webhook": f"http://127.0.0.1:{upstreams.port}/hooks/feishu",
            }
        }
    }
    Path(os.environ["VTAPI_USERS_JSON"]).write_text(
        json.dumps(user_config), encoding="utf-8"
    )
    _write_ffprobe_observer(run_dir)

    from video_transcript_api.api.app import create_app

    app = create_app()
    _register_state_inspector(app, run_dir, upstreams)
    api_server = uvicorn.Server(
        uvicorn.Config(app, log_level="error", access_log=False)
    )
    api_thread = threading.Thread(
        target=api_server.run,
        kwargs={"sockets": [api_socket]},
        name="local-upload-real-api",
        daemon=True,
    )

    def stop_server(_signum, _frame) -> None:
        api_server.should_exit = True

    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)
    api_thread.start()
    try:
        deadline = time.monotonic() + 30
        while not api_server.started:
            if not api_thread.is_alive():
                raise RuntimeError("production FastAPI server exited before startup")
            if time.monotonic() >= deadline:
                raise TimeoutError("production FastAPI server did not start")
            time.sleep(0.01)
        print(
            "VTA_REAL_SERVER_READY "
            + json.dumps(
                {
                    "api_url": f"http://127.0.0.1:{api_port}",
                    "upstream_url": f"http://127.0.0.1:{upstreams.port}",
                    "run_dir": str(run_dir),
                }
            ),
            flush=True,
        )
        api_thread.join()
    finally:
        api_server.should_exit = True
        api_thread.join(timeout=15)
        if api_thread.is_alive():
            raise TimeoutError("production FastAPI server did not stop")
        upstreams.stop()
        api_socket.close()


if __name__ == "__main__":
    main()
