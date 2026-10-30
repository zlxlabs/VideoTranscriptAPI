import json
import re

from video_transcript_api.i18n import ui_language_scope
from video_transcript_api.utils.rendering.dialog_renderer import DialogRenderer


def _render_payload(tmp_path, serialized_payload, language="zh-CN"):
    cache_dir = tmp_path / "render"
    cache_dir.mkdir()
    (cache_dir / "llm_processed.json").write_text(serialized_payload, encoding="utf-8")
    with ui_language_scope(language):
        return DialogRenderer()._render_from_structured_data(str(cache_dir))


def _legacy_html():
    return "\n".join([
        '<div class="dialog-container">',
        "",
        '                <div id="dlg-0" class="dialog-item">',
        "                    ",
        '                    <div class="speaker-header">',
        '                        <div class="speaker-tag" title="较可靠" data-confidence-level="较可靠" style="background-color: #3B82F6;">',
        "                            Alice",
        "                            ",
        "                        </div>",
        "                        ",
        "                    </div>",
        "                    ",
        '                    <div class="dialog-content">',
        "                        <p>Hello</p>",
        "                    </div>",
        "                </div>",
        "                ",
        "</div>",
    ])


def test_low_level_renders_guess_name_and_unverified_basis_from_serialized_shape(tmp_path):
    payload = {
        "dialogs": [{
            "segment_id": "seg_00000002_speaker2",
            "speaker": "说话人2",
            "speaker_id": "Speaker2",
            "text": "Hello",
        }],
        "speaker_mapping": {"Speaker2": "说话人2"},
        "speaker_inference_meta": {
            "Speaker2": {
                "confidence_level": "low",
                "guess_name": "Sam Altman",
                "basis": {
                    "evidence_type": "title_credit",
                    "quote": "@sama",
                    "role": "guest",
                    "rejection_reason": "name_not_verified",
                },
            },
        },
    }

    html = _render_payload(tmp_path, json.dumps(payload, ensure_ascii=False))

    assert 'data-speaker-level="low"' in html
    assert "说话人2 · 可能是 Sam Altman" in html
    assert 'title="依据：标题「@sama」；角色：嘉宾；未通过核对"' in html


def test_serialized_history_without_confidence_level_keeps_legacy_tag_bytes(tmp_path):
    payload = {
        "dialogs": [{"speaker": "Alice", "speaker_id": "S1", "text": "Hello"}],
        "speaker_mapping": {"S1": "Alice"},
        "speaker_inference_meta": {"S1": {"confidence": 0.8}},
    }

    html = _render_payload(tmp_path, json.dumps(payload, ensure_ascii=False))

    assert html == _legacy_html()


def test_serialized_valid_levels_render_each_label_badge_and_basis(tmp_path):
    payload = {
        "dialogs": [
            {"segment_id": "seg-high", "speaker": "木子", "speaker_id": "S1", "text": "one"},
            {"segment_id": "seg-medium", "speaker": "说话人1｜作者关联推测:John Collison", "speaker_id": "S2", "text": "two"},
            {"segment_id": "seg-low", "speaker": "说话人2", "speaker_id": "S3", "text": "three"},
            {"segment_id": "seg-none", "speaker": "说话人1", "speaker_id": "S4", "text": "four"},
        ],
        "speaker_mapping": {
            "S1": "木子",
            "S2": "说话人1｜作者关联推测:John Collison",
            "S3": "说话人2",
            "S4": "说话人1",
        },
        "speaker_inference_meta": {
            "S1": {"confidence_level": "high", "guess_name": "木子", "confidence": 0.99},
            "S2": {
                "confidence_level": "medium",
                "confidence": 0.99,
                "guess_name": "John Collison",
                "basis": {
                    "evidence_type": "author_host",
                    "quote": "John Collison",
                    "role": "host",
                },
            },
            "S3": {
                "confidence_level": "low",
                "confidence": 0.1,
                "guess_name": "Sam Altman",
                "basis": {
                    "evidence_type": "title_credit",
                    "quote": "@sama",
                    "role": "guest",
                },
            },
            "S4": {"confidence_level": "none", "confidence": 0.0},
        },
    }

    html = _render_payload(tmp_path, json.dumps(payload, ensure_ascii=False))
    tags = re.findall(r'<div class="speaker-tag"[^>]*>.*?</div>', html, re.DOTALL)

    assert len(tags) == 4
    assert "木子" in tags[0]
    assert 'data-speaker-level="high">高</span>' in tags[0]
    assert "speaker-pending" not in tags[0]
    assert "John Collison" in tags[1]
    assert "说话人1｜作者关联推测:John Collison" in tags[1]
    assert 'data-speaker-level="medium">中</span>' in tags[1]
    assert "依据：作者「John Collison」；角色：主持人" in tags[1]
    assert "说话人2 · 可能是 Sam Altman" in tags[2]
    assert 'data-speaker-level="low">低</span>' in tags[2]
    assert 'title="依据：标题「@sama」；角色：嘉宾；未通过核对"' in tags[2]
    assert 'data-confidence-level="none"' in tags[3]
    assert 'data-speaker-status="pending">待确认</span>' in tags[3]
    assert 'data-speaker-level=' not in tags[3]
    assert "较可靠" not in html
    assert "AI 推断" not in html


def test_confirmed_override_suppresses_level_badge(tmp_path):
    payload = {
        "dialogs": [{
            "segment_id": "seg-confirmed",
            "speaker": "木子",
            "speaker_id": "S1",
            "text": "Hello",
        }],
        "speaker_mapping": {"S1": "木子"},
        "segment_overrides": {
            "seg-confirmed": {"name": "木子", "status": "confirmed"},
        },
        "speaker_inference_meta": {"S1": {"confidence_level": "high", "guess_name": "木子"}},
    }

    html = _render_payload(tmp_path, json.dumps(payload, ensure_ascii=False))

    assert 'data-override-status="confirmed"' in html
    assert 'speaker-override-confirmed' in html
    assert 'data-speaker-level=' not in html
    assert 'data-confidence-level="high"' in html


def test_level_guess_and_basis_values_are_escaped_in_text_and_attributes(tmp_path):
    payload = {
        "dialogs": [{"speaker": "说话人2", "speaker_id": "S1", "text": "Hello"}],
        "speaker_mapping": {"S1": "说话人2"},
        "speaker_inference_meta": {
            "S1": {
                "confidence_level": "low",
                "guess_name": "<script>x</script>",
                "basis": {
                    "evidence_type": "title_credit",
                    "quote": '" onmouseover="bad&<',
                    "rejection_reason": 'bad" data-injected="true',
                },
            },
        },
    }

    html = _render_payload(tmp_path, json.dumps(payload, ensure_ascii=False))

    assert "&lt;script&gt;x&lt;/script&gt;" in html
    assert "<script>x</script>" not in html
    assert '&quot; onmouseover=&quot;bad&amp;&lt;' in html
    assert 'data-rejection-reason="bad&quot; data-injected=&quot;true"' in html
    assert ' data-injected="true"' not in html


def test_english_low_level_uses_localized_name_and_badge(tmp_path):
    payload = {
        "dialogs": [{"speaker": "Speaker 2", "speaker_id": "S1", "text": "Hello"}],
        "speaker_mapping": {"S1": "Speaker 2"},
        "speaker_inference_meta": {
            "S1": {
                "confidence_level": "low",
                "guess_name": "Sam Altman",
                "basis": {
                    "evidence_type": "title_credit",
                    "quote": "@sama",
                    "role": "guest",
                },
            },
        },
    }

    html = _render_payload(tmp_path, json.dumps(payload), language="en")

    assert "Speaker 2 · possibly Sam Altman" in html
    assert 'data-speaker-level="low">Low</span>' in html
    assert 'title="Basis: title &quot;@sama&quot;; role: guest; not verified"' in html


def test_each_basis_evidence_type_uses_a_localized_label(tmp_path):
    evidence_types = [
        "dialog_address",
        "title_credit",
        "author_host",
        "description_roster",
    ]
    payload = {
        "dialogs": [
            {"speaker": f"Name {index}", "speaker_id": f"S{index}", "text": "Hello"}
            for index in range(4)
        ],
        "speaker_mapping": {f"S{index}": f"Name {index}" for index in range(4)},
        "speaker_inference_meta": {
            f"S{index}": {
                "confidence_level": "high",
                "basis": {"evidence_type": evidence_type, "quote": f"Quote {index}"},
            }
            for index, evidence_type in enumerate(evidence_types)
        },
    }

    html = _render_payload(tmp_path, json.dumps(payload, ensure_ascii=False))
    tags = re.findall(r'<div class="speaker-tag"[^>]*>.*?</div>', html, re.DOTALL)

    assert '依据：对话称呼「Quote 0」' in tags[0]
    assert '依据：标题「Quote 1」' in tags[1]
    assert '依据：作者「Quote 2」' in tags[2]
    assert '依据：简介名单「Quote 3」' in tags[3]


def test_unknown_confidence_level_uses_exact_legacy_markup(tmp_path):
    for index, invalid_level in enumerate(("future-level", [], {})):
        case_dir = tmp_path / str(index)
        case_dir.mkdir()
        payload = {
            "dialogs": [{"speaker": "Alice", "speaker_id": "S1", "text": "Hello"}],
            "speaker_mapping": {"S1": "Alice"},
            "speaker_inference_meta": {
                "S1": {"confidence_level": invalid_level, "confidence": 0.8},
            },
        }

        html = _render_payload(case_dir, json.dumps(payload, ensure_ascii=False))

        assert html == _legacy_html()
