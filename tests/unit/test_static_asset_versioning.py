"""Unit tests for static asset cache-busting.

Two mechanisms work together:
- a content-hash version token (``asset_v``, sha256 of every file under
  ``src/web/static`` truncated to 8 hex chars) injected into the Jinja
  template globals, so rendered pages reference ``/static/...?v=<token>``
  and any static file change busts intermediary/browser caches;
- ``Cache-Control: no-cache`` on every ``/static/`` response, so caches
  always revalidate (ETag still yields 304 and saves bandwidth -- this is
  revalidation, not no-store).

All console output must be pure English (no emoji, no Chinese), per
project testing conventions.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_transcript_api.api.context import (
    compute_asset_version,
    get_static_dir,
    get_templates,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _minimal_llm_config() -> dict:
    """Smallest llm section satisfying LLMConfig.from_dict's hard keys;
    base_url points at a closed local port so nothing reaches the network."""
    return {
        "api_key": "test-llm-key",
        "base_url": "http://127.0.0.1:1/v1",
        "calibrate_model": "test-calibrate-model",
        "summary_model": "test-summary-model",
    }


def _minimal_config(tmp_path: Path) -> dict:
    return {
        "api": {"host": "127.0.0.1", "port": 8000, "auth_token": "test-token"},
        "concurrent": {"max_workers": 1, "queue_size": 2, "llm_max_workers": 1},
        "storage": {
            "cache_dir": str(tmp_path / "cache"),
            "workspace_dir": str(tmp_path / "workspace"),
            "temp_dir": str(tmp_path / "temp"),
            "audit_db": str(tmp_path / "audit.db"),
        },
        "web": {"base_url": "http://localhost:8000"},
        "llm": _minimal_llm_config(),
        "log": {"file": str(tmp_path / "app.log")},
    }


def _render_transcript(**overrides) -> str:
    """Render transcript.html through the app's own Jinja2Templates so the
    asset_v global injected by get_templates() is in scope."""
    ctx = {
        "title": "Sample Video Title",
        "author": "Sample Author",
        "url": "https://example.com/video/123",
        "created_at_display": "2026-07-11 10:00",
        "platform": "youtube",
        "summary_html": "<p>Summary body.</p>",
        "calibrated_html": "<p>Calibrated body.</p>",
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
    return get_templates().env.get_template("transcript.html").render(**ctx)


class TestComputeAssetVersion:
    def test_token_is_short_lowercase_hex(self, tmp_path):
        _write(tmp_path / "js" / "app.js", "console.log(1)")
        token = compute_asset_version(tmp_path)
        assert len(token) == 8
        assert all(c in "0123456789abcdef" for c in token)

    def test_token_stable_when_content_unchanged(self, tmp_path):
        """Same files -> same token, so unchanged assets keep their cache."""
        _write(tmp_path / "js" / "app.js", "alpha")
        _write(tmp_path / "css" / "site.css", "body {}")
        assert compute_asset_version(tmp_path) == compute_asset_version(tmp_path)

    def test_token_changes_when_file_content_changes(self, tmp_path):
        _write(tmp_path / "js" / "app.js", "alpha")
        before = compute_asset_version(tmp_path)
        _write(tmp_path / "js" / "app.js", "alpha-modified")
        assert compute_asset_version(tmp_path) != before

    def test_token_changes_when_file_added(self, tmp_path):
        _write(tmp_path / "js" / "app.js", "alpha")
        before = compute_asset_version(tmp_path)
        _write(tmp_path / "js" / "extra.js", "beta")
        assert compute_asset_version(tmp_path) != before

    def test_missing_directory_yields_stable_hex_token(self, tmp_path):
        token = compute_asset_version(tmp_path / "does-not-exist")
        assert len(token) == 8
        assert token == compute_asset_version(tmp_path / "does-not-exist")


class TestTemplateAssetVersioning:
    def test_templates_env_exposes_asset_v_global(self):
        token = get_templates().env.globals.get("asset_v")
        assert isinstance(token, str)
        assert token == compute_asset_version(get_static_dir())

    def test_transcript_page_versions_floating_toc_assets(self):
        html = _render_transcript()
        token = compute_asset_version(get_static_dir())
        assert f'href="/static/css/floating-toc.css?v={token}"' in html
        assert f'src="/static/js/floating-toc.js?v={token}"' in html

    def test_transcript_page_has_no_unversioned_static_asset_refs(self):
        """Every /static/ css/js reference in the rendered page must carry
        the version query parameter."""
        html = _render_transcript()
        assert 'href="/static/css/floating-toc.css"' not in html
        assert 'src="/static/js/floating-toc.js"' not in html

    def test_base_template_versions_history_page_link(self):
        """history.html suffers the same intermediary-cache disease; its
        link is versioned with the same token."""
        html = _render_transcript()
        assert '/static/history.html?v=' in html


class TestStaticNoCacheHeader:
    @pytest.fixture
    def client(self, tmp_path):
        from video_transcript_api.api.app import create_app

        config = _minimal_config(tmp_path)
        app = create_app(config_loader=lambda: config, start_background=False)
        with TestClient(app) as test_client:
            yield test_client

    def test_static_js_response_has_no_cache_header(self, client):
        resp = client.get("/static/js/floating-toc.js")
        assert resp.status_code == 200
        assert resp.headers["Cache-Control"] == "no-cache"

    def test_static_css_response_has_no_cache_header(self, client):
        resp = client.get("/static/css/floating-toc.css")
        assert resp.status_code == 200
        assert resp.headers["Cache-Control"] == "no-cache"

    def test_static_304_response_keeps_no_cache_header(self, client):
        """ETag revalidation must still save bandwidth (304) and the 304
        response itself must carry no-cache so caches keep revalidating."""
        first = client.get("/static/js/floating-toc.js")
        etag = first.headers.get("ETag")
        assert etag
        second = client.get(
            "/static/js/floating-toc.js", headers={"If-None-Match": etag}
        )
        assert second.status_code == 304
        assert second.headers["Cache-Control"] == "no-cache"
