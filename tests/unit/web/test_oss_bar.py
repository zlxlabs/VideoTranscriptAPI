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

    def test_oss_bar_has_no_role_banner(self):
        html = _render_transcript()
        bar_opening = re.search(r'<div[^>]*class="oss-bar"[^>]*>', html)
        assert bar_opening is not None
        assert 'role="banner"' not in bar_opening.group(0), "Expected .oss-bar not to have role=\"banner\""
        assert 'aria-label="开源项目信息"' in bar_opening.group(0)


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
        assert 'title="开源，可自行部署"' in gh_tag
        assert 'aria-label="GitHub 开源仓库 VideoTranscriptAPI"' in gh_tag

        x_pattern = re.compile(
            r'<a[^>]+href="' + re.escape(X_URL) + r'"[^>]*>', re.IGNORECASE
        )
        match_x = x_pattern.search(html)
        assert match_x is not None, "X link tag not found"
        x_tag = match_x.group(0)
        assert 'target="_blank"' in x_tag
        assert 'rel="noopener"' in x_tag
        assert 'title="在 X 上关注 @bylixing"' in x_tag
        assert 'aria-label="作者张立行的 X 账号 @bylixing"' in x_tag

    def test_link_inner_visible_text(self):
        html = _render_transcript()
        gh_match = re.search(
            r'<a[^>]+href="' + re.escape(GITHUB_URL) + r'"[^>]*>(.*?)</a>',
            html,
            re.DOTALL,
        )
        assert gh_match is not None, "GitHub link not found"
        gh_inner = re.sub(r'<svg.*?</svg>', '', gh_match.group(1), flags=re.DOTALL)
        gh_text = re.sub(r'<[^>]+>', '', gh_inner).strip()
        assert gh_text == "VideoTranscriptAPI"

        x_match = re.search(
            r'<a[^>]+href="' + re.escape(X_URL) + r'"[^>]*>(.*?)</a>',
            html,
            re.DOTALL,
        )
        assert x_match is not None, "X link not found"
        x_inner = re.sub(r'<svg.*?</svg>', '', x_match.group(1), flags=re.DOTALL)
        x_text = re.sub(r'<[^>]+>', '', x_inner).strip()
        assert x_text == "张立行"

    def test_inline_svg_icons_used_without_emojis(self):
        html = _render_transcript()
        bar_html = html[html.index('class="oss-bar"'):html.index('class="header"')]

        svg_matches = re.findall(r'<svg[^>]+class="oss-bar-icon"[^>]*>', bar_html)
        assert len(svg_matches) == 2, "Expected exactly 2 oss-bar SVG icons"
        for tag in svg_matches:
            assert 'fill="currentColor"' in tag
            assert 'aria-hidden="true"' in tag


class TestTopOssBarResponsiveStyling:
    """Ensure unified visible text across viewports and responsive touch targets."""

    def test_unified_oss_bar_visible_text(self):
        html = _render_transcript()
        match = re.search(r'<div[^>]*class="oss-bar"[^>]*>(.*?)</div>', html, re.DOTALL)
        assert match is not None, "oss-bar element not found"
        without_svg = re.sub(r'<svg.*?</svg>', '', match.group(1), flags=re.DOTALL)
        visible_text = re.sub(r'<[^>]+>', '', without_svg)
        normalized = re.sub(r'\s+', ' ', visible_text).strip()
        assert normalized == "由开源项目 VideoTranscriptAPI 生成 · by 张立行"

    def test_no_legacy_text_or_desktop_mobile_switching_classes(self):
        html = _render_transcript()
        match = re.search(r'<div[^>]*class="oss-bar"[^>]*>(.*?)</div>', html, re.DOTALL)
        assert match is not None, "oss-bar element not found"
        without_svg = re.sub(r'<svg.*?</svg>', '', match.group(1), flags=re.DOTALL)
        visible_text = re.sub(r'<[^>]+>', '', without_svg)

        assert "@bylixing" not in visible_text
        assert "GitHub ↗" not in visible_text
        assert "本页由开源项目" not in visible_text
        assert "可自行部署" not in visible_text

        for class_name in ["oss-bar-desc-desktop", "oss-bar-desc-mobile", "oss-bar-author-label"]:
            assert class_name not in html

    def test_media_query_and_touch_target_in_styles(self):
        base_html = (TEMPLATES_DIR / "base.html").read_text(encoding="utf-8")
        assert "@media (max-width: 640px)" in base_html
        assert "min-height: 32px" in base_html
        assert "white-space: nowrap" in base_html
        assert ".oss-bar-desc-desktop" not in base_html
        assert ".oss-bar-desc-mobile" not in base_html
        assert ".oss-bar-author-label" not in base_html


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
