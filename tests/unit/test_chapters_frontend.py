"""Unit tests for T11: chapters data island + inline chapter anchors.

T11 replaces the chapter card wall with:
- a JSON data island (``chapters_data`` view var, rendered into
  ``<script type="application/json" id="chapters-data">``), fields per
  chapter: ``{index,title,gist,start_time,start_seg,jump_ok}``, with every
  ``<`` escaped as ``\\u003c`` so the payload can neither close the script
  tag nor re-open it via script-data double-escape (``<!--<script>``);
- inline ``.chapter-anchor`` headers inside the structured transcript,
  inserted before the dialog item whose ``dlg_index == chapter.start_seg``
  (only for chapters with ``jump_ok``).

The fingerprint + dlg-anchor gating semantics from T7/T8 are unchanged:
``jump_ok`` is True only when the stored fingerprint matches the current
anchor source AND the page renders structured dialog anchors.

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
    render_calibrated_content_smart,
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


def _view_chapters(
    *,
    jump_ok: bool = True,
    title: str = "Intro",
    gist0: str = "About intro",
    gist1: str = "Second part",
):
    """Chapter dicts in the shape views.py hands to the renderer."""
    return [
        {
            "index": 0,
            "title": title,
            "gist": gist0,
            "start_time": 0.0,
            "start_seg": 0,
            "jump_ok": jump_ok,
        },
        {
            "index": 1,
            "title": "Middle",
            "gist": gist1,
            "start_time": 10.0,
            "start_seg": 2,
            "jump_ok": jump_ok,
        },
    ]


def _write_structured_cache(cache_dir: Path, dialogs):
    (cache_dir / "llm_processed.json").write_text(
        json.dumps({"dialogs": dialogs}, ensure_ascii=False), encoding="utf-8"
    )


def _write_status(cache_dir: Path, chapters_status: str):
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


def _build_view_task(cache_dir: Path, payload: dict, status: str = ChaptersStatus.GENERATED):
    dialogs = _sample_dialogs()
    (cache_dir / "transcript_capswriter.txt").write_text("orig", encoding="utf-8")
    (cache_dir / "llm_calibrated.txt").write_text("cal", encoding="utf-8")
    _write_structured_cache(cache_dir, dialogs)
    (cache_dir / "llm_chapters.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    _write_status(cache_dir, status)
    return dialogs


# ---------------------------------------------------------------------------
# Chapters data island (views._prepare_success_view -> chapters_data)
# ---------------------------------------------------------------------------


class TestChaptersDataIsland:
    def test_generated_with_matching_fingerprint_emits_data_island(
        self, tmp_path: Path
    ):
        from video_transcript_api.api.routes.views import _prepare_success_view

        dialogs = _sample_dialogs()
        fp = _fingerprint_for(dialogs)
        _build_view_task(tmp_path, _chapters_payload(fp))

        view_data = {"cache_dir": str(tmp_path), "summary": None}
        _prepare_success_view(view_data)

        chapters_data = view_data.get("chapters_data")
        assert isinstance(chapters_data, str) and chapters_data

        chapters = json.loads(chapters_data)
        assert len(chapters) == 2
        for ch in chapters:
            assert set(ch.keys()) == {
                "index",
                "title",
                "gist",
                "start_time",
                "start_seg",
                "jump_ok",
            }
        assert chapters[0]["index"] == 0
        assert chapters[0]["title"] == "Intro"
        assert chapters[0]["gist"] == "About intro"
        assert chapters[0]["start_time"] == 0.0
        assert chapters[0]["start_seg"] == 0
        assert chapters[0]["jump_ok"] is True
        assert chapters[1]["start_seg"] == 2
        assert chapters[1]["jump_ok"] is True

    def test_data_island_escapes_script_close_tag(self, tmp_path: Path):
        """Every ``<`` must be escaped as ``\\u003c`` so a title/gist
        containing ``</script>`` cannot break out of the JSON script
        island, while ``json.loads`` still round-trips the original text."""
        from video_transcript_api.api.routes.views import _prepare_success_view

        dialogs = _sample_dialogs()
        fp = _fingerprint_for(dialogs)
        payload = _chapters_payload(
            fp,
            title='x</script><script>alert(1)</script>',
            gist='g</script><img src=x onerror=alert(1)>',
        )
        _build_view_task(tmp_path, payload)

        view_data = {"cache_dir": str(tmp_path), "summary": None}
        _prepare_success_view(view_data)

        chapters_data = view_data.get("chapters_data")
        assert isinstance(chapters_data, str)
        assert "<" not in chapters_data
        assert "\\u003c" in chapters_data

        chapters = json.loads(chapters_data)
        assert chapters[0]["title"] == 'x</script><script>alert(1)</script>'
        assert chapters[0]["gist"] == 'g</script><img src=x onerror=alert(1)>'

    def test_data_island_escapes_script_data_double_escape(self, tmp_path: Path):
        """``<!--<script>`` is a script-data double-escape sequence: inside a
        script island it would re-open a real script context and swallow the
        page DOM up to the next ``</script>``. Escaping only ``</`` cannot
        stop this; escaping every ``<`` as ``\\u003c`` does, and
        ``json.loads`` must round-trip the original text."""
        from video_transcript_api.api.routes.views import _prepare_success_view

        dialogs = _sample_dialogs()
        fp = _fingerprint_for(dialogs)
        payload = _chapters_payload(
            fp,
            title='<!--<script>alert(1)</script>-->',
            gist='g<!--<script>',
        )
        _build_view_task(tmp_path, payload)

        view_data = {"cache_dir": str(tmp_path), "summary": None}
        _prepare_success_view(view_data)

        chapters_data = view_data.get("chapters_data")
        assert isinstance(chapters_data, str)
        assert "<" not in chapters_data
        assert "\\u003c" in chapters_data

        chapters = json.loads(chapters_data)
        assert chapters[0]["title"] == '<!--<script>alert(1)</script>-->'
        assert chapters[0]["gist"] == 'g<!--<script>'

    def test_data_island_start_time_non_finite_becomes_null(self, tmp_path: Path):
        """json.dumps emits ``Infinity``/``NaN`` literals for non-finite
        floats, which are invalid JSON and would make JSON.parse fail,
        silently dropping all chapters. inf/-inf/NaN (and non-numeric or
        missing) start_time values must serialize as null; finite numbers
        pass through unchanged."""
        from video_transcript_api.api.routes.views import _prepare_success_view

        dialogs = _sample_dialogs()
        fp = _fingerprint_for(dialogs)
        payload = _chapters_payload(fp)
        payload["chapters"] = [
            {"index": 0, "title": "pos_inf", "gist": "", "start_seg": 0,
             "start_time": float("inf")},
            {"index": 1, "title": "neg_inf", "gist": "", "start_seg": 0,
             "start_time": float("-inf")},
            {"index": 2, "title": "nan", "gist": "", "start_seg": 0,
             "start_time": float("nan")},
            {"index": 3, "title": "finite", "gist": "", "start_seg": 0,
             "start_time": 12.5},
            {"index": 4, "title": "missing", "gist": "", "start_seg": 0,
             "start_time": None},
        ]
        _build_view_task(tmp_path, payload)

        view_data = {"cache_dir": str(tmp_path), "summary": None}
        _prepare_success_view(view_data)

        chapters_data = view_data["chapters_data"]
        assert isinstance(chapters_data, str)
        assert "Infinity" not in chapters_data
        assert "NaN" not in chapters_data

        chapters = json.loads(chapters_data)
        by_title = {ch["title"]: ch for ch in chapters}
        assert by_title["pos_inf"]["start_time"] is None
        assert by_title["neg_inf"]["start_time"] is None
        assert by_title["nan"]["start_time"] is None
        assert by_title["finite"]["start_time"] == 12.5
        assert by_title["missing"]["start_time"] is None

    def test_mismatched_fingerprint_marks_jump_not_ok(self, tmp_path: Path):
        from video_transcript_api.api.routes.views import _prepare_success_view

        _build_view_task(tmp_path, _chapters_payload("stale-fingerprint"))

        view_data = {"cache_dir": str(tmp_path), "summary": None}
        _prepare_success_view(view_data)

        chapters = json.loads(view_data["chapters_data"])
        assert len(chapters) == 2
        assert all(ch["jump_ok"] is False for ch in chapters)
        # Titles still visible in the data island.
        assert chapters[0]["title"] == "Intro"
        # No inline anchors without jump ability.
        calibrated_html = view_data.get("calibrated_html") or ""
        assert "chapter-anchor" not in calibrated_html

    def test_non_generated_status_has_no_data_island(self, tmp_path: Path):
        from video_transcript_api.api.routes.views import _prepare_success_view

        dialogs = _sample_dialogs()
        fp = _fingerprint_for(dialogs)
        _build_view_task(tmp_path, _chapters_payload(fp), status=ChaptersStatus.FAILED)

        view_data = {"cache_dir": str(tmp_path), "summary": None}
        _prepare_success_view(view_data)

        assert view_data.get("chapters_data") is None
        calibrated_html = view_data.get("calibrated_html") or ""
        assert "chapter-anchor" not in calibrated_html

    def test_timeline_only_without_dlg_anchors_marks_jump_not_ok(
        self, tmp_path: Path
    ):
        """YouTube/CapsWriter timeline can fingerprint-match segments, but the
        page only emits id=\"dlg-{i}\" for structured dialogs. Without dialogs
        jump targets would be dead anchors -> jump_ok must be False and no
        inline chapter anchors are inserted."""
        from video_transcript_api.api.routes.views import _prepare_success_view

        segs = [
            {"start_time": 0.0, "end_time": 1.0, "text": "hello one"},
            {"start_time": 1.0, "end_time": 2.0, "text": "hello two"},
        ]
        fp = _compute_fingerprint(list(enumerate(segs)))
        (tmp_path / "transcript_capswriter.txt").write_text(
            "hello one hello two", encoding="utf-8"
        )
        (tmp_path / "transcript_capswriter.json").write_text(
            json.dumps({"segments": segs}, ensure_ascii=False), encoding="utf-8"
        )
        (tmp_path / "llm_calibrated.txt").write_text(
            "hello one hello two", encoding="utf-8"
        )
        # No llm_processed.json -> no #dlg-* anchors on the page.
        (tmp_path / "llm_chapters.json").write_text(
            json.dumps(_chapters_payload(fp), ensure_ascii=False), encoding="utf-8"
        )
        _write_status(tmp_path, ChaptersStatus.GENERATED)

        view_data = {"cache_dir": str(tmp_path), "summary": None}
        _prepare_success_view(view_data)

        chapters = json.loads(view_data["chapters_data"])
        assert len(chapters) == 2
        assert all(ch["jump_ok"] is False for ch in chapters)
        calibrated_html = view_data.get("calibrated_html") or ""
        assert "chapter-anchor" not in calibrated_html

    def test_xss_payload_contained_via_view_pipeline(self, tmp_path: Path):
        from video_transcript_api.api.routes.views import _prepare_success_view

        dialogs = _sample_dialogs()
        fp = _fingerprint_for(dialogs)
        payload = _chapters_payload(
            fp,
            title='</a><script>alert(1)</script>',
            gist='<img src=x onerror=alert(1)>',
        )
        _build_view_task(tmp_path, payload)

        view_data = {"cache_dir": str(tmp_path), "summary": None}
        _prepare_success_view(view_data)

        chapters_data = view_data["chapters_data"]
        assert "<" not in chapters_data
        # Inline anchor titles must be HTML-escaped too.
        calibrated_html = view_data.get("calibrated_html") or ""
        assert "<script>alert(1)</script>" not in calibrated_html
        assert "&lt;script&gt;" in calibrated_html


# ---------------------------------------------------------------------------
# Inline chapter anchors in the structured transcript rendering
# ---------------------------------------------------------------------------


class TestInlineChapterAnchors:
    def test_anchor_inserted_before_matching_dialog(self, tmp_path: Path):
        _write_structured_cache(tmp_path, _sample_dialogs())

        html_out = DialogRenderer()._render_from_structured_data(
            str(tmp_path), chapters=_view_chapters()
        )

        assert 'id="chapter-anchor-0"' in html_out
        assert 'id="chapter-anchor-1"' in html_out
        # Each anchor sits immediately before the dialog whose dlg_index
        # equals the chapter start_seg.
        anchor0 = html_out.find('id="chapter-anchor-0"')
        dlg0 = html_out.find('id="dlg-0"')
        anchor1 = html_out.find('id="chapter-anchor-1"')
        dlg1 = html_out.find('id="dlg-1"')
        dlg2 = html_out.find('id="dlg-2"')
        assert -1 < anchor0 < dlg0
        assert -1 < dlg1 < anchor1 < dlg2

    def test_anchor_dom_contract(self, tmp_path: Path):
        _write_structured_cache(tmp_path, _sample_dialogs())

        html_out = DialogRenderer()._render_from_structured_data(
            str(tmp_path), chapters=_view_chapters()
        )

        assert (
            '<div class="chapter-anchor" id="chapter-anchor-1" '
            'data-chapter-index="1">' in html_out
        )
        # mm:ss time label + bold-title span per the DOM contract.
        assert '<span class="chapter-anchor-time">00:10</span>' in html_out
        assert '<span class="chapter-anchor-title">Middle</span>' in html_out
        # Full gist paragraph follows the title.
        assert '<p class="chapter-anchor-gist">Second part</p>' in html_out

    def test_anchor_gist_is_html_escaped(self, tmp_path: Path):
        _write_structured_cache(tmp_path, _sample_dialogs())
        chapters = _view_chapters(gist0='<img src=x onerror=alert(1)>')

        html_out = DialogRenderer()._render_from_structured_data(
            str(tmp_path), chapters=chapters
        )

        assert "<img src=x onerror=alert(1)>" not in html_out
        assert (
            '<p class="chapter-anchor-gist">'
            "&lt;img src=x onerror=alert(1)&gt;</p>"
        ) in html_out

    def test_anchor_empty_gist_renders_no_paragraph(self, tmp_path: Path):
        _write_structured_cache(tmp_path, _sample_dialogs())

        html_out = DialogRenderer()._render_from_structured_data(
            str(tmp_path), chapters=_view_chapters(gist0="", gist1="")
        )

        assert 'id="chapter-anchor-0"' in html_out
        assert "chapter-anchor-gist" not in html_out

    def test_anchor_title_is_html_escaped(self, tmp_path: Path):
        _write_structured_cache(tmp_path, _sample_dialogs())
        chapters = _view_chapters(title='<script>alert(1)</script>')

        html_out = DialogRenderer()._render_from_structured_data(
            str(tmp_path), chapters=chapters
        )

        assert "<script>alert(1)</script>" not in html_out
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_out

    def test_jump_not_ok_chapter_not_inserted(self, tmp_path: Path):
        _write_structured_cache(tmp_path, _sample_dialogs())

        html_out = DialogRenderer()._render_from_structured_data(
            str(tmp_path), chapters=_view_chapters(jump_ok=False)
        )

        assert "chapter-anchor" not in html_out
        assert 'id="dlg-0"' in html_out

    def test_no_chapters_no_anchors(self, tmp_path: Path):
        _write_structured_cache(tmp_path, _sample_dialogs())

        html_out = DialogRenderer()._render_from_structured_data(str(tmp_path))

        assert "chapter-anchor" not in html_out

    def test_plain_text_path_never_inserts_anchors(self, tmp_path: Path):
        """CapsWriter long-text rendering has no dlg anchors; chapters must
        not leak into it even when provided."""
        (tmp_path / "transcript_capswriter.txt").write_text(
            "hello one hello two", encoding="utf-8"
        )
        (tmp_path / "llm_calibrated.txt").write_text(
            "hello one hello two", encoding="utf-8"
        )

        html_out = render_calibrated_content_smart(
            str(tmp_path), chapters=_view_chapters()
        )

        assert html_out is not None
        assert "chapter-anchor" not in html_out


# ---------------------------------------------------------------------------
# View pipeline: matching fingerprint -> data island + inline anchors
# ---------------------------------------------------------------------------


class TestPrepareSuccessViewChapters:
    def test_matching_fingerprint_inserts_inline_anchors(self, tmp_path: Path):
        from video_transcript_api.api.routes.views import _prepare_success_view

        dialogs = _sample_dialogs()
        fp = _fingerprint_for(dialogs)
        _build_view_task(tmp_path, _chapters_payload(fp))

        view_data = {"cache_dir": str(tmp_path), "summary": None}
        _prepare_success_view(view_data)

        calibrated_html = view_data.get("calibrated_html") or ""
        assert 'id="chapter-anchor-0"' in calibrated_html
        assert 'id="chapter-anchor-1"' in calibrated_html
        assert '<span class="chapter-anchor-title">Intro</span>' in calibrated_html
        # Full gist rides along inside each inline anchor (views.py passes the
        # same chapter dicts to the data island and the renderer).
        assert '<p class="chapter-anchor-gist">About intro</p>' in calibrated_html
        # Anchors must sit inside the transcript, before their dlg targets.
        assert calibrated_html.find('id="chapter-anchor-0"') < calibrated_html.find(
            'id="dlg-0"'
        )
        assert calibrated_html.find('id="chapter-anchor-1"') < calibrated_html.find(
            'id="dlg-2"'
        )


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

    def test_no_innerhtml_anywhere(self, toc_js: str):
        """Chapter titles/gists come from LLM output; the viewer must build
        every node via DOM API, so innerHTML must not appear at all."""
        assert "innerHTML" not in toc_js

    def test_reads_chapters_from_data_island(self, toc_js: str):
        """Phase 2 (T11): chapters come from the #chapters-data JSON island,
        not from scanning server-rendered chapter cards."""
        assert "chapters-data" in toc_js
        assert "JSON.parse" in toc_js
        assert "dlg-" in toc_js
        # The old DOM-card scanning extractor is gone.
        assert "extractChapters" not in toc_js
        assert "chapters-section" not in toc_js


# ---------------------------------------------------------------------------
# Chapter panel/drawer items: time + title row plus full gist text below
# ---------------------------------------------------------------------------


class TestChapterPanelGistItems:
    """Panel/drawer chapter rows show time + one-line title with the full
    gist text underneath. Gists are short (one or two sentences), so the
    gist is plain text: no button, no expand/collapse, and no
    clamping/max-height truncation anywhere. The whole row -- title line
    and gist alike -- is one jump target; rows with jump_ok=false stay
    inert (no dataset.targetId, so handleChapterJump no-ops)."""

    @pytest.fixture
    def toc_js(self) -> str:
        root = Path(__file__).resolve().parents[2]
        return (root / "src" / "web" / "static" / "js" / "floating-toc.js").read_text(
            encoding="utf-8"
        )

    @pytest.fixture
    def toc_css(self) -> str:
        root = Path(__file__).resolve().parents[2]
        return (
            root / "src" / "web" / "static" / "css" / "floating-toc.css"
        ).read_text(encoding="utf-8")

    def test_js_renders_full_gist_as_plain_element(self, toc_js: str):
        """The gist is injected via DOM API textContent on a plain div --
        not a button, and with no expand/collapse machinery left over."""
        assert "createEl('div', 'toc-chapter-gist'" in toc_js
        assert "gist-expanded" not in toc_js

    def test_whole_item_click_delegates_to_jump(self, toc_js: str):
        """Clicking the gist must reach the same jump handler as clicking
        the title row: the delegated click handler matches both
        .toc-chapter-main and .toc-chapter-gist, then routes through the
        row's shared .toc-chapter-main button (which carries the jump
        dataset; absent on jump_ok=false rows, so those stay inert)."""
        assert "'.toc-chapter-main, .toc-chapter-gist'" in toc_js
        assert "querySelector('.toc-chapter-main')" in toc_js

    def test_css_gist_full_text_no_truncation(self, toc_css: str):
        """The .toc-chapter-gist rule must not clamp: no -webkit-line-clamp,
        no max-height, no overflow hiding, and no gist-expanded styles."""
        m = re.search(r"\.toc-chapter-gist\s*\{([^}]*)\}", toc_css)
        assert m, ".toc-chapter-gist rule missing from floating-toc.css"
        rule = m.group(1)
        assert "line-clamp" not in rule
        assert "max-height" not in rule
        assert "overflow" not in rule
        assert "gist-expanded" not in toc_css

    def test_gist_shows_clickable_cursor(self, toc_css: str):
        """The gist area is part of the jump target -> pointer cursor,
        while disabled (jump_ok=false) rows keep the default cursor."""
        m = re.search(r"\.toc-chapter-gist\s*\{([^}]*)\}", toc_css)
        assert m, ".toc-chapter-gist rule missing from floating-toc.css"
        assert "cursor: pointer" in m.group(1)
        disabled = re.search(
            r"\.toc-chapter-item\.toc-chapter-disabled \.toc-chapter-gist\s*\{([^}]*)\}",
            toc_css,
        )
        assert disabled, "disabled gist cursor rule missing"
        assert "cursor: default" in disabled.group(1)

    def test_title_clamped_to_one_line(self, toc_css: str):
        """Titles still collapse to a single line with an ellipsis."""
        assert "text-overflow: ellipsis" in toc_css
