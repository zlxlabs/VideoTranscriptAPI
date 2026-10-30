"""Speaker pending badge uses applied flag, with placeholder-regex fallback.

All console output must be in English only (no emoji, no Chinese).
"""

import json

from video_transcript_api.utils.rendering.dialog_renderer import DialogRenderer


def _render(tmp_path, payload):
    cache_dir = tmp_path / "render"
    cache_dir.mkdir()
    (cache_dir / "llm_processed.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return DialogRenderer()._render_from_structured_data(str(cache_dir))


def test_applied_true_skips_pending_even_for_chinese_placeholder(tmp_path, language_config):
    language_config("zh-CN")
    html = _render(
        tmp_path,
        {
            "dialogs": [
                {
                    "segment_id": "seg_1",
                    "speaker": "说话人1",
                    "speaker_id": "S1",
                    "text": "hello",
                }
            ],
            "speaker_inference_meta": {
                "S1": {"confidence": 0.9, "applied": True},
            },
        },
    )
    assert "speaker-pending" not in html
    assert "待确认" not in html


def test_applied_false_marks_pending_for_named_speaker(tmp_path, language_config):
    language_config("zh-CN")
    html = _render(
        tmp_path,
        {
            "dialogs": [
                {
                    "segment_id": "seg_1",
                    "speaker": "Alice",
                    "speaker_id": "S1",
                    "text": "hello",
                }
            ],
            "speaker_inference_meta": {
                "S1": {"confidence": 0.4, "applied": False},
            },
        },
    )
    assert "speaker-pending" in html
    assert "待确认" in html


def test_legacy_english_placeholder_is_pending_without_meta(tmp_path, language_config):
    language_config("zh-CN")
    html = _render(
        tmp_path,
        {
            "dialogs": [
                {"segment_id": "seg_1", "speaker": "Speaker 1", "text": "hello"}
            ],
        },
    )
    assert "speaker-pending" in html
    assert "待确认" in html


def test_legacy_chinese_placeholder_is_pending_without_meta(tmp_path, language_config):
    language_config("zh-CN")
    html = _render(
        tmp_path,
        {
            "dialogs": [
                {"segment_id": "seg_1", "speaker": "说话人2", "text": "hello"}
            ],
        },
    )
    assert "speaker-pending" in html
    assert "待确认" in html


def test_english_catalog_uses_unconfirmed_label(tmp_path, language_config):
    language_config("en")
    html = _render(
        tmp_path,
        {
            "dialogs": [
                {
                    "segment_id": "seg_1",
                    "speaker": "Speaker 1",
                    "speaker_id": "S1",
                    "text": "hello",
                }
            ],
            "speaker_inference_meta": {
                "S1": {"confidence": 0.3, "applied": False},
            },
            "speaker_risk_flags": ["low_confidence_cluster_dropped"],
        },
    )
    assert "Unconfirmed" in html
    assert "Some speaker names could not be identified" in html
    assert 'data-speaker-risk="names_unresolved"' in html
    assert "Speaker labels in this episode may be inaccurate" not in html
    assert "待确认" not in html
    assert "本集说话人区分可能不准" not in html
