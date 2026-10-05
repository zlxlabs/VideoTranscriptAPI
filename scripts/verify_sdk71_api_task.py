"""Submit and poll one real recorder:// job through the production API."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

TASK_ID = "250f1810fba9422dbdee6f1a3c236763"
API_BASE = "http://127.0.0.1:8200"


def in_container(name: str, code: str):
    result = subprocess.run(
        ["docker", "exec", "-i", name, "/app/.venv/bin/python", "-"],
        input=code, text=True, capture_output=True, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"container helper failed: {name} exit={result.returncode}")
    return json.loads(result.stdout)


def api_token() -> str:
    token = in_container(
        "video-transcript-api",
        "import json\nd=json.load(open('/app/config/users.json'))\n"
        "print(json.dumps(next(k for k,v in d['users'].items() if v.get('enabled',True))))\n",
    )
    if not token:
        raise ValueError("no enabled production API token")
    return token


def open_response(req: Request):
    try:
        return urlopen(req, timeout=20)
    except HTTPError as exc:
        print(f"http_error={exc.code}", flush=True)
        raise SystemExit(1) from None
    except URLError as exc:
        print(f"transport_error={type(exc.reason).__name__}", flush=True)
        raise SystemExit(1) from None


def submit() -> int:
    rec = in_container(
        "live-recorder",
        "import json,sqlite3\n"
        "c=sqlite3.connect('file:/app/data/tasks.db?mode=ro',uri=True)\n"
        f"r=c.execute('select id,source,streamid,state,file_token from tasks where id=?',({TASK_ID!r},)).fetchone()\n"
        "if r is None: raise SystemExit('recording task missing')\n"
        "i,s,stream,state,t=r\n"
        "if state!='done' or not t: raise SystemExit('recording task not ready')\n"
        "print(json.dumps({'id':i,'source':s,'streamid':stream,'token':t}))\n",
    )
    media = f"/app/data/recordings/{TASK_ID}/{TASK_ID}.mp4"
    duration = float(subprocess.check_output(
        ["docker", "exec", "live-recorder", "ffprobe", "-v", "error",
         "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", media],
        text=True,
    ).strip())
    if not 0 < duration <= 900:
        raise ValueError(f"recording duration outside limit: {duration:.3f}")
    base = in_container(
        "live-recorder",
        "import json\nfrom live_recorder.config import load_settings\n"
        "print(json.dumps(load_settings().public_base_url.rstrip('/')))\n",
    )
    source = f"recorder://{rec['source']}/{rec['streamid']}/{TASK_ID}"
    payload = {
        "url": source,
        "download_url": f"{base}/files/{rec['token']}/sdk71-short-recording.mp4",
        "metadata_override": {
            "title": "SDK 492fe19 real recorder verification",
            "author": rec["source"],
        },
        "notification_config": {
            "channel": "sdk71_silent", "webhook": "https://sum.zlxlabs.com/",
        },
    }
    body = json.dumps(payload, ensure_ascii=False).encode()
    print(f"recording_source={source}", flush=True)
    print(f"recording_duration_seconds={duration:.3f} limit_seconds=900", flush=True)
    print(f"file_token_length={len(rec['token'])}", flush=True)
    print(f"payload.download_url={base}/files/<redacted-file-token>/sdk71-short-recording.mp4", flush=True)
    print(f"payload_sha256={hashlib.sha256(body).hexdigest()}", flush=True)
    print("notifications=suppressed via channel sdk71_silent (no registered target)", flush=True)
    req = Request(
        f"{API_BASE}/api/transcribe", data=body,
        headers={"Authorization": f"Bearer {api_token()}", "Content-Type": "application/json"},
        method="POST",
    )
    with open_response(req) as response:
        result = json.loads(response.read())
    task_id = (result.get("data") or {}).get("task_id")
    print(
        f"submitted_at={datetime.now(timezone.utc).isoformat(timespec='seconds')} "
        f"http_status={response.status} response_code={result.get('code')} task_id={task_id}",
        flush=True,
    )
    return 0 if response.status == 202 and result.get("code") == 202 and task_id else 1


def poll(task_id: str) -> int:
    token = api_token()
    started, deadline, last, count = time.monotonic(), time.monotonic() + 3600, None, 0
    while time.monotonic() < deadline:
        req = Request(
            f"{API_BASE}/api/task/{quote(task_id, safe='')}",
            headers={"Authorization": f"Bearer {token}"},
        )
        with open_response(req) as response:
            result = json.loads(response.read())
        data = result.get("data") or {}
        status = str(data.get("status") or "unknown")
        count += 1
        if status != last or count % 6 == 0:
            print(
                f"poll_count={count} http_status={response.status} response_code={result.get('code')} "
                f"status={status} elapsed_seconds={time.monotonic()-started:.1f}",
                flush=True,
            )
            last = status
        if status == "failed":
            print(f"terminal=failed error_present={bool(data.get('error'))}", flush=True)
            return 1
        if status == "success":
            view_token = data.get("view_token")
            if not view_token:
                print("terminal=success view_token_present=False", flush=True)
                return 1
            raw = Request(f"{API_BASE}/view/{quote(view_token,safe='')}?raw=transcript")
            with open_response(raw) as transcript_response:
                content = transcript_response.read().decode("utf-8",errors="replace")
            text = content.split("---",2)[-1].strip()
            print(
                f"terminal=success api_code={result.get('code')} transcript_nonempty={bool(text)} "
                f"transcript_chars={len(text)} elapsed_seconds={time.monotonic()-started:.1f}",
                flush=True,
            )
            return 0 if text else 1
        time.sleep(10)
    print(f"terminal=not_reached elapsed_seconds={time.monotonic()-started:.1f} limit_seconds=3600", flush=True)
    return 2


if __name__ == "__main__":
    action = sys.argv[1]
    raise SystemExit(submit() if action == "submit" else poll(sys.argv[2]))
