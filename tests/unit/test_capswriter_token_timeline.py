#!/usr/bin/env python
# coding: utf-8

"""CapsWriter token -> 时间轴映射的主测试（issue #109）。

锁住的是**约定**而不是当前这个 bug：只要 text 与 tokens 的书写习惯变了
（标点由独立模型插入、token 自带前导空格、大小写不同、中英混排），时间轴都
必须保持正确。旧实现在这些风格下会把整条时间轴压缩到约 81%。

核心不变式（每个都有对应测试）：
  * 每一句的 start 严格等于该句首 token 的已知时间；
  * 首段 start == timestamps[0]，末段 end >= timestamps[-1]；
  * 段落起点严格单调，时间轴无空洞；
  * 所有 span 的区间并集恰好覆盖 text（文本守恒）。
"""

from __future__ import annotations

import re
import time as _time
from typing import Dict, List, Sequence, Tuple

import pytest

from video_transcript_api.transcriber.capswriter_client import (
    TIMELINE_ALIGNED_RATIO_THRESHOLD,
    TIMELINE_COVERAGE_THRESHOLD,
    _create_segments_from_capswriter,
    _split_text_by_punctuation,
    _timeline_coverage,
)
from video_transcript_api.transcriber.token_timeline import (
    TextSpan,
    TokenTimeline,
    canonical_projection,
)

# ---------------------------------------------------------------------------
# 夹具生成器：先给「词 + 已知时间」，再按多种 token 风格渲染
# ---------------------------------------------------------------------------

Word = Tuple[str, float]

ENGLISH_SENTENCES: List[List[Word]] = [
    [("Hello", 0.0), ("there", 0.5), ("friend", 1.0), (".", 1.4)],
    [("The", 2.0), ("quick", 2.3), ("brown", 2.7), ("fox", 3.1), (".", 3.4)],
    [("It", 4.0), ("jumps", 4.2), ("over", 4.6), ("the", 4.9), ("dog", 5.2), (".", 5.5)],
    [("Now", 6.0), ("we", 6.3), ("test", 6.5), ("coverage", 6.9), ("!", 7.3)],
]

MIXED_SENTENCES: List[List[Word]] = [
    [("你好", 0.0), ("世界", 0.6), ("。", 1.2)],
    [("This", 2.0), ("is", 2.2), ("a", 2.3), ("mixed", 2.5), ("script", 2.8), (".", 3.0)],
    [("我们", 4.0), ("用", 4.4), ("中文", 4.7), ("和", 5.1), ("English", 5.4), ("。", 5.9)],
    [("结束", 6.0), ("了", 6.4), ("。", 6.7)],
]

CJK_ONLY_SENTENCES: List[List[Word]] = [
    [("前", 0.0), ("面", 0.4), ("的", 0.8), ("句", 1.2), ("子", 1.6), ("。", 1.9)],
    [("时", 2.0), ("间", 2.4), ("有", 2.8), ("效", 3.2), ("。", 3.6)],
    [("这", 4.0), ("是", 4.4), ("一", 4.8), ("个", 5.2), ("长", 5.6), ("。", 5.9)],
    [("句", 6.0), ("子", 6.4), ("用", 6.8), ("来", 7.2), ("。", 7.6)],
]


def _is_cjk(surface: str) -> bool:
    return all("㐀" <= ch <= "鿿" or ch in "。，" for ch in surface)


def _tokenize_paraformer(sentence: Sequence[Word]) -> List[Tuple[str, float]]:
    """Paraformer 式：无空格、无标点、子词带 ``@@`` 续接标记。"""
    out: List[Tuple[str, float]] = []
    for index, (surface, when) in enumerate(sentence):
        if _is_cjk(surface):
            out.append((surface, when))
            continue
        # 每个英文词切成两个子词：首个带 @@ 前缀式续接
        head, tail = surface[:1], surface[1:]
        out.append((head + "@@", when))
        if tail:
            out.append((tail, when))
    return out


def _render_sentencepiece(
    sentences: Sequence[Sequence[Word]],
) -> Tuple[str, List[str], List[float]]:
    """SentencePiece 式：词自带前导空格，标点是独立 token。"""
    text_parts: List[str] = []
    tokens: List[str] = []
    timestamps: List[float] = []
    for sentence_index, sentence in enumerate(sentences):
        rendered: List[str] = []
        for surface, when in sentence:
            if _is_cjk(surface):
                rendered.append(surface)
                tokens.append(surface)
            else:
                rendered.append(" " + surface)
                tokens.append(" " + surface)
            timestamps.append(when)
        chunk = "".join(rendered)
        if sentence_index < len(sentences) - 1:
            chunk += " "
        text_parts.append(chunk)
    text = "".join(text_parts)
    return text, tokens, timestamps


def _render_paraformer(
    sentences: Sequence[Sequence[Word]],
) -> Tuple[str, List[str], List[float]]:
    """Paraformer 式：token 无空格无标点；标点由独立标点模型只加在 text 上。"""
    tokens: List[str] = []
    timestamps: List[float] = []
    text_parts: List[str] = []
    for sentence_index, sentence in enumerate(sentences):
        rendered: List[str] = []
        for token, when in _tokenize_paraformer(sentence):
            if token in "。！":
                continue  # 标点模型产物，不进 token 流
            tokens.append(token)
            timestamps.append(when)
            rendered.append(token.replace("@@", ""))
        chunk = "".join(rendered)
        punctuation = sentence[-1][0]
        text_parts.append(chunk + punctuation + (" " if sentence_index < len(sentences) - 1 else ""))
    text = "".join(text_parts)
    if _is_cjk(sentences[0][0][0]):
        # 中文：词与词之间不插空格
        text = re.sub(r"(?<=[一-鿿]) (?=[一-鿿])", "", text)
    return text, tokens, timestamps


def _render_punctuation_model(
    sentences: Sequence[Sequence[Word]],
) -> Tuple[str, List[str], List[float]]:
    """text 比 tokens 多标点（英文词之间有空格，标点只在 text 上）。"""
    tokens: List[str] = []
    timestamps: List[float] = []
    text_parts: List[str] = []
    for sentence_index, sentence in enumerate(sentences):
        rendered: List[str] = []
        for surface, when in sentence:
            if _is_cjk(surface):
                rendered.append(surface)
                tokens.append(surface)
            else:
                rendered.append(" " + surface)
                tokens.append(surface)
            timestamps.append(when)
        chunk = "".join(rendered) + sentence[-1][0]
        text_parts.append(chunk + (" " if sentence_index < len(sentences) - 1 else ""))
    text = "".join(text_parts)
    if _is_cjk(sentences[0][0][0]):
        text = re.sub(r"(?<=[一-鿿]) (?=[一-鿿])", "", text)
    return text, tokens, timestamps


def _render_case_variant(
    sentences: Sequence[Sequence[Word]],
) -> Tuple[str, List[str], List[float]]:
    """tokens 全大写，text 保持原样（大小写差异由投影吸收）。"""
    text, tokens, timestamps = _render_sentencepiece(sentences)
    return text, [tok.upper() for tok in tokens], timestamps


STYLES = {
    "paraformer": _render_paraformer,
    "sentencepiece": _render_sentencepiece,
    "punctuation_model": _render_punctuation_model,
    "case_variant": _render_case_variant,
}


def _build(style: str, sentences: Sequence[Sequence[Word]]) -> Tuple[str, List[str], List[float]]:
    text, tokens, timestamps = STYLES[style](sentences)
    assert tokens and len(tokens) == len(timestamps)
    return text, tokens, timestamps


def _sentence_starts(text: str) -> List[TextSpan]:
    return _split_text_by_punctuation(text)


# ---------------------------------------------------------------------------
# 1. 多 token 风格：每句 start 必须等于该句首词的已知时间
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("style", sorted(STYLES))
@pytest.mark.parametrize(
    "sentences",
    [ENGLISH_SENTENCES, MIXED_SENTENCES, CJK_ONLY_SENTENCES],
    ids=["english", "mixed", "cjk"],
)
def test_every_sentence_start_matches_its_first_token_time(style, sentences):
    text, tokens, timestamps = _build(style, sentences)

    timeline = TokenTimeline.align(text, tokens, timestamps)
    spans = _sentence_starts(text)
    assert len(spans) == len(sentences), "sentence split must match fixture sentences"

    for sentence, span in zip(sentences, spans):
        expected = sentence[0][1]
        start, anchored = timeline.start_of(span)
        assert anchored, "every sentence head must be anchored"
        assert start == pytest.approx(expected), (
            f"style={style} sentence start {start} != expected {expected}"
        )


@pytest.mark.parametrize("style", sorted(STYLES))
def test_full_pipeline_sentence_times_are_exact(style):
    """走完整管线（分句 -> 合并成段）后，每段起点仍落在已知时间上。"""
    text, tokens, timestamps = _build(style, ENGLISH_SENTENCES)
    segments = _create_segments_from_capswriter(
        text=text, tokens=tokens, timestamps=timestamps, min_len=1, max_len=100_000
    )
    # min_len=1 -> buffer 不满足「过短」分支，每句独立成段，不发生合并
    assert len(segments) == len(ENGLISH_SENTENCES)
    for sentence, seg in zip(ENGLISH_SENTENCES, segments):
        assert seg["start_time"] == pytest.approx(sentence[0][1])


# ---------------------------------------------------------------------------
# 2. 生产量级复现（合成英文数据，126k 字符量级）
# ---------------------------------------------------------------------------


def _synthesize_english_sentences(count: int) -> List[List[Word]]:
    """合成英文句子：空格占比约 19%、标点独立成 token、时间戳铺满全长。"""
    # 短词：空格占比贴近生产的 ~19%（词均长 ~5 字符）
    words = [
        "the", "quick", "brown", "fox", "jumps", "over", "lazy", "dog",
        "time", "line", "axis", "word", "map", "span", "time", "code",
    ]
    sentences: List[List[Word]] = []
    when = 0.0
    for index in range(count):
        sentence: List[Word] = []
        length = 8 + (index % 5)
        for step in range(length):
            sentence.append((words[(index + step) % len(words)], when))
            when += 0.08
        sentence.append((".", when))
        when += 0.08
        sentences.append(sentence)
    return sentences


def _legacy_coverage(
    text: str, tokens: Sequence[str], timestamps: Sequence[float]
) -> float:
    """旧实现（累加游标 + 两套坐标系）的末段覆盖率，仅作对照基准。

    这是缺陷本身的重述，放在测试里只为量化「旧 0.81 / 新 ~1.0」，不代表可用路径。
    """
    positions: List[int] = []
    reconstructed = ""
    for token in tokens:
        positions.append(len(reconstructed))
        reconstructed += token.replace("@@", "")
    positions.append(len(reconstructed))

    def strip_all(value: str) -> str:
        return re.sub(r"[，。！？、；：,;:!?\s]", "", value)

    sentences = [s for s in re.split(r"([。！？!?])", text) if s.strip()]
    offset = 0
    last_end = timestamps[0]
    for sentence in sentences:
        size = len(strip_all(sentence))
        if size == 0:
            continue
        end_index = 0
        for index in range(len(positions) - 1):
            if positions[index] <= offset + size - 1 < positions[index + 1]:
                end_index = index
                break
        last_end = timestamps[min(end_index, len(timestamps) - 1)]
        offset += size
    return last_end / timestamps[-1]


def test_production_scale_english_recovers_full_coverage():
    sentences = _synthesize_english_sentences(2400)
    text, tokens, timestamps = _build("sentencepiece", sentences)
    assert len(text) > 120_000, f"synthetic sample too small: {len(text)}"

    started = _time.perf_counter()
    timeline = TokenTimeline.align(text, tokens, timestamps)
    align_seconds = _time.perf_counter() - started

    segments = _create_segments_from_capswriter(
        text=text,
        tokens=tokens,
        timestamps=timestamps,
        min_len=80,
        max_len=300,
        timeline=timeline,
    )
    coverage = _timeline_coverage(segments, timestamps[-1])

    legacy = _legacy_coverage(text, tokens, timestamps)
    # 旧实现被压缩到 0.81 附近；新实现必须贴近 1.0
    assert 0.78 < legacy < 0.86, f"legacy reference drifted: {legacy}"
    assert coverage > 0.99, f"new coverage must approach 1.0, got {coverage}"
    # 末段 end 与末 token 时间戳的差落在末句长度以内
    last_sentence = sentences[-1]
    sentence_span = _split_text_by_punctuation(text)[-1]
    end_time, _ = timeline.end_of(sentence_span)
    assert end_time is not None
    assert timestamps[-1] - end_time <= last_sentence[-1][1] - last_sentence[0][1] + 1e-6

    print(
        f"\n[perf] chars={len(text)} tokens={len(tokens)} align_seconds={align_seconds:.3f} "
        f"legacy_coverage={legacy:.4f} new_coverage={coverage:.4f}"
    )


# ---------------------------------------------------------------------------
# 4. 管线不变式
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("style", sorted(STYLES))
@pytest.mark.parametrize(
    "sentences", [ENGLISH_SENTENCES, MIXED_SENTENCES], ids=["english", "mixed"]
)
def test_pipeline_invariants(style, sentences):
    text, tokens, timestamps = _build(style, sentences)
    segments = _create_segments_from_capswriter(
        text=text, tokens=tokens, timestamps=timestamps, min_len=1, max_len=100_000
    )
    assert segments

    # 首段 start == timestamps[0]
    assert segments[0]["start_time"] == pytest.approx(timestamps[0])
    # 末段 end >= timestamps[-1]
    assert segments[-1]["end_time"] >= timestamps[-1] - 1e-6

    starts = [seg["start_time"] for seg in segments]
    ends = [seg["end_time"] for seg in segments]
    # 起点严格单调
    for earlier, later in zip(starts, starts[1:]):
        assert later > earlier, "segment starts must be strictly monotonic"
    # 时间轴无空洞：后一段起点不早于前一段终点
    for index in range(len(segments) - 1):
        assert starts[index + 1] >= ends[index], "timeline must not have holes"
        assert ends[index + 1] >= ends[index]

    # 文本守恒
    assert "".join(seg["text"] for seg in segments) == text


@pytest.mark.parametrize("style", sorted(STYLES))
def test_span_union_covers_text_exactly(style):
    text, _tokens, _timestamps = _build(style, MIXED_SENTENCES)
    spans = _split_text_by_punctuation(text)

    cursor = 0
    for span in spans:
        assert span.start == cursor, "spans must be contiguous and in order"
        assert span.end > span.start
        cursor = span.end
    assert cursor == len(text), "span union must cover the whole text"
    assert text[spans[0].start:spans[-1].end] == text


@pytest.mark.parametrize(
    "text",
    [
        "!!!???...",
        "Hello world.   ",
        "   Hello world.",
        "   ",
        "。。。",
        "你好，世界。  ",
        "Straße ist groß. Fi ligatur hier.",
        "Hi. Next one.",
        "?!  ",
        "Mixed 中文 and English. 第二句。",
    ],
)
def test_span_union_covers_boundary_inputs(text):
    """#111：原判据的输入全是「干净」文本（恒真）。此处覆盖尾空白/全标点/纯空白。"""
    spans = _split_text_by_punctuation(text)

    if not text:
        assert spans == []
        return

    cursor = 0
    for span in spans:
        assert span.start == cursor
        assert span.end > span.start
        cursor = span.end
    assert cursor == len(text), "span union must cover every character, whitespace included"
    assert "".join(text[span.start:span.end] for span in spans) == text


def test_span_text_round_trips_without_stripping():
    text = "Hello there.  Second sentence follows here!  Third one."
    spans = _split_text_by_punctuation(text)
    assert "".join(text[s.start:s.end] for s in spans) == text
    # 句间空白必须留在区间里（不 strip），否则段落文本会与原文脱节
    assert text[spans[0].start:spans[0].end] == "Hello there."
    assert text[spans[1].start:spans[1].end].startswith("  ")


# ---------------------------------------------------------------------------
# 3/5. 英文句号切句
# ---------------------------------------------------------------------------


def test_english_period_splits_sentences():
    text = "This is the first English statement. And this is the second one."
    spans = _split_text_by_punctuation(text)
    assert len(spans) == 2
    assert text[spans[0].start:spans[0].end].strip() == "This is the first English statement."
    assert text[spans[1].start:spans[1].end].strip() == "And this is the second one."


def test_period_inside_number_is_not_a_boundary():
    text = "Pi equals 3.14 in this run. Done."
    spans = _split_text_by_punctuation(text)
    assert len(spans) == 2


def test_cjk_punctuation_still_splits():
    spans = _split_text_by_punctuation("你好。世界！真不错？")
    assert len(spans) == 3


# ---------------------------------------------------------------------------
# 质量画像与降级守卫
# ---------------------------------------------------------------------------


def test_quality_reports_full_alignment_for_matching_styles():
    text, tokens, timestamps = _build("sentencepiece", ENGLISH_SENTENCES)
    quality = TokenTimeline.align(text, tokens, timestamps).quality
    assert quality.aligned_ratio == 1.0
    assert quality.unmatched_chars == 0


def test_quality_reports_unmatched_when_tokens_diverge():
    text, tokens, timestamps = _build("sentencepiece", ENGLISH_SENTENCES)
    # 服务端丢词：token 流少一段真实内容
    dropped = tokens[:10] + tokens[20:]
    quality = TokenTimeline.align(text, dropped, timestamps[:len(dropped)]).quality
    assert 0.0 < quality.aligned_ratio < 1.0
    assert quality.unmatched_chars > 0


def test_coverage_guard_flags_short_timeline():
    segments = [{"start_time": 0.0, "end_time": 80.0}]
    assert _timeline_coverage(segments, 100.0) == pytest.approx(0.8)
    assert 0.8 < TIMELINE_COVERAGE_THRESHOLD
    assert _timeline_coverage([{"start_time": 0.0, "end_time": 99.0}], 100.0) > TIMELINE_COVERAGE_THRESHOLD


def test_degraded_run_logs_greppable_line(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    from video_transcript_api.transcriber.capswriter_client import (
        CapsWriterClient,
        Config,
    )

    monkeypatch.setattr(Config, "generate_funasr_compat", True)
    monkeypatch.setattr(Config, "generate_txt", False)
    monkeypatch.setattr(Config, "generate_merge_txt", False)
    monkeypatch.setattr(Config, "generate_json", False)

    records = []
    monkeypatch.setattr(
        "video_transcript_api.transcriber.capswriter_client.logger.warning",
        lambda message, *a, **k: records.append(message),
    )

    with __import__("unittest.mock", fromlist=["patch"]).patch.object(
        CapsWriterClient, "__init__", lambda self: None
    ):
        client = CapsWriterClient()
    client.output_dir = str(tmp_path)
    client.log = MagicMock()

    import asyncio

    text, tokens, timestamps = _build("sentencepiece", ENGLISH_SENTENCES)
    result = {
        "task_id": "task-degraded",
        "text": text,
        "tokens": tokens,
        "timestamps": timestamps,
        # 音频真实时长远大于 token 时间轴 -> 覆盖率必然偏低
        "duration": timestamps[-1] * 2,
        "time_complete": 1.0,
        "time_start": 0.0,
    }
    asyncio.run(client._save_results(tmp_path / "audio.mp3", result))

    assert records, "degraded run must emit a warning"
    line = records[0]
    assert "capswriter timeline degraded:" in line
    assert "coverage=" in line and "aligned_ratio=" in line

    import json

    payload = json.loads((tmp_path / "audio_funasr.json").read_text(encoding="utf-8"))
    assert payload["timeline_quality"]["degraded"] is True
    assert payload["timeline_quality"]["coverage"] < TIMELINE_COVERAGE_THRESHOLD
    assert payload["timeline_quality"]["aligned_ratio"] >= TIMELINE_ALIGNED_RATIO_THRESHOLD


def test_canonical_projection_is_style_agnostic():
    left = "Hello, World!  It's fine."
    right = "▁hello ▁world ▁it's ▁fine ."
    assert canonical_projection(left) == canonical_projection(right)


# ---------------------------------------------------------------------------
# 两侧真的分叉时（非恒等路径）：锚点对齐的约束
# ---------------------------------------------------------------------------


def test_divergent_token_stream_stays_monotone_and_anchored():
    """服务端中途丢词时：映射必须单调（无时间空洞），且首句仍精确锚定。"""
    text, tokens, timestamps = _build("sentencepiece", ENGLISH_SENTENCES)
    kept = tokens[:12] + tokens[20:]
    kept_timestamps = timestamps[:12] + timestamps[20:]

    timeline = TokenTimeline.align(text, kept, kept_timestamps)
    mapping = timeline._canonical_to_token  # 私有：测试内部白盒检查单调性

    assert all(
        mapping[i] <= mapping[i + 1] for i in range(len(mapping) - 1)
    ), "token mapping must be monotone non-decreasing"
    assert timeline.quality.aligned_ratio > 0.5
    assert timeline.quality.unmatched_chars > 0

    spans = _split_text_by_punctuation(text)
    start, anchored = timeline.start_of(spans[0])
    assert anchored and start == pytest.approx(ENGLISH_SENTENCES[0][0][1])


def test_production_scale_alignment_is_fast_even_when_streams_diverge():
    """对齐耗时的回归防线：字符级 difflib 在同样输入下要 999.978s。"""
    sentences = _synthesize_english_sentences(2400)
    text, tokens, timestamps = _build("sentencepiece", sentences)
    # 局部丢词（模拟服务端只丢了中间一段），两侧无法走恒等快速路径
    truncated = tokens[:8000] + tokens[9000:]

    started = _time.perf_counter()
    timeline = TokenTimeline.align(text, truncated, timestamps[: len(truncated)])
    elapsed = _time.perf_counter() - started

    assert elapsed < 5.0, f"alignment too slow: {elapsed:.3f}s"
    assert timeline.quality.aligned_ratio > 0.9


# ---------------------------------------------------------------------------
# #111 修复 1：canonical 为空的 span 不得静默跳过（文本永不丢失）
# ---------------------------------------------------------------------------

# 覆盖主脑实测失败的两个输入，以及同类边界：全标点、纯空白、首尾空白、
# 中英混排、含 casefold 扩展字符。
TEXT_CONSERVATION_CASES = [
    "!!!???...",
    "Hello world.   ",
    "   Hello world.",
    "   ",
    "。。。",
    "你好，世界。  ",
    "Straße ist groß. Fi ligatur hier.",
    "Hi. Next one.",
    "?!  ",
    "Mixed 中文 and English. 第二句。",
]


@pytest.mark.parametrize("text", TEXT_CONSERVATION_CASES)
def test_pipeline_never_loses_text_for_any_input(text):
    """属性式判据：对任意输入，拼回全部段落文本必须逐字等于原文。"""
    tokens = list(text)
    timestamps = [round(index * 0.1, 2) for index in range(len(tokens))]

    segments = _create_segments_from_capswriter(
        text=text, tokens=tokens, timestamps=timestamps
    )

    joined = "".join(seg["text"] for seg in segments)
    assert joined == text, "no character of the transcript may be dropped"
    assert all(seg["text"] for seg in segments), "no empty-text segment"
    # 逐 span 校验：每个分句区间的原文必须原样出现在某个段落里
    for span in _split_text_by_punctuation(text):
        chunk = text[span.start:span.end]
        assert chunk in joined, f"span text[{span.start}:{span.end}] vanished"


def test_punctuation_only_input_still_produces_a_segment():
    """全标点输入曾整段消失（段数=0）：现在必须成段，时间诚实降级为 None。"""
    text = "!!!???..."
    segments = _create_segments_from_capswriter(
        text=text, tokens=list(text), timestamps=[0.1] * len(text)
    )
    assert len(segments) == 1
    assert segments[0]["text"] == text
    assert segments[0]["start_time"] is None and segments[0]["end_time"] is None


def test_trailing_whitespace_survives_merging():
    text = "Hello world.   "
    segments = _create_segments_from_capswriter(
        text=text, tokens=list(text), timestamps=[0.1] * len(text)
    )
    assert "".join(seg["text"] for seg in segments) == text
    assert segments[-1]["text"].endswith("   ")


def test_zero_length_span_is_skipped_but_punctuation_only_span_is_not():
    """"min_len == 0 的空文本 span 仍可跳过" 与 "纯标点 span 必须保留" 的区分。"""
    text = "Hi.  ..."
    spans = _split_text_by_punctuation(text)
    assert all(span.end > span.start for span in spans), "no zero-length spans emitted"
    segments = _create_segments_from_capswriter(
        text=text, tokens=list(text), timestamps=[0.1] * len(text)
    )
    assert "".join(seg["text"] for seg in segments) == text


# ---------------------------------------------------------------------------
# #111 修复 2：tokens/timestamps 不等长 -> 强制 degraded + 结构化日志
# ---------------------------------------------------------------------------


def _run_sidecar(tmp_path, monkeypatch, result):
    """跑真实落盘路径（_save_results），返回 (侧车 JSON, 采集到的 warning 行)。"""
    import asyncio
    import json as _json
    from unittest.mock import MagicMock, patch

    from video_transcript_api.transcriber.capswriter_client import CapsWriterClient, Config

    monkeypatch.setattr(Config, "generate_funasr_compat", True)
    monkeypatch.setattr(Config, "generate_txt", False)
    monkeypatch.setattr(Config, "generate_merge_txt", False)
    monkeypatch.setattr(Config, "generate_json", False)

    records = []
    monkeypatch.setattr(
        "video_transcript_api.transcriber.capswriter_client.logger.warning",
        lambda message, *a, **k: records.append(message),
    )
    with patch.object(CapsWriterClient, "__init__", lambda self: None):
        client = CapsWriterClient()
    client.output_dir = str(tmp_path)
    client.log = MagicMock()

    asyncio.run(client._save_results(tmp_path / "audio.mp3", result))
    payload = _json.loads((tmp_path / "audio_funasr.json").read_text(encoding="utf-8"))
    return payload, records


def test_length_mismatch_forces_degraded_even_when_thresholds_pass(tmp_path, monkeypatch):
    """两个阈值都达标时，**只有**不等长本身能把 degraded 拉高——否则本断言恒真。"""
    # tokens 比 timestamps 多一个（服务端多吐了 token），截断后时间轴完全对齐
    text = " Hello there."
    tokens = [" Hello", " there", " .", "!"]
    timestamps = [0.0, 0.5, 1.0]

    payload, records = _run_sidecar(
        tmp_path,
        monkeypatch,
        {
            "task_id": "task-mismatch",
            "text": text,
            "tokens": tokens,
            "timestamps": timestamps,
            "duration": timestamps[-1],
            "time_complete": 1.0,
            "time_start": 0.0,
        },
    )

    quality = payload["timeline_quality"]
    assert quality["input_mismatch"] == {
        "tokens_len": 4,
        "timestamps_len": 3,
        "truncated_to": 3,
    }
    # 前置条件：两个阈值都达标 —— degraded 若为 False，只可能是不等长判定失效
    assert quality["coverage"] >= TIMELINE_COVERAGE_THRESHOLD
    assert quality["aligned_ratio"] >= TIMELINE_ALIGNED_RATIO_THRESHOLD
    assert quality["degraded"] is True

    mismatches = [line for line in records if "capswriter timeline input_mismatch:" in line]
    assert mismatches, "length mismatch must leave a greppable structured warning"
    assert "tokens_len=4" in mismatches[0]
    assert "timestamps_len=3" in mismatches[0]


@pytest.mark.parametrize("style", ["sentencepiece", "punctuation_model"])
def test_matching_lengths_report_no_mismatch(tmp_path, monkeypatch, style):
    text, tokens, timestamps = _build(style, ENGLISH_SENTENCES)
    assert canonical_projection(text) == canonical_projection("".join(tokens))
    payload, _records = _run_sidecar(
        tmp_path,
        monkeypatch,
        {
            "task_id": "task-ok",
            "text": text,
            "tokens": tokens,
            "timestamps": timestamps,
            "duration": timestamps[-1],
            "time_complete": 1.0,
            "time_start": 0.0,
        },
    )
    quality = payload["timeline_quality"]
    assert quality["input_mismatch"] is None
    assert quality["degraded"] is False


def _unanchored_edge_case():
    tail = "abcdefghijklmnopqrstuvwxyz0123456789"
    text = "abcd" + tail
    tokens = ["abcd", *tail]
    timestamps = [0.0, *[float(index) for index in range(1, len(tokens))]]
    return text, tokens, timestamps


def test_opening_unmatched_token_forces_degraded_even_when_product_thresholds_pass(
    tmp_path, monkeypatch
):
    text, tokens, timestamps = _unanchored_edge_case()
    tokens = tokens[1:]
    timestamps = timestamps[1:]
    timeline = TokenTimeline.align(text, tokens, timestamps)
    first_span = _split_text_by_punctuation(text)[0]
    inferred_start, is_anchored = timeline.start_of(first_span)

    assert is_anchored is False
    assert inferred_start == pytest.approx(timestamps[0])

    payload, records = _run_sidecar(
        tmp_path,
        monkeypatch,
        {
            "task_id": "task-opening-unmatched",
            "text": text,
            "tokens": tokens,
            "timestamps": timestamps,
            "duration": timestamps[-1],
            "time_complete": 1.0,
            "time_start": 0.0,
        },
    )

    quality = payload["timeline_quality"]
    assert quality["aligned_ratio"] >= TIMELINE_ALIGNED_RATIO_THRESHOLD
    assert quality["coverage"] >= TIMELINE_COVERAGE_THRESHOLD
    assert quality["unmatched_chars"] == 4
    assert quality["degraded"] is True
    assert payload["segments"][0]["start_time"] is None
    assert payload["segments"][0]["end_time"] == pytest.approx(timestamps[-1])
    degraded_lines = [line for line in records if "capswriter timeline degraded:" in line]
    assert degraded_lines
    assert "unmatched_chars_threshold=0" in degraded_lines[0]


def test_trailing_unmatched_token_forces_degraded(tmp_path, monkeypatch):
    text, tokens, timestamps = _unanchored_edge_case()
    timestamps[-1] = 50.0
    tokens = tokens[:-1]
    timestamps = timestamps[:-1]

    payload, records = _run_sidecar(
        tmp_path,
        monkeypatch,
        {
            "task_id": "task-trailing-unmatched",
            "text": text,
            "tokens": tokens,
            "timestamps": timestamps,
            "duration": 50.0,
            "time_complete": 1.0,
            "time_start": 0.0,
        },
    )

    quality = payload["timeline_quality"]
    assert quality["unmatched_chars"] > 0
    assert quality["coverage"] < TIMELINE_COVERAGE_THRESHOLD
    assert quality["degraded"] is True
    assert payload["segments"][0]["end_time"] is None
    assert any("capswriter timeline degraded:" in line for line in records)


# ---------------------------------------------------------------------------
# #111 第二轮：空输入 / None 输入不得崩，且文本永不丢失
# ---------------------------------------------------------------------------

# 主脑探针的 5 个输入 + 3 个 None 元素/非字符串元素变体
EMPTY_INPUT_CASES = [
    ("全空", "", [], []),
    ("空text有tok", "", ["a"], [1.0]),
    ("有text空tok", "Hello.", [], []),
    ("text有tok但ts空", "Hello.", ["a"], []),
    ("timestamps为None", "Hello.", ["a"], None),
    ("timestamps含None元素", "Hello world.", [" Hello", " world", " ."], [0.0, None, 1.0]),
    ("tokens含None元素", "Hello world.", [" Hello", None, " world", " ."], [0.0, 0.5, 1.0, 1.5]),
    ("tokens含非字符串", "Hello world.", [" Hello", 5, " world", " ."], [0.0, 0.5, 1.0, 1.5]),
    ("tokens非序列", "Hello world.", "not a list", [0.0, 1.0]),
]


@pytest.mark.parametrize(
    "label,text,tokens,timestamps", EMPTY_INPUT_CASES, ids=[case[0] for case in EMPTY_INPUT_CASES]
)
def test_degenerate_inputs_never_crash_and_never_drop_text(
    label, text, tokens, timestamps
):
    # 不抛异常本身就是断言：修复前 "" + 非空 tokens 抛 ValueError、
    # timestamps=None 抛 TypeError: object of type 'NoneType' has no len()
    segments = _create_segments_from_capswriter(
        text=text, tokens=tokens, timestamps=timestamps
    )

    assert isinstance(segments, list)
    assert "".join(seg["text"] for seg in segments) == text, (
        f"{label}: 文本永不丢失（不可用的时间输入不能换来空结果）"
    )
    if not text:
        assert segments == [], f"{label}: 空文本没有可产出的分段"
    # 时间轴整体不可用的输入（tokens/timestamps 非序列、为空、含非法元素）
    # 必须诚实降级为 None；仅含 None 元素的 timestamps 仍能给出有效时间，
    # 那种情形由 test_none_timestamps_element_keeps_surrounding_times 单独锁死。
    if label in {"timestamps为None", "tokens含None元素", "tokens含非字符串", "tokens非序列"}:
        for seg in segments:
            assert seg["start_time"] is None and seg["end_time"] is None, (
                f"{label}: 时间轴不可用时必须诚实降级为 None"
            )


def test_timestamps_none_is_blocked_before_any_len_call():
    """显式 is None/isinstance 判定必须挡在 len() 之前（不靠 truthiness）。"""
    segments = _create_segments_from_capswriter(
        text="Hello there.", tokens=[" Hello", " there", " ."], timestamps=None
    )
    assert [seg["text"] for seg in segments] == ["Hello there."]
    assert segments[0]["start_time"] is None


def test_none_timestamps_element_keeps_surrounding_times():
    """timestamps 含 None 元素：其它有效时间照常使用，无效位置降级为 None。"""
    text = " Hello world."
    segments = _create_segments_from_capswriter(
        text=text,
        tokens=[" Hello", " world", " ."],
        timestamps=[0.0, None, 2.0],
    )
    assert "".join(seg["text"] for seg in segments) == text
    assert segments[0]["start_time"] == pytest.approx(0.0)
    assert segments[0]["end_time"] == pytest.approx(2.0)


def test_token_timeline_still_rejects_empty_text():
    """内部对象的不变式保留：为迁就调用方放宽断言是被禁止的。"""
    from video_transcript_api.transcriber.token_timeline import TokenTimeline

    with pytest.raises(ValueError):
        TokenTimeline.align("", ["a"], [1.0])
