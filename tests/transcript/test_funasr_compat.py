#!/usr/bin/env python
# coding: utf-8

"""
Test FunASR compatible JSON generation from CapsWriter client

门禁里跑的是「FunASR 兼容 JSON 的结构契约」：用合成的 CapsWriter 数据走一遍
分段→组装 payload→校验的路径。端到端真转写（需要 tests/sample_files/spk_extract.mp3
和可达的 CapsWriter 服务）保留在 __main__ 脚本入口里。
"""

import json
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))

from video_transcript_api.transcriber.capswriter_client import (
    CapsWriterClient,
    Config,
    _create_segments_from_capswriter,
)

REQUIRED_FIELDS = [
    'task_id', 'file_name', 'duration', 'segments', 'created_at',
    'processing_time', 'error',
]
REQUIRED_SEGMENT_FIELDS = ['start_time', 'end_time', 'text']


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


def _validate_funasr_payload(data):
    """校验 FunASR 兼容 JSON 的结构契约，返回 segments"""
    missing_fields = [f for f in REQUIRED_FIELDS if f not in data]
    assert not missing_fields, f'Missing fields: {missing_fields}'

    segments = data['segments']
    assert segments, 'No segments found!'

    for i, seg in enumerate(segments):
        missing = [f for f in REQUIRED_SEGMENT_FIELDS if f not in seg]
        assert not missing, f'Segment {i + 1} missing fields: {missing}'
        assert seg['start_time'] is not None and seg['start_time'] >= 0, \
            f'Segment {i + 1} has invalid start_time'
        assert seg['end_time'] is not None and seg['end_time'] > seg['start_time'], \
            f'Segment {i + 1} end_time <= start_time'
        assert seg['text'], f'Segment {i + 1} has empty text'

    return segments


def test_funasr_generation():
    """测试 FunASR 兼容格式的结构契约（pytest 入口）"""
    assert _run_structure_cases()


def _run_structure_cases():
    """用合成数据走一遍 分段 → 组装 payload → 校验（返回 bool 供脚本入口用）"""
    print('=' * 80)
    print('TESTING FUNASR COMPATIBLE JSON STRUCTURE')
    print('=' * 80)

    text, tokens, timestamps = _synthetic_capswriter_data()

    segments = _create_segments_from_capswriter(
        tokens=tokens, timestamps=timestamps, min_len=2, max_len=30
    )
    print(f'  Generated {len(segments)} segments')

    funasr_data = {
        'task_id': 'test-task-123',
        'file_name': 'test_audio.mp3',
        'duration': timestamps[-1] if timestamps else 0,
        'segments': [
            {
                'start_time': seg['start_time'],
                'end_time': seg['end_time'],
                'text': seg['text'],
            }
            for seg in segments
        ],
        'created_at': '2025-10-27T12:00:00',
        'processing_time': 10.5,
        'error': None,
    }

    # 经一次 JSON 往返再校验，确保落盘格式本身可解析
    reloaded = json.loads(json.dumps(funasr_data, ensure_ascii=False))
    _validate_funasr_payload(reloaded)
    assert reloaded['segments'][0]['text'] == funasr_data['segments'][0]['text'], \
        '中文文本在 JSON 往返中损坏'

    print('\n' + '=' * 80)
    print('TEST PASSED!')
    print('=' * 80)

    return True


def _run_end_to_end_cases():
    """端到端真转写（需要音频文件 + 可达的 CapsWriter 服务），仅 __main__ 脚本入口使用"""
    audio_file = Path('tests/sample_files/spk_extract.mp3')
    output_dir = Path('tests/output/funasr_compat_test')

    if not audio_file.exists():
        print(f'ERROR: Audio file not found: {audio_file}')
        return False

    output_dir.mkdir(parents=True, exist_ok=True)

    Config.generate_txt = True
    Config.generate_json = False
    Config.generate_merge_txt = False
    Config.generate_funasr_compat = True

    client = CapsWriterClient(output_dir=str(output_dir))
    success, generated_files = client.transcribe_file(str(audio_file))
    assert success, 'Transcription failed!'
    print(f'Generated {len(generated_files)} files')

    funasr_file = output_dir / 'transcript_capswriter.json'
    assert funasr_file.exists(), 'FunASR compatible file not generated!'

    with open(funasr_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    segments = _validate_funasr_payload(data)
    print(f'  Validated {len(segments)} segments in {funasr_file}')
    return True


if __name__ == '__main__':
    try:
        ok = _run_structure_cases() and _run_end_to_end_cases()
    except AssertionError as e:
        print(f'[FAIL] 断言失败: {e}')
        sys.exit(1)
    sys.exit(0 if ok else 1)