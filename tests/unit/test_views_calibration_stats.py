"""Test views._prepare_success_view calibration_status/calibration_stats sourcing.

Covers the honest-status-model wiring on the read side: the warning banner in
transcript.html now needs stats.calibration_status/stats.calibration_stats for
BOTH the plain-text and speaker-aware paths (previously only the speaker-aware
path had any visibility via llm_processed.json).

All console output must be in English only (no emoji, no Chinese).
"""

import json

from video_transcript_api.api.routes.views import (
    _prepare_success_view,
    _derive_legacy_calibration_status,
)
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
