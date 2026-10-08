import base64
import json

import pytest

from video_transcript_api.api.routes.uploads import decode_upload_metadata


def encode_metadata(payload):
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def test_decode_upload_metadata_preserves_unicode_and_normalizes_options():
    payload = {
        "filename": "../会议 录音.mp4",
        "byte_size": 17,
        "title": "讨论：咖啡与模型",
        "source_url": "https://example.com/watch?id=1",
        "retention": "30d",
        "processing_options": {"calibrate": False, "summarize": True},
    }

    metadata = decode_upload_metadata(encode_metadata(payload))

    assert metadata == {
        **payload,
        "filename": "会议 录音.mp4",
        "processing_options": {
            "calibrate": False,
            "summarize": True,
            "infer_speaker_names": True,
            "chapters": True,
        },
    }


@pytest.mark.parametrize(
    "mutation",
    [
        {"title": "x" * 201},
        {"filename": "é" * 128},
        {"source_url": "https://user:secret@example.com/a"},
        {"source_url": "javascript:alert(1)"},
        {"byte_size": 0},
    ],
)
def test_decode_upload_metadata_rejects_invalid_bounded_fields(mutation):
    payload = {
        "filename": "clip.mp4",
        "byte_size": 17,
        "title": None,
        "source_url": None,
        "retention": "30d",
        "processing_options": {},
        **mutation,
    }

    with pytest.raises(ValueError):
        decode_upload_metadata(encode_metadata(payload))
