"""
Structural regression tests for the top open-source / author bar:
- Presence on all pages inheriting base.html (transcript.html, error.html, etc.)
- Positioned as the first child within .container before .header
- Links pointing to zlxlabs/VideoTranscriptAPI and x.com/bylixing with target="_blank", rel="noopener", aria-label
- Inline SVGs for GitHub mark and X logo with fill="currentColor" and aria-hidden="true"
- Responsive switching via <=640px media query
- No legacy repository URL (zj1123581321) in src/web and views.py

Templates are rendered directly via a plain Jinja2 environment pointed at
src/web/templates, independent of FastAPI/config bootstrap.

All console output must be in English only per project testing conventions.
"""

from pathlib import Path
import re
import jinja2
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
TEMPLATES_DIR = PROJECT_ROOT / "src" / "web" / "templates"
STATIC_DIR = PROJECT_ROOT / "src" / "web" / "static"
VIEWS_FILE = PROJECT_ROOT / "src" / "video_transcript_api" / "api" / "routes" / "views.py"

GITHUB_URL = "https://github.com/zlxlabs/VideoTranscriptAPI"
X_URL = "https://x.com/bylixing"
LEGACY_REPO = "zj1123581321/VideoTranscriptAPI"


def _jinja_env() -> jinja2.Environment:
    """Build a standalone Jinja2 environment mirroring app settings."""
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=True,
    )
    env.globals["asset_v"] = "test-asset-v"
    return env


def _render_transcript(**overrides) -> str:
    ctx = {
        "title": "Sample Transcript Title",
        "author": "Sample Author",
        "url": "https://example.com/video/123",
        "created_at_display": "2026-07-11 10:00",
        "platform": "youtube",
        "summary_html": "<p>Summary text.</p>",
        "calibrated_html": "<p>Calibrated transcript text.</p>",
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
    return _jinja_env().get_template("transcript.html").render(**ctx)


def _render_error(**overrides) -> str:
    ctx = {
        "title": "出现错误",
        "message": "页面无法正常显示",
        "url": "https://example.com/video/123",
        "created_at_display": "2026-07-11 10:00",
    }
    ctx.update(overrides)
    return _jinja_env().get_template("error.html").render(**ctx)


class TestTopOssBarPresenceAndPlacement:
    """Assert top OSS bar markup and placement on templates inheriting base.html."""

    @pytest.mark.parametrize("renderer", [_render_transcript, _render_error])
    def test_renders_oss_bar_with_exact_urls(self, renderer):
        html = renderer()
        assert 'class="oss-bar"' in html
        assert GITHUB_URL in html
        assert X_URL in html

    @pytest.mark.parametrize("renderer", [_render_transcript, _render_error])
    def test_oss_bar_placed_before_header(self, renderer):
        html = renderer()
        bar_idx = html.index('class="oss-bar"')
        header_idx = html.index('class="header"')
        assert bar_idx < header_idx, "Expected .oss-bar to appear before .header"

    def test_container_encloses_oss_bar_as_first_child(self):
        html = _render_transcript()
        container_tag = '<div class="container">'
        container_idx = html.index(container_tag)
        bar_idx = html.index('class="oss-bar"')
        assert container_idx < bar_idx
        snippet = html[container_idx + len(container_tag):bar_idx].strip()
        assert snippet == "<div", f"Expected only div tag before class=\"oss-bar\", got: {snippet!r}"


class TestTopOssBarLinkAttributesAndIcons:
    """Verify link security attributes, accessibility labels, and inline SVGs."""

    def test_links_security_and_accessibility_attributes(self):
        html = _render_transcript()

        github_pattern = re.compile(
            r'<a[^>]+href="' + re.escape(GITHUB_URL) + r'"[^>]*>', re.IGNORECASE
        )
        match_gh = github_pattern.search(html)
        assert match_gh is not None, "GitHub link tag not found"
        gh_tag = match_gh.group(0)
        assert 'target="_blank"' in gh_tag
        assert 'rel="noopener"' in gh_tag
        assert "aria-label=" in gh_tag

        x_pattern = re.compile(
            r'<a[^>]+href="' + re.escape(X_URL) + r'"[^>]*>', re.IGNORECASE
        )
        match_x = x_pattern.search(html)
        assert match_x is not None, "X link tag not found"
        x_tag = match_x.group(0)
        assert 'target="_blank"' in x_tag
        assert 'rel="noopener"' in x_tag
        assert "aria-label=" in x_tag

    def test_inline_svg_icons_used_without_emojis(self):
        html = _render_transcript()
        bar_html = html[html.index('class="oss-bar"'):html.index('class="header"')]

        svg_matches = re.findall(r'<svg[^>]+class="oss-bar-icon"[^>]*>', bar_html)
        assert len(svg_matches) == 2, "Expected exactly 2 oss-bar SVG icons"
        for tag in svg_matches:
            assert 'fill="currentColor"' in tag
            assert 'aria-hidden="true"' in tag


class TestTopOssBarResponsiveStyling:
    """Ensure media query <=640px and desktop/mobile text classes exist."""

    def test_desktop_and_mobile_text_elements_present(self):
        html = _render_transcript()
        assert "oss-bar-desc-desktop" in html
        assert "本页由开源项目 VideoTranscriptAPI 生成，可自行部署" in html
        assert "oss-bar-desc-mobile" in html
        assert "开源项目" in html
        assert "GitHub ↗" in html
        assert "@bylixing" in html

    def test_media_query_and_touch_target_in_styles(self):
        base_html = (TEMPLATES_DIR / "base.html").read_text(encoding="utf-8")
        assert "@media (max-width: 640px)" in base_html
        assert ".oss-bar-desc-desktop" in base_html
        assert ".oss-bar-desc-mobile" in base_html
        assert "min-height: 32px" in base_html


class TestNoLegacyRepoUrlsInWebAssets:
    """Ensure zj1123581321/VideoTranscriptAPI does not exist in web templates, static files, or views.py."""

    def test_no_legacy_repo_in_templates(self):
        for template_file in TEMPLATES_DIR.glob("*.html"):
            content = template_file.read_text(encoding="utf-8")
            assert LEGACY_REPO not in content, f"Found legacy repo URL in {template_file.name}"

    def test_no_legacy_repo_in_static_pages(self):
        for static_file in STATIC_DIR.glob("*.html"):
            content = static_file.read_text(encoding="utf-8")
            assert LEGACY_REPO not in content, f"Found legacy repo URL in {static_file.name}"

    def test_no_legacy_repo_in_views(self):
        content = VIEWS_FILE.read_text(encoding="utf-8")
        assert LEGACY_REPO not in content, "Found legacy repo URL in views.py"
