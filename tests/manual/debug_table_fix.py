#!/usr/bin/env python3
"""
调试表格修复功能
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

def main():
    print("Debug table fix functionality")

    cache_dir = r"D:\prod\VideoTranscriptAPI\data\cache\bilibili\2025\202509\BV14AnVznEMp"

    summary_file = os.path.join(cache_dir, 'llm_summary.txt')
    if not os.path.exists(summary_file):
        print("Summary file not exists")
        return

    with open(summary_file, 'r', encoding='utf-8') as f:
        original_content = f.read()

    # Test the fix function directly
    from video_transcript_api.utils.rendering.markdown_renderer import _fix_indented_tables

    print("Testing fix function...")
    fixed_content = _fix_indented_tables(original_content)

    print(f"Original length: {len(original_content)}")
    print(f"Fixed length: {len(fixed_content)}")

    # Find table sections in both versions
    print("\nSearching for table in original content:")
    original_lines = original_content.split('\n')
    for i, line in enumerate(original_lines):
        if '|' in line and ('情境' in line or 'table' in line.lower()):
            print(f"  Line {i+1}: {repr(line)}")
            # Show surrounding lines
            for j in range(max(0, i-1), min(len(original_lines), i+5)):
                print(f"    {j+1}: {repr(original_lines[j])}")
            break

    print("\nSearching for table in fixed content:")
    fixed_lines = fixed_content.split('\n')
    for i, line in enumerate(fixed_lines):
        if '|' in line and ('情境' in line or 'table' in line.lower()):
            print(f"  Line {i+1}: {repr(line)}")
            # Show surrounding lines
            for j in range(max(0, i-1), min(len(fixed_lines), i+5)):
                print(f"    {j+1}: {repr(fixed_lines[j])}")
            break

    # Test markdown rendering on both
    import markdown

    print("\nTesting markdown on original content:")
    try:
        md = markdown.Markdown(extensions=['tables'])
        original_html = md.convert(original_content)
        print(f"Original HTML length: {len(original_html)}")
        print(f"Contains table tag: {'<table>' in original_html}")
    except Exception as e:
        print(f"Original markdown failed: {e}")

    print("\nTesting markdown on fixed content:")
    try:
        md = markdown.Markdown(extensions=['tables'])
        fixed_html = md.convert(fixed_content)
        print(f"Fixed HTML length: {len(fixed_html)}")
        print(f"Contains table tag: {'<table>' in fixed_html}")

        if '<table>' in fixed_html:
            print("SUCCESS: Table rendering fixed!")
            # Show table content
            table_start = fixed_html.find('<table>')
            table_end = fixed_html.find('</table>', table_start) + 8
            if table_start != -1 and table_end != -1:
                table_html = fixed_html[table_start:table_end]
                print("Table HTML:")
                print(table_html[:300])
        else:
            print("Still failed...")
            # Save both for comparison
            with open('debug_original.txt', 'w', encoding='utf-8') as f:
                f.write(original_content)
            with open('debug_fixed.txt', 'w', encoding='utf-8') as f:
                f.write(fixed_content)
            print("Saved debug files for comparison")

    except Exception as e:
        print(f"Fixed markdown failed: {e}")

if __name__ == "__main__":
    main()
