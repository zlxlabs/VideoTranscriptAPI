"""Template rendering tests for the two "disabled" honest-status text blocks
added by the per-task processing-depth feature:

- summary_state == 'disabled'  -> "该任务未启用内容总结" in the summary section
- stats.calibration_status == 'disabled' -> an info banner at the top of the
  calibrated-text section explaining the shown text is raw/uncalibrated

Templates are rendered directly via a plain Jinja2 environment (same pattern
as tests/unit/web/test_frontend_nav.py), independent of the FastAPI/config
bootstrap.

All console output must be in English only (no emoji, no Chinese).
"""

from pathlib import Path

import jinja2

PROJECT_ROOT = Path(__file__).resolve().parents[3]
TEMPLATES_DIR = PROJECT_ROOT / "src" / "web" / "templates"


def _jinja_env() -> jinja2.Environment:
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=True,
    )


def _base_context(**overrides) -> dict:
    ctx = {
        "title": "Sample Video Title",
        "author": "Sample Author",
        "url": "https://example.com/video/123",
        "created_at_display": "2026-07-11 10:00",
        "platform": "youtube",
        "summary_html": None,
        "summary_state": "generated",
        "calibrated_html": "<p>Body text.</p>",
        "use_speaker_recognition": False,
        "view_token": "test-view-token-123",
        "stats": {
            "original_length": 100,
            "calibrated_length": 90,
            "summary_length": 0,
        },
        "llm_config": None,
    }
    ctx.update(overrides)
    return ctx


def _render(**overrides) -> str:
    env = _jinja_env()
    return env.get_template("transcript.html").render(**_base_context(**overrides))


class TestSummaryDisabledState:
    def test_summary_disabled_shows_dedicated_message(self):
        html = _render(summary_html=None, summary_state="disabled")
        assert "该任务未启用内容总结" in html

    def test_summary_disabled_does_not_show_failed_or_pending_text(self):
        html = _render(summary_html=None, summary_state="disabled")
        assert "总结生成失败" not in html
        assert "总结处理中" not in html

    def test_summary_generated_state_unaffected(self):
        """Regression: existing generated/failed/skipped_short paths must be
        untouched by adding the new disabled branch."""
        html = _render(summary_html="<p>Real summary.</p>", summary_state="generated")
        assert "Real summary." in html
        assert "该任务未启用内容总结" not in html

    def test_summary_failed_state_unaffected(self):
        html = _render(summary_html=None, summary_state="failed")
        assert "总结生成失败" in html
        assert "该任务未启用内容总结" not in html

    def test_summary_skipped_short_state_unaffected(self):
        html = _render(summary_html=None, summary_state="skipped_short")
        assert "原始文本过短" in html
        assert "该任务未启用内容总结" not in html


class TestCalibrationDisabledBanner:
    def test_calibration_disabled_shows_info_banner(self):
        html = _render(
            calibrated_html="<p>Raw transcript text.</p>",
            stats={
                "original_length": 100,
                "calibrated_length": 100,
                "summary_length": 0,
                "calibration_status": "disabled",
            },
        )
        assert "本任务未启用 AI 校对" in html
        assert "以下为原始转录" in html

    def test_calibration_disabled_with_speaker_recognition_mentions_speaker_tags(self):
        html = _render(
            calibrated_html="<p>Speaker-labeled text.</p>",
            use_speaker_recognition=True,
            stats={
                "original_length": 100,
                "calibrated_length": 100,
                "summary_length": 0,
                "calibration_status": "disabled",
            },
        )
        assert "含说话人标注" in html

    def test_calibration_disabled_does_not_trigger_failure_warning(self):
        """The pre-existing none/partial warning banner must not also fire
        for 'disabled' -- it is documented as a separate, non-error state."""
        html = _render(
            calibrated_html="<p>Raw transcript text.</p>",
            stats={
                "original_length": 100,
                "calibrated_length": 100,
                "summary_length": 0,
                "calibration_status": "disabled",
            },
        )
        assert "校准失败" not in html
        assert "校准部分异常" not in html

    def test_calibration_full_status_unaffected(self):
        """Regression: normal 'full' status must not show the disabled banner."""
        html = _render(
            calibrated_html="<p>Calibrated text.</p>",
            stats={
                "original_length": 100,
                "calibrated_length": 100,
                "summary_length": 0,
                "calibration_status": "full",
            },
        )
        assert "本任务未启用 AI 校对" not in html

    def test_calibration_none_status_still_shows_failure_warning(self):
        """Regression: 'none' (real failure) must keep showing the existing
        failure warning, unaffected by the disabled-exclusion change."""
        html = _render(
            calibrated_html="<p>Fallback raw text.</p>",
            stats={
                "original_length": 100,
                "calibrated_length": 100,
                "summary_length": 0,
                "calibration_status": "none",
            },
        )
        assert "校准失败" in html
        assert "本任务未启用 AI 校对" not in html
