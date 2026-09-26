#!/usr/bin/env python3
"""Check saved LLM artifact bytes against their per-layer provenance records."""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional


_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LAYER_FILES = {
    "calibration": "llm_calibrated.txt",
    "summary": "llm_summary.txt",
}
_RECORD_FIELDS = (
    "schema_version",
    "recipe_version",
    "source_fingerprint",
    "output_sha256",
    "generated_at",
    "producer_version",
    "generation_kind",
    "requested_model",
    "actual_model",
    "prompt_version",
)


def _public_record_fields(record: Dict[str, Any]) -> Dict[str, Any]:
    """Return only documented source labels and hashes, never cached content."""
    public_record = {
        key: record[key]
        for key in _RECORD_FIELDS
        if key in record and isinstance(record[key], (str, int, float, bool, type(None)))
    }
    return public_record


def _layer_report(
    cache_dir: Path,
    layer: str,
    record: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compare one recorded output SHA-256 with the current TXT file bytes."""
    if record is None:
        return {"status": "unknown", "record": None}

    output_path = cache_dir / _LAYER_FILES[layer]
    if not output_path.is_file():
        status = "mismatch"
    else:
        expected_hash = record.get("output_sha256")
        source_hash = record.get("source_fingerprint")
        if (
            not isinstance(expected_hash, str)
            or _HEX_SHA256.fullmatch(expected_hash) is None
            or not isinstance(source_hash, str)
            or _HEX_SHA256.fullmatch(source_hash) is None
            or record.get("schema_version") != "artifact-record-v1"
        ):
            status = "mismatch"
        else:
            actual_hash = hashlib.sha256(output_path.read_bytes()).hexdigest()
            status = "verified_output" if actual_hash == expected_hash else "mismatch"
    return {"status": status, "record": _public_record_fields(record)}


def _read_artifact_provenance(cache_dir: Path) -> Dict[str, Dict[str, Any]]:
    """Read one media leaf's provenance; missing legacy records stay unknown."""
    status_path = cache_dir / "llm_status.json"
    if not status_path.exists():
        return {}
    try:
        status_data = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read llm_status.json: {exc}") from exc
    if not isinstance(status_data, dict):
        raise ValueError("llm_status.json must contain a JSON object")

    provenance = status_data.get("artifact_provenance")
    if provenance is None:
        return {}
    if not isinstance(provenance, dict):
        raise ValueError("artifact_provenance must contain a JSON object")
    if provenance.get("schema_version") != "artifact-provenance-v1":
        raise ValueError("unsupported artifact_provenance schema_version")
    layers = provenance.get("layers")
    if not isinstance(layers, dict):
        raise ValueError("artifact_provenance.layers must contain a JSON object")
    records = {}
    for layer in _LAYER_FILES:
        record = layers.get(layer)
        if record is None:
            continue
        if not isinstance(record, dict):
            raise ValueError(f"artifact provenance for {layer} must be a JSON object")
        records[layer] = record
    return records


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Check TXT bytes against provenance for one media artifact leaf directory. "
            "This does not verify semantic accuracy or current-input equivalence."
        )
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        required=True,
        help="one media artifact leaf directory (not the cache root; no recursive scan)",
    )
    args = parser.parse_args(argv)
    if not args.cache_dir.is_dir():
        print("--cache-dir must name an existing media artifact leaf directory", file=sys.stderr)
        return 2
    try:
        records = _read_artifact_provenance(args.cache_dir)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        layers = {
            layer: _layer_report(args.cache_dir, layer, records.get(layer))
            for layer in _LAYER_FILES
        }
    except OSError as exc:
        print(f"cannot read artifact TXT file: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"schema_version": "artifact-provenance-report-v1", "layers": layers}, ensure_ascii=False))
    return 1 if any(item["status"] == "mismatch" for item in layers.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
