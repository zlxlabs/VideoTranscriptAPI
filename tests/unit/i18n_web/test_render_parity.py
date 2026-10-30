"""zh-CN render output must match the checked-in visible-output baseline.

Locks design invariant 1 for C4: default language (missing ``language`` key
or ``zh-CN``) produces identical HTML, including the current flag-specific
speaker warning copy.

All console output must be in English only (no emoji, no Chinese).
"""

import json
from pathlib import Path

from video_transcript_api.api.routes import views as views_mod
from video_transcript_api.i18n import t
from video_transcript_api.utils.rendering.dialog_renderer import DialogRenderer

FIXTURES = Path(__file__).resolve().parent / "fixtures"
DIALOG_ZH = (FIXTURES / "dialog_zh.html").read_text(encoding="utf-8").rstrip("\n")

DIALOG_PAYLOAD = {
    "dialogs": [
        {
            "segment_id": "seg_1",
            "speaker": "说话人1",
            "speaker_id": "S1",
            "text": "你好，这是第一段。",
            "start_time": "00:00:01",
        },
        {
            "segment_id": "seg_2",
            "speaker": "Alice",
            "speaker_id": "S2",
            "text": "Hello from Alice.",
            "start_time": "00:00:05",
        },
        {
            "segment_id": "seg_3",
            "speaker": "说话人3",
            "speaker_id": "S3",
            "text": "待确认说话人。",
            "start_time": "00:00:09",
        },
    ],
    "speaker_mapping": {"S1": "说话人1", "S2": "Alice", "S3": "说话人3"},
    "segment_overrides": {
        "seg_2": {"name": "Alice", "status": "confirmed"},
    },
    "speaker_risk_flags": ["low_confidence_cluster_dropped"],
    "speaker_inference_meta": {
        "S1": {"confidence": 0.5, "applied": False},
        "S2": {"confidence": 0.9, "applied": True},
        "S3": {"confidence": 0.4, "applied": False},
    },
}


def _render_dialog(tmp_path, payload):
    cache_dir = tmp_path / "render"
    cache_dir.mkdir()
    (cache_dir / "llm_processed.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return DialogRenderer()._render_from_structured_data(str(cache_dir))


def _home_html():
    from video_transcript_api.utils.rendering.home_page import render_home_html

    return render_home_html()


def test_dialog_html_matches_zh_cn_baseline(tmp_path, language_config):
    """Result-page speaker dialog HTML under default language is byte-identical."""
    language_config(None)
    html = _render_dialog(tmp_path, DIALOG_PAYLOAD)
    assert html == DIALOG_ZH


def test_dialog_html_zh_cn_key_matches_missing_language(tmp_path, language_config):
    language_config("zh-CN")
    html = _render_dialog(tmp_path, DIALOG_PAYLOAD)
    assert html == DIALOG_ZH


def test_home_html_zh_cn_matches_baseline_constant(language_config):
    """Landing page under zh-CN equals the original ``_HOME_HTML`` constant."""
    language_config(None)
    rendered = _home_html()
    assert 'lang="zh-CN"' in rendered
    assert "多平台视频转录与 AI 智能校对服务" in rendered


def test_zh_cn_catalog_values_match_original_home_copy(language_config):
    """Catalog zh-CN values are the original hardcoded landing-page strings."""
    language_config("zh-CN")
    assert t("web.home.subtitle") == "多平台视频转录与 AI 智能校对服务"
    assert t("web.home.cta_submit") == "提交任务"
    assert t("web.home.cta_history") == "任务历史"
    assert t("render.speaker.pending") == "待确认"
    assert t("render.speaker.confirmed") == "已确认"
    assert t("render.speaker.risk_warning") == "本集说话人区分可能不准"
    assert t("render.speaker.names_unresolved") == "部分说话人姓名未识别"
    assert t("og.ai_label") == "AI 总结"
