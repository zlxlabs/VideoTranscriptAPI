#!/usr/bin/env python
# coding: utf-8

"""
Test script to analyze the alignment between text and tokens from CapsWriter
"""

import json
import re
from pathlib import Path

def remove_punctuation(text):
    """移除所有标点符号"""
    return re.sub(r'[，。！？、；：,;:!?]', '', text)

def analyze_alignment(json_file, txt_file):
    """分析 text 和 tokens 的对齐关系"""

    # 读取 JSON（只有 tokens 和 timestamps）
    with open(json_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # 读取完整 text（从 merge.txt）
    with open(txt_file, 'r', encoding='utf-8') as f:
        text = f.read().strip()

    tokens = data.get('tokens', [])
    timestamps = data.get('timestamps', [])

    print('=' * 80)
    print('TEXT AND TOKENS ALIGNMENT ANALYSIS')
    print('=' * 80)

    print(f'\n[TEXT] Text (with punctuation):')
    print(f'Length: {len(text)} chars')
    print(f'Preview: {text[:150]}...')

    print(f'\n[CLEAN] Text without punctuation:')
    text_clean = remove_punctuation(text)
    print(f'Length: {len(text_clean)} chars')
    print(f'Preview: {text_clean[:150]}...')

    print(f'\n[TOKENS] Tokens:')
    print(f'Count: {len(tokens)}')
    print(f'Type: {type(tokens[0]) if tokens else "N/A"}')

    # 尝试拼接 tokens
    try:
        tokens_joined = ''.join(tokens)
        print(f'Joined length: {len(tokens_joined)} chars')
        print(f'Joined preview: {tokens_joined[:150]}...')
    except:
        print('[ERROR] Cannot join tokens (might contain non-string items)')
        tokens_joined = None

    print(f'\n[TIME] Timestamps:')
    print(f'Count: {len(timestamps)}')
    if timestamps:
        print(f'Range: {timestamps[0]:.2f}s - {timestamps[-1]:.2f}s')

    # 检查对齐关系
    print(f'\n[ALIGN] Alignment Check:')
    print(f'text_clean length: {len(text_clean)}')
    print(f'tokens count: {len(tokens)}')
    print(f'timestamps count: {len(timestamps)}')

    if tokens_joined:
        print(f'tokens joined length: {len(tokens_joined)}')

        # 比较前 50 个字符
        print(f'\n[COMPARE] First 50 chars comparison:')
        for i in range(min(50, len(text_clean), len(tokens_joined))):
            text_char = text_clean[i]
            token_char = tokens_joined[i] if i < len(tokens_joined) else '?'
            match = 'MATCH' if text_char == token_char else 'DIFF'
            print(f'{i:2d}: text[{text_char}] vs tokens[{token_char}] {match}')

    # 分析 tokens 的详细内容
    print(f'\n[DETAIL] Detailed tokens (first 30):')
    for i in range(min(30, len(tokens))):
        token = tokens[i]
        time = timestamps[i] if i < len(timestamps) else -1
        # 尝试显示 token 的多种表示
        try:
            print(f'{i:3d}: [{token}] (repr: {repr(token)}) @ {time:.2f}s')
        except:
            print(f'{i:3d}: [ERROR] @ {time:.2f}s')

def test_sentence_split(text):
    """测试按标点分句的效果"""
    print('\n' + '=' * 80)
    print('SENTENCE SPLITTING TEST')
    print('=' * 80)

    # 主要标点分句
    primary_puncts = r'([。！？!?])'
    parts = re.split(primary_puncts, text)

    sentences = []
    for i in range(0, len(parts), 2):
        if i < len(parts):
            sentence = parts[i]
            if i + 1 < len(parts):
                sentence += parts[i + 1]  # 加上标点
            if sentence.strip():
                sentences.append(sentence.strip())

    print(f'\n[SENTENCES] Found {len(sentences)} sentences:')
    for i, sent in enumerate(sentences):
        print(f'{i+1:2d}. ({len(sent):3d} chars) {sent[:80]}{"..." if len(sent) > 80 else ""}')

    # 长度分析
    print(f'\n[LENGTH] Length distribution:')
    short = sum(1 for s in sentences if len(s) < 80)
    medium = sum(1 for s in sentences if 80 <= len(s) <= 300)
    long_sent = sum(1 for s in sentences if len(s) > 300)
    print(f'  < 80 chars:  {short} sentences')
    print(f'  80-300 chars: {medium} sentences')
    print(f'  > 300 chars:  {long_sent} sentences')

if __name__ == '__main__':
    json_file = Path('tests/output/capswriter_format_test/json/spk_extract.json')
    txt_file = Path('tests/output/capswriter_format_test/all/spk_extract.merge.txt')

    if not json_file.exists():
        print(f'ERROR: JSON file not found: {json_file}')
        exit(1)

    if not txt_file.exists():
        print(f'ERROR: TXT file not found: {txt_file}')
        exit(1)

    analyze_alignment(json_file, txt_file)

    # 读取 text 进行分句测试
    with open(txt_file, 'r', encoding='utf-8') as f:
        text = f.read().strip()

    test_sentence_split(text)
