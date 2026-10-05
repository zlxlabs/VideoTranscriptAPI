"""Tests for summary ratio monitoring service."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from video_transcript_api.api.services.summary_ratio_stats import compute_summary_ratio_stats
from video_transcript_api.cache.cache_manager import CacheManager
from video_transcript_api.utils.logging.audit_logger import AuditLogger
from video_transcript_api.utils.llm_status import SummaryStatus


def _seed_task(
    tmp_path: Path,
    *,
    task_id: str,
    media_id: str,
    original_text: str,
    summary_text: str,
    original_length_band: int,
):
  cache_root = tmp_path / "cache"
  audit_path = tmp_path / "audit.db"
  cm = CacheManager(cache_dir=str(cache_root))
  audit = AuditLogger(str(audit_path))

  cm.save_cache(
    platform="youtube",
    url=f"https://www.youtube.com/watch?v={media_id}",
    media_id=media_id,
    use_speaker_recognition=False,
    transcript_data=original_text,
    transcript_type="capswriter",
    title="Title",
    author="Author",
    description="Desc",
  )
  cm.save_llm_result(
    platform="youtube",
    media_id=media_id,
    use_speaker_recognition=False,
    llm_type="summary",
    content=summary_text,
  )
  task = cm.create_task(
    url=f"https://www.youtube.com/watch?v={media_id}",
    use_speaker_recognition=False,
    platform="youtube",
    media_id=media_id,
  )
  created_task_id = task["task_id"]
  cm.update_task_status(created_task_id, "success", summary_status=SummaryStatus.GENERATED)

  audit.archive_task_snapshot(
    {
      "task_id": created_task_id,
      "view_token": task["view_token"],
      "title": "Title",
      "author": "Author",
      "platform": "youtube",
      "status": "success",
      "summary_status": SummaryStatus.GENERATED,
      "submitted_by": "user-1",
      "completed_at": "2026-08-24 12:00:00",
    }
  )

  # Pad original transcript file to target band without changing summary ratio much
  artifact_dir = Path(cm.get_cache("youtube", media_id, False)["file_path"])
  transcript_path = artifact_dir / "transcript_capswriter.txt"
  padded = original_text + ("x" * max(0, original_length_band - len(original_text)))
  transcript_path.write_text(padded, encoding="utf-8")

  audit.close()
  cm.close()
  return created_task_id, cache_root, audit_path


def test_compute_summary_ratio_stats_fixture(tmp_path):
  _seed_task(
    tmp_path,
    task_id="t1",
    media_id="vid-s",
    original_text="original transcript text for ratio",
    summary_text="summary " * 200,
    original_length_band=2000,
  )

  cache_root = tmp_path / "cache"
  audit_path = tmp_path / "audit.db"
  cache_db = cache_root / "cache.db"

  result = compute_summary_ratio_stats(
    audit_db_path=str(audit_path),
    cache_db_path=str(cache_db),
    cache_root=cache_root,
    days=30,
  )

  assert result["bands"]["S"]["n"] == 1
  assert result["bands"]["S"]["median_ratio"] > 0
  assert result["sampled_tasks"] == 1


def test_over_hardcap_counted(tmp_path):
  _seed_task(
    tmp_path,
    task_id="t2",
    media_id="vid-hardcap",
    original_text="short",
    summary_text="x" * 5000,
    original_length_band=2000,
  )

  cache_root = tmp_path / "cache"
  result = compute_summary_ratio_stats(
    audit_db_path=str(tmp_path / "audit.db"),
    cache_db_path=str(cache_root / "cache.db"),
    cache_root=cache_root,
    days=30,
  )
  assert result["bands"]["S"]["over_hardcap"] == 1


def test_summary_ratio_endpoint_ok_when_audit_db_config_missing(tmp_path):
  """Config may omit storage.audit_db; the route must use get_audit_logger().db_path."""
  cache_root = tmp_path / "cache"
  cache_root.mkdir()
  cm = CacheManager(cache_dir=str(cache_root))
  al = AuditLogger(str(tmp_path / "audit.db"))

  async def _fake_verify_token():
    return {
      "user_id": "test-user",
      "api_key": "sk-test",
      "wechat_webhook": None,
      "is_legacy": True,
    }

  from video_transcript_api.api.services.transcription import verify_token
  from video_transcript_api.api.routes import audit

  app = FastAPI()
  app.include_router(audit.router)
  app.dependency_overrides[verify_token] = _fake_verify_token

  try:
    with patch.object(audit, "get_config", return_value={"storage": {}}), \
         patch.object(audit, "get_audit_logger", return_value=al), \
         patch.object(audit, "get_cache_manager", return_value=cm):
      client = TestClient(app)
      resp = client.get("/api/audit/summary-ratio")
  finally:
    al.close()
    cm.close()

  assert resp.status_code == 200
  body = resp.json()
  assert body["code"] == 200
  assert "bands" in body["data"]
