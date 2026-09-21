"""Test views._prepare_success_view calibration_status/calibration_stats sourcing.

Covers the honest-status-model wiring on the read side: the warning banner in
transcript.html now needs stats.calibration_status/stats.calibration_stats for
BOTH the plain-text and speaker-aware paths (previously only the speaker-aware
path had any visibility via llm_processed.json).

All console output must be in English only (no emoji, no Chinese).
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import jinja2
from fastapi import FastAPI
from fastapi.testclient import TestClient

from video_transcript_api.api.routes.views import (
    _prepare_success_view,
    _derive_legacy_calibration_status,
)
from video_transcript_api.api.routes import views as views_mod
from video_transcript_api.utils.llm_status import CalibrationStatus


class TestPrepareSuccessViewCalibrationStatus:
    def test_reads_from_llm_status_json_plain_text_shape(self, tmp_path):
        (tmp_path / "transcript_capswriter.txt").write_text("original text", encoding="utf-8")
        (tmp_path / "llm_calibrated.txt").write_text("calibrated text", encoding="utf-8")
        (tmp_path / "llm_status.json").write_text(
            json.dumps({
                "calibration_status": "partial",
                "calibration_stats": {
                    "total_segments": 3, "calibrated_segments": 2,
                    "fallback_segments": 1, "low_quality_segments": 0,
                },
                "summary_status": "generated",
                "updated_at": "2026-01-01 00:00:00",
            }),
            encoding="utf-8",
        )

        stats = _prepare_success_view({"cache_dir": str(tmp_path), "summary": None})

        assert stats["calibration_status"] == "partial"
        assert stats["calibration_stats"]["total_segments"] == 3

    def test_reads_from_llm_status_json_speaker_aware_shape(self, tmp_path):
        (tmp_path / "transcript_capswriter.txt").write_text("original text", encoding="utf-8")
        (tmp_path / "llm_calibrated.txt").write_text("calibrated text", encoding="utf-8")
        (tmp_path / "llm_status.json").write_text(
            json.dumps({
                "calibration_status": "full",
                "calibration_stats": {
                    "total_chunks": 2, "success_count": 2,
                    "partial_count": 0, "fallback_count": 0, "failed_count": 0,
                    "dialog_counts": {}, "calibration_status": "full",
                },
                "summary_status": "generated",
                "updated_at": "2026-01-01 00:00:00",
            }),
            encoding="utf-8",
        )

        stats = _prepare_success_view({"cache_dir": str(tmp_path), "summary": None})

        assert stats["calibration_status"] == "full"
        assert stats["calibration_stats"]["total_chunks"] == 2

    def test_legacy_llm_processed_json_without_status_file_derives_status(self, tmp_path):
        """Old structured caches predating llm_status.json: calibration_stats
        exists in llm_processed.json but has no calibration_status field --
        it must be derived so the warning banner still works."""
        (tmp_path / "transcript_capswriter.txt").write_text("original text", encoding="utf-8")
        (tmp_path / "llm_calibrated.txt").write_text("calibrated text", encoding="utf-8")
        (tmp_path / "llm_processed.json").write_text(
            json.dumps({
                "format_version": "v3",
                "dialogs": [],
                "calibration_stats": {
                    "total_chunks": 4, "success_count": 1,
                    "partial_count": 0, "fallback_count": 1, "failed_count": 2,
                    "dialog_counts": {},
                    # note: no "calibration_status" key -- legacy data
                },
            }),
            encoding="utf-8",
        )

        stats = _prepare_success_view({"cache_dir": str(tmp_path), "summary": None})

        assert stats["calibration_stats"]["total_chunks"] == 4
        assert stats["calibration_status"] == CalibrationStatus.PARTIAL

    def test_reads_disabled_calibration_status_from_llm_status_json(self, tmp_path):
        """calibration_status=disabled (processing_options.calibrate=False) must
        pass through like any other status value -- the warning banner template
        branch treats it specially, but the data-sourcing layer here is agnostic."""
        (tmp_path / "transcript_capswriter.txt").write_text("original text", encoding="utf-8")
        (tmp_path / "llm_calibrated.txt").write_text("formatted passthrough text", encoding="utf-8")
        (tmp_path / "llm_status.json").write_text(
            json.dumps({
                "calibration_status": "disabled",
                "calibration_stats": {
                    "total_segments": 0, "calibrated_segments": 0,
                    "fallback_segments": 0, "low_quality_segments": 0,
                },
                "summary_status": "disabled",
                "updated_at": "2026-01-01 00:00:00",
            }),
            encoding="utf-8",
        )

        stats = _prepare_success_view({"cache_dir": str(tmp_path), "summary": None})

        assert stats["calibration_status"] == CalibrationStatus.DISABLED

    def test_no_status_files_at_all_omits_calibration_status(self, tmp_path):
        """Very old plain-text caches with neither file: no crash, no fabricated warning."""
        (tmp_path / "transcript_capswriter.txt").write_text("original text", encoding="utf-8")
        (tmp_path / "llm_calibrated.txt").write_text("calibrated text", encoding="utf-8")

        stats = _prepare_success_view({"cache_dir": str(tmp_path), "summary": None})

        assert "calibration_status" not in stats
        assert "calibration_stats" not in stats


class TestDeriveLegacyCalibrationStatus:
    def test_full_when_no_failed_or_fallback(self):
        assert _derive_legacy_calibration_status(
            {"total_chunks": 3, "failed_count": 0, "fallback_count": 0}
        ) == CalibrationStatus.FULL

    def test_none_when_all_failed_or_fallback(self):
        assert _derive_legacy_calibration_status(
            {"total_chunks": 3, "failed_count": 2, "fallback_count": 1}
        ) == CalibrationStatus.NONE

    def test_partial_otherwise(self):
        assert _derive_legacy_calibration_status(
            {"total_chunks": 5, "failed_count": 1, "fallback_count": 0}
        ) == CalibrationStatus.PARTIAL


def test_interrupted_view_route_prepares_success_view(tmp_path):
    """interrupted must not early-return on failed/file_cleaned; stats are computed."""
    (tmp_path / "transcript_capswriter.txt").write_text(
        "original transcript body", encoding="utf-8"
    )
    (tmp_path / "llm_calibrated.txt").write_text(
        "calibrated transcript body", encoding="utf-8"
    )
    view_data = {
        "status": "interrupted",
        "title": "Interrupted Video",
        "author": "Author",
        "url": "https://example.com/interrupted",
        "transcript": "calibrated transcript body",
        "summary": None,
        "notes": None,
        "cache_dir": str(tmp_path),
        "interrupted_reason": "orphan recovered after deploy restart",
        "interrupted_at": "2026-09-21 13:04:00",
        "created_at": None,
        "platform": "bilibili",
        "use_speaker_recognition": False,
        "llm_config": {"calibrate_model": "test-model"},
    }
    resolver = MagicMock()
    resolver.get_view_data_by_token.return_value = view_data

    app = FastAPI()
    app.include_router(views_mod.router)
    with patch.object(views_mod, "ViewTokenResolver", return_value=resolver), patch(
        "video_transcript_api.api.routes.views.get_config",
        return_value={"web": {}, "llm": {}},
    ):
        resp = TestClient(app).get("/view/view_interrupted_token")

    assert resp.status_code == 200
    body = resp.text
    assert "calibrated transcript body" in body
    assert "转录任务失败，请重新提交" not in body
    assert "该文件已被清理" not in body
    assert "Interrupted Video" in body
    assert 'id="interrupted-banner"' in body
    assert "任务被中断判定" in body
    assert "以下是中断前已生成的部分" in body
    assert "orphan recovered after deploy restart" in body
    assert "2026-09-21 13:04:00" in body


def test_transcript_template_banner_only_for_interrupted():
    templates_dir = Path(__file__).resolve().parents[2] / "src" / "web" / "templates"
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(templates_dir)),
        autoescape=True,
    )
    ctx = {
        "title": "Sample",
        "author": "Author",
        "url": "https://example.com",
        "created_at_display": "2026-09-21",
        "platform": "youtube",
        "summary_html": None,
        "summary_state": "skipped_short",
        "calibrated_html": "<p>Body</p>",
        "use_speaker_recognition": False,
        "view_token": "token",
        "stats": {
            "original_length": 600,
            "calibrated_length": 500,
            "summary_length": 0,
        },
        "llm_config": None,
        "status": "success",
    }
    success_html = env.get_template("transcript.html").render(**ctx)
    assert "interrupted-banner" not in success_html
    assert "任务被中断判定" not in success_html

    interrupted_html = env.get_template("transcript.html").render(
        **{
            **ctx,
            "status": "interrupted",
            "interrupted_reason": "orphan recovered after deploy restart",
            "interrupted_at": "2026-09-21 13:04:00",
        }
    )
    assert 'id="interrupted-banner"' in interrupted_html
    assert "任务被中断判定" in interrupted_html
    assert "orphan recovered after deploy restart" in interrupted_html
    assert "interrupted-at" in interrupted_html
    assert "2026-09-21 13:04:00" in interrupted_html
    assert "Body" in interrupted_html
