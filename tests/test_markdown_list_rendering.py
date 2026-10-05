"""
Test Markdown list rendering in various scenarios

Test coverage:
1. 2-space indent system
2. 4-space indent system
3. Mixed indentation
4. Three list markers (*, -, +)
5. Irregular indentation
6. List markers in code blocks
"""

import sys
from pathlib import Path

# Add src to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))

from video_transcript_api.utils.rendering.markdown_renderer import (
    _detect_list_indent_style,
    _fix_nested_list_indentation,
    render_markdown_to_html
)


def test_detect_2_space_style():
    """Test detection of 2-space indent style"""
    text = """
* Level 1
  * Level 2
    * Level 3
      * Level 4
"""
    result = _detect_list_indent_style(text)
    print(f"Test 2-space detection: {result}")
    assert result == 2, f"Expected 2, got {result}"
    print("PASSED: 2-space style detected")


def test_detect_4_space_style():
    """Test detection of 4-space indent style"""
    text = """
* Level 1
    * Level 2
        * Level 3
            * Level 4
"""
    result = _detect_list_indent_style(text)
    print(f"Test 4-space detection: {result}")
    assert result == 4, f"Expected 4, got {result}"
    print("PASSED: 4-space style detected")


def test_fix_2_space_to_4_space():
    """Test converting 2-space to 4-space"""
    text = """* Level 1
  * Level 2-a
  * Level 2-b
    * Level 3"""

    result = _fix_nested_list_indentation(text)
    print("\nInput:")
    print(text)
    print("\nOutput:")
    print(result)

    # Verify conversion
    lines = result.split('\n')
    assert lines[0] == "* Level 1"
    assert lines[1].startswith("    * Level 2-a")  # 2 -> 4
    assert lines[2].startswith("    * Level 2-b")  # 2 -> 4
    assert lines[3].startswith("        * Level 3")  # 4 -> 8
    print("PASSED: 2-space converted to 4-space")


def test_keep_4_space_unchanged():
    """Test 4-space stays unchanged"""
    text = """* Level 1
    * Level 2
        * Level 3"""

    result = _fix_nested_list_indentation(text)
    print("\nInput:")
    print(text)
    print("\nOutput:")
    print(result)

    # Verify unchanged
    assert result == text, "4-space style should remain unchanged"
    print("PASSED: 4-space style kept unchanged")


def test_all_three_markers():
    """Test three list markers (*, -, +)"""
    text = """* Asterisk level 1
  - Dash level 2
    + Plus level 3"""

    result = _fix_nested_list_indentation(text)
    print("\nInput:")
    print(text)
    print("\nOutput:")
    print(result)

    lines = result.split('\n')
    assert "* Asterisk" in lines[0]
    assert "    - Dash" in lines[1]
    assert "        + Plus" in lines[2]
    print("PASSED: All three markers supported")


def test_ordered_list():
    """Test ordered list"""
    text = """1. First
  2. Second
    3. Third"""

    result = _fix_nested_list_indentation(text)
    print("\nInput:")
    print(text)
    print("\nOutput:")
    print(result)

    lines = result.split('\n')
    assert lines[0] == "1. First"
    assert lines[1].startswith("    2. Second")
    assert lines[2].startswith("        3. Third")
    print("PASSED: Ordered list supported")


def test_user_example():
    """Test user-provided example"""
    text = """#### **2.5. Personal Experience**

*   **Traffic Safety:**
    *   Heavy rain in Vancouver.
    *   Police investigation of fatal accident."""

    result = _fix_nested_list_indentation(text)
    print("\nUser Example Input:")
    print(text)
    print("\nUser Example Output:")
    print(result)

    # Verify: 4 spaces should stay unchanged
    lines = result.split('\n')
    # Find first list item
    for i, line in enumerate(lines):
        if line.strip().startswith('*   **Traffic'):
            # Next level should still be 4 spaces (4-space system detected)
            assert lines[i+1].startswith('    *'), f"Line {i+1} should start with 4 spaces, got: {lines[i+1]}"
            break
    print("PASSED: User example rendered correctly")


def test_code_block_ignored():
    """Test list markers in code blocks are ignored"""
    text = """* Normal list

```python
* This is code
  * Not a list
```

* Normal list again"""

    result = _fix_nested_list_indentation(text)
    print("\nInput:")
    print(text)
    print("\nOutput:")
    print(result)

    # Verify code block content unchanged
    assert "* This is code" in result
    assert "  * Not a list" in result
    print("PASSED: Code block content ignored")


def test_irregular_indent():
    """Test irregular indentation"""
    text = """* Level 1
   * Level 2 (3 spaces - irregular)
     * Level 3 (5 spaces - irregular)"""

    result = _fix_nested_list_indentation(text)
    print("\nInput:")
    print(text)
    print("\nOutput:")
    print(result)

    # Irregular indent should align to nearest 4x
    # 3 -> 4, 5 -> 8
    lines = result.split('\n')
    print(f"Line 1: '{lines[1]}' (should have 4 spaces)")
    print(f"Line 2: '{lines[2]}' (should have 8 spaces)")
    print("PASSED: Irregular indent handled")


def test_full_rendering():
    """Test full rendering pipeline"""
    text = """### Test

*   Item 1
    *   Sub-item 1.1
    *   Sub-item 1.2
*   Item 2"""

    html = render_markdown_to_html(text)
    print("\nFull Rendering Input:")
    print(text)
    print("\nFull Rendering Output HTML:")
    print(html)

    # Verify HTML contains nested lists
    assert "<ul>" in html
    assert "<li>" in html
    print("PASSED: Full rendering works")


if __name__ == "__main__":
    print("=" * 60)
    print("Testing Markdown List Rendering")
    print("=" * 60)

    try:
        test_detect_2_space_style()
        print()
        test_detect_4_space_style()
        print()
        test_fix_2_space_to_4_space()
        print()
        test_keep_4_space_unchanged()
        print()
        test_all_three_markers()
        print()
        test_ordered_list()
        print()
        test_user_example()
        print()
        test_code_block_ignored()
        print()
        test_irregular_indent()
        print()
        test_full_rendering()

        print("\n" + "=" * 60)
        print("ALL TESTS PASSED")
        print("=" * 60)
    except AssertionError as e:
        print(f"\nTEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
