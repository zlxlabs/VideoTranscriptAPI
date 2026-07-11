"""
Structural regression tests for the first batch of frontend improvements:
- unified in-site navigation (submit page / history page links) on
  base.html, index.html and history.html
- responsive breakpoints added to history.html
- "copy content" buttons on the transcript result page

These are lightweight structural assertions (markup/CSS presence), not full
DOM/browser tests. Templates are rendered directly via a plain Jinja2
environment pointed at src/web/templates, independent of the app's
FastAPI/config bootstrap, so this test suite does not require a real
config/config.jsonc to be present.

All console output must be in English only (no emoji, no Chinese), per
project testing conventions.
"""

from pathlib import Path

import jinja2
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
TEMPLATES_DIR = PROJECT_ROOT / "src" / "web" / "templates"
STATIC_DIR = PROJECT_ROOT / "src" / "web" / "static"


def _jinja_env() -> jinja2.Environment:
    """Build a standalone Jinja2 environment mirroring the app's autoescape setting."""
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=True,
    )


def _base_context(**overrides) -> dict:
    """Minimal context matching the fields views.py feeds into transcript.html."""
    ctx = {
        "title": "Sample Video Title",
        "author": "Sample Author",
        "url": "https://example.com/video/123",
        "created_at_display": "2026-07-11 10:00",
        "platform": "youtube",
        "summary_html": "<p>This is the summary body text.</p>",
        "calibrated_html": "<p>This is the calibrated transcript body text.</p>",
        "use_speaker_recognition": False,
        "view_token": "test-view-token-123",
        "stats": {
            "original_length": 100,
            "calibrated_length": 90,
            "summary_length": 50,
        },
        "llm_config": None,
    }
    ctx.update(overrides)
    return ctx


class TestTranscriptTemplateRendersWithoutErrors:
    """transcript.html (extends base.html) must render for typical view states."""

    def test_renders_success_state_without_raising(self):
        env = _jinja_env()
        template = env.get_template("transcript.html")
        html = template.render(**_base_context())
        assert "<html" in html
        assert "</html>" in html

    def test_renders_with_empty_summary_without_raising(self):
        """summary_html falsy -> placeholder text path, must still render cleanly."""
        env = _jinja_env()
        template = env.get_template("transcript.html")
        html = template.render(**_base_context(summary_html=""))
        assert "总结处理中" in html

    def test_renders_without_view_token_without_raising(self):
        """No view_token -> quick-copy/export/recalibrate blocks are skipped."""
        env = _jinja_env()
        template = env.get_template("transcript.html")
        html = template.render(**_base_context(view_token=None))
        assert "<html" in html


class TestUnifiedNavigationInTranscriptPage:
    """base.html header must expose site-wide navigation links."""

    def _render(self, **overrides) -> str:
        env = _jinja_env()
        return env.get_template("transcript.html").render(**_base_context(**overrides))

    def test_contains_submit_task_link(self):
        html = self._render()
        assert 'href="/add_task_by_web"' in html
        assert "site-nav-link" in html

    def test_contains_history_page_link(self):
        html = self._render()
        assert 'href="/static/history.html"' in html

    def test_nav_links_live_inside_site_nav_container(self):
        html = self._render()
        assert '<nav class="site-nav">' in html


class TestCopyContentButtonsInTranscriptPage:
    """Result page must offer a 'copy content' action for summary and calibrated text."""

    def _render(self, **overrides) -> str:
        env = _jinja_env()
        return env.get_template("transcript.html").render(**_base_context(**overrides))

    def test_summary_section_has_copy_button_targeting_its_block(self):
        html = self._render()
        assert 'data-copy-target="summary-content-block"' in html
        assert 'id="summary-content-block"' in html

    def test_calibrated_section_has_copy_button_targeting_its_block(self):
        html = self._render()
        assert 'data-copy-target="calibrated-content-block"' in html
        assert 'id="calibrated-content-block"' in html

    def test_copy_buttons_share_common_class_hook(self):
        html = self._render()
        assert html.count('class="recalibrate-btn copy-content-btn"') == 2

    def test_copy_button_hidden_when_summary_is_empty(self):
        """Placeholder summary text ('总结处理中') must not ship a copy button."""
        html = self._render(summary_html="")
        assert 'data-copy-target="summary-content-block"' not in html
        # The calibrated-text copy button must still be present.
        assert 'data-copy-target="calibrated-content-block"' in html

    def test_shared_clipboard_helper_is_wired_once(self):
        """Both URL-copy and content-copy buttons must reuse one clipboard function."""
        html = self._render()
        assert html.count("function copyTextToClipboard(") == 1
        assert "function fallbackCopyToClipboard(" in html
        assert ".copy-content-btn" in html
        assert ".quick-copy-btn" in html


class TestIndexPageNavigation:
    """index.html (submit page) header must link to the history page and home."""

    @pytest.fixture(scope="class")
    def html(self) -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    def test_contains_history_link(self, html):
        assert 'href="/static/history.html"' in html

    def test_contains_site_nav_container(self, html):
        assert 'class="site-nav"' in html
        assert "site-nav-link" in html


class TestHistoryPageNavigation:
    """history.html header must gain a submit-task link alongside the existing home link."""

    @pytest.fixture(scope="class")
    def html(self) -> str:
        return (STATIC_DIR / "history.html").read_text(encoding="utf-8")

    def test_contains_submit_task_link(self, html):
        assert 'href="/add_task_by_web"' in html

    def test_still_contains_home_link(self, html):
        assert 'href="/"' in html

    def test_nav_links_share_class_convention_with_other_pages(self, html):
        assert "site-nav-link" in html


class TestHistoryPageResponsiveBreakpoints:
    """history.html previously had zero @media rules; two breakpoints must exist now."""

    @pytest.fixture(scope="class")
    def html(self) -> str:
        return (STATIC_DIR / "history.html").read_text(encoding="utf-8")

    def test_has_768px_breakpoint(self, html):
        assert "@media (max-width: 768px)" in html

    def test_has_480px_breakpoint(self, html):
        assert "@media (max-width: 480px)" in html

    def test_task_row_grid_degrades_at_breakpoints(self, html):
        """Grid must be redefined (not just font-size tweaks) to avoid overflow."""
        media_section = html.split("@media (max-width: 768px)", 1)[1]
        assert "grid-template-columns" in media_section

    def test_filter_bar_can_stack_on_narrow_screens(self, html):
        narrow_section = html.split("@media (max-width: 480px)", 1)[1]
        assert ".filter-bar" in narrow_section
        assert "column" in narrow_section
