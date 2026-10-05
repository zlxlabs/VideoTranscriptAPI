"""Template tests for skipped_short summary stats display."""

from pathlib import Path

import jinja2

PROJECT_ROOT = Path(__file__).resolve().parents[3]
TEMPLATES_DIR = PROJECT_ROOT / "src" / "web" / "templates"


def _render(**overrides) -> str:
  env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=True,
  )
  ctx = {
    "title": "Sample",
    "author": "Author",
    "url": "https://example.com",
    "created_at_display": "2026-08-24",
    "platform": "youtube",
    "summary_html": None,
    "summary_state": "skipped_short",
    "calibrated_html": "<p>Body</p>",
    "use_speaker_recognition": False,
    "view_token": "token",
    "stats": {
      "original_length": 600,
      "calibrated_length": 500,
      "summary_length": 500,
    },
    "llm_config": None,
  }
  ctx.update(overrides)
  return env.get_template("transcript.html").render(**ctx)


def test_skipped_short_stats_line_neutral_message():
  html = _render()
  assert "原文过短未生成总结（以下为校对后全文）" in html
  assert "占原文" not in html


def test_generated_summary_stats_still_show_ratio():
  html = _render(
    summary_state="generated",
    summary_html="<p>Summary</p>",
    stats={
      "original_length": 5000,
      "calibrated_length": 4800,
      "summary_length": 3000,
    },
  )
  assert "占原文" in html
