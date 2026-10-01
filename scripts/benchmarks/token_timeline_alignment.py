#!/usr/bin/env python3
"""Reproducible token-to-timeline alignment benchmark for issue #109.

The benchmark is intentionally self-contained: the current checkout predates
PR #111, so it does not import production code.  It measures the alignment
cores on deterministic 120k-character English, repeated-English, and mixed
Chinese/English fixtures.
"""

from __future__ import annotations

import argparse
import gc
import importlib.metadata
import json
import math
import os
import platform
import statistics
import time
import tracemalloc
from bisect import bisect_right
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Callable, Sequence


@dataclass
class Fixture:
    name: str
    text: str
    text_units: list[str]
    token_units: list[str]
    token_times: list[float]
    expected_times: list[float]
    phrase_starts: list[int]
    drift: bool


def _canonical(value: str) -> str:
    return "".join(ch.casefold() for ch in value if ch.isalnum())


def _timestamp(index: int) -> float:
    return round(0.7 + index * 0.43, 6)


def _render(units: Sequence[str], punctuation_every: int = 0) -> str:
    parts: list[str] = []
    for index, unit in enumerate(units):
        if index and unit.isascii() and units[index - 1].isascii():
            parts.append(" ")
        parts.append(unit)
        if punctuation_every and (index + 1) % punctuation_every == 0:
            parts.append(". ")
    return "".join(parts).strip()


def _make_fixture(name: str, units: list[str], phrase_starts: list[int],
                  drift: bool, punctuation_every: int = 0) -> Fixture:
    expected = [_timestamp(index) for index in range(len(units))]
    tokens = list(units)
    times = list(expected)
    if drift:
        insert_at = 3 if name.startswith("repeat") else 101
        tokens.insert(insert_at, "noiseinserted")
        times.insert(insert_at, expected[insert_at] - 0.2)
    return Fixture(
        name=f"{name}_{'drift' if drift else 'clean'}",
        text=_render(units, punctuation_every),
        text_units=units,
        token_units=tokens,
        token_times=times,
        expected_times=expected,
        phrase_starts=phrase_starts,
        drift=drift,
    )


def make_fixtures(target_chars: int) -> list[Fixture]:
    english_words = (
        "the quick brown fox jumps over the lazy dog while careful engineers "
        "measure token timing across every sentence before shipping reliable "
        "transcription software that keeps context and speaker boundaries stable"
    ).split()
    english: list[str] = []
    while len(_render(english, 13)) < target_chars:
        offset = len(english) % len(english_words)
        english.extend(english_words[offset:] + english_words[:offset])

    literal_phrase = (
        "the quick brown fox jumps over lazy dog today now"
    ).split()
    repeated: list[str] = ["intro", "line", "here"]
    literal_starts: list[int] = []
    while len(_render(repeated)) < target_chars - 20:
        literal_starts.append(len(repeated))
        repeated.extend(literal_phrase)
    repeated.extend(["outro", "line", "here"])

    heavy_phrase = ["repeat"] * 100
    heavy: list[str] = ["intro", "line", "here"]
    heavy_starts: list[int] = []
    while len(_render(heavy)) < target_chars - 20:
        heavy_starts.append(len(heavy))
        heavy.extend(heavy_phrase)
    heavy.extend(["outro", "line", "here"])

    mixed_pattern = (
        "我们", "用", "English", "token", "对齐", "来", "检查", "重复",
        "内容", "and", "time", "轴", "必须", "保持", "单调", "稳定",
    )
    mixed: list[str] = []
    while len(_render(mixed, 16)) < target_chars:
        mixed.extend(mixed_pattern)

    return [
        _make_fixture("english", english, [], False, 13),
        _make_fixture("english", english, [], True, 13),
        _make_fixture("repeat_literal", repeated, literal_starts, False),
        _make_fixture("repeat_literal", repeated, literal_starts, True),
        _make_fixture("repeat_heavy", heavy, heavy_starts, False),
        _make_fixture("repeat_heavy", heavy, heavy_starts, True),
        _make_fixture("mixed", mixed, [], False, 16),
        _make_fixture("mixed", mixed, [], True, 16),
    ]


def _token_offsets(tokens: Sequence[str]) -> tuple[str, list[int]]:
    offsets = [0]
    parts: list[str] = []
    for token in tokens:
        parts.append(_canonical(token))
        offsets.append(offsets[-1] + len(parts[-1]))
    return "".join(parts), offsets


def _token_at(offsets: Sequence[int], position: int) -> int:
    return min(len(offsets) - 2, max(0, bisect_right(offsets, position) - 1))


def _anchor_mapping(fixture: Fixture, anchor_size: int = 12) -> tuple[list[int], dict]:
    text = "".join(_canonical(unit) for unit in fixture.text_units)
    token_text, offsets = _token_offsets(fixture.token_units)
    mapping = [-1] * len(text)
    anchors: list[tuple[int, int]] = []
    if text == token_text:
        mapping = [_token_at(offsets, index) for index in range(len(text))]
    elif len(text) >= anchor_size and len(token_text) >= anchor_size:
        index: dict[str, list[int]] = {}
        for start in range(len(token_text) - anchor_size + 1):
            index.setdefault(
                token_text[start:start + anchor_size], []
            ).append(start)
        last_token_start = -1
        position = 0
        while position <= len(text) - anchor_size:
            bucket = index.get(text[position:position + anchor_size])
            if bucket:
                slot = bisect_right(bucket, last_token_start)
                if slot < len(bucket):
                    token_start = bucket[slot]
                    anchors.append((position, token_start))
                    for offset in range(anchor_size):
                        mapping[position + offset] = _token_at(
                            offsets, token_start + offset
                        )
                    last_token_start = token_start
                    position += anchor_size
                    continue
            position += 1
        last = -1
        for index, value in enumerate(mapping):
            if value >= 0:
                last = value
            elif last >= 0:
                mapping[index] = last
        following = -1
        for index in range(len(mapping) - 1, -1, -1):
            if mapping[index] >= 0:
                following = mapping[index]
            elif following >= 0:
                mapping[index] = following
    unit_starts: list[int] = []
    cursor = 0
    for unit in fixture.text_units:
        unit_starts.append(cursor)
        cursor += len(_canonical(unit))
    unit_mapping = [mapping[start] if mapping else -1 for start in unit_starts]
    densities = [0] * 16
    for position, _token in anchors:
        bucket = min(15, position * 16 // max(1, len(text)))
        densities[bucket] += 1
    mean = statistics.fmean(densities) if anchors else 0.0
    density_cv = (
        statistics.pstdev(densities) / mean if mean else 0.0
    )
    slopes = [
        (unit_mapping[i + 1] - unit_mapping[i])
        for i in range(len(unit_mapping) - 1)
        if unit_mapping[i] >= 0 and unit_mapping[i + 1] >= 0
    ]
    slope_mean = statistics.fmean(slopes) if slopes else 0.0
    slope_cv = statistics.pstdev(slopes) / slope_mean if slope_mean else 0.0
    return unit_mapping, {
        "anchor_count": len(anchors),
        "density_cv": round(density_cv, 6),
        "slope_cv": round(slope_cv, 6),
        "postcheck_flag": bool(density_cv > 1.0 or slope_cv > 0.5),
        "canonical_equal": text == token_text,
    }


def _sequence_mapping(
    text_units: Sequence[str],
    token_units: Sequence[str],
    block_size: int = 512,
) -> list[int]:
    mapping = [-1] * len(text_units)
    text_cursor = token_cursor = 0
    while text_cursor < len(text_units):
        text_end = min(len(text_units), text_cursor + block_size)
        token_end = min(len(token_units), token_cursor + block_size + 32)
        left = text_units[text_cursor:text_end]
        right = token_units[token_cursor:token_end]
        matcher = SequenceMatcher(None, left, right, autojunk=False)
        consumed = 0
        for tag, a1, a2, b1, b2 in matcher.get_opcodes():
            if tag == "equal":
                for offset in range(a2 - a1):
                    mapping[text_cursor + a1 + offset] = token_cursor + b1 + offset
                consumed = max(consumed, b2)
            elif tag in {"delete", "replace"}:
                consumed = max(consumed, b2)
        text_cursor = text_end
        token_cursor += max(consumed, len(left))
    return mapping


def _unique_layer_mapping(fixture: Fixture) -> list[int]:
    text = fixture.text_units
    tokens = fixture.token_units
    text_positions: dict[str, list[int]] = {}
    token_positions: dict[str, list[int]] = {}
    for index, value in enumerate(text):
        text_positions.setdefault(value, []).append(index)
    for index, value in enumerate(tokens):
        token_positions.setdefault(value, []).append(index)
    anchors = [
        (text_positions[value][0], token_positions[value][0])
        for value in text_positions.keys() & token_positions.keys()
        if len(text_positions[value]) == len(token_positions[value]) == 1
    ]
    anchors.sort()
    monotone: list[tuple[int, int]] = []
    last_token = -1
    for text_index, token_index in anchors:
        if token_index > last_token:
            monotone.append((text_index, token_index))
            last_token = token_index
    mapping = [-1] * len(text)
    text_cursor = token_cursor = 0
    for text_anchor, token_anchor in monotone + [(len(text), len(tokens))]:
        gap = _sequence_mapping(
            text[text_cursor:text_anchor],
            tokens[token_cursor:token_anchor],
        )
        for index, token_index in enumerate(gap):
            if token_index >= 0:
                mapping[text_cursor + index] = token_cursor + token_index
        if text_anchor < len(text):
            mapping[text_anchor] = token_anchor
        text_cursor, token_cursor = text_anchor + 1, token_anchor + 1
    return mapping


def _block_difflib_mapping(fixture: Fixture) -> list[int]:
    return _sequence_mapping(fixture.text_units, fixture.token_units, 512)


def _rapidfuzz_mapping(fixture: Fixture) -> list[int]:
    try:
        from rapidfuzz.distance import Levenshtein
    except ImportError as exc:
        raise RuntimeError(
            "rapidfuzz is required; run with "
            "`uv run --with rapidfuzz ...`"
        ) from exc
    mapping = [-1] * len(fixture.text_units)
    opcodes = Levenshtein.opcodes(fixture.text_units, fixture.token_units)
    for opcode in opcodes:
        if opcode.tag == "equal":
            for offset in range(opcode.src_end - opcode.src_start):
                mapping[opcode.src_start + offset] = (
                    opcode.dest_start + offset
                )
    return mapping


Candidate = Callable[[Fixture], tuple[list[int], dict]]


def _run_anchor(fixture: Fixture) -> tuple[list[int], dict]:
    return _anchor_mapping(fixture)


def _run_unique(fixture: Fixture) -> tuple[list[int], dict]:
    return _unique_layer_mapping(fixture), {}


def _run_blocks(fixture: Fixture) -> tuple[list[int], dict]:
    return _block_difflib_mapping(fixture), {}


def _run_library(fixture: Fixture) -> tuple[list[int], dict]:
    return _rapidfuzz_mapping(fixture), {"library": "rapidfuzz"}


CANDIDATES: dict[str, Candidate] = {
    "anchor_postcheck": _run_anchor,
    "unique_token_blocks": _run_unique,
    "blocked_difflib": _run_blocks,
    "rapidfuzz_levenshtein": _run_library,
}


def _evaluate(mapping: Sequence[int], fixture: Fixture) -> dict:
    correct = missing = 0
    errors: list[float] = []
    for index, token_index in enumerate(mapping):
        if token_index < 0 or token_index >= len(fixture.token_times):
            missing += 1
            continue
        error = abs(fixture.token_times[token_index] - fixture.expected_times[index])
        errors.append(error)
        if error <= 1e-9:
            correct += 1
    starts = [
        fixture.token_times[mapping[index]]
        if 0 <= mapping[index] < len(fixture.token_times) else None
        for index in fixture.phrase_starts[:3]
    ]
    return {
        "correct_pct": round(100 * correct / len(mapping), 3),
        "missing": missing,
        "max_error_seconds": round(max(errors, default=0.0), 6),
        "phrase_starts": starts,
    }


def _bench(candidate: Candidate, fixture: Fixture, runs: int) -> dict:
    samples: list[float] = []
    peaks: list[int] = []
    result: dict = {}
    for _ in range(runs):
        gc.collect()
        tracemalloc.start()
        started = time.perf_counter()
        mapping, diagnostics = candidate(fixture)
        elapsed = time.perf_counter() - started
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        samples.append(elapsed * 1000)
        peaks.append(peak)
        result = {**_evaluate(mapping, fixture), **diagnostics}
    p95_index = min(len(samples) - 1, math.ceil(0.95 * len(samples)) - 1)
    return {
        **result,
        "p50_ms": round(statistics.median(samples), 3),
        "p95_ms": round(sorted(samples)[p95_index], 3),
        "peak_python_mib": round(max(peaks) / 1024 / 1024, 3),
    }


def _package_info() -> dict:
    info: dict[str, dict] = {}
    for name in ("rapidfuzz", "Levenshtein", "edlib"):
        try:
            distribution = importlib.metadata.distribution(name)
            size = sum(
                file_path.stat().st_size
                for file in distribution.files or []
                if (file_path := distribution.locate_file(file)).is_file()
            )
            info[name] = {
                "version": distribution.version,
                "installed_mib": round(size / 1024 / 1024, 3),
            }
        except importlib.metadata.PackageNotFoundError:
            info[name] = {"version": "missing"}
    return info


def _print_reproduction() -> None:
    phrase = "the quick brown fox jumps over lazy dog today now".split()
    units = ["intro", "line", "here"] + phrase * 3 + ["outro", "line", "here"]
    starts = [3, 13, 23]
    exact = Fixture(
        name="repro_exact",
        text=_render(units),
        text_units=units,
        token_units=list(units),
        token_times=[float(index) for index in range(len(units))],
        expected_times=[float(index) for index in range(len(units))],
        phrase_starts=starts,
        drift=False,
    )
    drift_tokens = list(units)
    drift_tokens.insert(3, "extra")
    drift = Fixture(
        name="repro_drift",
        text=_render(units),
        text_units=units,
        token_units=drift_tokens,
        token_times=[float(index) for index in range(len(drift_tokens))],
        expected_times=[float(index) for index in range(len(units))],
        phrase_starts=starts,
        drift=True,
    )
    for fixture in (exact, drift):
        mapping, diagnostics = _anchor_mapping(fixture)
        values = [
            fixture.token_times[mapping[index]]
            for index in fixture.phrase_starts
        ]
        print("repro", json.dumps({
            "case": fixture.name,
            "canonical_equal": diagnostics["canonical_equal"],
            "expected_starts": [
                fixture.expected_times[index] for index in starts
            ],
            "observed_starts": values,
            "postcheck_flag": diagnostics["postcheck_flag"],
        }, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-chars", type=int, default=120_000)
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args()
    if args.runs < 3:
        raise ValueError("--runs must be at least 3 for p50/p95")
    fixtures = make_fixtures(args.target_chars)
    _print_reproduction()
    print("machine", json.dumps({
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
    }, sort_keys=True))
    print("packages", json.dumps(_package_info(), sort_keys=True))
    print("fixtures", json.dumps([
        {
            "name": fixture.name,
            "chars": len(fixture.text),
            "text_units": len(fixture.text_units),
            "tokens": len(fixture.token_units),
            "drift": fixture.drift,
        }
        for fixture in fixtures
    ], sort_keys=True))
    for fixture in fixtures:
        for name, candidate in CANDIDATES.items():
            result = _bench(candidate, fixture, args.runs)
            print("result", json.dumps({
                "candidate": name,
                "fixture": fixture.name,
                **result,
            }, sort_keys=True))
    for fixture in fixtures:
        if not fixture.drift:
            continue
        for anchor_size in (12, 24, 32):
            result = _bench(
                lambda item, size=anchor_size: _anchor_mapping(item, size),
                fixture,
                max(3, args.runs),
            )
            print("anchor_size", json.dumps({
                "fixture": fixture.name,
                "anchor_size": anchor_size,
                **result,
            }, sort_keys=True))


if __name__ == "__main__":
    main()
