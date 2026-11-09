"""Rendering contract for transcript partial-calibration warnings."""

from video_transcript_api.api.routes import views as views_mod
from video_transcript_api.i18n import ui_language_scope


def _render_partial_banner(partial_count: int) -> str:
    stats = {
        "calibration_status": "partial",
        "calibration_stats": {
            "total_chunks": 4,
            "success_count": 2,
            "fallback_count": 0,
            "failed_count": 0,
            "partial_count": partial_count,
        },
    }
    return views_mod.templates.env.get_template("transcript.html").render(stats=stats)


def test_partial_coverage_count_is_rendered_in_both_languages_only_when_nonzero():
    for language, expected in (
        ("zh-CN", "另有 2 段覆盖率不足。"),
        ("en", "Also, 2 segments had low coverage."),
    ):
        with ui_language_scope(language):
            rendered = _render_partial_banner(2)
            assert expected in rendered

            zero_rendered = _render_partial_banner(0)
            assert "覆盖率不足" not in zero_rendered
            assert "segments had low coverage" not in zero_rendered
