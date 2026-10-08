"""Share receipt contracts mirror the web page's rendered first paragraph."""

import pytest

from src.video_transcript_api.utils.notifications.completion_share import (
    build_completion_share_receipt,
    extract_first_summary_paragraph,
)


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        (
            "# 摘要标题\n\n含 **强调**、[链接文本](https://example.test/x) 和中文。\n\n第二段不能带入。",
            "含 强调、链接文本 和中文。",
        ),
        ("# 只有标题\n\n---", ""),
        ("`inline` <b>literal</b>", "inline literal"),
        ("", ""),
    ],
)
def test_extracts_visible_text_from_first_rendered_paragraph(summary, expected):
    assert extract_first_summary_paragraph(summary) == expected


def test_receipt_uses_original_url_exact_view_url_and_first_paragraph():
    task = {
        "task_id": "task_abc123ff",
        "title": "分享标题",
        "url": "https://example.test/video?id=original&track=keep",
        "terminal_snapshot": {
            "result": {
                "内容总结": "# 摘要标题\n\n第一段 **重点**。\n\n第二段不要复制。",
                "skip_summary": False,
                "stats": {"summary_status": "generated"},
            },
            "calibrate_only": False,
        },
    }

    receipt = build_completion_share_receipt(task, "https://view.test/view/view-token")

    assert receipt == (
        "✅ [#abc123] 分享标题\n"
        "原始地址：https://example.test/video?id=original&track=keep\n\n"
        "总结和校对：https://view.test/view/view-token\n\n"
        "第一段 重点。"
    )
    assert "第二段不要复制" not in receipt


@pytest.mark.parametrize(
    ("snapshot", "calibrate_only", "expected_notice"),
    [
        (None, False, "总结未能载入"),
        ({"result": None}, False, "总结未能载入"),
        ({"result": {}}, False, "总结未能载入"),
        (
            {"result": {"内容总结": "", "skip_summary": False, "stats": {"summary_status": "failed"}}},
            False,
            "总结生成失败",
        ),
        (
            {"result": {"内容总结": "不要复制", "skip_summary": True, "stats": {}}},
            False,
            "总结未生成",
        ),
        (
            {"result": {"内容总结": "不要复制", "skip_summary": False, "stats": {}}},
            True,
            "仅完成校对",
        ),
    ],
)
def test_receipt_keeps_view_url_without_fabricating_summary(snapshot, calibrate_only, expected_notice):
    task = {
        "task_id": "task_12345678",
        "title": "状态标题",
        "url": "",
        "terminal_snapshot": {
            **(snapshot or {}),
            "calibrate_only": calibrate_only,
        },
    }

    receipt = build_completion_share_receipt(task, "https://view.test/view/token")

    assert "总结和校对：https://view.test/view/token" in receipt
    assert expected_notice in receipt
    assert "不要复制" not in receipt
    assert "原始地址：" not in receipt
