"""Unit tests for the processing_options request schema and its plumbing into
the task dict / LLM queue payload.

Covers:
- TranscribeRequest.processing_options: default (None -> all True), explicit
  combinations, invalid type -> 422.
- normalize_processing_options(): the single normalization helper reused by
  tasks.py / transcription.py / llm_ops.py.
- POST /api/transcribe: the task dict handed to the asyncio task_queue carries
  a normalized processing_options dict.

All console output must be in English only (no emoji, no Chinese).
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from video_transcript_api.api.services.transcription import (
    ProcessingOptions,
    TranscribeRequest,
    normalize_processing_options,
)


class TestProcessingOptionsSchema:
    def test_rejects_unknown_feature_gate(self):
        with pytest.raises(ValidationError):
            ProcessingOptions.model_validate({"summarizee": False})

    def test_default_is_all_true_except_chapters_is_none(self):
        """chapters defaults to None (follow summarize on normalize), not True.

        A constant True default would make legacy {summarize:false} clients
        still pay for chapters generation (design §5.2 / R2).
        """
        opts = ProcessingOptions()
        assert opts.calibrate is True
        assert opts.summarize is True
        assert opts.infer_speaker_names is True
        assert opts.chapters is None

    def test_explicit_calibrate_false_summarize_false(self):
        opts = ProcessingOptions(calibrate=False, summarize=False)
        assert opts.calibrate is False
        assert opts.summarize is False

    def test_explicit_calibrate_true_summarize_false(self):
        opts = ProcessingOptions(calibrate=True, summarize=False)
        assert opts.calibrate is True
        assert opts.summarize is False

    def test_calibrate_false_summarize_true_is_legal(self):
        """summarize=True with calibrate=False is a legal combination -- the
        summary will be generated from the raw (uncalibrated) transcript."""
        opts = ProcessingOptions(calibrate=False, summarize=True)
        assert opts.calibrate is False
        assert opts.summarize is True

    def test_invalid_type_raises(self):
        with pytest.raises(Exception):
            ProcessingOptions(calibrate="not-a-bool-and-not-coercible")

    @pytest.mark.parametrize(
        "field_name",
        ["calibrate", "summarize", "infer_speaker_names", "chapters"],
    )
    @pytest.mark.parametrize(
        "loose_value", ["yes", "no", "1", "0", "true", "false", 1, 0]
    )
    def test_loosely_coercible_value_is_rejected_not_silently_coerced(
        self, field_name, loose_value
    ):
        """ci-gate review: plain Pydantic `bool` would silently coerce
        "yes"/"1"/"no"/"0" strings AND JSON numbers 1/0 into True/False,
        contradicting the API's documented JSON-boolean contract -- a
        caller's string/number typo could unknowingly toggle a real,
        cost-bearing LLM stage on/off. StrictBool must reject these instead
        of coercing them. Covers both StrictBool fields, not just calibrate."""
        with pytest.raises(Exception):
            ProcessingOptions(**{field_name: loose_value})

    def test_transcribe_request_defaults_processing_options_to_none(self):
        req = TranscribeRequest(url="https://example.com/v1")
        assert req.processing_options is None

    def test_transcribe_request_accepts_nested_processing_options(self):
        req = TranscribeRequest(
            url="https://example.com/v1",
            processing_options={"calibrate": False, "summarize": True},
        )
        assert req.processing_options.calibrate is False
        assert req.processing_options.summarize is True
        assert req.processing_options.chapters is None

    def test_transcribe_request_accepts_explicit_chapters(self):
        req = TranscribeRequest(
            url="https://example.com/v1",
            processing_options={
                "calibrate": False,
                "summarize": False,
                "chapters": True,
            },
        )
        assert req.processing_options.chapters is True

    @pytest.mark.parametrize("loose_value", ["yes", "1", "0", "true", 1, 0])
    def test_use_speaker_recognition_rejects_loosely_coercible_value(
        self, loose_value
    ):
        """ci-gate review: same StrictBool contract as ProcessingOptions --
        this field switches the transcription engine and affects the cache
        key, so it must not silently coerce "yes"/"1"/1/0 either."""
        with pytest.raises(Exception):
            TranscribeRequest(
                url="https://example.com/v1",
                use_speaker_recognition=loose_value,
            )


class TestNormalizeProcessingOptions:
    def test_none_normalizes_to_all_true_including_chapters(self):
        assert normalize_processing_options(None) == {
            "calibrate": True,
            "summarize": True,
            "infer_speaker_names": True,
            "chapters": True,
        }

    def test_explicit_options_pass_through_as_dict(self):
        opts = ProcessingOptions(calibrate=False, summarize=True)
        assert normalize_processing_options(opts) == {
            "calibrate": False,
            "summarize": True,
            "infer_speaker_names": True,
            "chapters": True,  # follows summarize when omitted
        }

    @pytest.mark.parametrize(
        "summarize,expected_chapters",
        [(True, True), (False, False)],
    )
    def test_omitted_chapters_follows_summarize(self, summarize, expected_chapters):
        result = normalize_processing_options({"summarize": summarize})
        assert result["chapters"] is expected_chapters
        assert result["summarize"] is summarize

    @pytest.mark.parametrize(
        "chapters,summarize",
        [
            (True, True),
            (True, False),
            (False, True),
            (False, False),
        ],
    )
    def test_explicit_chapters_preserved_regardless_of_summarize(
        self, chapters, summarize
    ):
        result = normalize_processing_options(
            {"chapters": chapters, "summarize": summarize}
        )
        assert result["chapters"] is chapters
        assert result["summarize"] is summarize

    def test_dict_without_chapters_key_follows_summarize_false(self):
        """Legacy clients that only set calibrate/summarize false must not
        accidentally enable chapters."""
        result = normalize_processing_options(
            {"calibrate": False, "summarize": False}
        )
        assert result == {
            "calibrate": False,
            "summarize": False,
            "infer_speaker_names": True,
            "chapters": False,
        }

    def test_unknown_field_still_raises(self):
        with pytest.raises(ValidationError):
            normalize_processing_options({"chapters": True, "bogus": 1})


# ---------------------------------------------------------------------------
# POST /api/transcribe -> task dict plumbing
# ---------------------------------------------------------------------------

_FAKE_USER_INFO = {
    "user_id": "test-user",
    "api_key": "sk-test-key-123456",
    "wechat_webhook": None,
}


async def _fake_verify_token():
    return _FAKE_USER_INFO


def _build_test_app() -> FastAPI:
    app = FastAPI()
    from video_transcript_api.api.services.transcription import verify_token
    from video_transcript_api.api.routes import tasks

    app.include_router(tasks.router)
    app.dependency_overrides[verify_token] = _fake_verify_token
    return app


@pytest.fixture()
def mock_audit_logger():
    mock = MagicMock()
    mock.log_api_call.return_value = None
    with patch("video_transcript_api.api.routes.tasks.audit_logger", mock):
        yield mock


@pytest.fixture()
def mock_cache_manager():
    mock = MagicMock()
    mock.create_task.return_value = {
        "task_id": "task-abc-123",
        "view_token": "vt-xyz-789",
    }
    with patch("video_transcript_api.api.routes.tasks.cache_manager", mock):
        yield mock


@pytest.fixture()
def mock_task_queue():
    q = asyncio.Queue(maxsize=10)
    with patch(
        "video_transcript_api.api.routes.tasks.get_task_queue", return_value=q
    ):
        yield q


@pytest.fixture()
def mock_send_notification():
    with patch(
        "video_transcript_api.api.routes.tasks.send_view_link_wechat"
    ) as mock:
        yield mock


@pytest.fixture()
def mock_notification_router():
    with patch(
        "video_transcript_api.api.routes.tasks.get_notification_router"
    ) as mock:
        mock.return_value = MagicMock()
        yield mock


@pytest.fixture()
def client(
    mock_audit_logger,
    mock_cache_manager,
    mock_task_queue,
    mock_send_notification,
    mock_notification_router,
):
    app = _build_test_app()
    return TestClient(app)


class TestTranscribeTaskDictProcessingOptions:
    """POST /api/transcribe must hand a normalized processing_options dict to
    the queued task, regardless of whether the request specified it."""

    def test_no_processing_options_defaults_to_all_true_in_task_dict(
        self, client, mock_task_queue
    ):
        resp = client.post(
            "/api/transcribe",
            json={"url": "https://www.youtube.com/watch?v=abc123"},
        )
        assert resp.status_code == 200
        queued_task = mock_task_queue.get_nowait()
        assert queued_task["processing_options"] == {
            "calibrate": True,
            "summarize": True,
            "infer_speaker_names": True,
            "chapters": True,
        }

    def test_explicit_processing_options_reach_task_dict(self, client, mock_task_queue):
        resp = client.post(
            "/api/transcribe",
            json={
                "url": "https://www.youtube.com/watch?v=abc123",
                "processing_options": {"calibrate": False, "summarize": True},
            },
        )
        assert resp.status_code == 200
        queued_task = mock_task_queue.get_nowait()
        assert queued_task["processing_options"] == {
            "calibrate": False,
            "summarize": True,
            "infer_speaker_names": True,
            "chapters": True,  # omitted -> follows summarize
        }

    def test_calibrate_and_summarize_both_false(self, client, mock_task_queue):
        resp = client.post(
            "/api/transcribe",
            json={
                "url": "https://www.youtube.com/watch?v=abc123",
                "processing_options": {"calibrate": False, "summarize": False},
            },
        )
        assert resp.status_code == 200
        queued_task = mock_task_queue.get_nowait()
        assert queued_task["processing_options"] == {
            "calibrate": False,
            "summarize": False,
            "infer_speaker_names": True,
            "chapters": False,  # omitted -> follows summarize=false
        }

    def test_explicit_chapters_true_with_summarize_false(self, client, mock_task_queue):
        resp = client.post(
            "/api/transcribe",
            json={
                "url": "https://www.youtube.com/watch?v=abc123",
                "processing_options": {
                    "calibrate": False,
                    "summarize": False,
                    "chapters": True,
                },
            },
        )
        assert resp.status_code == 200
        queued_task = mock_task_queue.get_nowait()
        assert queued_task["processing_options"]["chapters"] is True
        assert queued_task["processing_options"]["summarize"] is False

    def test_invalid_processing_options_type_returns_422(self, client):
        resp = client.post(
            "/api/transcribe",
            json={
                "url": "https://www.youtube.com/watch?v=abc123",
                "processing_options": {"calibrate": "yes-please"},
            },
        )
        assert resp.status_code == 422

    @pytest.mark.parametrize(
        "field_name",
        ["calibrate", "summarize", "infer_speaker_names", "chapters"],
    )
    @pytest.mark.parametrize("loose_value", ["yes", "1", "0", "true", 1, 0])
    def test_loosely_coercible_boolean_value_returns_422(
        self, client, field_name, loose_value
    ):
        """ci-gate review: "yes"/"1"/1/0 etc. are NOT rejected by plain bool's
        lenient coercion (they'd silently become True/False) -- unlike the
        already-covered "yes-please" case above, which fails even lenient
        coercion. This is the actual gap StrictBool closes. Covers both
        StrictBool fields (calibrate/summarize), plus JSON numbers."""
        resp = client.post(
            "/api/transcribe",
            json={
                "url": "https://www.youtube.com/watch?v=abc123",
                "processing_options": {field_name: loose_value},
            },
        )
        assert resp.status_code == 422, f"{field_name}={loose_value!r} should be rejected"

    @pytest.mark.parametrize("loose_value", ["yes", "1", "0", "true", 1, 0])
    def test_loosely_coercible_use_speaker_recognition_returns_422(
        self, client, loose_value
    ):
        """use_speaker_recognition switches the transcription engine and
        affects the cache key -- the same StrictBool contract applies
        (ci-gate review, extending the ProcessingOptions fix to this field)."""
        resp = client.post(
            "/api/transcribe",
            json={
                "url": "https://www.youtube.com/watch?v=abc123",
                "use_speaker_recognition": loose_value,
            },
        )
        assert resp.status_code == 422, f"value {loose_value!r} should be rejected"
