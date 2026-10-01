#!/usr/bin/env python
# coding: utf-8

"""
Unit test for FunASR conversion functions (without running CapsWriter server)
"""

import json
import sys
import tempfile
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))

from video_transcript_api.transcriber.capswriter_client import (
    _create_segments_from_capswriter,
    _token_char_prefix,
    _split_text_by_punctuation
)

# 历史上这里依赖 tests/output/capswriter_format_test/ 下的中间产物，由一个已不存在的
# 脚本生成，干净检出里恒定缺失 → 函数走 return False 被 pytest 静默判绿。
# 现在没有这些文件时改用自带合成数据（逐字 token，正文 = "".join(tokens)）。
_REGRESSION_JSON = Path('tests/output/capswriter_format_test/json/spk_extract.json')
_REGRESSION_TXT = Path('tests/output/capswriter_format_test/all/spk_extract.merge.txt')


def _synthetic_capswriter_data():
    """造一份最小的 CapsWriter 形态数据：(text, tokens, timestamps)

    满足上游契约：正文 = "".join(tokens) = text_accu。
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


def _load_capswriter_data():
    """优先用回归运行生成的真实转写数据；不存在则用合成数据（保证门禁里真跑）"""
    if _REGRESSION_JSON.exists() and _REGRESSION_TXT.exists():
        with open(_REGRESSION_JSON, 'r', encoding='utf-8') as f:
            data = json.load(f)
        with open(_REGRESSION_TXT, 'r', encoding='utf-8') as f:
            text = f.read().strip()
        return text, data.get('tokens', []), data.get('timestamps', [])
    print('[INFO] 回归数据缺失，改用合成数据')
    return _synthetic_capswriter_data()


def test_conversion_functions():
    """测试转换函数（pytest 入口）"""
    assert _run_conversion_cases()


def _run_conversion_cases():
    """实际断言各转换函数与 FunASR JSON 结构（__main__ 需要 bool 算退出码）"""
    print('=' * 80)
    print('TESTING FUNASR CONVERSION FUNCTIONS')
    print('=' * 80)

    text, tokens, timestamps = _load_capswriter_data()

    print(f'\n[INPUT DATA]')
    print(f'  Text length: {len(text)} chars')
    print(f'  Tokens count: {len(tokens)}')
    print(f'  Timestamps count: {len(timestamps)}')

    # 测试辅助函数
    print(f'\n[TEST 1] Testing helper functions...')

    # Test _token_char_prefix
    test_tokens = ['好', '欢', 'l', 'ily']
    prefix = _token_char_prefix(test_tokens)
    assert prefix == [0, 1, 2, 3, 6], f'Failed: prefix = {prefix}'
    print('  _token_char_prefix: OK')

    # Test _split_text_by_punctuation
    sentences = _split_text_by_punctuation("你好。我是主持人。欢迎！")
    assert [s for s, _, _ in sentences] == ['你好。', '我是主持人。', '欢迎！'], \
        f'Failed: sentences = {sentences}'
    assert [offset for _, offset, _ in sentences] == [0, 3, 9], \
        f'Failed: offsets = {[o for _, o, _ in sentences]}'
    print('  _split_text_by_punctuation: OK')

    # 测试主转换函数
    print(f'\n[TEST 2] Testing main conversion function...')

    segments = _create_segments_from_capswriter(
        tokens=tokens,
        timestamps=timestamps,
        min_len=80,
        max_len=300
    )

    print(f'  Generated segments: {len(segments)}')
    assert segments, 'ERROR: No segments generated!'

    # 验证 segments 结构
    print(f'\n[TEST 3] Validating segment structure...')

    required_fields = ['start_time', 'end_time', 'text']
    for i, seg in enumerate(segments):
        for field in required_fields:
            assert field in seg, f'ERROR: Segment {i + 1} missing field: {field}'

        assert seg['start_time'] is not None and seg['start_time'] >= 0, \
            f'ERROR: Segment {i + 1} has invalid start_time: {seg["start_time"]}'
        assert seg['end_time'] is not None and seg['end_time'] > seg['start_time'], \
            f'ERROR: Segment {i + 1} end_time <= start_time'
        assert seg['text'], f'ERROR: Segment {i + 1} has empty text'

    print('  All segments valid: OK')

    # 统计分析
    print(f'\n[SEGMENTS]')
    for i, seg in enumerate(segments):
        print(f'{i + 1}. [{seg["start_time"]:6.2f}s - {seg["end_time"]:6.2f}s] '
              f'({seg["end_time"] - seg["start_time"]:5.2f}s) {len(seg["text"]):3d} chars')
        print(f'   "{seg["text"][:60]}"')

    lengths = [len(seg['text']) for seg in segments]
    print(f'\n[STATISTICS]')
    print(f'  Segments count: {len(segments)}')
    print(f'  Length range: {min(lengths)} - {max(lengths)} chars')

    in_range = sum(1 for l in lengths if 80 <= l <= 300)
    print(f'  Segments in 80-300 range: {in_range}/{len(segments)}')

    # 测试 FunASR 格式构建
    print(f'\n[TEST 4] Building FunASR format...')

    funasr_data = {
        'task_id': 'test-task-123',
        'file_name': 'test_audio.mp3',
        'duration': timestamps[-1] if timestamps else 0,
        'segments': [
            {
                'start_time': seg['start_time'],
                'end_time': seg['end_time'],
                'text': seg['text']
            }
            for seg in segments
        ],
        'created_at': '2025-10-27T12:00:00',
        'processing_time': 10.5,
        'error': None
    }

    # 保存后读回，验证 JSON 可正确往返（写进临时目录，不污染仓库）
    with tempfile.TemporaryDirectory() as tmp_dir:
        output_file = Path(tmp_dir) / 'test_funasr_conversion.json'
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(funasr_data, f, ensure_ascii=False, indent=2)
        with open(output_file, 'r', encoding='utf-8') as f:
            loaded = json.load(f)

        assert loaded['segments'] == funasr_data['segments'], 'JSON load/save mismatch'
        assert loaded['segments'][0]['text'] == segments[0]['text'], '中文文本在 JSON 往返中损坏'
        print(f'  Saved to: {output_file}')
        print('  JSON serialization: OK')

    print('\n' + '=' * 80)
    print('ALL TESTS PASSED!')
    print('=' * 80)

    return True


if __name__ == '__main__':
    try:
        success = _run_conversion_cases()
    except AssertionError as e:
        print(f'[FAIL] 断言失败: {e}')
        sys.exit(1)
    sys.exit(0 if success else 1)