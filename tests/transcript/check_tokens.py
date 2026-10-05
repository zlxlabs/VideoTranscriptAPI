#!/usr/bin/env python
# coding: utf-8

import json

json_file = r'tests\output\capswriter_format_test\json\spk_extract.json'

with open(json_file, 'r', encoding='utf-8') as f:
    data = json.load(f)

text = data.get('text', '')
tokens = data.get('tokens', [])
timestamps = data.get('timestamps', [])

print('=== Text Analysis ===')
print(f'Text length: {len(text)} chars')
print(f'Text preview: {text[:150]}')
print(f'\nText has punctuation: {"，" in text or "。" in text}')

print('\n=== Tokens Analysis ===')
print(f'Total tokens: {len(tokens)}')
print(f'Total timestamps: {len(timestamps)}')
print(f'\nFirst 40 tokens:')
for i, token in enumerate(tokens[:40]):
    print(f'{i:3d}: [{token}] (time: {timestamps[i]:.2f}s)')

print('\n=== Punctuation Check ===')
punctuations = ['，', '。', '！', '？', '、', '；', '：']
found_puncts = []
for i, token in enumerate(tokens):
    if token in punctuations:
        found_puncts.append((i, token, timestamps[i]))

if found_puncts:
    print(f'Found {len(found_puncts)} punctuation marks in tokens:')
    for idx, punct, time in found_puncts[:10]:
        print(f'  Index {idx}: [{punct}] at {time:.2f}s')
else:
    print('NO punctuation marks found in tokens!')
