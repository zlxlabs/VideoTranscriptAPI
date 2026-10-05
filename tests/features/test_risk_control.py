"""
风控模块测试脚本

测试敏感词检测和消敏功能（新策略）
- 总结文本：如有敏感词则替换为"内容风险，请通过url查看"
- 标题/作者：移除敏感词后取前6字符
- 普通文本：移除所有敏感词
"""

import sys
import os

# 添加项目根目录到路径
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, project_root)

from src.video_transcript_api.risk_control import TextSanitizer


def test_general_text_sanitization():
    """测试普通文本的敏感词移除功能"""
    print("=" * 60)
    print("Test 1: General Text Sanitization (Remove Sensitive Words)")
    print("=" * 60)

    # 创建测试用的敏感词库
    sensitive_words = {"力工男", "sm", "测试敏感词"}

    sanitizer = TextSanitizer(sensitive_words)

    # 测试用例1：包含敏感词的文本
    test_text = "这是一段包含力工男的测试文本"
    result = sanitizer.sanitize(test_text, text_type="general")

    print(f"\nOriginal text: {test_text}")
    print(f"Has sensitive: {result['has_sensitive']}")
    print(f"Sensitive words: {result['sensitive_words']}")
    print(f"Sanitized text: {result['sanitized_text']}")
    print(f"Expected: Sensitive words removed from text")

    # 测试用例2：包含多个敏感词
    test_text2 = "SM和力工男都是测试敏感词"
    result2 = sanitizer.sanitize(test_text2, text_type="general")

    print(f"\nOriginal text: {test_text2}")
    print(f"Has sensitive: {result2['has_sensitive']}")
    print(f"Sensitive words: {result2['sensitive_words']}")
    print(f"Sanitized text: {result2['sanitized_text']}")
    print(f"Expected: All sensitive words removed")

    # 测试用例3：不包含敏感词
    test_text3 = "这是一段正常的文本内容"
    result3 = sanitizer.sanitize(test_text3, text_type="general")

    print(f"\nOriginal text: {test_text3}")
    print(f"Has sensitive: {result3['has_sensitive']}")
    print(f"Sanitized text: {result3['sanitized_text']}")
    print(f"Expected: Text unchanged")


def test_summary_text_replacement():
    """测试总结文本的风控提示替换功能"""
    print("\n" + "=" * 60)
    print("Test 2: Summary Text Replacement (Risk Warning)")
    print("=" * 60)

    sensitive_words = {"力工男", "敏感词"}
    sanitizer = TextSanitizer(sensitive_words)

    # 测试用例1：包含敏感词的总结文本
    test_summary = "这是一段很长的总结文本，包含了力工男等内容，需要替换为风控提示。"
    result = sanitizer.sanitize(test_summary, text_type="summary")

    print(f"\nOriginal summary: {test_summary}")
    print(f"Has sensitive: {result['has_sensitive']}")
    print(f"Sensitive words: {result['sensitive_words']}")
    print(f"Sanitized text: {result['sanitized_text']}")
    print(f"Expected: '内容风险，请通过url查看'")

    # 测试用例2：不包含敏感词的总结文本
    test_summary2 = "这是一段正常的总结文本，没有任何问题。"
    result2 = sanitizer.sanitize(test_summary2, text_type="summary")

    print(f"\nOriginal summary: {test_summary2}")
    print(f"Has sensitive: {result2['has_sensitive']}")
    print(f"Sanitized text: {result2['sanitized_text']}")
    print(f"Expected: Text unchanged")


def test_title_author_truncation():
    """测试标题和作者的截断功能"""
    print("\n" + "=" * 60)
    print("Test 3: Title/Author Truncation (Remove + Take First 6 Chars)")
    print("=" * 60)

    sensitive_words = {"敏感词", "关键词"}
    sanitizer = TextSanitizer(sensitive_words)

    # 测试用例1：标题包含敏感词
    test_title = "这是包含敏感词的标题文本"
    result = sanitizer.sanitize(test_title, text_type="title")

    print(f"\nOriginal title: {test_title}")
    print(f"Has sensitive: {result['has_sensitive']}")
    print(f"Sensitive words: {result['sensitive_words']}")
    print(f"Sanitized text: {result['sanitized_text']}")
    print(f"Expected: First 6 chars after removing sensitive words")

    # 测试用例2：作者包含敏感词
    test_author = "关键词作者名字"
    result2 = sanitizer.sanitize(test_author, text_type="author")

    print(f"\nOriginal author: {test_author}")
    print(f"Has sensitive: {result2['has_sensitive']}")
    print(f"Sensitive words: {result2['sensitive_words']}")
    print(f"Sanitized text: {result2['sanitized_text']}")
    print(f"Expected: First 6 chars after removing sensitive words")

    # 测试用例3：标题不包含敏感词
    test_title3 = "这是正常标题文本"
    result3 = sanitizer.sanitize(test_title3, text_type="title")

    print(f"\nOriginal title: {test_title3}")
    print(f"Has sensitive: {result3['has_sensitive']}")
    print(f"Sanitized text: {result3['sanitized_text']}")
    print(f"Expected: Text unchanged")


def test_url_exclusion():
    """测试URL排除功能"""
    print("\n" + "=" * 60)
    print("Test 4: URL Exclusion")
    print("=" * 60)

    sensitive_words = {"test", "example"}
    sanitizer = TextSanitizer(sensitive_words)

    # 测试用例：文本中包含URL，URL中也包含敏感词
    test_text = "这是test文本，链接是 https://example.com/test，还有test"
    result = sanitizer.sanitize(test_text, text_type="general")

    print(f"\nOriginal text: {test_text}")
    print(f"Has sensitive: {result['has_sensitive']}")
    print(f"Sensitive words: {result['sensitive_words']}")
    print(f"Sanitized text: {result['sanitized_text']}")
    print(f"Note: URLs should not be sanitized, only text outside URLs")
    print(f"Expected: 'test' in URL preserved, 'test' outside URL removed")


def test_case_insensitive():
    """测试不区分大小写"""
    print("\n" + "=" * 60)
    print("Test 5: Case Insensitive Matching")
    print("=" * 60)

    sensitive_words = {"sensitive"}
    sanitizer = TextSanitizer(sensitive_words)

    # 测试不同大小写形式
    test_cases = [
        "This is SENSITIVE content",
        "This is Sensitive content",
        "This is sensitive content"
    ]

    for test_text in test_cases:
        result = sanitizer.sanitize(test_text, text_type="general")
        print(f"\nOriginal: {test_text}")
        print(f"Has sensitive: {result['has_sensitive']}")
        print(f"Sanitized: {result['sanitized_text']}")
        print(f"Expected: 'SENSITIVE'/'Sensitive'/'sensitive' removed")


def test_mixed_content():
    """测试混合内容（中英文敏感词）"""
    print("\n" + "=" * 60)
    print("Test 6: Mixed Chinese and English Sensitive Words")
    print("=" * 60)

    sensitive_words = {"力工男", "sm", "test"}
    sanitizer = TextSanitizer(sensitive_words)

    test_text = "这段文字包含力工男和SM，还有test等敏感词。访问 https://test.com 查看详情。"
    result = sanitizer.sanitize(test_text, text_type="general")

    print(f"\nOriginal text: {test_text}")
    print(f"Has sensitive: {result['has_sensitive']}")
    print(f"Sensitive words found: {result['sensitive_words']}")
    print(f"Sanitized text: {result['sanitized_text']}")
    print(f"Expected: All sensitive words removed except in URL")


def test_long_text():
    """测试长文本处理"""
    print("\n" + "=" * 60)
    print("Test 7: Long Text Processing")
    print("=" * 60)

    sensitive_words = {"敏感词"}
    sanitizer = TextSanitizer(sensitive_words)

    # 创建包含多处敏感词的长文本
    test_text = """
    这是一段很长的测试文本。
    第一段包含敏感词。
    第二段是正常内容，没有问题。
    第三段又出现了敏感词。
    参考链接：https://example.com/敏感词
    最后一段也包含敏感词。
    """

    result = sanitizer.sanitize(test_text, text_type="general")

    print(f"\nOriginal text length: {len(test_text)}")
    print(f"Has sensitive: {result['has_sensitive']}")
    print(f"Sensitive words count: {len(result['sensitive_words'])}")
    print(f"Sanitized text length: {len(result['sanitized_text'])}")
    print(f"\nSanitized text preview (first 200 chars):")
    print(result['sanitized_text'][:200])
    print(f"Expected: All '敏感词' removed except in URL")


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("Risk Control Module Test Suite (New Strategy)")
    print("=" * 60)

    try:
        test_general_text_sanitization()
        test_summary_text_replacement()
        test_title_author_truncation()
        test_url_exclusion()
        test_case_insensitive()
        test_mixed_content()
        test_long_text()

        print("\n" + "=" * 60)
        print("All tests completed!")
        print("=" * 60)

    except Exception as e:
        print(f"\nTest failed with error: {e}")
        import traceback
        traceback.print_exc()
