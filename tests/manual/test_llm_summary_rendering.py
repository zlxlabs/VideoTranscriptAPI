"""
Manual test script to verify llm_summary.txt rendering

This script tests the actual llm_summary.txt file from cache
to ensure lists are properly rendered.
"""

import sys
from pathlib import Path

# Add src directory to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root / "src"))

from video_transcript_api.utils.rendering import render_markdown_to_html


def test_llm_summary_file():
    """Test rendering of actual llm_summary.txt file"""

    # Path to the actual file
    summary_file = project_root / "data" / "cache" / "xiaoyuzhou" / "2025" / "202510" / "68f5e973226547302058ad9c" / "llm_summary.txt"

    if not summary_file.exists():
        print(f"[ERROR] File not found: {summary_file}")
        return False

    print("=" * 70)
    print("Testing LLM Summary File Rendering")
    print("=" * 70)
    print(f"\nFile: {summary_file}")

    # Read the file
    with open(summary_file, 'r', encoding='utf-8') as f:
        content = f.read()

    print(f"File size: {len(content)} characters")
    print(f"File lines: {len(content.splitlines())} lines")

    # Render to HTML
    html = render_markdown_to_html(content)

    print(f"\nHTML output size: {len(html)} characters")

    # Count list items in HTML
    ul_count = html.count('<ul>')
    ol_count = html.count('<ol>')
    li_count = html.count('<li>')

    print(f"\nList statistics:")
    print(f"  Unordered lists (<ul>): {ul_count}")
    print(f"  Ordered lists (<ol>): {ol_count}")
    print(f"  Total list items (<li>): {li_count}")

    # Check specific problematic section
    print("\n" + "=" * 70)
    print("Checking problematic section (lines 9-11)")
    print("=" * 70)

    lines = content.splitlines()
    section = '\n'.join(lines[8:12])  # Lines 9-12 (0-indexed)

    print("\nOriginal text:")
    print(section)

    section_html = render_markdown_to_html(section)
    print("\nRendered HTML:")
    print(section_html)

    # Validate: should contain <ul> and <li> tags
    if '<ul>' in section_html and '<li>' in section_html:
        print("\n[PASSED] Lists are properly rendered with <ul> and <li> tags")
        success = True
    else:
        print("\n[FAILED] Lists not properly rendered")
        success = False

    # Save HTML output for inspection
    output_file = project_root / "tests" / "manual" / "llm_summary_test_output.html"
    with open(output_file, 'w', encoding='utf-8') as f:
        # Create a complete HTML document
        html_doc = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>LLM Summary Test Output</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
            line-height: 1.6;
            max-width: 900px;
            margin: 40px auto;
            padding: 0 20px;
        }}
        h2, h3 {{ border-bottom: 1px solid #eee; padding-bottom: 10px; }}
        ul, ol {{ margin: 10px 0; padding-left: 30px; }}
        li {{ margin: 8px 0; }}
        code {{ background: #f6f8fa; padding: 2px 6px; border-radius: 3px; }}
        pre {{ background: #f6f8fa; padding: 16px; border-radius: 6px; overflow-x: auto; }}
    </style>
</head>
<body>
{html}
</body>
</html>"""
        f.write(html_doc)

    print(f"\nFull HTML output saved to: {output_file}")
    print("You can open this file in a browser to visually inspect the rendering.")

    return success


if __name__ == "__main__":
    success = test_llm_summary_file()
    sys.exit(0 if success else 1)
