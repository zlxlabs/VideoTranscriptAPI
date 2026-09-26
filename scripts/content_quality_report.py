#!/usr/bin/env python3
"""Summarize human content reviews; this command does not judge text meaning."""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

DIMENSIONS = ("numbers", "negation", "conditions", "speaker_attribution", "key_omissions")
OUTCOMES = ("correct", "error", "unknown", "unreviewed")
SOURCES = ("synthetic", "youtube", "tiktok", "bilibili", "podcast", "other")


def _need(ok, message):
    if not ok:
        raise ValueError(message)


def _object(value, fields, where):
    if type(value) is dict and "reviewer" in value and "reviewer" not in fields:
        raise ValueError("reviewer is file-scoped; mixed reviewer records are unsupported")
    _need(type(value) is dict and set(value) == set(fields), f"{where}: expected fields {sorted(fields)}")
    return value


def _text(value, where):
    _need(isinstance(value, str) and bool(value.strip()), f"{where}: expected non-empty text")


def summarize(data):
    _object(data, ("schema_version", "reviewer", "samples"), "root")
    _need(type(data["schema_version"]) is int and data["schema_version"] == 1, "schema_version must be 1")
    _text(data["reviewer"], "reviewer")
    _need(isinstance(data["samples"], list) and data["samples"], "samples must be a non-empty list")
    seen_samples, seen_pairs, versions = set(), set(), {}
    for sample in data["samples"]:
        _object(sample, ("sample_id", "source_type", "input_ref", "input_text", "candidates"), "sample")
        sample_id = sample["sample_id"]
        _text(sample_id, "sample_id")
        _need(sample_id not in seen_samples, f"duplicate sample_id: {sample_id}")
        seen_samples.add(sample_id)
        _need(sample["source_type"] in SOURCES, f"unknown source_type: {sample['source_type']}")
        _text(sample["input_ref"], f"{sample_id}.input_ref")
        _text(sample["input_text"], f"{sample_id}.input_text")
        _need(isinstance(sample["candidates"], list) and sample["candidates"], f"{sample_id}.candidates must be non-empty")
        for candidate in sample["candidates"]:
            _object(candidate, ("version", "version_note", "generated_at", "generation_note", "output_ref", "output_text", "dimensions"), "candidate")
            version = candidate["version"]
            _text(version, f"{sample_id}.version")
            _need(isinstance(candidate["version_note"], str), "version_note must be text")
            generated = candidate["generated_at"]
            _need(isinstance(generated, str), "generated_at must be an ISO date or unknown")
            if generated == "unknown":
                _text(candidate["generation_note"], "generation_note")
            else:
                _need(isinstance(candidate["generation_note"], str), "generation_note must be text")
                try:
                    _need(date.fromisoformat(generated).isoformat() == generated, "generated_at must be YYYY-MM-DD")
                except ValueError as exc:
                    raise ValueError(f"{sample_id}: invalid generated_at") from exc
            if version == "unknown":
                _text(candidate["version_note"], "version_note")
            _text(candidate["output_ref"], f"{sample_id}.output_ref")
            _text(candidate["output_text"], f"{sample_id}.output_text")
            pair = (sample_id, version, data["reviewer"])
            _need(pair not in seen_pairs, f"duplicate sample/candidate/reviewer: {pair}")
            seen_pairs.add(pair)
            dimensions = _object(candidate["dimensions"], DIMENSIONS, f"{sample_id}.dimensions")
            row = versions.setdefault(version, {"samples": set(), "counts": {d: dict.fromkeys(OUTCOMES, 0) for d in DIMENSIONS}, "metadata_notes": []})
            row["samples"].add(sample_id)
            if version == "unknown" or generated == "unknown":
                note = {"sample_id": sample_id}
                if version == "unknown":
                    note["version_note"] = candidate["version_note"]
                if generated == "unknown":
                    note["generation_note"] = candidate["generation_note"]
                row["metadata_notes"].append(note)
            for dimension in DIMENSIONS:
                review = _object(dimensions[dimension], ("outcome", "evidence"), f"{sample_id}.{dimension}")
                outcome, evidence = review["outcome"], review["evidence"]
                _need(outcome in OUTCOMES, f"unknown outcome for {dimension}: {outcome}")
                if outcome == "error":
                    _object(evidence, ("input", "output"), f"{sample_id}.{dimension}.evidence")
                    _text(evidence["input"], "evidence.input")
                    _text(evidence["output"], "evidence.output")
                else:
                    _need(evidence is None, f"{sample_id}.{dimension}: evidence is required only for errors")
                row["counts"][dimension][outcome] += 1
    report = {"reviewer": data["reviewer"], "versions": {}}
    for version, row in sorted(versions.items()):
        dimensions = {}
        for name, counts in row["counts"].items():
            correct, errors = counts["correct"], counts["error"]
            scored = correct + errors
            dimensions[name] = {
                "reviewed": scored + counts["unknown"], "scored": scored, "correct": correct,
                "errors": errors, "unknown": counts["unknown"],
                "unreviewed": counts["unreviewed"],
                "error_rate": errors / scored if scored else None,
            }
        report["versions"][version] = {
            "sample_count": len(row["samples"]), "dimensions": dimensions,
            "metadata_notes": row["metadata_notes"],
        }
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reviews", required=True, type=Path, help="human review JSON file")
    args = parser.parse_args()
    try:
        report = summarize(json.loads(args.reviews.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"content quality report: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
