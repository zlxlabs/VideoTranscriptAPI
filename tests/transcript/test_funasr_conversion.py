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
    _find_token_idx,
    _split_text_by_punctuation,
    _token_prefixes,
)

_REGRESSION_JSON = Path('tests/output/capswriter_format_test/json/spk_extract.json')
_REGRESSION_TXT = Path('tests/output/capswriter_format_test/all/spk_extract.merge.txt')


def _synthetic_capswriter_data():
    """Build a contract-valid CapsWriter payload."""
    sentences = [
        "今天我们聊一聊语音转写这件事",
        "先说结论再展开细节",
        "最后总结一下要点",
    ]
    text = "。".join(sentences)
    tokens = list(text)
    timestamps = [round(i * 0.2, 2) for i in range(len(tokens))]
    return text, tokens, timestamps


def _load_capswriter_data():
    """优先用回归运行生成的真实转写数据；不存在则用合成数据（保证门禁里真跑）"""
    if _REGRESSION_JSON.exists() and _REGRESSION_TXT.exists():
        with open(_REGRESSION_JSON, 'r', encoding='utf-8') as f:
            data = json.load(f)
        with open(_REGRESSION_TXT, 'r', encoding='utf-8') as f:
            text = f.read().strip()
        return text, data.get('tokens', []), data.get('timestamps', [])
    print('[INFO] Regression data missing; using synthetic data')
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

    # Test exact prefix sums
    test_tokens = ['好', '欢', 'l', 'ily']
    positions = _token_prefixes(test_tokens)
    assert positions == [0, 1, 2, 3, 6], f'Failed: positions = {positions}'
    assert _find_token_idx(positions, 3) == 3
    print('  prefix sum: OK')

    # Test _split_text_by_punctuation
    test_text = "你好。我是主持人。欢迎！"
    sentences = _split_text_by_punctuation(test_text)
    assert len(sentences) == 3, f'Failed: expected 3 sentences, got {len(sentences)}'
    assert [test_text[start:end] for start, end in sentences] == [
        "你好。",
        "我是主持人。",
        "欢迎！",
    ]
    print('  _split_text_by_punctuation: OK')

    # 测试主转换函数
    print(f'\n[TEST 2] Testing main conversion function...')

    segments = _create_segments_from_capswriter(
        text=text,
        tokens=tokens,
        timestamps=timestamps,
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
        assert loaded['segments'][0]['text'] == segments[0]['text'], 'JSON text round-trip failed'
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
        print(f'[FAIL] Assertion failed: {e}')
        sys.exit(1)
    sys.exit(0 if success else 1)