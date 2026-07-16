"""Test that SpeakerAwareProcessor.process() surfaces speaker-inference metadata
in its stats output, alongside calibration_stats.

This covers the "confidence downgrade" consumption side: SpeakerInferencer.infer()
now returns {mapping, meta, low_confidence} instead of a flat mapping dict, and
SpeakerAwareProcessor must (a) still apply the flat "mapping" for text normalization
(backward-compatible structured_data.speaker_mapping), and (b) fold the "meta" into
stats.speaker_inference for downstream observability (e.g. llm_processed.json).
"""

import unittest
from unittest.mock import MagicMock

from video_transcript_api.llm.processors.speaker_aware_processor import (
    SpeakerAwareProcessor,
)
from video_transcript_api.llm.core.config import LLMConfig
from video_transcript_api.llm.core.key_info_extractor import KeyInfo


def _make_config():
    """Real LLMConfig instance (not a MagicMock) so DialogSegmenter's arithmetic
    on min/max/preferred chunk length works without special-casing every attr."""
    return LLMConfig(
        api_key="k",
        base_url="http://test",
        calibrate_model="test-model",
        summary_model="test-model",
    )


def _make_calibration_response(corrections):
    response = MagicMock()
    response.structured_output = {"corrections": corrections}
    return response


class TestSpeakerAwareProcessorStats(unittest.TestCase):
    def test_stats_include_speaker_inference_metadata(self):
        config = _make_config()
        llm_client = MagicMock()
        llm_client.call = MagicMock(
            return_value=_make_calibration_response(
                [{"id": 0, "text": "Hello there"}, {"id": 1, "text": "Hi back"}]
            )
        )

        key_info_extractor = MagicMock()
        key_info_extractor.extract = MagicMock(
            return_value=KeyInfo(
                names=[], places=[], technical_terms=[], brands=[],
                abbreviations=[], foreign_terms=[], other_entities=[],
            )
        )

        speaker_inferencer = MagicMock()
        speaker_inferencer.infer = MagicMock(
            return_value={
                "mapping": {"Speaker1": "Alice", "Speaker2": "说话人2"},
                "meta": {
                    "Speaker1": {"name": "Alice", "confidence": 0.9, "applied": True},
                    "Speaker2": {"name": "Bob", "confidence": 0.3, "applied": False},
                },
                "low_confidence": ["Speaker2"],
                "source": "llm",
            }
        )

        quality_validator = MagicMock()

        processor = SpeakerAwareProcessor(
            config=config,
            llm_client=llm_client,
            key_info_extractor=key_info_extractor,
            speaker_inferencer=speaker_inferencer,
            quality_validator=quality_validator,
        )

        dialogs = [
            {"speaker": "Speaker1", "text": "hello there", "start_time": 0.0, "end_time": 1.0},
            {"speaker": "Speaker2", "text": "hi back", "start_time": 1.0, "end_time": 2.0},
        ]

        result = processor.process(dialogs=dialogs, title="Test Video")

        # speaker_inference metadata surfaces next to calibration_stats.
        self.assertIn("calibration_stats", result["stats"])
        self.assertIn("speaker_inference", result["stats"])
        self.assertEqual(
            result["stats"]["speaker_inference"],
            {
                "Speaker1": {"name": "Alice", "confidence": 0.9, "applied": True},
                "Speaker2": {"name": "Bob", "confidence": 0.3, "applied": False},
            },
        )

        # structured_data.speaker_mapping stays a flat, backward-compatible dict
        # (downgraded labels show the placeholder, not the raw low-confidence guess).
        self.assertEqual(
            result["structured_data"]["speaker_mapping"],
            {"Speaker1": "Alice", "Speaker2": "说话人2"},
        )

        # And the flat mapping is what actually got applied to the dialog text.
        speakers_in_output = {d["speaker"] for d in result["structured_data"]["dialogs"]}
        self.assertEqual(speakers_in_output, {"Alice", "说话人2"})

        # G4 (local codex review round 6): infer()'s "source" tag must be
        # threaded through to stats too -- llm_ops._save_llm_results reads
        # stats.speaker_inference_source to decide whether the "refresh
        # existing display names" path is allowed to run at all.
        self.assertEqual(result["stats"]["speaker_inference_source"], "llm")


if __name__ == "__main__":
    unittest.main()
