"""Unit tests for T7: chapters block rendering, dlg anchors, fingerprint unlink, TOC XSS.

Console output must be pure English (no emoji, no Chinese).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from video_transcript_api.llm.processors.chapters_processor import (
    _compute_fingerprint,
)
from video_transcript_api.utils.llm_status import ChaptersStatus
from video_transcript_api.utils.rendering.dialog_renderer import (
    DialogRenderer,
    render_chapters_html,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample_dialogs():
    return [
        {"speaker": "Alice", "text": "Hello world", "start_time": "0:00", "end_time": "0:05"},
        {"speaker": "Bob", "text": "Hi there", "start_time": "0:05", "end_time": "0:10"},
        {"speaker": "Alice", "text": "More talk", "start_time": "0:10", "end_time": "0:20"},
    ]


def _fingerprint_for(segments):
    pairs = []
    for i, seg in enumerate(segments):
        if not isinstance(seg, dict):
            continue
        text = seg.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        pairs.append((i, seg))
    return _compute_fingerprint(pairs) if pairs else None


def _chapters_payload(fingerprint: str | None, *, title: str = "Intro", gist: str = "About intro"):
    return {
        "format_version": "v1",
        "source": {
            "kind": "dialogs",
            "segment_count": 3,
            "fingerprint": fingerprint,
            "generated_at": "2026-07-19T00:00:00+00:00",
        },
        "chapters": [
            {
                "index": 0,
                "title": title,
                "gist": gist,
                "start_seg": 0,
                "end_seg": 1,
                "start_time": 0.0,
                "end_time": 10.0,
            },
            {
                "index": 1,
                "title": "Middle",
                "gist": "Second part",
                "start_seg": 2,
                "end_seg": 2,
                "start_time": 10.0,
                "end_time": 20.0,
            },
        ],
    }


# ---------------------------------------------------------------------------
# render_chapters_html: XSS + fingerprint unlink
# ---------------------------------------------------------------------------


class TestRenderChaptersHtml:
    def test_escapes_script_in_title_and_gist(self):
        payload = _chapters_payload(
            "abc",
            title='<script>alert(1)</script>',
            gist='<img src=x onerror=alert(1)> evil',
        )
        html_out = render_chapters_html(payload, fingerprint_ok=True)

        assert "<script>" not in html_out
        assert "<img src=x" not in html_out
        assert "onerror=" not in html_out or "&lt;" in html_out
        assert "&lt;script&gt;" in html_out
        assert "&lt;img" in html_out

    def test_fingerprint_ok_emits_dlg_links(self):
        payload = _chapters_payload("fp-ok")
        html_out = render_chapters_html(payload, fingerprint_ok=True)

        assert 'href="#dlg-0"' in html_out
        assert 'href="#dlg-2"' in html_out
        assert 'data-jump-ok="1"' in html_out
        assert "chapter-title-link" in html_out

    def test_fingerprint_mismatch_removes_jump_links(self):
        payload = _chapters_payload("fp-stale")
        html_out = render_chapters_html(payload, fingerprint_ok=False)

        assert 'href="#dlg-' not in html_out
        assert "chapter-title-link" not in html_out
        assert 'data-jump-ok="0"' in html_out
        # Titles still visible (escaped plain text)
        assert "Intro" in html_out
        assert "Middle" in html_out
        assert "chapter-card" in html_out

    def test_empty_payload_returns_empty_string(self):
        assert render_chapters_html(None, fingerprint_ok=True) == ""
        assert render_chapters_html({}, fingerprint_ok=True) == ""
        assert render_chapters_html({"chapters": []}, fingerprint_ok=True) == ""

    def test_title_with_quotes_is_escaped(self):
        payload = _chapters_payload("fp", title='Foo" onclick="alert(1)', gist="bar")
        html_out = render_chapters_html(payload, fingerprint_ok=True)
        assert 'onclick="alert(1)' not in html_out
        assert "&quot;" in html_out or "&#x27;" in html_out or "Foo" in html_out


# ---------------------------------------------------------------------------
# Structured dialog rendering: dlg-{i} anchors
# ---------------------------------------------------------------------------


class TestDialogAnchors:
    def test_structured_render_adds_dlg_ids(self, tmp_path: Path):
        data = {
            "format_version": "v3",
            "dialogs": _sample_dialogs(),
            "speaker_mapping": {},
        }
        (tmp_path / "llm_processed.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )

        html_out = DialogRenderer()._render_from_structured_data(str(tmp_path))

        assert 'id="dlg-0"' in html_out
        assert 'id="dlg-1"' in html_out
        assert 'id="dlg-2"' in html_out
        assert 'data-start-time="0:00"' in html_out
        assert 'data-start-time="0:05"' in html_out

    def test_structured_render_escapes_start_time_attr(self, tmp_path: Path):
        dialogs = [
            {
                "speaker": "A",
                "text": "hi",
                "start_time": '0:00" onmouseover="alert(1)',
            }
        ]
        (tmp_path / "llm_processed.json").write_text(
            json.dumps({"dialogs": dialogs}, ensure_ascii=False),
            encoding="utf-8",
        )
        html_out = DialogRenderer()._render_from_structured_data(str(tmp_path))
        assert 'onmouseover="alert(1)' not in html_out
        assert 'id="dlg-0"' in html_out


# ---------------------------------------------------------------------------
# views._prepare_success_view chapters wiring
# ---------------------------------------------------------------------------


class TestPrepareSuccessViewChapters:
    def _write_status(self, cache_dir: Path, chapters_status: str):
        (cache_dir / "llm_status.json").write_text(
            json.dumps(
                {
                    "calibration_status": "full",
                    "summary_status": "generated",
                    "chapters_status": chapters_status,
                }
            ),
            encoding="utf-8",
        )

    def test_generated_with_matching_fingerprint_renders_links(self, tmp_path: Path):
        from video_transcript_api.api.routes.views import _prepare_success_view

        dialogs = _sample_dialogs()
        fp = _fingerprint_for(dialogs)
        assert fp is not None

        (tmp_path / "transcript_capswriter.txt").write_text("orig", encoding="utf-8")
        (tmp_path / "llm_calibrated.txt").write_text("cal", encoding="utf-8")
        (tmp_path / "llm_processed.json").write_text(
            json.dumps({"dialogs": dialogs}, ensure_ascii=False), encoding="utf-8"
        )
        (tmp_path / "llm_chapters.json").write_text(
            json.dumps(_chapters_payload(fp), ensure_ascii=False), encoding="utf-8"
        )
        self._write_status(tmp_path, ChaptersStatus.GENERATED)

        view_data = {"cache_dir": str(tmp_path), "summary": None}
        _prepare_success_view(view_data)

        chapters_html = view_data.get("chapters_html") or ""
        assert chapters_html
        assert 'href="#dlg-0"' in chapters_html
        assert "Intro" in chapters_html

    def test_generated_with_mismatched_fingerprint_drops_links(self, tmp_path: Path):
        from video_transcript_api.api.routes.views import _prepare_success_view

        dialogs = _sample_dialogs()
        (tmp_path / "transcript_capswriter.txt").write_text("orig", encoding="utf-8")
        (tmp_path / "llm_calibrated.txt").write_text("cal", encoding="utf-8")
        (tmp_path / "llm_processed.json").write_text(
            json.dumps({"dialogs": dialogs}, ensure_ascii=False), encoding="utf-8"
        )
        (tmp_path / "llm_chapters.json").write_text(
            json.dumps(_chapters_payload("stale-fingerprint"), ensure_ascii=False),
            encoding="utf-8",
        )
        self._write_status(tmp_path, ChaptersStatus.GENERATED)

        view_data = {"cache_dir": str(tmp_path), "summary": None}
        _prepare_success_view(view_data)

        chapters_html = view_data.get("chapters_html") or ""
        assert chapters_html
        assert 'href="#dlg-' not in chapters_html
        assert "Intro" in chapters_html

    def test_non_generated_status_does_not_render_chapters(self, tmp_path: Path):
        from video_transcript_api.api.routes.views import _prepare_success_view

        dialogs = _sample_dialogs()
        fp = _fingerprint_for(dialogs)
        (tmp_path / "transcript_capswriter.txt").write_text("orig", encoding="utf-8")
        (tmp_path / "llm_calibrated.txt").write_text("cal", encoding="utf-8")
        (tmp_path / "llm_chapters.json").write_text(
            json.dumps(_chapters_payload(fp), ensure_ascii=False), encoding="utf-8"
        )
        self._write_status(tmp_path, ChaptersStatus.FAILED)

        view_data = {"cache_dir": str(tmp_path), "summary": None}
        _prepare_success_view(view_data)

        assert not view_data.get("chapters_html")

    def test_xss_payload_escaped_via_view_pipeline(self, tmp_path: Path):
        from video_transcript_api.api.routes.views import _prepare_success_view

        dialogs = _sample_dialogs()
        fp = _fingerprint_for(dialogs)
        payload = _chapters_payload(
            fp,
            title='</a><script>alert(1)</script>',
            gist='<img src=x onerror=alert(1)>',
        )
        (tmp_path / "transcript_capswriter.txt").write_text("orig", encoding="utf-8")
        (tmp_path / "llm_calibrated.txt").write_text("cal", encoding="utf-8")
        (tmp_path / "llm_processed.json").write_text(
            json.dumps({"dialogs": dialogs}, ensure_ascii=False), encoding="utf-8"
        )
        (tmp_path / "llm_chapters.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        self._write_status(tmp_path, ChaptersStatus.GENERATED)

        view_data = {"cache_dir": str(tmp_path), "summary": None}
        _prepare_success_view(view_data)
        html_out = view_data.get("chapters_html") or ""

        assert "<script>" not in html_out
        assert "<img src=x" not in html_out


# ---------------------------------------------------------------------------
# floating-toc.js: DOM API / no HTML string concat of user titles
# ---------------------------------------------------------------------------


class TestFloatingTocXssHardening:
    @pytest.fixture
    def toc_js(self) -> str:
        root = Path(__file__).resolve().parents[2]
        path = root / "src" / "web" / "static" / "js" / "floating-toc.js"
        return path.read_text(encoding="utf-8")

    def test_uses_text_content_for_titles(self, toc_js: str):
        assert "textContent" in toc_js
        assert "createElement" in toc_js

    def test_no_insert_adjacent_html_with_heading_text(self, toc_js: str):
        """User/chapter titles must not be interpolated into HTML strings.

        Allow insertAdjacentHTML only for static shells if any; forbid patterns
        that splice heading.text / chapter title into markup.
        """
        # Forbidden: template literals embedding heading/chapter text into HTML
        bad_patterns = [
            r"\$\{heading\.text\}",
            r"\$\{chapter\.text\}",
            r"\$\{.*\.text\}[^`]*</a>",
            r"innerHTML\s*=\s*`[^`]*\$\{",
        ]
        for pattern in bad_patterns:
            assert not re.search(pattern, toc_js), f"Unsafe pattern still present: {pattern}"

    def test_extracts_chapters_group(self, toc_js: str):
        assert "extractChapters" in toc_js or "chapters-section" in toc_js
        assert "dlg-" in toc_js
