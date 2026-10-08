"""Completion renderer labels, links, and text threshold."""
from video_transcript_api.api.services.llm_ops import _render_completion_body


def _result(text="calibrated", summary=None, skip=True, stats=None):
    return {
        "校对文本": text, "内容总结": summary, "skip_summary": skip,
        "stats": stats or {"original_length": len(text), "calibrated_length": len(text)},
        "models_used": {"calibrate_model": "cal-model", "summary_model": "sum-model"},
    }


def test_short_and_exact_threshold_text_is_included_with_models():
    short = "A" * 3000
    exact = "B" * 5000
    short_body = _render_completion_body(_result(text=short), "http://test/view/t")
    exact_body = _render_completion_body(_result(text=exact), "http://test/view/t")
    assert short in short_body and exact in exact_body
    assert "?raw=calibrated" in short_body
    assert "cal-model" in short_body and "sum-model" in short_body


def test_over_threshold_text_is_replaced_by_link_and_stats():
    long = "C" * 6000
    body = _render_completion_body(
        _result(text=long, stats={"original_length": 7000, "calibrated_length": 6000}),
        "http://test/view/t",
    )
    assert long not in body
    assert "6,000 字" in body and "7,000" in body
    assert "http://test/view/t?raw=calibrated" in body


def test_summary_and_status_warnings_are_preserved():
    summary = "persisted summary"
    summary_body = _render_completion_body(
        _result(text="raw", summary=summary, skip=False,
                stats={"summary_length": len(summary), "calibration_status": "disabled"}),
        "http://test/view/t",
    )
    disabled = _render_completion_body(
        _result(stats={"summary_status": "disabled"}), "http://test/view/t",
    )
    failed = _render_completion_body(
        _result(stats={"summary_status": "failed"}), "http://test/view/t",
    )
    assert summary in summary_body and "AI 校对未启用" in summary_body
    assert "未启用" in disabled and "生成失败" in failed


def test_calibrate_only_returns_only_view_link():
    body = _render_completion_body(
        _result(summary="not sent", skip=False), "http://test/view/t", calibrate_only=True,
    )
    assert body == "🌐 网页查看：http://test/view/t"
