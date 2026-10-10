"""Offline regression for wecom-notifier markdown_v2 chunk budget.

Locks the 0.3.2 fix for zlxlabs/wecom-notifier#2: after short lines, a
~4.5KB single line must still yield chunks within the WeCom markdown_v2
UTF-8 byte budget (page markers included).
"""

from wecom_notifier import MessageSegmenter

# Upstream closeout (wecom-notifier#2): final markdown_v2 budget includes
# page indicator, fences, and moderation expansion.
WECOM_MARKDOWN_V2_BUDGET_BYTES = 3800


def test_markdown_v2_chunks_stay_within_wecom_budget():
    short_lines = "short line\n" * 500
    oversized_line = "X" * 4500
    content = short_lines + oversized_line + "\ntail line\n" * 50
    assert len(content.encode("utf-8")) >= 10_000
    assert "\n" not in oversized_line
    assert len(oversized_line.encode("utf-8")) >= 4500

    segments = MessageSegmenter().segment(content, "markdown_v2")

    assert segments
    oversize = [
        (index, len(segment.content.encode("utf-8")))
        for index, segment in enumerate(segments)
        if len(segment.content.encode("utf-8")) > WECOM_MARKDOWN_V2_BUDGET_BYTES
    ]
    assert oversize == [], (
        f"chunks exceeded markdown_v2 budget {WECOM_MARKDOWN_V2_BUDGET_BYTES}: {oversize}"
    )
