"""Unit tests for the skip_calibration code path (processing_options.calibrate=False).

Covers three layers:
- PlainTextProcessor.process(skip_calibration=True): no LLM call at all, output is
  the locally formatted original text, calibration_status=DISABLED.
- SpeakerAwareProcessor.process(skip_calibration=True): speaker inference / mapping /
  dialog normalization still run (they are "transcription" deliverables, not
  "calibration"), but no chunk-level LLM calibration call happens.
- LLMCoordinator.process(skip_calibration=True): threads the flag down to whichever
  processor gets routed to, and the resulting stats.calibration_status is normalized
  to DISABLED at the coordinator's top level.

All console output must be in English only (no emoji, no Chinese).
"""

from unittest.mock import MagicMock, Mock, patch

import pytest

from video_transcript_api.llm.core.config import LLMConfig
from video_transcript_api.llm.core.key_info_extractor import KeyInfo
from video_transcript_api.llm.coordinator import LLMCoordinator
from video_transcript_api.llm.processors.plain_text_processor import PlainTextProcessor
from video_transcript_api.llm.processors.speaker_aware_processor import (
    SpeakerAwareProcessor,
)
from video_transcript_api.utils.llm_status import CalibrationStatus


def _make_config():
    return LLMConfig(
        api_key="k",
        base_url="http://test",
        calibrate_model="test-model",
        summary_model="test-model",
    )


class TestPlainTextProcessorSkipCalibration:
    def test_no_llm_call_at_all(self):
        """skip_calibration=True must not touch the LLM client (no key_info,
        no calibration call) -- it's a pure local formatting operation."""
        config = _make_config()
        llm_client = MagicMock()
        key_info_extractor = MagicMock()
        quality_validator = MagicMock()

        processor = PlainTextProcessor(
            config=config,
            llm_client=llm_client,
            key_info_extractor=key_info_extractor,
            quality_validator=quality_validator,
        )

        text = "raw transcript text " * 20
        result = processor.process(text=text, title="t", skip_calibration=True)

        llm_client.call.assert_not_called()
        key_info_extractor.extract.assert_not_called()
        assert result["stats"]["calibration_status"] == CalibrationStatus.DISABLED
        assert result["calibrated_text"]  # non-empty formatted passthrough

    def test_calibrated_text_is_formatted_passthrough(self):
        """The output must come from _format_plain_text, not raw text verbatim
        when formatting would change it (e.g. a text-wall gets split)."""
        config = _make_config()
        processor = PlainTextProcessor(
            config=config,
            llm_client=MagicMock(),
            key_info_extractor=MagicMock(),
            quality_validator=MagicMock(),
        )

        text = "一二三四五六七八九十。" * 30  # long single-line text wall
        result = processor.process(text=text, title="t", skip_calibration=True)

        assert result["calibrated_text"] == processor._format_plain_text(text)


class TestSpeakerAwareProcessorSkipCalibration:
    def _make_processor(self, llm_client, key_info_extractor, speaker_inferencer):
        return SpeakerAwareProcessor(
            config=_make_config(),
            llm_client=llm_client,
            key_info_extractor=key_info_extractor,
            speaker_inferencer=speaker_inferencer,
            quality_validator=MagicMock(),
        )

    def test_speaker_inference_still_runs_but_no_chunk_calibration_call(self):
        """Speaker inference/mapping/merge are transcription deliverables and must
        still execute; only the chunk-level LLM calibration call is skipped."""
        llm_client = MagicMock()
        key_info_extractor = MagicMock()
        key_info_extractor.extract = MagicMock(
            return_value=KeyInfo([], [], [], [], [], [], [])
        )
        speaker_inferencer = MagicMock()
        speaker_inferencer.infer = MagicMock(
            return_value={
                "mapping": {"Speaker1": "Alice", "Speaker2": "Bob"},
                "meta": {},
                "low_confidence": [],
            }
        )

        processor = self._make_processor(llm_client, key_info_extractor, speaker_inferencer)

        dialogs = [
            {"speaker": "Speaker1", "text": "hello there", "start_time": 0.0, "end_time": 1.0},
            {"speaker": "Speaker2", "text": "hi back", "start_time": 1.0, "end_time": 2.0},
        ]

        result = processor.process(dialogs=dialogs, title="t", skip_calibration=True)

        # Speaker inference deliverables still happened.
        speaker_inferencer.infer.assert_called_once()
        key_info_extractor.extract.assert_called_once()
        assert result["structured_data"]["speaker_mapping"] == {
            "Speaker1": "Alice", "Speaker2": "Bob",
        }
        speakers_in_output = {d["speaker"] for d in result["structured_data"]["dialogs"]}
        assert speakers_in_output == {"Alice", "Bob"}

        # No chunk-level LLM calibration call was made.
        llm_client.call.assert_not_called()

        assert result["stats"]["calibration_stats"]["calibration_status"] == (
            CalibrationStatus.DISABLED
        )
        assert result["stats"]["calibration_stats"]["total_chunks"] == 0
        assert result["stats"]["chunk_count"] == 0


class TestCoordinatorSkipCalibration:
    """Coordinator threads skip_calibration to whichever processor gets routed to,
    and normalizes the resulting DISABLED status to the top-level stats key."""

    @pytest.fixture
    def config_dict(self):
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
    def coordinator(self, config_dict, tmp_path):
        with patch("video_transcript_api.llm.coordinator.PlainTextProcessor"), \
             patch("video_transcript_api.llm.coordinator.SpeakerAwareProcessor"), \
             patch("video_transcript_api.llm.coordinator.SummaryProcessor"):
            c = LLMCoordinator(config_dict=config_dict, cache_dir=str(tmp_path))
            yield c

    def test_plain_text_route_receives_skip_calibration_flag(self, coordinator):
        coordinator.plain_text_processor.process = Mock(
            return_value={
                "calibrated_text": "formatted text",
                "key_info": {},
                "stats": {
                    "original_length": 10,
                    "calibrated_length": 10,
                    "calibration_status": CalibrationStatus.DISABLED,
                },
            }
        )
        coordinator.summary_processor.process = Mock()

        result = coordinator.process(content="short text", title="t", skip_calibration=True)

        _, kwargs = coordinator.plain_text_processor.process.call_args
        assert kwargs["skip_calibration"] is True
        assert result["stats"]["calibration_status"] == CalibrationStatus.DISABLED

    def test_speaker_aware_route_receives_skip_calibration_flag(self, coordinator):
        coordinator.speaker_aware_processor.process = Mock(
            return_value={
                "calibrated_text": "Alice: hi",
                "key_info": {},
                "stats": {
                    "original_length": 10,
                    "calibrated_length": 10,
                    "calibration_stats": {
                        "total_chunks": 0,
                        "calibration_status": CalibrationStatus.DISABLED,
                    },
                },
                "structured_data": {"dialogs": [], "speaker_mapping": {"S1": "Alice"}},
            }
        )
        coordinator.summary_processor.process = Mock()

        result = coordinator.process(
            content=[{"speaker": "S1", "text": "hi"}], title="t", skip_calibration=True
        )

        _, kwargs = coordinator.speaker_aware_processor.process.call_args
        assert kwargs["skip_calibration"] is True
        assert kwargs["infer_speaker_names"] is True
        assert result["stats"]["calibration_status"] == CalibrationStatus.DISABLED

    def test_speaker_name_switch_is_independent(self, coordinator):
        coordinator.speaker_aware_processor.process = Mock(
            return_value={
                "calibrated_text": "S1: hi",
                "key_info": {},
                "stats": {
                    "calibration_stats": {
                        "total_chunks": 0,
                        "calibration_status": CalibrationStatus.DISABLED,
                    },
                },
                "structured_data": {"dialogs": [], "speaker_mapping": {"S1": "S1"}},
            }
        )
        coordinator.process(
            content=[{"speaker": "S1", "text": "hi"}],
            title="t",
            skip_calibration=True,
            skip_summary=True,
            infer_speaker_names=False,
        )
        assert coordinator.speaker_aware_processor.process.call_args.kwargs[
            "infer_speaker_names"
        ] is False

    def test_default_skip_calibration_is_false(self, coordinator):
        """Backward compatibility: omitting skip_calibration must behave exactly
        like before (real calibration requested)."""
        coordinator.plain_text_processor.process = Mock(
            return_value={
                "calibrated_text": "calibrated",
                "key_info": {},
                "stats": {
                    "original_length": 10,
                    "calibrated_length": 10,
                    "calibration_status": CalibrationStatus.FULL,
                },
            }
        )
        coordinator.summary_processor.process = Mock()

        coordinator.process(content="short text", title="t")

        _, kwargs = coordinator.plain_text_processor.process.call_args
        assert kwargs["skip_calibration"] is False
