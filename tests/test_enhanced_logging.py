#!/usr/bin/env python
# coding: utf-8

"""
Test enhanced logging for FunASR conversion

原实现依赖 tests/output/capswriter_format_test/ 下由另一个已不存在的脚本
（tests/test_capswriter_formats.py）生成的中间产物，在干净检出里恒定缺失，
于是函数走 return False 分支被 pytest 静默判绿。这里改成自带一份合成的
CapsWriter 数据（逐字 token，正文 = "".join(tokens) = text_accu），
让四条边界路径真正被执行和断言。
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))

from video_transcript_api.transcriber.capswriter_client import (
    _create_segments_from_capswriter,
    _validate_capswriter_contract,
)
from loguru import logger


def _synthetic_capswriter_data():
    """造一份最小的 CapsWriter 形态数据：(text, tokens, timestamps)。

    token 逐字（标点也在正文里），使 "".join(tokens) == text_accu。
    """
    sentences = [
        "今天我们聊一聊语音转写这件事",
        "先说结论再展开细节",
        "最后总结一下要点",
    ]
    text = "。".join(sentences)
    chars = list(text)
    timestamps = [round(i * 0.2, 2) for i in range(len(chars))]
    return text, chars, timestamps


def test_enhanced_logging():
    """测试增强后的日志输出（pytest 入口）"""
    assert _run_enhanced_logging_cases()


def _run_enhanced_logging_cases():
    """实际执行四条边界路径（__main__ 脚本入口需要 bool 来算 sys.exit 退出码）"""
    print('=' * 80)
    print('TESTING ENHANCED LOGGING')
    print('=' * 80)

    text, tokens, timestamps = _synthetic_capswriter_data()

    print('\n[TEST 1] Normal case - should succeed')
    print('-' * 80)

    segments = _create_segments_from_capswriter(
        tokens=tokens,
        timestamps=timestamps,
        min_len=2,
        max_len=30
    )

    print(f'\nResult: {len(segments)} segments generated')
    assert segments, "正常输入未生成任何 segment"
    for seg in segments:
        assert set(seg) >= {'start_time', 'end_time', 'text'}, f"segment 缺字段: {seg}"
        assert seg['text'], "segment 文本为空"
        assert seg['start_time'] is not None and seg['end_time'] is not None, \
            f"segment 时间为 None: {seg}"
        assert seg['end_time'] > seg['start_time'], f"segment 结束时间不晚于开始: {seg}"

    print('\n[TEST 2] Empty tokens - should fail gracefully')
    print('-' * 80)

    segments = _create_segments_from_capswriter(
        tokens=[],
        timestamps=[],
        min_len=80,
        max_len=300
    )

    print(f'\nResult: {len(segments)} segments generated')
    assert segments == [], "空 tokens 应返回空 segments"

    print('\n[TEST 3] Length mismatch - must be rejected by the contract')
    print('-' * 80)

    try:
        _validate_capswriter_contract({
            'text_accu': text,
            'tokens': tokens[:max(1, len(tokens) // 2)],  # 故意不匹配
            'timestamps': timestamps,
        })
        raise AssertionError("长度不匹配必须被契约拒绝")
    except Exception as e:
        if 'condition=tokens_timestamps_length_mismatch' not in str(e):
            raise AssertionError(f"契约拒绝原因不可 grep: {e}") from e

    print('\n[TEST 4] Token join mismatch - must be rejected by the contract')
    print('-' * 80)

    try:
        _validate_capswriter_contract({
            'text_accu': text.replace('。', ''),
            'tokens': tokens,
            'timestamps': timestamps,
        })
        raise AssertionError("join 与 text_accu 不一致必须被契约拒绝")
    except Exception as e:
        if 'condition=tokens_join_text_accu_mismatch' not in str(e):
            raise AssertionError(f"契约拒绝原因不可 grep: {e}") from e

    print('\n' + '=' * 80)
    print('LOGGING TEST COMPLETED')
    print('=' * 80)

    return True


if __name__ == '__main__':
    # 配置日志级别以显示 debug 信息
    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
        level="DEBUG"
    )

    try:
        success = _run_enhanced_logging_cases()
    except AssertionError as e:
        print(f'[FAIL] 断言失败: {e}')
        sys.exit(1)
    sys.exit(0 if success else 1)