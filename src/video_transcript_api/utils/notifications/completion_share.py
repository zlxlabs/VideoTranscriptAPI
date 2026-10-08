"""Build the short, copyable receipt appended after a full completion summary."""

from html.parser import HTMLParser
import re

from .channel import _apply_risk_control_safe, build_task_notification_heading
from ..rendering.markdown_renderer import render_markdown_to_html


class _FirstParagraphText(HTMLParser):
    """Collect visible text from the first rendered HTML paragraph only."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.started = False
        self.finished = False
        self.depth = 0
        self.parts = []

    def handle_starttag(self, tag, _attrs):
        if tag == "p" and not self.started:
            self.started = True
            self.depth = 1
        elif self.started and not self.finished and self.depth:
            if tag == "br":
                self.parts.append("\n")
            elif tag == "p":
                self.depth += 1

    def handle_endtag(self, tag):
        if self.started and not self.finished and tag == "p":
            self.depth -= 1
            if self.depth == 0:
                self.finished = True

    def handle_data(self, data):
        if self.started and not self.finished and self.depth:
            self.parts.append(data)

    def visible_text(self):
        lines = [
            re.sub(r"\s+", " ", line).strip()
            for line in "".join(self.parts).splitlines()
        ]
        return "\n".join(line for line in lines if line).strip()


def extract_first_summary_paragraph(summary_markdown: str) -> str:
    """Return visible text of the first ``<p>`` after rendering summary Markdown."""
    if not isinstance(summary_markdown, str) or not summary_markdown:
        return ""
    parser = _FirstParagraphText()
    parser.feed(render_markdown_to_html(summary_markdown))
    parser.close()
    return parser.visible_text()


def build_completion_share_receipt(task: dict, view_url: str) -> str:
    """Build a compact receipt from the persisted task snapshot and view URL."""
    task = task or {}
    lines = [
        build_task_notification_heading(
            task.get("task_id"), task.get("title"), task.get("url") or "", icon="✅",
        )
    ]
    original_url = task.get("url")
    if original_url:
        lines.append(f"原始地址：{original_url}")
    if view_url:
        lines.extend(["", f"总结和校对：{view_url}"])

    snapshot = task.get("terminal_snapshot") or {}
    result = snapshot.get("result") if isinstance(snapshot, dict) else None
    notice = None
    paragraph = ""
    if not isinstance(result, dict) or not result:
        notice = "⚠️ 总结未能载入，请在网页查看"
    elif snapshot.get("calibrate_only"):
        notice = "ℹ️ 仅完成校对，未生成总结"
    elif result.get("skip_summary"):
        notice = "ℹ️ 总结未生成"
    else:
        stats = result.get("stats", {})
        summary_status = stats.get("summary_status")
        if summary_status == "failed":
            notice = "⚠️ 总结生成失败"
        elif summary_status == "disabled":
            notice = "ℹ️ 总结未启用"
        else:
            summary = result.get("内容总结")
            if not isinstance(summary, str) or not summary.strip():
                notice = "ℹ️ 总结未生成"
            else:
                paragraph = extract_first_summary_paragraph(summary)
                if paragraph:
                    paragraph = _apply_risk_control_safe(
                        paragraph, text_type="summary",
                    )

    if paragraph:
        lines.extend(["", paragraph])
    elif notice:
        lines.extend(["", notice])
    return "\n".join(lines)
