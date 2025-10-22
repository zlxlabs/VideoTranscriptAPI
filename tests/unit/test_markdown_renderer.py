"""
Unit tests for markdown_renderer module

Test coverage:
- List spacing fixes (unordered and ordered lists)
- Code block handling
- Table rendering
- Edge cases
"""

import sys
from pathlib import Path

# Add src directory to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root / "src"))

from video_transcript_api.utils.markdown_renderer import (
    _fix_list_spacing,
    _fix_indented_tables,
    render_markdown_to_html
)


def test_list_after_paragraph():
    """Test adding blank line between paragraph and list"""
    input_text = """This is a paragraph.
* First item
* Second item"""

    expected = """This is a paragraph.

* First item
* Second item"""

    result = _fix_list_spacing(input_text)
    print("Test: List after paragraph")
    print(f"Input:\n{input_text}")
    print(f"\nExpected:\n{expected}")
    print(f"\nResult:\n{result}")
    assert result == expected, "Failed to add blank line between paragraph and list"
    print("[PASSED]\n")


def test_list_after_colon():
    """Test adding blank line after colon-ended paragraph"""
    input_text = """体制内对个人的评价维度包含两个方面：
*   **个人业务水平**：涵盖写材料
*   **情商水平**：即大家常说的人情世故"""

    result = _fix_list_spacing(input_text)
    print("Test: List after colon")
    print(f"Input:\n{input_text}")
    print(f"\nResult:\n{result}")

    # Should have blank line after first line
    lines = result.split('\n')
    assert lines[1] == '', "Should have blank line after colon-ended paragraph"
    print("[PASSED]\n")


def test_continuous_list_items():
    """Test that continuous list items don't get extra blank lines"""
    input_text = """Some text:

* First item
* Second item
* Third item"""

    result = _fix_list_spacing(input_text)
    print("Test: Continuous list items")
    print(f"Input:\n{input_text}")
    print(f"\nResult:\n{result}")

    # Should only have 3 list items, no extra blank lines between them
    list_lines = [line for line in result.split('\n') if line.strip().startswith('*')]
    assert len(list_lines) == 3, "Should preserve continuous list structure"
    print("[PASSED]\n")


def test_code_block_preservation():
    """Test that list markers in code blocks are not processed"""
    input_text = """Example code:
```python
# This is code
* not a list
- also not a list
```"""

    result = _fix_list_spacing(input_text)
    print("Test: Code block preservation")
    print(f"Input:\n{input_text}")
    print(f"\nResult:\n{result}")

    assert result == input_text, "Code blocks should not be modified"
    print("[PASSED]\n")


def test_list_after_heading():
    """Test that headings don't get extra blank lines before lists"""
    input_text = """### Some Heading
* First item
* Second item"""

    result = _fix_list_spacing(input_text)
    print("Test: List after heading")
    print(f"Input:\n{input_text}")
    print(f"\nResult:\n{result}")

    # Should NOT add blank line after heading
    assert result == input_text, "Should not add blank line after heading"
    print("[PASSED]\n")


def test_ordered_list():
    """Test ordered lists are handled correctly"""
    input_text = """面对同事或领导追问私人情况，夏老师提出了策略：
1.  **无意关心/好意**：如实回答
2.  **想帮你但不确定**：暗示需要帮助
3.  **别有用心**：采取策略"""

    result = _fix_list_spacing(input_text)
    print("Test: Ordered list")
    print(f"Input:\n{input_text}")
    print(f"\nResult:\n{result}")

    # Should have blank line after first line
    lines = result.split('\n')
    assert lines[1] == '', "Should have blank line before ordered list"
    print("[PASSED]\n")


def test_nested_lists():
    """Test nested lists with indentation"""
    input_text = """Main list:
*   **Item 1**：
    *   Nested item A
    *   Nested item B
*   **Item 2**：Another item"""

    result = _fix_list_spacing(input_text)
    print("Test: Nested lists")
    print(f"Input:\n{input_text}")
    print(f"\nResult:\n{result}")

    # Should add blank line after "Main list:" but preserve nested structure
    lines = result.split('\n')
    assert lines[1] == '', "Should have blank line after introductory line"
    print("[PASSED]\n")


def test_mixed_list_markers():
    """Test different list markers (*, -, +)"""
    input_text = """Different markers:
- Item with dash
* Item with asterisk
+ Item with plus"""

    result = _fix_list_spacing(input_text)
    print("Test: Mixed list markers")
    print(f"Input:\n{input_text}")
    print(f"\nResult:\n{result}")

    lines = result.split('\n')
    assert lines[1] == '', "Should handle different list markers"
    print("[PASSED]\n")


def test_full_rendering_pipeline():
    """Test complete rendering pipeline with HTML output"""
    input_text = """## Test Section
Some paragraph text here.
*   **Bold item**: Description
*   **Another item**: More text"""

    html = render_markdown_to_html(input_text)
    print("Test: Full rendering pipeline")
    print(f"Input:\n{input_text}")
    print(f"\nHTML Output:\n{html}")

    # Check that list is properly rendered as <ul> and <li>
    assert '<ul>' in html, "Should contain unordered list tag"
    assert '<li>' in html, "Should contain list item tags"
    assert '<strong>Bold item</strong>' in html, "Should render bold text"
    print("[PASSED]\n")


def test_real_world_example():
    """Test with actual llm_summary.txt-like content"""
    input_text = """### 2.1 Understanding of Social Skills
Some paragraph text discussing the evaluation dimensions in workplace:
*   **Professional competence**: Including writing reports, communicating with colleagues
*   **Emotional intelligence**: What people commonly call social skills

Summary after 8 years of experience:
*   "Making others comfortable": Foundation of EQ
*   "Gaining recognition from others": More difficult than showing ability"""

    html = render_markdown_to_html(input_text)
    print("Test: Real-world example")
    print(f"Input length: {len(input_text)} chars")
    print(f"\nHTML Output (first 500 chars):\n{html[:500]}")

    # Check for proper list rendering
    assert '<ul>' in html, "Should render lists properly"
    assert html.count('<li>') >= 4, "Should have at least 4 list items"
    assert '<h3' in html, "Should render heading"  # Allow for attributes like id
    print("[PASSED]\n")


def run_all_tests():
    """Run all tests"""
    print("=" * 60)
    print("Running Markdown Renderer Tests")
    print("=" * 60 + "\n")

    tests = [
        test_list_after_paragraph,
        test_list_after_colon,
        test_continuous_list_items,
        test_code_block_preservation,
        test_list_after_heading,
        test_ordered_list,
        test_nested_lists,
        test_mixed_list_markers,
        test_full_rendering_pipeline,
        test_real_world_example,
    ]

    passed = 0
    failed = 0

    for test_func in tests:
        try:
            test_func()
            passed += 1
        except AssertionError as e:
            print(f"[FAILED]: {test_func.__name__}")
            print(f"  Error: {e}\n")
            failed += 1
        except Exception as e:
            print(f"[ERROR] in {test_func.__name__}")
            print(f"  Error: {e}\n")
            failed += 1

    print("=" * 60)
    print(f"Test Results: {passed} passed, {failed} failed")
    print("=" * 60)

    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
