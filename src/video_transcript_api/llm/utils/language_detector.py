"""Language detection utility based on character ratio analysis.

Provides zero-cost language detection by analyzing Unicode character
distributions in text, avoiding external API calls or heavy NLP models.
"""

import unicodedata


# Sampling limit to avoid scanning extremely long texts
_SAMPLE_SIZE = 2000

# Threshold: if CJK characters exceed this ratio, treat as Chinese
_CJK_THRESHOLD = 0.30


def detect_language(text: str) -> str:
    """Detect the primary language of the given text.

    Uses Unicode character ratio analysis: counts CJK Unified Ideographs
    against total alphabetic/CJK characters. This is fast and sufficient
    for distinguishing Chinese-dominant vs English-dominant content.

    Args:
        text: Input text to analyze.

    Returns:
        Language code string:
        - "zh" for Chinese-dominant text
        - "en" for English/other-dominant text
    """
    if not text or not text.strip():
        return "zh"

    sample = text[:_SAMPLE_SIZE]

    cjk_count = 0
    alpha_count = 0

    for char in sample:
        if _is_cjk(char):
            cjk_count += 1
        elif char.isalpha():
            alpha_count += 1

    total = cjk_count + alpha_count
    if total == 0:
        return "zh"

    cjk_ratio = cjk_count / total
    return "zh" if cjk_ratio > _CJK_THRESHOLD else "en"


def _is_cjk(char: str) -> bool:
    """Check if a character is a CJK Unified Ideograph.

    Covers the main CJK Unicode blocks used in Chinese text.

    Args:
        char: Single character to check.

    Returns:
        True if the character is a CJK ideograph.
    """
    cp = ord(char)
    return (
        0x4E00 <= cp <= 0x9FFF        # CJK Unified Ideographs
        or 0x3400 <= cp <= 0x4DBF     # CJK Unified Ideographs Extension A
        or 0x20000 <= cp <= 0x2A6DF   # CJK Unified Ideographs Extension B
        or 0xF900 <= cp <= 0xFAFF     # CJK Compatibility Ideographs
        or 0x2F800 <= cp <= 0x2FA1F   # CJK Compatibility Ideographs Supplement
    )
