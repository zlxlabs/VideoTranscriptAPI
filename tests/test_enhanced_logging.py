#!/usr/bin/env python
# coding: utf-8

"""
Test enhanced logging for FunASR conversion

原实现依赖 tests/output/capswriter_format_test/ 下由另一个已不存在的脚本
（tests/test_capswriter_formats.py）生成的中间产物，在干净检出里恒定缺失，
于是函数走 return False 分支被 pytest 静默判绿。这里改成自带一份合成的
CapsWriter 数据（token 逐字、去标点，与 _remove_punctuation 对齐），
让四条边界路径真正被执行和断言。
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))

from video_transcript_api.transcriber.capswriter_client import (
    _create_segments_from_capswriter
)
from loguru import logger

_PUNCT = "，。！？、；：,;:!? "


def _synthetic_capswriter_data():
    """造一份最小的 CapsWriter 形态数据：(text, tokens, timestamps)。

    token 按「去掉标点后的每个字一个 token」构造，使 reconstructed 与
    text_clean 对齐（否则函数只会打一条对齐警告，测不到真实分段行为）。
    """
    sentences = [
        "今天我们聊一聊语音转写这件事",
        "先说结论再展开细节",
        "最后总结一下要点",
    ]
    text = "。".join(sentences)
    chars = [c for c in text if c not in _PUNCT]
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
        text=text,
        tokens=tokens,
        timestamps=timestamps,
    )

    print(f'\nResult: {len(segments)} segments generated')
    assert segments, "正常输入未生成任何 segment"
    for seg in segments:
        assert set(seg) >= {'start_time', 'end_time', 'text'}, f"segment 缺字段: {seg}"
        assert seg['text'], "segment 文本为空"
        assert seg['start_time'] is not None and seg['end_time'] is not None, \
            f"segment 时间为 None: {seg}"
        assert seg['end_time'] > seg['start_time'], f"segment 结束时间不晚于开始: {seg}"

    print('\n[TEST 2] Empty text - should fail gracefully')
    print('-' * 80)

    try:
        # 正文一律由 tokens 原样拼接，text 仅为兼容旧调用方保留：空 text
        # 不得丢弃合法的 token 流（上游契约保证 text_accu == join(tokens)）。
        segments = _create_segments_from_capswriter(
            text="",
            tokens=tokens,
            timestamps=timestamps,
        )
        print(f'\nResult: {len(segments)} segments generated')
        assert segments, "空 text 但 tokens 非空时应按 tokens 产出"
        assert "".join(s["text"] for s in segments) == "".join(tokens)

        # tokens 本身为空才是真的无内容
        assert _create_segments_from_capswriter(
            text="", tokens=[], timestamps=[]
        ) == [], "tokens 为空时应返回空 segments"
    except Exception as e:
        raise AssertionError(f"空文本不应抛异常: {e}") from e

    print('\n[TEST 3] Length mismatch - must fail fast per upstream contract')
    print('-' * 80)

    # 上游文件任务契约保证 len(tokens) == len(timestamps)，且明确要求
    # "不需要自行清洗或模糊对齐"。旧实现在这里截断后继续产出 segment，
    # 等于把不一致的输入当成可交付结果；改为 fail fast（#121）。
    mismatched_tokens = tokens[:max(1, len(tokens) // 2)]  # 故意不匹配
    try:
        segments = _create_segments_from_capswriter(
            text=text,
            tokens=mismatched_tokens,
            timestamps=timestamps,
        )
        raise AssertionError(
            f"长度不匹配时应当抛异常，却返回了 {len(segments)} 个 segment"
        )
    except ValueError as exc:
        message = str(exc)
        assert "CAPSWRITER_CONTRACT_FAILED" in message, message
        assert "tokens_timestamps_length_mismatch" in message, message
        print(f'\nCorrectly rejected: {message}')

    print('\n[TEST 4] Empty tokens - should fail gracefully')
    print('-' * 80)

    segments = _create_segments_from_capswriter(
        text=text,
        tokens=[],
        timestamps=[],
    )

    print(f'\nResult: {len(segments)} segments generated')
    assert segments == [], "空 tokens 应返回空 segments"

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