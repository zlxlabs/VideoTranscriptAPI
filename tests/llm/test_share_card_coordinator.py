"""Coordinator wiring tests for share-card generation (og card A).

Locked behaviours:
- Share-card data is generated exactly once, right after a GENERATED
  summary round, and is carried out of coordinator.process().
- Share-card failure never affects summary_text or the round's success
  (design invariant 2); the error reason is surfaced for llm_ops to log
  together with task_id.
- skip_summary / non-GENERATED summary rounds never trigger the call.

All LLM calls are mocked; no real request is ever sent.
Console output must be English only.
"""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from video_transcript_api.llm import LLMCoordinator

CONFIG_DICT = {
    "llm": {
        "api_key": "test_key",
        "base_url": "http://test.api.com",
        "calibrate_model": "test-model",
        "summary_model": "test-summary-model",
        "key_info_model": "test-model",
        "speaker_model": "test-model",
        "min_summary_threshold": 500,
        "segmentation": {},
        "structured_calibration": {},
    }
}

VALID_CARD = {
    "headline": "吸睛标题",
    "points": ["要点一", "要点二"],
}


def _mock_llm_response(*args, **kwargs):
    """Route mocked LLM calls by task_type."""
    response = Mock()
    task_type = kwargs.get("task_type", "")

    if task_type == "key_info_extraction":
        response.structured_output = {
            "names": [],
            "places": [],
            "technical_terms": [],
            "brands": [],
            "abbreviations": {},
            "foreign_terms": [],
            "other_entities": [],
        }
        response.text = ""
    elif task_type == "share_card":
        response.structured_output = dict(VALID_CARD)
        response.text = ""
    elif task_type == "summary":
        # Length must clear the English S-band floor (600 chars) so the floor
        # guard does not turn this into a retry/FAILED round.
        response.text = "## Overview\nGenerated summary body. " * 20
        response.structured_output = None
    else:
        response.text = "Calibrated long text. " * 100
        response.structured_output = None

    return response


def _make_coordinator():
    with patch("video_transcript_api.llm.core.llm_client.LLMClient.call"):
        coordinator = LLMCoordinator(
            config_dict=CONFIG_DICT,
            cache_dir="./test_cache_dir",
        )
    return coordinator


@pytest.fixture(autouse=True)
def _english_summary_language(monkeypatch):
    monkeypatch.setattr(
        "video_transcript_api.llm.processors.summary_processor.get_language",
        lambda: "en",
    )
    monkeypatch.setattr(
        "video_transcript_api.llm.prompts.localized.get_language",
        lambda: "en",
    )


class TestShareCardWiring:
    @patch("video_transcript_api.llm.core.llm_client.LLMClient.call")
    def test_generated_summary_produces_share_card(self, mock_call):
        """A GENERATED summary round must carry the contract-shaped card and
        no error out of process()."""
        mock_call.side_effect = _mock_llm_response
        coordinator = _make_coordinator()

        result = coordinator.process(
            content="Long enough text for summary. " * 100,
            title="Test Title",
            platform="youtube",
        )

        assert result["summary_text"] is not None
        card = result["share_card"]
        assert set(card.keys()) == {"version", "headline", "points", "generated_at"}
        assert card["version"] == 1
        assert result["share_card_error"] is None

        share_card_calls = [
            c for c in mock_call.call_args_list
            if c.kwargs.get("task_type") == "share_card"
        ]
        assert len(share_card_calls) == 1
        assert share_card_calls[0].kwargs["model"] == "test-summary-model"

    @patch("video_transcript_api.llm.core.llm_client.LLMClient.call")
    def test_share_card_failure_does_not_affect_summary(self, mock_call):
        """Share-card LLM failure must leave summary_text intact, return
        card=None and surface the failure reason (invariant 2)."""

        def failing_response(*args, **kwargs):
            if kwargs.get("task_type") == "share_card":
                raise RuntimeError("provider exploded")
            return _mock_llm_response(*args, **kwargs)

        mock_call.side_effect = failing_response
        coordinator = _make_coordinator()

        result = coordinator.process(
            content="Long enough text for summary. " * 100,
            title="Test Title",
        )

        assert result["summary_text"] is not None
        assert result["share_card"] is None
        assert result["share_card_error"] is not None
        assert "provider exploded" in result["share_card_error"]

    @patch("video_transcript_api.llm.core.llm_client.LLMClient.call")
    def test_skip_summary_never_triggers_share_card(self, mock_call):
        """skip_summary=True (recalibrate-only round) must not attempt the
        share-card call at all."""
        mock_call.side_effect = _mock_llm_response
        coordinator = _make_coordinator()

        result = coordinator.process(
            content="Long enough text for summary. " * 100,
            title="Test Title",
            skip_summary=True,
        )

        assert result["summary_text"] is None
        assert result["share_card"] is None
        assert result["share_card_error"] is None
        assert not any(
            c.kwargs.get("task_type") == "share_card"
            for c in mock_call.call_args_list
        )

    @patch("video_transcript_api.llm.core.llm_client.LLMClient.call")
    def test_invalid_card_output_surfaces_error(self, mock_call):
        """A contract-violating card (empty points) must come back as
        card=None + reason, with the summary unaffected."""

        def bad_card_response(*args, **kwargs):
            if kwargs.get("task_type") == "share_card":
                response = Mock()
                response.structured_output = {"headline": "", "points": []}
                response.text = ""
                return response
            return _mock_llm_response(*args, **kwargs)

        mock_call.side_effect = bad_card_response
        coordinator = _make_coordinator()

        result = coordinator.process(
            content="Long enough text for summary. " * 100,
            title="Test Title",
        )

        assert result["summary_text"] is not None
        assert result["share_card"] is None
        assert "headline" in result["share_card_error"]
