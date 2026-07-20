"""Unit tests for T8 rendering layer: no-speaker KeyError defense + plain_structured gate.

Covers:
- ``_render_from_structured_data`` must not KeyError on dialog dicts without
  the ``speaker`` key (T8 plain_structured artifacts), must still emit
  ``id="dlg-{i}"`` anchors, and must not leave speaker-tag residue.
- Strategy gating (plan b): an ``llm_processed.json`` with top-level
  ``"mode": "plain_structured"`` is ignored by the rendering strategy unless
  ``plain_structured_enabled`` is passed in; FunASR artifacts (no ``mode``
  key) are unaffected.
- ``views._page_has_dialog_anchors`` applies the same gate so chapter entries
  are never marked jumpable (``jump_ok``) when the body renders as plain text.
- Paragraphs whose ``start_time``/``end_time`` are None must still render
  with ``id="dlg-{i}"`` anchors and must NOT be stamped with a fallback
  ``00:00:00`` time tag (T8 review R1 F7).

Console output must be pure English (no emoji, no Chinese).
"""

from __future__ import annotations

import json
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


def _plain_dialogs():
    """Paragraph dicts WITHOUT the speaker key (T8 plain_structured artifact)."""
    return [
        {"text": "First paragraph of plain text.", "start_time": "0:00", "end_time": "0:08"},
        {"text": "Second paragraph of plain text.", "start_time": "0:08", "end_time": "0:20"},
    ]


def _funasr_dialogs():
    return [
        {"speaker": "Alice", "text": "Hello world", "start_time": "0:00", "end_time": "0:05"},
        {"speaker": "Bob", "text": "Hi there", "start_time": "0:05", "end_time": "0:10"},
    ]


def _write_processed(cache_dir: Path, dialogs, *, mode: str | None = None):
    payload = {"format_version": "v3", "dialogs": dialogs}
    if mode is not None:
        payload["mode"] = mode
    (cache_dir / "llm_processed.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _write_plain_sidecars(cache_dir: Path):
    (cache_dir / "transcript_capswriter.txt").write_text(
        "First paragraph of plain text. Second paragraph of plain text.",
        encoding="utf-8",
    )
    (cache_dir / "llm_calibrated.txt").write_text(
        "First paragraph of plain text. Second paragraph of plain text.",
        encoding="utf-8",
    )


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


def _chapters_payload(fingerprint):
    return {
        "format_version": "v1",
        "source": {
            "kind": "dialogs",
            "segment_count": 2,
            "fingerprint": fingerprint,
            "generated_at": "2026-07-19T00:00:00+00:00",
        },
        "chapters": [
            {
                "index": 0,
                "title": "Intro",
                "gist": "About intro",
                "start_seg": 0,
                "end_seg": 0,
                "start_time": 0.0,
                "end_time": 8.0,
            },
            {
                "index": 1,
                "title": "Middle",
                "gist": "Second part",
                "start_seg": 1,
                "end_seg": 1,
                "start_time": 8.0,
                "end_time": 20.0,
            },
        ],
    }


# ---------------------------------------------------------------------------
# KeyError defense: dialogs without the speaker key
# ---------------------------------------------------------------------------


class TestRenderDialogsWithoutSpeakerKey:
    def test_no_speaker_key_renders_anchors_without_speaker_tags(self, tmp_path: Path):
        _write_processed(tmp_path, _plain_dialogs(), mode="plain_structured")

        html_out = DialogRenderer()._render_from_structured_data(str(tmp_path))

        assert 'id="dlg-0"' in html_out
        assert 'id="dlg-1"' in html_out
        assert "unknown" not in html_out
        assert "speaker-tag" not in html_out
        # Timeline blocks keep their time tags.
        assert "time-tag" in html_out
        assert "First paragraph of plain text." in html_out

    def test_mixed_speaker_and_missing_key_does_not_raise(self, tmp_path: Path):
        dialogs = [
            {"speaker": "Alice", "text": "Has speaker", "start_time": "0:00"},
            {"text": "No speaker key", "start_time": "0:05"},
        ]
        _write_processed(tmp_path, dialogs)

        html_out = DialogRenderer()._render_from_structured_data(str(tmp_path))

        assert 'id="dlg-0"' in html_out
        assert 'id="dlg-1"' in html_out
        assert "Alice" in html_out
        assert "unknown" not in html_out
        assert "No speaker key" in html_out

    def test_no_speaker_and_none_time_renders_anchor_without_fabrication(
        self, tmp_path: Path
    ):
        """Dialog without speaker key AND with None times must still render its
        dlg-* anchor, without fabricating a speaker tag or a 00:00:00 time."""
        dialogs = [
            {"speaker": "Alice", "text": "Has speaker", "start_time": "0:00"},
            {"text": "No speaker, no time", "start_time": None, "end_time": None},
        ]
        _write_processed(tmp_path, dialogs)

        html_out = DialogRenderer()._render_from_structured_data(str(tmp_path))

        assert 'id="dlg-1"' in html_out
        assert "No speaker, no time" in html_out
        # No fabricated speaker label / time for the speakerless, timeless dialog.
        assert "unknown" not in html_out
        assert "00:00:00" not in html_out
        assert "None" not in html_out
        # Exactly one speaker-tag (Alice's) and one time-tag (hers) overall.
        assert html_out.count("speaker-tag") == 1
        assert html_out.count("time-tag") == 1


# ---------------------------------------------------------------------------
# Strategy gating (plan b): plain_structured artifact ignored when switch off
# ---------------------------------------------------------------------------


class TestStructuredStrategyGating:
    def test_plain_structured_ignored_when_switch_off(self, tmp_path: Path):
        _write_processed(tmp_path, _plain_dialogs(), mode="plain_structured")
        _write_plain_sidecars(tmp_path)

        renderer = DialogRenderer()
        # Default (conservative) is off.
        assert renderer._get_optimal_rendering_strategy(str(tmp_path)) == "capswriter_long_text"
        assert (
            renderer._get_optimal_rendering_strategy(
                str(tmp_path), plain_structured_enabled=False
            )
            == "capswriter_long_text"
        )

    def test_plain_structured_used_when_switch_on(self, tmp_path: Path):
        _write_processed(tmp_path, _plain_dialogs(), mode="plain_structured")
        _write_plain_sidecars(tmp_path)

        renderer = DialogRenderer()
        assert (
            renderer._get_optimal_rendering_strategy(
                str(tmp_path), plain_structured_enabled=True
            )
            == "structured"
        )

    def test_funasr_artifact_unaffected_when_switch_off(self, tmp_path: Path):
        # FunASR artifacts carry no top-level "mode" key.
        _write_processed(tmp_path, _funasr_dialogs())
        _write_plain_sidecars(tmp_path)

        renderer = DialogRenderer()
        assert renderer._get_optimal_rendering_strategy(str(tmp_path)) == "structured"
        assert (
            renderer._get_optimal_rendering_strategy(
                str(tmp_path), plain_structured_enabled=False
            )
            == "structured"
        )

    def test_gated_render_falls_back_to_plain_html(self, tmp_path: Path):
        """Switch off: body must not contain dlg anchors (plain rendering);
        switch on: structured rendering emits them."""
        _write_processed(tmp_path, _plain_dialogs(), mode="plain_structured")
        _write_plain_sidecars(tmp_path)

        html_off = render_calibrated_content_smart(str(tmp_path))
        assert html_off is not None
        assert 'id="dlg-' not in html_off

        html_on = render_calibrated_content_smart(
            str(tmp_path), plain_structured_enabled=True
        )
        assert html_on is not None
        assert 'id="dlg-0"' in html_on
        assert "unknown" not in html_on


# ---------------------------------------------------------------------------
# views._page_has_dialog_anchors: same gate as the rendering strategy
# ---------------------------------------------------------------------------


class TestPageHasDialogAnchorsGating:
    def test_plain_structured_off_returns_false(self, tmp_path: Path):
        from video_transcript_api.api.routes.views import _page_has_dialog_anchors

        _write_processed(tmp_path, _plain_dialogs(), mode="plain_structured")

        assert _page_has_dialog_anchors(tmp_path) is False
        assert (
            _page_has_dialog_anchors(tmp_path, plain_structured_enabled=False) is False
        )

    def test_plain_structured_on_returns_true(self, tmp_path: Path):
        from video_transcript_api.api.routes.views import _page_has_dialog_anchors

        _write_processed(tmp_path, _plain_dialogs(), mode="plain_structured")

        assert (
            _page_has_dialog_anchors(tmp_path, plain_structured_enabled=True) is True
        )

    def test_funasr_artifact_unaffected_when_switch_off(self, tmp_path: Path):
        from video_transcript_api.api.routes.views import _page_has_dialog_anchors

        _write_processed(tmp_path, _funasr_dialogs())

        assert _page_has_dialog_anchors(tmp_path) is True
        assert (
            _page_has_dialog_anchors(tmp_path, plain_structured_enabled=False) is True
        )


# ---------------------------------------------------------------------------
# _prepare_success_view integration: switch read from config, gate consistent
# ---------------------------------------------------------------------------


class TestPrepareSuccessViewGateIntegration:
    def _build_plain_task(self, tmp_path: Path):
        dialogs = _plain_dialogs()
        fp = _fingerprint_for(dialogs)
        assert fp is not None

        _write_plain_sidecars(tmp_path)
        _write_processed(tmp_path, dialogs, mode="plain_structured")
        (tmp_path / "llm_chapters.json").write_text(
            json.dumps(_chapters_payload(fp), ensure_ascii=False), encoding="utf-8"
        )
        (tmp_path / "llm_status.json").write_text(
            json.dumps(
                {
                    "calibration_status": "full",
                    "summary_status": "generated",
                    "chapters_status": ChaptersStatus.GENERATED,
                }
            ),
            encoding="utf-8",
        )

    def _patch_switch(self, monkeypatch: pytest.MonkeyPatch, enabled: bool):
        monkeypatch.setattr(
            "video_transcript_api.api.routes.views.get_config",
            lambda: {"llm": {"structured_calibration_for_plain": enabled}},
        )

    def test_switch_off_no_dead_links_and_plain_body(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from video_transcript_api.api.routes.views import _prepare_success_view

        self._build_plain_task(tmp_path)
        self._patch_switch(monkeypatch, False)

        view_data = {"cache_dir": str(tmp_path), "summary": None}
        _prepare_success_view(view_data)

        chapters_data = view_data.get("chapters_data")
        assert chapters_data, "chapters data island should still be emitted"
        chapters = json.loads(chapters_data)
        assert all(
            ch["jump_ok"] is False for ch in chapters
        ), "no dead jump ability when gated"
        calibrated_html = view_data.get("calibrated_html") or ""
        assert 'id="dlg-' not in calibrated_html, "body falls back to plain rendering"
        assert "chapter-anchor" not in calibrated_html

    def test_switch_on_links_and_structured_body(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from video_transcript_api.api.routes.views import _prepare_success_view

        self._build_plain_task(tmp_path)
        self._patch_switch(monkeypatch, True)

        view_data = {"cache_dir": str(tmp_path), "summary": None}
        _prepare_success_view(view_data)

        chapters_data = view_data.get("chapters_data")
        assert chapters_data, "chapters data island should be emitted"
        chapters = json.loads(chapters_data)
        assert chapters[0]["jump_ok"] is True
        calibrated_html = view_data.get("calibrated_html") or ""
        assert 'id="dlg-0"' in calibrated_html
        assert 'id="chapter-anchor-0"' in calibrated_html


# ---------------------------------------------------------------------------
# None timestamps: paragraphs without timing still render jumpable anchors
# ---------------------------------------------------------------------------


class TestRenderNoneTimeParagraphs:
    """T8 review R1 F7: plain_structured paragraphs may carry
    ``start_time=None`` / ``end_time=None`` (timeless segments). They must
    render through the real DialogRenderer without raising, keep their
    ``id="dlg-{i}"`` chapter anchors, and must NOT be stamped with the old
    ``00:00:00`` fallback time tag."""

    def test_none_time_paragraphs_render_anchors_without_time_tags(
        self, tmp_path: Path
    ):
        dialogs = [
            {"text": "First timeless paragraph.", "start_time": None, "end_time": None},
            {"text": "Second timeless paragraph.", "start_time": None, "end_time": None},
        ]
        _write_processed(tmp_path, dialogs, mode="plain_structured")
        _write_plain_sidecars(tmp_path)

        renderer = DialogRenderer()
        # Sanity: the structured strategy is really selected (the anchors
        # below must come from _render_from_structured_data, not a fallback).
        assert (
            renderer._get_optimal_rendering_strategy(
                str(tmp_path), plain_structured_enabled=True
            )
            == "structured"
        )

        # Full entry path with the T8 switch on: strategy gate + structured
        # render. A raised exception inside would be swallowed into a
        # fallback body without anchors, so the anchor assertions below also
        # pin "does not raise".
        html_out = renderer.render_calibrated_content_smart(
            str(tmp_path), plain_structured_enabled=True
        )

        assert html_out is not None
        assert 'id="dlg-0"' in html_out
        assert 'id="dlg-1"' in html_out
        assert "00:00:00" not in html_out
        assert "time-tag" not in html_out
        assert "data-start-time" not in html_out
        assert "First timeless paragraph." in html_out
        assert "Second timeless paragraph." in html_out

    def test_mixed_none_and_timed_paragraphs(self, tmp_path: Path):
        """A timed paragraph keeps its real time tag while the timeless one
        next to it stays tag-less -- and no ``00:00:00`` appears anywhere."""
        dialogs = [
            {"text": "Timeless paragraph.", "start_time": None, "end_time": None},
            {
                "text": "Timed paragraph.",
                "start_time": "00:00:05",
                "end_time": "00:00:11",
            },
        ]
        _write_processed(tmp_path, dialogs, mode="plain_structured")
        _write_plain_sidecars(tmp_path)

        html_out = DialogRenderer().render_calibrated_content_smart(
            str(tmp_path), plain_structured_enabled=True
        )

        assert html_out is not None
        assert 'id="dlg-0"' in html_out
        assert 'id="dlg-1"' in html_out
        # The timed paragraph keeps its real timestamp...
        assert 'data-start-time="00:00:05"' in html_out
        assert "00:00:05" in html_out
        # ...while the None-time paragraph is NOT stamped with "00:00:00".
        assert "00:00:00" not in html_out
