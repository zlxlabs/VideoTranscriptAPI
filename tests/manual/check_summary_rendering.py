#!/usr/bin/env python3
"""
检查总结文件的渲染
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

def main():
    print("Check summary file rendering")

    cache_dir = r"D:\prod\VideoTranscriptAPI\data\cache\bilibili\2025\202509\BV14AnVznEMp"

    summary_file = os.path.join(cache_dir, 'llm_summary.txt')
    if not os.path.exists(summary_file):
        print("Summary file not exists")
        return

    with open(summary_file, 'r', encoding='utf-8') as f:
        content = f.read()

    print(f"Summary content length: {len(content)}")

    # Check if contains table marks
    has_pipe = '|' in content
    has_dash = '---' in content or '|-' in content

    print(f"Contains '|': {has_pipe}")
    print(f"Contains '---' or '|-': {has_dash}")

    if has_pipe:
        # Find table content
        lines = content.split('\n')
        table_found = False
        for i, line in enumerate(lines):
            if '|' in line and ('情境' in line or '评分' in line):
                print(f"Table found at line {i+1}")
                table_found = True
                # Show table content
                for j in range(max(0, i-1), min(len(lines), i+10)):
                    if lines[j].strip():
                        print(f"  Line {j+1}: {lines[j]}")
                break

        if not table_found:
            pipe_index = content.find('|')
            print(f"First '|' at position: {pipe_index}")
            start = max(0, pipe_index - 50)
            end = min(len(content), pipe_index + 150)
            context = content[start:end]
            print("Context around first '|':")
            print(repr(context))

    # Test markdown rendering on summary
    from video_transcript_api.utils.rendering import render_markdown_to_html

    try:
        html_result = render_markdown_to_html(content)
        print(f"\nMarkdown result length: {len(html_result)}")

        # Check for table tags
        if '<table>' in html_result:
            print("SUCCESS: HTML contains table tags!")
            table_count = html_result.count('<table>')
            print(f"Found {table_count} tables")

            # Show first table
            table_start = html_result.find('<table>')
            table_end = html_result.find('</table>', table_start) + 8
            if table_start != -1 and table_end != -1:
                table_html = html_result[table_start:table_end]
                print("\nFirst table HTML:")
                print(table_html[:500])
        else:
            print("FAIL: HTML does not contain table tags")

            # Check for pipe characters
            if '|' in html_result:
                print("But contains pipe characters")

    except Exception as e:
        print(f"Markdown rendering failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
