"""Language-aware length budget checks for generated notes."""

from unittest.mock import patch

import pytest

from video_transcript_api.llm.core.config import LLMConfig
from video_transcript_api.llm.processors.notes_processor import (
    NotesProcessor,
    compute_notes_anchor_fingerprint,
)
from video_transcript_api.utils.llm_status import NotesStatus


class FakeNotesClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def call(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


@pytest.mark.parametrize(("language", "expected_calls"), [("en", 1), ("zh-CN", 2)])
def test_notes_density_retry_uses_language_budget(language, expected_calls):
    segments = [{"start_time": 0, "end_time": 10, "text": "s" * 100}]
    chapters = {
        "source": {
            "kind": "segments",
            "fingerprint": compute_notes_anchor_fingerprint(segments),
        },
        "chapters": [
            {"title": "Chapter", "gist": "A gist", "start_seg": 0, "end_seg": 0}
        ],
    }
    responses = (
        ["x" * 81, "y" * 70]
        if language == "en"
        else ["中" * 81, "文" * 70]
    )
    client = FakeNotesClient(responses)
    config = LLMConfig(
        api_key="test-key",
        base_url="http://localhost",
        calibrate_model="calibrate-model",
        summary_model="summary-model",
        notes_model="notes-model",
        notes_concurrency=1,
    )

    with patch(
        "video_transcript_api.llm.processors.notes_processor.get_language",
        return_value=language,
    ), patch(
        "video_transcript_api.llm.prompts.localized.get_language",
        return_value=language,
    ):
        result = NotesProcessor(client, config).process(
            chapters=chapters,
            source_segments=segments,
            source_kind="segments",
        )

    assert result.status is NotesStatus.GENERATED
    assert len(client.calls) == expected_calls
