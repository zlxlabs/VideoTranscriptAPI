"""
Terminology database unit tests.

Covers:
- Loading default terms from JSON
- Loading custom terms
- Query matching (case-insensitive)
- Prompt formatting
- Graceful degradation on bad JSON

All console output must be in English only (no emoji, no Chinese).
"""

import json
import os
import sys

import pytest


from video_transcript_api.terminology.terminology_db import TerminologyDB


@pytest.fixture
def default_db():
    """Create a TerminologyDB with default terms only."""
    return TerminologyDB()


@pytest.fixture
def custom_terms_file(tmp_path):
    """Create a temporary custom terms JSON file."""
    data = {
        "version": 1,
        "terms": [
            {"incorrect": "Russ", "correct": "Rust", "category": "tech"},
            {"incorrect": "pythen", "correct": "Python", "category": "tech"},
        ]
    }
    path = tmp_path / "custom_terms.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(path)


class TestTerminologyLoading:
    """Verify term loading from JSON files."""

    def test_default_terms_loaded(self, default_db):
        """Default terms file should load successfully."""
        assert len(default_db.terms) > 0

    def test_default_terms_have_required_fields(self, default_db):
        """Each term should have incorrect, correct, and category fields."""
        for term in default_db.terms:
            assert "incorrect" in term
            assert "correct" in term
            assert "category" in term

    def test_custom_terms_merged(self, custom_terms_file):
        """Custom terms should be merged with default terms."""
        db = TerminologyDB(custom_path=custom_terms_file)
        # Should have default + custom terms
        assert len(db.terms) > 2
        # Custom terms should be present
        correct_values = [t["correct"] for t in db.terms]
        assert "Rust" in correct_values
        assert "Python" in correct_values

    def test_missing_custom_file_graceful(self):
        """Missing custom file should not raise, just log warning."""
        db = TerminologyDB(custom_path="/nonexistent/path/terms.json")
        # Should still have default terms
        assert len(db.terms) > 0

    def test_invalid_json_graceful(self, tmp_path):
        """Invalid JSON should not raise."""
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("not json at all", encoding="utf-8")
        db = TerminologyDB(custom_path=str(bad_file))
        # Should still have default terms
        assert len(db.terms) > 0

    def test_invalid_term_entries_skipped(self, tmp_path):
        """Terms missing required fields should be skipped."""
        data = {
            "version": 1,
            "terms": [
                {"incorrect": "good", "correct": "Good"},
                {"only_incorrect": "bad"},  # Missing correct
                {"correct": "also bad"},  # Missing incorrect
            ]
        }
        path = tmp_path / "partial.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        db = TerminologyDB(custom_path=str(path))
        custom_terms = [t for t in db.terms if t["correct"] == "Good"]
        assert len(custom_terms) == 1


class TestTerminologyQuery:
    """Verify query matching behavior."""

    def test_case_insensitive_match(self, default_db):
        """Query should be case-insensitive."""
        result = default_db.query("I use chatgpt every day")
        correct_values = [t["correct"] for t in result]
        assert "ChatGPT" in correct_values

    def test_no_match_returns_empty(self, default_db):
        """No matches should return empty list."""
        result = default_db.query("nothing special here")
        assert result == []

    def test_empty_text_returns_empty(self, default_db):
        """Empty text should return empty list."""
        assert default_db.query("") == []
        assert default_db.query(None) == []

    def test_multiple_matches(self, default_db):
        """Multiple terms in text should all be matched."""
        result = default_db.query("I use chatgpt and pytorch for my work on github")
        correct_values = [t["correct"] for t in result]
        assert "ChatGPT" in correct_values
        assert "PyTorch" in correct_values
        assert "GitHub" in correct_values

    def test_no_duplicate_corrections(self, default_db):
        """Same correct term should not appear twice."""
        # "github" and "Github" both map to "GitHub"
        result = default_db.query("check Github and github repos")
        github_entries = [t for t in result if t["correct"] == "GitHub"]
        assert len(github_entries) == 1

    def test_export_correct_terms_uses_only_correct_spellings_in_order(self):
        db = TerminologyDB()
        db.terms = [
            {"incorrect": "Russt", "correct": "Rust", "category": "tech"},
            {"incorrect": "Pythen", "correct": "Python", "category": "tech"},
            {"incorrect": "Rustt", "correct": "Rust", "category": "tech"},
            {"incorrect": "invalid", "correct": 42, "category": "tech"},
        ]

        assert db.export_correct_terms() == ["Rust", "Python", "Rust"]


class TestTerminologyPromptFormat:
    """Verify prompt formatting output."""

    def test_format_for_prompt_with_matches(self, default_db):
        """Matched terms should produce formatted output."""
        matched = default_db.query("using pytorch and chatgpt")
        text = default_db.format_for_prompt(matched)
        assert "tech" in text.lower() or "PyTorch" in text
        assert len(text) > 0

    def test_format_for_prompt_empty(self, default_db):
        """No matches should return empty string."""
        text = default_db.format_for_prompt([])
        assert text == ""

    def test_format_matched_for_prompt(self, default_db):
        """Combined query+format should work."""
        text = default_db.format_matched_for_prompt("using chatgpt daily")
        assert "ChatGPT" in text

    def test_format_grouped_by_category(self, default_db):
        """Output should group terms by category."""
        matched = [
            {"incorrect": "a", "correct": "A", "category": "tech"},
            {"incorrect": "b", "correct": "B", "category": "brand"},
        ]
        text = default_db.format_for_prompt(matched)
        assert "tech" in text
        assert "brand" in text
