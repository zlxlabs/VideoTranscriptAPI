"""
Test raw export functionality
测试原始文件导出功能
"""
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from src.video_transcript_api.api.server import sanitize_filename, generate_download_filename


def test_sanitize_filename():
    """Test filename sanitization function"""
    print("Testing filename sanitization...")

    test_cases = [
        # (input, expected_output)
        ("Normal Title", "Normal Title"),
        ("Title with / slash", "Title with _ slash"),
        ("Title with : colon", "Title with _ colon"),
        ("Title with <> brackets", "Title with __ brackets"),
        ("Title with \"quotes\"", "Title with _quotes_"),
        ("Title with \\backslash", "Title with _backslash"),
        ("Title with |pipe", "Title with _pipe"),
        ("Title with ?question", "Title with _question"),
        ("Title with *asterisk", "Title with _asterisk"),
        ("   Leading and trailing spaces   ", "Leading and trailing spaces"),
        ("...dots...", "dots"),
        ("", "未命名"),
        ("中文标题", "中文标题"),
        ("Mixed 中英文 Title", "Mixed 中英文 Title"),
    ]

    passed = 0
    failed = 0

    for input_str, expected in test_cases:
        result = sanitize_filename(input_str)
        if result == expected:
            print(f"[PASS] '{input_str}' -> '{result}'")
            passed += 1
        else:
            print(f"[FAIL] '{input_str}' -> Expected: '{expected}', Got: '{result}'")
            failed += 1

    print(f"\nSanitize filename tests: {passed} passed, {failed} failed\n")
    return failed == 0


def test_generate_download_filename():
    """Test download filename generation function"""
    print("Testing download filename generation...")

    test_cases = [
        # (title, platform, content_type, expected_output)
        ("深度学习入门", "bilibili", "calibrated", "深度学习入门-校对文本-哔哩哔哩.txt"),
        ("Python Tutorial", "youtube", "summary", "Python Tutorial-总结文本-YouTube.txt"),
        ("短视频", "douyin", "transcript", "短视频-原始转录-抖音.txt"),
        ("美食分享", "xiaohongshu", "calibrated", "美食分享-校对文本-小红书.txt"),
        ("播客节目", "xiaoyuzhou", "calibrated", "播客节目-校对文本-小宇宙.txt"),
        ("本地文件", "generic", "calibrated", "本地文件-校对文本-自定义.txt"),
        # 长标题测试
        ("这是一个非常非常非常非常非常非常非常非常非常非常长的标题" * 2, "bilibili", "calibrated",
         "这是一个非常非常非常非常非常非常非常非常非常非常长的标题这是一个非常非常非常非常非常非常非常非常非常非常长的标题...-校对文本-哔哩哔哩.txt"),
        # 特殊字符测试
        ("Title with / and :", "youtube", "calibrated", "Title with _ and _-校对文本-YouTube.txt"),
    ]

    passed = 0
    failed = 0

    for title, platform, content_type, expected in test_cases:
        result = generate_download_filename(title, platform, content_type)
        # 对于长标题，只检查前缀和后缀
        if len(title) > 50:
            if result.endswith("-校对文本-哔哩哔哩.txt") and "..." in result:
                print(f"[PASS] Long title -> '{result[:30]}...'")
                passed += 1
            else:
                print(f"[FAIL] Long title -> '{result}'")
                failed += 1
        else:
            if result == expected:
                print(f"[PASS] '{title}' ({platform}, {content_type}) -> '{result}'")
                passed += 1
            else:
                print(f"[FAIL] '{title}' ({platform}, {content_type})")
                print(f"  Expected: '{expected}'")
                print(f"  Got:      '{result}'")
                failed += 1

    print(f"\nGenerate filename tests: {passed} passed, {failed} failed\n")
    return failed == 0


def test_url_encoding():
    """Test URL encoding for Chinese filenames"""
    print("Testing URL encoding...")

    from urllib.parse import quote

    test_cases = [
        "深度学习入门-校对文本-哔哩哔哩.txt",
        "Python Tutorial-总结文本-YouTube.txt",
        "Mixed 中英文 Title-校对文本-哔哩哔哩.txt",
    ]

    for filename in test_cases:
        encoded = quote(filename)
        print(f"Original: {filename}")
        print(f"Encoded:  {encoded}")
        print()

    return True


if __name__ == "__main__":
    print("=" * 60)
    print("Raw Export Functionality Tests")
    print("=" * 60)
    print()

    results = []

    # Run all tests
    results.append(("Sanitize Filename", test_sanitize_filename()))
    results.append(("Generate Download Filename", test_generate_download_filename()))
    results.append(("URL Encoding", test_url_encoding()))

    # Summary
    print("=" * 60)
    print("Test Summary")
    print("=" * 60)

    all_passed = True
    for test_name, passed in results:
        status = "[PASSED]" if passed else "[FAILED]"
        print(f"{test_name}: {status}")
        if not passed:
            all_passed = False

    print()
    if all_passed:
        print("All tests passed!")
        sys.exit(0)
    else:
        print("Some tests failed!")
        sys.exit(1)
