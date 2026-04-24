"""Tests for normalize_reasoning_effort.

Covers 2026 whitelist expansion:
  - New values: "disabled", "minimal", "max", "xhigh"
  - Case normalization: "HIGH" -> "high"
  - Legacy migration: "none" -> "disabled" (semantic preservation)
  - Whitelist enforcement: "ludicrous" -> None + warn
"""
import logging

import pytest

from video_transcript_api.llm import normalize_reasoning_effort


class TestNormalizeReasoningEffortValidValues:
    """Happy path: valid whitelist values."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("disabled", "disabled"),
            ("minimal", "minimal"),
            ("low", "low"),
            ("medium", "medium"),
            ("high", "high"),
            ("max", "max"),
            ("xhigh", "xhigh"),
        ],
    )
    def test_valid_lowercase_passthrough(self, value, expected):
        assert normalize_reasoning_effort(value) == expected

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("HIGH", "high"),
            ("Low", "low"),
            ("MEDIUM", "medium"),
            ("Disabled", "disabled"),
            ("MiNiMaL", "minimal"),
        ],
    )
    def test_case_insensitive_normalization(self, value, expected):
        assert normalize_reasoning_effort(value) == expected

    def test_whitespace_stripped(self):
        assert normalize_reasoning_effort("  high  ") == "high"


class TestNormalizeReasoningEffortNoneAndDefault:
    """None / empty / null variants."""

    def test_none_returns_none(self):
        assert normalize_reasoning_effort(None) is None

    def test_empty_string_returns_none(self):
        assert normalize_reasoning_effort("") is None

    def test_whitespace_only_returns_none(self):
        assert normalize_reasoning_effort("   ") is None

    def test_null_string_returns_none(self):
        assert normalize_reasoning_effort("null") is None

    def test_null_string_case_insensitive(self):
        assert normalize_reasoning_effort("NULL") is None


class TestNormalizeReasoningEffortLegacyMigration:
    """REGRESSION: legacy 'none' must map to 'disabled', not None.

    Prior contract: 'none' meant 'disable thinking' on Gemini 2.5.
    New contract: 'none' maps to 'disabled', dispatcher translates per provider.
    A silent map to None would change DeepSeek V4 behavior from 'default enabled@high'
    to 'default enabled@high' (no-op) but would change Gemini 2.5 behavior from
    'disabled' to 'default' (semantic break).
    """

    def test_legacy_none_maps_to_disabled(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = normalize_reasoning_effort("none")
        assert result == "disabled"

    def test_legacy_none_emits_deprecation_warning(self, caplog):
        with caplog.at_level(logging.WARNING):
            normalize_reasoning_effort("none")
        text = caplog.text.lower()
        assert "deprecated" in text or "legacy" in text

    def test_legacy_none_case_insensitive(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert normalize_reasoning_effort("NONE") == "disabled"
            assert normalize_reasoning_effort("None") == "disabled"


class TestNormalizeReasoningEffortInvalidValues:
    """Unknown values: warn + return None."""

    def test_unknown_value_returns_none(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = normalize_reasoning_effort("ludicrous")
        assert result is None

    def test_unknown_value_emits_warning(self, caplog):
        with caplog.at_level(logging.WARNING):
            normalize_reasoning_effort("ludicrous")
        assert "ludicrous" in caplog.text

    def test_non_string_returns_none(self):
        assert normalize_reasoning_effort(42) is None
        assert normalize_reasoning_effort([]) is None
        assert normalize_reasoning_effort({}) is None

    def test_negative_number_returns_none(self):
        assert normalize_reasoning_effort(-1) is None
