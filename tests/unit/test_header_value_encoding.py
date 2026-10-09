"""Unit and contract tests for HTTP header value encoding and front matter title folding.

Validates that user-controlled free text (such as titles) flowing into
HTTP response headers is strictly legal per RFC and h11 protocol validation.
All console output must stay ASCII-only.
"""

import asyncio
from typing import Mapping
import h11
import pytest
from starlette.requests import Request

from video_transcript_api.api.routes import views
from video_transcript_api.api.routes.views import (
    _build_metadata_headers,
    _build_text_metadata_header,
    _safe_header_value,
)
from video_transcript_api.cache.cache_manager import CacheManager
from video_transcript_api.utils.task_status import TaskStatus

ADVERSARIAL_SAMPLES = [
    "line1\n\nline2",
    "a\r\nb",
    "\t开头Tab",
    "结尾空格 ",
    "100% 完成",
    "中文标题🚀",
    "x" * 5000,
    "\x01\x02",
    "---\n伪front matter",
]


def assert_h11_legal_headers(headers: Mapping[str, str]) -> None:
    """Validate that headers parse cleanly with h11 without LocalProtocolError."""
    h11_headers = [
        (k.encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()
    ]
    # h11.Response constructor performs strict ABNF verification on headers
    h11.Response(status_code=200, headers=h11_headers)


@pytest.mark.parametrize("sample", ADVERSARIAL_SAMPLES)
def test_safe_header_value_contract_on_adversarial_samples(sample: str) -> None:
    """Every adversarial sample must yield an h11-legal header <= 512 bytes."""
    encoded = _safe_header_value(sample)
    raw_bytes = encoded.encode("ascii")

    # 1. Byte length <= 512
    assert len(raw_bytes) <= 512, f"Length {len(raw_bytes)} exceeds 512 bytes"

    # 2. No line-discipline characters (\r, \n, \t)
    assert "\r" not in encoded, "Header value must not contain \\r"
    assert "\n" not in encoded, "Header value must not contain \\n"
    assert "\t" not in encoded, "Header value must not contain \\t"

    # 3. Leading and trailing characters must not be spaces or tabs
    if encoded:
        assert encoded[0] not in (" ", "\t"), "Header value must not start with whitespace"
        assert encoded[-1] not in (" ", "\t"), "Header value must not end with whitespace"

    # 4. Must pass strict h11 protocol verification
    assert_h11_legal_headers({"x-document-title": encoded})


def test_safe_header_value_clean_ascii_preservation() -> None:
    """Clean ASCII values must be preserved verbatim without percent-encoding."""
    clean_val = "Clean Title 123 - Safe_Chars.Only"
    encoded = _safe_header_value(clean_val)
    assert encoded == clean_val
    assert_h11_legal_headers({"x-document-title": encoded})


def test_safe_header_value_empty_and_spaces() -> None:
    """Empty strings and purely whitespace strings produce legal header values."""
    assert _safe_header_value("") == ""
    assert_h11_legal_value = _safe_header_value("   \t  ")
    assert_h11_legal_headers({"x-document-title": assert_h11_legal_value})


@pytest.mark.parametrize("sample", ADVERSARIAL_SAMPLES)
def test_front_matter_title_folds_linebreaks(sample: str) -> None:
    """Front matter Title line must fold \r\n\t into a single space and stay single line."""
    view_data = {
        "title": sample,
        "platform": "youtube",
        "url": "https://example.com/test",
    }
    fm = _build_text_metadata_header(view_data, "calibrated")
    lines = fm.split("\n")

    # Find the Title: line
    title_lines = [line for line in lines if line.startswith("Title: ")]
    assert len(title_lines) == 1, "Exactly one Title line must exist in front matter"

    title_line = title_lines[0]
    assert "\r" not in title_line
    assert "\n" not in title_line
    assert "\t" not in title_line
    assert lines[0] == "---"
    assert lines[-2] == "---"


def _setup_mock_task(tmp_path, title: str):
    from video_transcript_api.api.services.view_token_resolver import ViewTokenResolver
    from pathlib import Path

    manager = CacheManager(str(tmp_path / "cache"))
    task = manager.create_task(
        url="https://example.com/test",
        platform="twitter",
        media_id="test-media",
    )
    manager.save_cache(
        platform="twitter",
        url="https://example.com/test",
        media_id="test-media",
        use_speaker_recognition=False,
        transcript_data="dummy transcript",
        transcript_type="capswriter",
        title=title,
        author="Author",
    )
    manager.update_task_status(
        task["task_id"],
        TaskStatus.SUCCESS,
        platform="twitter",
        media_id="test-media",
    )
    # Write calibrated export file using resolver's cache_dir
    view_data = ViewTokenResolver(manager).get_view_data_by_token(task["view_token"])
    cache_dir = Path(view_data["cache_dir"])
    calibrated_file = cache_dir / "llm_calibrated.txt"
    calibrated_file.write_text("calibrated body content", encoding="utf-8")
    return manager, task


@pytest.mark.parametrize("sample", ADVERSARIAL_SAMPLES)
def test_three_entrypoints_contract(tmp_path, monkeypatch, sample: str) -> None:
    """All three entrypoints (?raw=, ?page=, /export/) must return 200 with h11-legal headers."""
    manager, task = _setup_mock_task(tmp_path, sample)
    try:
        monkeypatch.setattr(views, "cache_manager", manager)

        async def _inline_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        monkeypatch.setattr(views.asyncio, "to_thread", _inline_to_thread)

        # 1. Entrypoint ?raw=calibrated
        req_raw = Request(
            {
                "type": "http",
                "method": "GET",
                "path": f"/view/{task['view_token']}",
                "query_string": b"raw=calibrated",
                "headers": [],
                "client": ("127.0.0.1", 12345),
            }
        )
        resp_raw = asyncio.run(
            views.view_transcript(
                view_token=task["view_token"],
                request=req_raw,
                raw="calibrated",
            )
        )
        assert resp_raw.status_code == 200
        assert_h11_legal_headers(resp_raw.headers)
        title_hdr = resp_raw.headers.get("x-document-title", "")
        assert len(title_hdr.encode("ascii")) <= 512
        assert not any(c in title_hdr for c in "\r\n\t")
        # Front matter title check
        body_text = resp_raw.body.decode("utf-8")
        assert "Title: " in body_text
        fm_title_line = [
            line for line in body_text.splitlines() if line.startswith("Title: ")
        ][0]
        assert not any(c in fm_title_line for c in "\r\n\t")

        # 2. Entrypoint ?page=calibrated
        req_page = Request(
            {
                "type": "http",
                "method": "GET",
                "path": f"/view/{task['view_token']}",
                "query_string": b"page=calibrated",
                "headers": [],
                "client": ("127.0.0.1", 12345),
            }
        )
        resp_page = asyncio.run(
            views.view_transcript(
                view_token=task["view_token"],
                request=req_page,
                page="calibrated",
            )
        )
        assert resp_page.status_code == 200
        assert_h11_legal_headers(resp_page.headers)
        page_title_hdr = resp_page.headers.get("x-document-title", "")
        assert len(page_title_hdr.encode("ascii")) <= 512
        assert not any(c in page_title_hdr for c in "\r\n\t")

        # 3. Entrypoint /export/{view_token}/calibrated
        req_export = Request(
            {
                "type": "http",
                "method": "GET",
                "path": f"/export/{task['view_token']}/calibrated",
                "query_string": b"",
                "headers": [],
                "client": ("127.0.0.1", 12345),
            }
        )
        resp_export = asyncio.run(
            views.export_content(
                view_token=task["view_token"],
                export_type="calibrated",
                request=req_export,
            )
        )
        assert resp_export.status_code == 200
        assert_h11_legal_headers(resp_export.headers)
        export_title_hdr = resp_export.headers.get("x-document-title", "")
        assert len(export_title_hdr.encode("ascii")) <= 512
        assert not any(c in export_title_hdr for c in "\r\n\t")
    finally:
        manager.close()


def test_no_query_html_page_behavior_unchanged(tmp_path, monkeypatch) -> None:
    """Standard HTML page view without query parameters remains unaffected."""
    raw_title = "Here is an unescaped title with special chars: 100% & <hello>"
    manager, task = _setup_mock_task(tmp_path, raw_title)
    try:
        monkeypatch.setattr(views, "cache_manager", manager)

        async def _inline_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        monkeypatch.setattr(views.asyncio, "to_thread", _inline_to_thread)

        req = Request(
            {
                "type": "http",
                "method": "GET",
                "path": f"/view/{task['view_token']}",
                "query_string": b"",
                "headers": [],
                "client": ("127.0.0.1", 12345),
            }
        )
        resp = asyncio.run(
            views.view_transcript(
                view_token=task["view_token"],
                request=req,
            )
        )
        assert resp.status_code == 200
        # Normal HTML view uses Jinja2 template response and does not include x-document-title header
        assert "x-document-title" not in resp.headers
    finally:
        manager.close()


def test_view_token_logged_honestly_in_export_logs(tmp_path, monkeypatch) -> None:
    """Verify F3 fix: raw, page, and export loggers record the honest view_token instead of unknown."""
    from loguru import logger

    manager, task = _setup_mock_task(tmp_path, "Clean Title")
    token = task["view_token"]
    expected_prefix = token[:20]

    captured_logs = []
    sink_id = logger.add(lambda msg: captured_logs.append(str(msg)), level="INFO")

    try:
        monkeypatch.setattr(views, "cache_manager", manager)

        async def _inline_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        monkeypatch.setattr(views.asyncio, "to_thread", _inline_to_thread)

        # 1. Raw export log
        req_raw = Request(
            {
                "type": "http",
                "method": "GET",
                "path": f"/view/{token}",
                "query_string": b"raw=calibrated",
                "headers": [],
                "client": ("127.0.0.1", 12345),
            }
        )
        asyncio.run(views.view_transcript(token, req_raw, raw="calibrated"))

        # 2. Page export log
        req_page = Request(
            {
                "type": "http",
                "method": "GET",
                "path": f"/view/{token}",
                "query_string": b"page=calibrated",
                "headers": [],
                "client": ("127.0.0.1", 12345),
            }
        )
        asyncio.run(views.view_transcript(token, req_page, page="calibrated"))

        # 3. Export content log
        req_export = Request(
            {
                "type": "http",
                "method": "GET",
                "path": f"/export/{token}/calibrated",
                "query_string": b"",
                "headers": [],
                "client": ("127.0.0.1", 12345),
            }
        )
        asyncio.run(views.export_content(token, "calibrated", req_export))

        # Check that captured logs contain honest view_token
        combined = " ".join(captured_logs)
        assert f"Raw export: type=calibrated, view_token={expected_prefix}" in combined
        assert f"Page export: type=calibrated, view_token={expected_prefix}" in combined
        assert f"view_token: {expected_prefix}" in combined
        assert "view_token=unknown" not in combined
    finally:
        logger.remove(sink_id)
        manager.close()

