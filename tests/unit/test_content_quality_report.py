import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "content_quality_report.py"
FIXTURE = ROOT / "tests" / "fixtures" / "content_quality" / "synthetic_review.json"
DIMS = ("numbers", "negation", "conditions", "speaker_attribution", "key_omissions")


def _run(path):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--reviews", str(path)],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )


def _review(outcome):
    return {
        "schema_version": 1,
        "reviewer": "reviewer-test",
        "samples": [{
            "sample_id": "synthetic-null",
            "source_type": "synthetic",
            "input_ref": "synthetic://input/1",
            "input_text": "synthetic input",
            "candidates": [{
                "version": "only-unknown",
                "version_note": "",
                "generated_at": "2026-09-25",
                "generation_note": "",
                "output_ref": "synthetic://output/1",
                "output_text": "synthetic output",
                "dimensions": {d: {"outcome": outcome, "evidence": None} for d in DIMS},
            }],
        }],
    }


def test_synthetic_fixture_reports_exact_counts_and_metadata():
    result = _run(FIXTURE)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert set(report) == {"reviewer", "versions"}
    versions = report["versions"]
    assert versions["candidate-synthetic-v1"]["sample_count"] == 3
    assert versions["candidate-synthetic-v1"]["dimensions"]["numbers"] == {
        "reviewed": 2, "scored": 2, "correct": 0, "errors": 2,
        "unknown": 0, "unreviewed": 1, "error_rate": 1.0,
    }
    assert versions["candidate-synthetic-v1"]["dimensions"]["conditions"] == {
        "reviewed": 3, "scored": 3, "correct": 2, "errors": 1, "unknown": 0, "unreviewed": 0,
        "error_rate": 1 / 3,
    }
    assert versions["candidate-synthetic-v2"]["sample_count"] == 3
    assert versions["unknown"]["sample_count"] == 1
    assert versions["unknown"]["metadata_notes"] == [{
        "sample_id": "syn-003", "version_note": "Synthetic candidate label unavailable.",
        "generation_note": "Synthetic generation date unavailable.",
    }]


def test_unknown_and_unreviewed_are_not_scored_as_correct_and_zero_denominator_is_null(tmp_path):
    path = tmp_path / "reviews.json"
    path.write_text(json.dumps(_review("unknown")), encoding="utf-8")
    result = _run(path)
    assert result.returncode == 0, result.stderr
    dimensions = json.loads(result.stdout)["versions"]["only-unknown"]["dimensions"]
    assert dimensions["numbers"] == {
        "reviewed": 1, "scored": 0, "correct": 0, "errors": 0,
        "unknown": 1, "unreviewed": 0, "error_rate": None,
    }


def test_duplicate_sample_candidate_reviewer_is_rejected(tmp_path):
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    sample = payload["samples"][0]
    sample["candidates"].append(sample["candidates"][0])
    path = tmp_path / "duplicate.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = _run(path)
    assert result.returncode != 0
    assert "duplicate" in result.stderr.lower()


def test_reviewer_must_be_file_scoped(tmp_path):
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["samples"][0]["candidates"][0]["reviewer"] = "second-reviewer"
    path = tmp_path / "mixed-reviewers.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = _run(path)
    assert result.returncode != 0
    assert "mixed reviewer" in result.stderr.lower()


def test_error_requires_input_and_output_evidence_positions(tmp_path):
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["samples"][0]["candidates"][0]["dimensions"]["numbers"]["evidence"]["output"] = ""
    path = tmp_path / "missing-evidence.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = _run(path)
    assert result.returncode != 0
    assert result.stdout == ""


def test_bad_schema_and_unknown_enum_exit_nonzero(tmp_path):
    cases = [{"schema_version": 9, "reviewer": "r", "samples": []}, _review("perfect"), _review("perfect")]
    cases[1]["samples"][0]["source_type"] = "invented-source"
    cases[2]["samples"][0]["source_type"] = "synthetic"
    for index, payload in enumerate(cases):
        path = tmp_path / f"bad-{index}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        result = _run(path)
        assert result.returncode != 0
        assert result.stdout == ""
        assert result.stderr
