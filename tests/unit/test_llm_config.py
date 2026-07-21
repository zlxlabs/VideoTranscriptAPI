"""
LLMConfig unit tests.

Covers:
- Default values
- from_dict parsing from config dictionary
- get_models
- Edge cases (missing keys, partial config)

All console output must be in English only (no emoji, no Chinese).
"""

import pytest
from video_transcript_api.llm.core.config import LLMConfig


class TestLLMConfigDefaults:
    """Test LLMConfig default values."""

    def test_required_fields(self):
        """Should require api_key, base_url, calibrate_model, summary_model."""
        config = LLMConfig(
            api_key="key",
            base_url="https://api.test.com",
            calibrate_model="model-a",
            summary_model="model-b",
        )
        assert config.api_key == "key"
        assert config.calibrate_model == "model-a"

    def test_default_retry_values(self):
        """Default retry config should be sensible."""
        config = LLMConfig(
            api_key="k", base_url="u",
            calibrate_model="m", summary_model="s",
        )
        assert config.max_retries == 3
        assert config.retry_delay == 5

    def test_default_segment_sizes(self):
        """Default segmentation config should be set."""
        config = LLMConfig(
            api_key="k", base_url="u",
            calibrate_model="m", summary_model="s",
        )
        assert config.segment_size == 2000
        assert config.max_segment_size == 3000
        assert config.enable_threshold == 5000

    def test_default_quality_weights(self):
        """Default quality score weights should sum to 1.0."""
        config = LLMConfig(
            api_key="k", base_url="u",
            calibrate_model="m", summary_model="s",
        )
        total = sum(config.quality_score_weights.values())
        assert abs(total - 1.0) < 0.01


class TestLLMConfigFromDict:
    """Test from_dict parsing."""

    def test_basic_config(self):
        """Should parse basic config dict."""
        config_dict = {
            "llm": {
                "api_key": "test-key",
                "base_url": "https://api.test.com",
                "calibrate_model": "deepseek-v4-flash",
                "summary_model": "deepseek-v4-pro",
            }
        }
        config = LLMConfig.from_dict(config_dict)
        assert config.api_key == "test-key"
        assert config.calibrate_model == "deepseek-v4-flash"
        assert config.summary_model == "deepseek-v4-pro"

    def test_old_risk_fields_ignored(self):
        """Old risk model fields in config should be silently ignored."""
        config_dict = {
            "llm": {
                "api_key": "k",
                "base_url": "u",
                "calibrate_model": "normal",
                "summary_model": "normal-summary",
                "risk_calibrate_model": "risk-model",
                "enable_risk_model_selection": True,
            }
        }
        config = LLMConfig.from_dict(config_dict)
        assert config.calibrate_model == "normal"
        assert not hasattr(config, "risk_calibrate_model")

    def test_segmentation_config(self):
        """Should parse segmentation config."""
        config_dict = {
            "llm": {
                "api_key": "k", "base_url": "u",
                "calibrate_model": "m", "summary_model": "s",
                "segmentation": {
                    "segment_size": 1500,
                    "max_segment_size": 2500,
                },
            }
        }
        config = LLMConfig.from_dict(config_dict)
        assert config.segment_size == 1500
        assert config.max_segment_size == 2500

    def test_missing_optional_fields_use_defaults(self):
        """Missing optional fields should use defaults."""
        config_dict = {
            "llm": {
                "api_key": "k", "base_url": "u",
                "calibrate_model": "m", "summary_model": "s",
            }
        }
        config = LLMConfig.from_dict(config_dict)
        assert config.max_retries == 3
        assert config.concurrent_workers == 10


class TestLLMConfigSpeakerInference:
    """Test speaker_inference sub-config parsing (per-speaker sampling + confidence gate)."""

    def test_defaults_when_section_missing(self):
        """No speaker_inference section -> falls back to documented defaults."""
        config_dict = {
            "llm": {
                "api_key": "k", "base_url": "u",
                "calibrate_model": "m", "summary_model": "s",
            }
        }
        config = LLMConfig.from_dict(config_dict)
        assert config.speaker_samples_per_speaker == 3
        assert config.speaker_max_chars_per_speaker == 400
        assert config.speaker_context_dialogs == 2
        assert config.speaker_confidence_threshold == 0.6

    def test_explicit_overrides_are_parsed(self):
        """Explicit speaker_inference values in config dict must override defaults."""
        config_dict = {
            "llm": {
                "api_key": "k", "base_url": "u",
                "calibrate_model": "m", "summary_model": "s",
                "speaker_inference": {
                    "samples_per_speaker": 5,
                    "max_chars_per_speaker": 600,
                    "context_dialogs": 4,
                    "confidence_threshold": 0.75,
                },
            }
        }
        config = LLMConfig.from_dict(config_dict)
        assert config.speaker_samples_per_speaker == 5
        assert config.speaker_max_chars_per_speaker == 600
        assert config.speaker_context_dialogs == 4
        assert config.speaker_confidence_threshold == 0.75

    def test_dataclass_defaults_match_from_dict_defaults(self):
        """Constructing LLMConfig directly (no from_dict) must use the same defaults."""
        config = LLMConfig(
            api_key="k", base_url="u",
            calibrate_model="m", summary_model="s",
        )
        assert config.speaker_samples_per_speaker == 3
        assert config.speaker_max_chars_per_speaker == 400
        assert config.speaker_context_dialogs == 2
        assert config.speaker_confidence_threshold == 0.6


class TestLLMConfigChapters:
    """Test chapters_* config fields (章节梗概生成器配置)."""

    def test_dataclass_defaults(self):
        """Direct construction (no from_dict) must expose sensible chapters defaults."""
        config = LLMConfig(
            api_key="k", base_url="u",
            calibrate_model="m", summary_model="s",
        )
        assert config.chapters_model is None
        assert config.chapters_reasoning_effort is None
        assert config.min_chapters_threshold == 10000
        assert config.max_chapters_input_chars == 500000

    def test_from_dict_defaults_fallback_to_calibrate_model(self):
        """chapters_model missing -> falls back to calibrate_model, like key_info_model/speaker_model."""
        config_dict = {
            "llm": {
                "api_key": "k", "base_url": "u",
                "calibrate_model": "calib-model", "summary_model": "s",
            }
        }
        config = LLMConfig.from_dict(config_dict)
        assert config.chapters_model == "calib-model"
        assert config.min_chapters_threshold == 10000
        assert config.max_chapters_input_chars == 500000

    def test_from_dict_explicit_overrides(self):
        """Explicit chapters_* values in config dict must override defaults."""
        config_dict = {
            "llm": {
                "api_key": "k", "base_url": "u",
                "calibrate_model": "calib-model", "summary_model": "s",
                "chapters_model": "chapters-model",
                "chapters_reasoning_effort": "high",
                "min_chapters_threshold": 5000,
                "max_chapters_input_chars": 200000,
            }
        }
        config = LLMConfig.from_dict(config_dict)
        assert config.chapters_model == "chapters-model"
        assert config.chapters_reasoning_effort == "high"
        assert config.min_chapters_threshold == 5000
        assert config.max_chapters_input_chars == 200000


class TestLLMConfigPlainStructured:
    """Test plain-source structured calibration + paragraphization config fields."""

    def test_dataclass_defaults(self):
        """Direct construction (no from_dict) must expose the new fields with defaults."""
        config = LLMConfig(
            api_key="k", base_url="u",
            calibrate_model="m", summary_model="s",
        )
        assert config.structured_calibration_for_plain is False
        assert config.plain_structured_preferred_chunk_length == 3000
        assert config.plain_structured_max_chunk_length == 4000
        assert config.paragraphization_target_chars == 300
        assert config.paragraphization_hard_max_chars == 600
        assert config.paragraphization_pause_threshold_seconds == 2.0

    def test_from_dict_defaults(self):
        """Missing keys -> defaults (dark-launch switch stays off)."""
        config_dict = {
            "llm": {
                "api_key": "k", "base_url": "u",
                "calibrate_model": "m", "summary_model": "s",
            }
        }
        config = LLMConfig.from_dict(config_dict)
        assert config.structured_calibration_for_plain is False
        assert config.plain_structured_preferred_chunk_length == 3000
        assert config.plain_structured_max_chunk_length == 4000
        assert config.paragraphization_target_chars == 300
        assert config.paragraphization_hard_max_chars == 600
        assert config.paragraphization_pause_threshold_seconds == 2.0

    def test_from_dict_explicit_overrides(self):
        """Explicit values in the three key groups must override defaults."""
        config_dict = {
            "llm": {
                "api_key": "k", "base_url": "u",
                "calibrate_model": "m", "summary_model": "s",
                "structured_calibration_for_plain": True,
                "structured_calibration": {
                    "plain_preferred_chunk_length": 2500,
                    "plain_max_chunk_length": 3500,
                },
                "paragraphization": {
                    "target_chars": 400,
                    "hard_max_chars": 800,
                    "pause_threshold_seconds": 1.5,
                },
            }
        }
        config = LLMConfig.from_dict(config_dict)
        assert config.structured_calibration_for_plain is True
        assert config.plain_structured_preferred_chunk_length == 2500
        assert config.plain_structured_max_chunk_length == 3500
        assert config.paragraphization_target_chars == 400
        assert config.paragraphization_hard_max_chars == 800
        assert config.paragraphization_pause_threshold_seconds == 1.5

    def test_positional_args_compatibility(self):
        """New fields appended at the end must not break positional construction."""
        config = LLMConfig("k", "u", "m", "s")
        assert config.api_key == "k"
        assert config.summary_model == "s"
        assert config.structured_calibration_for_plain is False


class TestLLMConfigGetModels:
    """Test get_models method."""

    def test_returns_configured_models(self):
        """get_models should return all configured models."""
        config = LLMConfig(
            api_key="k", base_url="u",
            calibrate_model="cal-model",
            summary_model="sum-model",
        )
        models = config.get_models()
        assert models["calibrate_model"] == "cal-model"
        assert models["summary_model"] == "sum-model"

    def test_no_has_risk_in_result(self):
        """get_models result should not contain has_risk field."""
        config = LLMConfig(
            api_key="k", base_url="u",
            calibrate_model="m", summary_model="s",
        )
        models = config.get_models()
        assert "has_risk" not in models
