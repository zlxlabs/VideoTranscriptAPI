"""Template rendering tests for summary condensation hint visibility."""

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
    context = {
        "title": "Sample Video Title",
        "url": "https://example.com/video/123",
        "author": "Sample Author",
        "created_at_display": "2026-07-11 10:00",
        "platform": "youtube",
        "summary_html": "<p>First summary paragraph.</p>",
        "summary_state": "generated",
        "calibrated_html": "<p>Calibrated body.</p>",
        "view_token": "test-view-token-123",
        "stats": {
            "original_length": 100,
            "calibrated_length": 80,
            "summary_length": 20,
        },
        "llm_config": None,
    }
    context.update(overrides)
    return context


def _render(**overrides) -> str:
    return _jinja_env().get_template("transcript.html").render(**_base_context(**overrides))


class TestSummaryCondensationHintStates:
    def test_failed_summary_state_omits_condensation_hint(self):
        html = _render(
            summary_html=None,
            summary_state="failed",
            stats={
                "original_length": 100,
                "calibrated_length": 80,
                "summary_length": 20,
            },
        )
        assert "总结生成失败" in html
        assert "内容高度浓缩，可能省略较多细节。" not in html

    def test_generated_summary_state_keeps_condensation_hint(self):
        html = _render(
            summary_state="generated",
            stats={
                "original_length": 100,
                "calibrated_length": 80,
                "summary_length": 20,
            },
        )
        assert "内容高度浓缩，可能省略较多细节。" in html
