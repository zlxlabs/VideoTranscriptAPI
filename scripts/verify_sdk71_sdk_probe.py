"""One-time SDK 492fe19 probes, executed inside the production container."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from capswriter_asr import AsrError, transcribe_file_sync


def main() -> int:
    mode, audio_path = sys.argv[1:3]
    path = Path(audio_path)
    if mode == "c1":
        url = "ws://127.0.0.1:9"
        print(
            "CALL transcribe_file_sync(path, 'ws://127.0.0.1:9'); "
            "deadline_total omitted",
            flush=True,
        )
        started = time.monotonic()
        try:
            transcript = transcribe_file_sync(str(path), url)
        except AsrError as exc:
            elapsed = time.monotonic() - started
            print(
                f"RESULT code={exc.code} elapsed_seconds={elapsed:.3f}",
                flush=True,
            )
            return 0 if exc.code == "connection_lost" and elapsed < 5 else 1
        elapsed = time.monotonic() - started
        print(
            f"RESULT unexpected_success elapsed_seconds={elapsed:.3f}",
            flush=True,
        )
        return 1

    if mode == "c2":
        # The ASR endpoint is passed in: it lives in config/config.jsonc, which is
        # untracked, and this repo is publicly visible (pre-push public-scan rejects
        # private IPs). The production invocation passed ws://<capswriter-host>:6016.
        url = sys.argv[3]
        print(
            f"CALL transcript = transcribe_file_sync(path, '{url}'); "
            "deadline_total omitted",
            flush=True,
        )
        started = time.monotonic()
        transcript = transcribe_file_sync(str(path), url)
        elapsed = time.monotonic() - started
        text = transcript.text or ""
        print(
            "RESULT code=done "
            f"transcript_nonempty={bool(text)} transcript_chars={len(text)} "
            f"elapsed_seconds={elapsed:.3f} sdk_task_uuid={transcript.task_id}",
            flush=True,
        )
        return 0 if text else 1

    raise ValueError(f"unsupported probe mode: {mode}")


if __name__ == "__main__":
    raise SystemExit(main())
