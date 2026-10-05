#!/usr/bin/env python
# coding: utf-8

"""
Test implementation of precise segment mapping algorithm (Solution A)
"""

import json
import re
from pathlib import Path
from typing import List, Dict, Tuple


def clean_token(token: str) -> str:
    """清理 token，去除 BPE 标记"""
    return token.replace('@@', '')


def build_token_position_map(tokens: List[str]) -> Tuple[List[int], str]:
    """
    构建 token 到字符位置的映射

    Returns:
        (token_to_char_pos, reconstructed_text)
        - token_to_char_pos[i]: 第 i 个 token 对应的字符起始位置
        - reconstructed_text: 重建的完整文本
    """
    token_to_char_pos = []
    reconstructed = ""

    for token in tokens:
        token_to_char_pos.append(len(reconstructed))
        clean = clean_token(token)
        reconstructed += clean

    # 添加最后一个位置（结束位置）
    token_to_char_pos.append(len(reconstructed))

    return token_to_char_pos, reconstructed


def find_token_idx(token_positions: List[int], char_pos: int) -> int:
    """
    二分查找：给定字符位置，找到对应的 token 索引

    Args:
        token_positions: token 到字符位置的映射
        char_pos: 目标字符位置

    Returns:
        对应的 token 索引
    """
    # 简单的线性查找（tokens 数量不多，性能可接受）
    for i in range(len(token_positions) - 1):
        if token_positions[i] <= char_pos < token_positions[i + 1]:
            return i

    # 如果超出范围，返回最后一个有效索引
    return len(token_positions) - 2


def split_text_by_punctuation(text: str) -> List[str]:
    """
    按主要标点符号分句，保留标点

    Args:
        text: 带标点的原始文本

    Returns:
        句子列表，每个句子包含结尾标点
    """
    # 主要分句标点
    primary_punct = r'([。！？!?])'

    # 分割并保留标点
    parts = re.split(primary_punct, text)

    sentences = []
    i = 0
    while i < len(parts):
        sentence = parts[i]
        # 如果下一个是标点，合并
        if i + 1 < len(parts) and parts[i + 1] in '。！？!?':
            sentence += parts[i + 1]
            i += 2
        else:
            i += 1

        if sentence.strip():
            sentences.append(sentence.strip())

    return sentences


def remove_punctuation(text: str) -> str:
    """移除文本中的标点符号"""
    return re.sub(r'[，。！？、；：,;:!?\s]', '', text)


def create_segments_precise(text: str, tokens: List[str], timestamps: List[float],
                            min_len: int = 80, max_len: int = 300) -> List[Dict]:
    """
    精确分段算法（方案 A）

    Args:
        text: 带标点的完整文本
        tokens: BPE token 列表
        timestamps: 时间戳列表
        min_len: 最小段落长度
        max_len: 最大段落长度

    Returns:
        segments 列表，每个包含 start_time, end_time, text
    """
    print('[ALGORITHM] Starting precise segment mapping...')

    # 检查长度是否匹配
    if len(tokens) != len(timestamps):
        print(f'[WARNING] Length mismatch: tokens={len(tokens)}, timestamps={len(timestamps)}')
        min_len_val = min(len(tokens), len(timestamps))
        tokens = tokens[:min_len_val]
        timestamps = timestamps[:min_len_val]

    # 步骤1: 构建 token 位置映射
    token_positions, reconstructed = build_token_position_map(tokens)
    print(f'[STEP1] Built position map: {len(tokens)} tokens -> {len(reconstructed)} chars')

    # 步骤2: 分句
    sentences = split_text_by_punctuation(text)
    print(f'[STEP2] Split into {len(sentences)} sentences')

    # 步骤3: 对齐验证
    text_clean = remove_punctuation(text)
    print(f'[STEP3] Alignment check:')
    print(f'  text_clean length: {len(text_clean)}')
    print(f'  reconstructed length: {len(reconstructed)}')
    print(f'  difference: {abs(len(text_clean) - len(reconstructed))}')

    # 步骤4: 映射每个句子到 token 范围
    segments = []
    char_offset = 0  # 在 text_clean 中的当前位置

    for idx, sentence in enumerate(sentences):
        sentence_clean = remove_punctuation(sentence)
        sentence_len = len(sentence_clean)

        if sentence_len == 0:
            continue

        # 查找对应的 token 范围
        start_token_idx = find_token_idx(token_positions, char_offset)
        end_token_idx = find_token_idx(token_positions, char_offset + sentence_len - 1)

        # 安全范围检查
        start_token_idx = max(0, min(start_token_idx, len(timestamps) - 1))
        end_token_idx = max(0, min(end_token_idx, len(timestamps) - 1))

        # 提取时间
        start_time = timestamps[start_token_idx]
        end_time = timestamps[end_token_idx]

        segments.append({
            'start_time': round(start_time, 2),
            'end_time': round(end_time, 2),
            'text': sentence,
            'length': len(sentence),
            'token_range': (start_token_idx, end_token_idx)
        })

        print(f'  Sentence {idx+1}: chars[{char_offset}:{char_offset+sentence_len}] '
              f'-> tokens[{start_token_idx}:{end_token_idx}] '
              f'-> time[{start_time:.2f}s:{end_time:.2f}s] '
              f'({len(sentence)} chars)')

        char_offset += sentence_len

    print(f'[STEP4] Mapped {len(segments)} segments')

    # 步骤5: 长度优化
    optimized = optimize_segment_lengths(segments, min_len, max_len)
    print(f'[STEP5] Optimized to {len(optimized)} segments')

    return optimized


def optimize_segment_lengths(segments: List[Dict], min_len: int, max_len: int) -> List[Dict]:
    """
    优化段落长度：合并短句、分割长句

    Args:
        segments: 原始 segments
        min_len: 最小长度
        max_len: 最大长度

    Returns:
        优化后的 segments
    """
    if not segments:
        return []

    optimized = []
    buffer = None

    for seg in segments:
        seg_len = seg['length']

        # 如果缓冲区为空，初始化
        if buffer is None:
            buffer = seg.copy()
            continue

        buffer_len = buffer['length']
        combined_len = buffer_len + seg_len

        # 情况1: 缓冲区太短，尝试合并
        if buffer_len < min_len:
            if combined_len <= max_len:
                # 合并
                buffer['end_time'] = seg['end_time']
                buffer['text'] = buffer['text'] + seg['text']
                buffer['length'] = combined_len
                buffer['token_range'] = (buffer['token_range'][0], seg['token_range'][1])
            else:
                # 合并后会超过 max_len，先输出缓冲区，当前句子作为新缓冲区
                optimized.append(buffer)
                buffer = seg.copy()

        # 情况2: 缓冲区长度合适
        elif min_len <= buffer_len <= max_len:
            # 输出缓冲区，当前句子作为新缓冲区
            optimized.append(buffer)
            buffer = seg.copy()

        # 情况3: 缓冲区太长（理论上不应该发生，但保险起见）
        else:
            # 直接输出缓冲区，当前句子作为新缓冲区
            optimized.append(buffer)
            buffer = seg.copy()

    # 处理最后的缓冲区
    if buffer is not None:
        optimized.append(buffer)

    # 处理超长句子（需要在次级标点处分割）
    final = []
    for seg in optimized:
        if seg['length'] > max_len:
            # 在逗号等次级标点处分割
            split_segs = split_long_segment(seg, max_len)
            final.extend(split_segs)
        else:
            final.append(seg)

    return final


def split_long_segment(segment: Dict, max_len: int) -> List[Dict]:
    """
    分割超长句子（在次级标点处）

    Args:
        segment: 超长的 segment
        max_len: 最大长度

    Returns:
        分割后的 segments
    """
    text = segment['text']

    # 尝试在次级标点（逗号、分号等）处分割
    secondary_punct = r'([，,；;])'
    parts = re.split(secondary_punct, text)

    # 如果没有次级标点，强制按长度分割
    if len(parts) <= 1:
        return [segment]  # 无法分割，保持原样

    # 合并分割后的部分，确保不超过 max_len
    split_segments = []
    current = ""
    start_time = segment['start_time']
    duration = segment['end_time'] - segment['start_time']
    total_len = segment['length']

    for part in parts:
        if len(current + part) <= max_len:
            current += part
        else:
            if current:
                # 估算时间（按比例）
                progress = len(current) / total_len
                end_time = start_time + duration * progress

                split_segments.append({
                    'start_time': round(start_time, 2),
                    'end_time': round(end_time, 2),
                    'text': current,
                    'length': len(current)
                })

                start_time = end_time
                current = part
            else:
                current = part

    # 添加最后一部分
    if current:
        split_segments.append({
            'start_time': round(start_time, 2),
            'end_time': round(segment['end_time'], 2),
            'text': current,
            'length': len(current)
        })

    return split_segments if split_segments else [segment]


def test_algorithm():
    """测试算法"""
    print('=' * 80)
    print('TESTING PRECISE SEGMENT MAPPING ALGORITHM (Solution A)')
    print('=' * 80)

    # 加载数据
    json_file = Path('tests/output/capswriter_format_test/json/spk_extract.json')
    txt_file = Path('tests/output/capswriter_format_test/all/spk_extract.merge.txt')

    if not json_file.exists() or not txt_file.exists():
        print('ERROR: Test files not found')
        return

    with open(json_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    with open(txt_file, 'r', encoding='utf-8') as f:
        text = f.read().strip()

    tokens = data.get('tokens', [])
    timestamps = data.get('timestamps', [])

    print(f'\n[INPUT]')
    print(f'  Text: {len(text)} chars')
    print(f'  Tokens: {len(tokens)}')
    print(f'  Timestamps: {len(timestamps)}')
    print(f'  Duration: {timestamps[-1]:.2f}s')

    # 运行算法
    segments = create_segments_precise(text, tokens, timestamps, min_len=80, max_len=300)

    # 输出结果
    print('\n' + '=' * 80)
    print('RESULTS')
    print('=' * 80)

    print(f'\nGenerated {len(segments)} segments:\n')
    for i, seg in enumerate(segments):
        duration = seg['end_time'] - seg['start_time']
        print(f'{i+1:2d}. [{seg["start_time"]:6.2f}s - {seg["end_time"]:6.2f}s] '
              f'({duration:5.2f}s) {seg["length"]:3d} chars')
        print(f'    {seg["text"][:100]}{"..." if len(seg["text"]) > 100 else ""}')
        print()

    # 统计分析
    print('=' * 80)
    print('STATISTICS')
    print('=' * 80)

    lengths = [seg['length'] for seg in segments]
    durations = [seg['end_time'] - seg['start_time'] for seg in segments]

    print(f'\nLength distribution:')
    print(f'  Min: {min(lengths)} chars')
    print(f'  Max: {max(lengths)} chars')
    print(f'  Avg: {sum(lengths) / len(lengths):.1f} chars')
    print(f'  < 80:      {sum(1 for l in lengths if l < 80)} segments')
    print(f'  80-300:    {sum(1 for l in lengths if 80 <= l <= 300)} segments')
    print(f'  > 300:     {sum(1 for l in lengths if l > 300)} segments')

    print(f'\nDuration distribution:')
    print(f'  Min: {min(durations):.2f}s')
    print(f'  Max: {max(durations):.2f}s')
    print(f'  Avg: {sum(durations) / len(durations):.2f}s')

    print(f'\nCoverage:')
    total_time = timestamps[-1]
    covered_time = sum(durations)
    print(f'  Total audio duration: {total_time:.2f}s')
    print(f'  Covered by segments: {covered_time:.2f}s')
    print(f'  Coverage: {covered_time / total_time * 100:.1f}%')


if __name__ == '__main__':
    test_algorithm()
