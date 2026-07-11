"""Test LLMCoordinator.process() normalizes calibration_status/calibration_stats
to a single top-level location in the returned stats dict, regardless of which
processor (plain-text vs speaker-aware) produced the calibration result.

Plain-text processor puts calibration_status and the segment counters flat at
the top of its own stats dict. Speaker-aware processor nests everything under
stats["calibration_stats"]["calibration_status"]. Downstream consumers
(llm_ops, cache_manager, templates) should not need to know which shape they
are looking at -- coordinator.process() must always expose:
    stats["calibration_status"]   -> CalibrationStatus value
    stats["calibration_stats"]    -> dict with the detail counters (or None)

All console output must be in English only (no emoji, no Chinese).
"""

from unittest.mock import Mock, patch

import pytest

from video_transcript_api.llm.coordinator import LLMCoordinator
from video_transcript_api.utils.llm_status import CalibrationStatus


@pytest.fixture
def config_dict():
    return {
        "llm": {
            "api_key": "test-key",
            "base_url": "http://test",
            "calibrate_model": "test-model",
            "summary_model": "test-model",
            "min_summary_threshold": 500,
        }
    }


@pytest.fixture
def coordinator(config_dict, tmp_path):
    with patch("video_transcript_api.llm.coordinator.PlainTextProcessor"), \
         patch("video_transcript_api.llm.coordinator.SpeakerAwareProcessor"), \
         patch("video_transcript_api.llm.coordinator.SummaryProcessor"):
        c = LLMCoordinator(config_dict=config_dict, cache_dir=str(tmp_path))
        yield c


def test_plain_text_flat_calibration_status_is_normalized_to_top(coordinator):
    """Plain-text processor's flat stats.calibration_status must surface at
    the coordinator's top-level stats.calibration_status, and the segment
    counters must also be mirrored into stats.calibration_stats for uniform
    downstream consumption."""
    coordinator.plain_text_processor.process = Mock(
        return_value={
            "calibrated_text": "calibrated",
            "key_info": {},
            "stats": {
                "original_length": 100,
                "calibrated_length": 90,
                "segment_count": 1,
                "total_segments": 1,
                "calibrated_segments": 0,
                "fallback_segments": 1,
                "low_quality_segments": 0,
                "calibration_status": CalibrationStatus.NONE,
            },
        }
    )
    coordinator.summary_processor.process = Mock()

    result = coordinator.process(content="short text", title="t")

    assert result["stats"]["calibration_status"] == CalibrationStatus.NONE
    assert result["stats"]["calibration_stats"]["total_segments"] == 1
    assert result["stats"]["calibration_stats"]["fallback_segments"] == 1


def test_speaker_aware_nested_calibration_status_is_normalized_to_top(coordinator):
    """Speaker-aware processor's nested calibration_stats.calibration_status
    must surface at the coordinator's top-level stats.calibration_status too."""
    coordinator.speaker_aware_processor.process = Mock(
        return_value={
            "calibrated_text": "calibrated dialog",
            "key_info": {},
            "stats": {
                "original_length": 100,
                "calibrated_length": 100,
                "dialog_count": 2,
                "chunk_count": 1,
                "calibration_stats": {
                    "total_chunks": 1,
                    "success_count": 1,
                    "partial_count": 0,
                    "fallback_count": 0,
                    "failed_count": 0,
                    "dialog_counts": {},
                    "calibration_status": CalibrationStatus.FULL,
                },
            },
            "structured_data": {"dialogs": [], "speaker_mapping": {}},
        }
    )
    coordinator.summary_processor.process = Mock()

    result = coordinator.process(
        content=[{"speaker": "A", "text": "hi"}],
        title="t",
    )

    assert result["stats"]["calibration_status"] == CalibrationStatus.FULL
    assert result["stats"]["calibration_stats"]["total_chunks"] == 1


def test_missing_calibration_status_stays_none_no_crash(coordinator):
    """Legacy/mocked processors that don't produce calibration_status at all
    (e.g. hand-rolled test doubles) must not crash the coordinator."""
    coordinator.plain_text_processor.process = Mock(
        return_value={
            "calibrated_text": "calibrated",
            "key_info": {},
            "stats": {"original_length": 100, "calibrated_length": 100},
        }
    )
    coordinator.summary_processor.process = Mock()

    result = coordinator.process(content="short text", title="t")

    assert result["stats"]["calibration_status"] is None
    assert result["stats"]["calibration_stats"] is None
