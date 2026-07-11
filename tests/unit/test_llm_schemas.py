"""
Unit tests for LLM schemas, prompt builders, and QualityValidator.

Covers:
- JSON Schema structure validation (calibration, speaker_mapping, validation, unified_validation, key_info)
- Prompt building functions (unified_validation, validation, calibrate, speaker_inference, key_info)
- QualityValidator: validate_by_length, _check_threshold
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ============================================================
# Schema imports
# ============================================================
from src.video_transcript_api.llm.schemas.calibration import CALIBRATION_RESULT_SCHEMA
# SPEAKER_MAPPING_SCHEMA 唯一定义在 prompts.schemas（llm.schemas 中的重复定义已删除）
from src.video_transcript_api.llm.prompts.schemas.speaker_mapping import SPEAKER_MAPPING_SCHEMA
from src.video_transcript_api.llm.schemas.validation import VALIDATION_RESULT_SCHEMA
from src.video_transcript_api.llm.schemas.unified_validation import UNIFIED_VALIDATION_SCHEMA
from src.video_transcript_api.llm.prompts.schemas.key_info import (
    KEY_INFO_SCHEMA,
    KEY_INFO_SYSTEM_PROMPT,
    build_key_info_user_prompt,
)

# Prompt imports
from src.video_transcript_api.llm.prompts import (
    CALIBRATE_SYSTEM_PROMPT,
    CALIBRATE_SYSTEM_PROMPT_EN,
    VALIDATION_SYSTEM_PROMPT,
    UNIFIED_VALIDATION_SYSTEM_PROMPT,
    SPEAKER_INFERENCE_SYSTEM_PROMPT,
    build_calibrate_user_prompt,
    build_structured_calibrate_user_prompt,
    build_validation_user_prompt,
    build_summary_user_prompt,
    build_speaker_inference_user_prompt,
    build_unified_validation_user_prompt,
)
from src.video_transcript_api.llm.prompts.unified_validation_prompts import _sample_dialogs

# QualityValidator
from src.video_transcript_api.llm.core.quality_validator import QualityValidator


# ============================================================
# Helper: JSON Schema basic structure checks
# ============================================================

def _assert_json_schema_object(schema: dict, required_keys: list):
    """Verify a JSON Schema dict is well-formed with expected required keys."""
    assert schema["type"] == "object"
    assert "properties" in schema
    assert schema.get("required") == required_keys


# ============================================================
# 1. CALIBRATION_RESULT_SCHEMA
# ============================================================

class TestCalibrationResultSchema:
    """Tests for the calibration result JSON Schema."""

    def test_top_level_structure(self):
        _assert_json_schema_object(CALIBRATION_RESULT_SCHEMA, ["corrections"])
        assert CALIBRATION_RESULT_SCHEMA["additionalProperties"] is False

    def test_corrections_is_array(self):
        corrections_prop = CALIBRATION_RESULT_SCHEMA["properties"]["corrections"]
        assert corrections_prop["type"] == "array"

    def test_correction_item_required_fields(self):
        # ID 锚点：每项仅 {id, text}，不含 speaker/start_time（结构是 ground truth）
        item_schema = CALIBRATION_RESULT_SCHEMA["properties"]["corrections"]["items"]
        assert item_schema["type"] == "object"
        assert set(item_schema["required"]) == {"id", "text"}
        assert item_schema["additionalProperties"] is False

    def test_correction_item_field_types(self):
        props = CALIBRATION_RESULT_SCHEMA["properties"]["corrections"]["items"]["properties"]
        assert props["id"]["type"] == "integer"
        assert props["text"]["type"] == "string"


# ============================================================
# 2. SPEAKER_MAPPING_SCHEMA
# ============================================================

class TestSpeakerMappingSchema:
    """Tests for the speaker mapping JSON Schema."""

    def test_top_level_structure(self):
        _assert_json_schema_object(
            SPEAKER_MAPPING_SCHEMA,
            ["speaker_mapping", "confidence", "reasoning"],
        )
        assert SPEAKER_MAPPING_SCHEMA["additionalProperties"] is False

    def test_speaker_mapping_allows_additional_string_props(self):
        sm = SPEAKER_MAPPING_SCHEMA["properties"]["speaker_mapping"]
        assert sm["type"] == "object"
        assert sm["additionalProperties"]["type"] == "string"

    def test_confidence_allows_additional_number_props(self):
        conf = SPEAKER_MAPPING_SCHEMA["properties"]["confidence"]
        assert conf["type"] == "object"
        assert conf["additionalProperties"]["type"] == "number"

    def test_reasoning_is_string(self):
        assert SPEAKER_MAPPING_SCHEMA["properties"]["reasoning"]["type"] == "string"


# ============================================================
# 3. VALIDATION_RESULT_SCHEMA
# ============================================================

class TestValidationResultSchema:
    """Tests for the validation result JSON Schema."""

    def test_top_level_structure(self):
        expected_required = [
            "overall_score", "scores", "pass", "issues", "recommendation"
        ]
        _assert_json_schema_object(VALIDATION_RESULT_SCHEMA, expected_required)
        assert VALIDATION_RESULT_SCHEMA["additionalProperties"] is False

    def test_overall_score_is_number(self):
        assert VALIDATION_RESULT_SCHEMA["properties"]["overall_score"]["type"] == "number"

    def test_pass_is_boolean(self):
        assert VALIDATION_RESULT_SCHEMA["properties"]["pass"]["type"] == "boolean"

    def test_issues_is_string_array(self):
        issues = VALIDATION_RESULT_SCHEMA["properties"]["issues"]
        assert issues["type"] == "array"
        assert issues["items"]["type"] == "string"

    def test_scores_sub_fields(self):
        scores = VALIDATION_RESULT_SCHEMA["properties"]["scores"]
        expected = {
            "format_correctness",
            "content_fidelity",
            "text_quality",
            "speaker_consistency",
            "time_consistency",
        }
        assert set(scores["required"]) == expected
        for key in expected:
            assert scores["properties"][key]["type"] == "number"


# ============================================================
# 4. UNIFIED_VALIDATION_SCHEMA
# ============================================================

class TestUnifiedValidationSchema:
    """Tests for the unified validation JSON Schema."""

    def test_top_level_structure(self):
        _assert_json_schema_object(UNIFIED_VALIDATION_SCHEMA, ["scores"])
        assert UNIFIED_VALIDATION_SCHEMA["additionalProperties"] is False

    def test_scores_dimensions(self):
        scores = UNIFIED_VALIDATION_SCHEMA["properties"]["scores"]
        expected = {"accuracy", "completeness", "fluency", "format"}
        assert set(scores["required"]) == expected
        for key in expected:
            prop = scores["properties"][key]
            assert prop["type"] == "number"
            assert prop["minimum"] == 0
            assert prop["maximum"] == 10

    def test_optional_fields_exist(self):
        props = UNIFIED_VALIDATION_SCHEMA["properties"]
        assert "issues" in props
        assert props["issues"]["type"] == "array"
        assert "deleted_content_analysis" in props
        assert props["deleted_content_analysis"]["type"] == "string"
        assert "recommendation" in props
        assert props["recommendation"]["type"] == "string"


# ============================================================
# 5. KEY_INFO_SCHEMA
# ============================================================

class TestKeyInfoSchema:
    """Tests for the key info extraction JSON Schema."""

    EXPECTED_CATEGORIES = [
        "names", "places", "technical_terms",
        "brands", "abbreviations", "foreign_terms", "other_entities",
    ]

    def test_top_level_structure(self):
        _assert_json_schema_object(KEY_INFO_SCHEMA, self.EXPECTED_CATEGORIES)

    def test_each_category_is_string_array(self):
        for cat in self.EXPECTED_CATEGORIES:
            prop = KEY_INFO_SCHEMA["properties"][cat]
            assert prop["type"] == "array", f"{cat} should be array"
            assert prop["items"]["type"] == "string", f"{cat} items should be string"


# ============================================================
# 6. Prompt builder: build_key_info_user_prompt
# ============================================================

class TestBuildKeyInfoUserPrompt:

    def test_basic_output_contains_title(self):
        result = build_key_info_user_prompt(title="Test Video")
        assert "Test Video" in result

    def test_includes_author_and_description(self):
        result = build_key_info_user_prompt(
            title="T", author="Author1", description="Desc here"
        )
        assert "Author1" in result
        assert "Desc here" in result

    def test_empty_metadata_fallback(self):
        result = build_key_info_user_prompt(title="", author="", description="")
        # Should contain a fallback indication
        assert result  # non-empty


# ============================================================
# 7. Prompt builder: build_calibrate_user_prompt
# ============================================================

class TestBuildCalibrateUserPrompt:

    def test_basic_chinese(self):
        result = build_calibrate_user_prompt(transcript="Hello world")
        assert "Hello world" in result
        assert "<transcript>" in result

    def test_english_language(self):
        result = build_calibrate_user_prompt(
            transcript="Hello", language="en"
        )
        assert "Transcript to proofread" in result

    def test_metadata_injection(self):
        result = build_calibrate_user_prompt(
            transcript="text",
            video_title="My Video",
            author="Chan",
            description="A short desc",
        )
        assert "My Video" in result
        assert "Chan" in result
        assert "A short desc" in result

    def test_description_truncation(self):
        long_desc = "x" * 600
        result = build_calibrate_user_prompt(
            transcript="t", description=long_desc
        )
        # Description should be truncated to 500 chars + "..."
        assert "..." in result

    def test_retry_hint_included(self):
        result = build_calibrate_user_prompt(
            transcript="t", retry_hint="Please keep full length"
        )
        assert "Please keep full length" in result

    def test_key_info_included(self):
        result = build_calibrate_user_prompt(
            transcript="t", key_info="- Claude\n- GPT"
        )
        assert "Claude" in result
        assert "GPT" in result


# ============================================================
# 8. Prompt builder: build_structured_calibrate_user_prompt
# ============================================================

class TestBuildStructuredCalibrateUserPrompt:

    def test_old_api_with_input_data(self):
        data = {"dialogs": [{"start_time": "00:00:01", "speaker": "A", "text": "hi"}]}
        result = build_structured_calibrate_user_prompt(input_data=data)
        assert "1" in result  # dialog count
        assert '"hi"' in result

    def test_new_api_with_dialogs_text(self):
        text = "[00:00:01][Speaker1]: Hello"
        result = build_structured_calibrate_user_prompt(
            dialogs_text=text, dialog_count=1
        )
        assert "Hello" in result
        assert "<dialogs>" in result

    def test_raises_without_input(self):
        with pytest.raises(ValueError, match="Must provide"):
            build_structured_calibrate_user_prompt()

    def test_english_mode(self):
        data = {"dialogs": [{"start_time": "00:00:01", "speaker": "A", "text": "hi"}]}
        result = build_structured_calibrate_user_prompt(
            input_data=data, language="en"
        )
        assert "Dialog count constraint" in result


# ============================================================
# 9. Prompt builder: build_validation_user_prompt
# ============================================================

class TestBuildValidationUserPrompt:

    def test_basic_rendering(self):
        original = {"dialogs": [{"text": "aaa"}], "total_count": 1}
        calibrated = {"dialogs": [{"text": "bbb"}], "total_count": 1}
        result = build_validation_user_prompt(original, calibrated)
        assert "aaa" in result
        assert "bbb" in result

    def test_metadata_included(self):
        result = build_validation_user_prompt(
            {"dialogs": []}, {"dialogs": []},
            video_title="Title", author="Auth", description="Desc",
        )
        assert "Title" in result
        assert "Auth" in result
        assert "Desc" in result


# ============================================================
# 10. Prompt builder: build_summary_user_prompt
# ============================================================

class TestBuildSummaryUserPrompt:

    def test_basic(self):
        result = build_summary_user_prompt(transcript="Some transcript")
        assert "Some transcript" in result

    def test_metadata(self):
        result = build_summary_user_prompt(
            transcript="t", video_title="V", author="A", description="D"
        )
        assert "V" in result
        assert "A" in result
        assert "D" in result


# ============================================================
# 11. Prompt builder: build_speaker_inference_user_prompt
# ============================================================

class TestBuildSpeakerInferenceUserPrompt:

    def test_basic(self):
        result = build_speaker_inference_user_prompt(
            context_snippets="Speaker1: hello",
            original_speakers=["Speaker1", "Speaker2"],
            video_title="Title",
            author="Author",
        )
        assert "Speaker1" in result
        assert "Speaker2" in result
        assert "Title" in result

    def test_description_optional(self):
        result = build_speaker_inference_user_prompt(
            context_snippets="snippet",
            original_speakers=["S1"],
            video_title="T",
            author="A",
            description="",
        )
        assert "snippet" in result


# ============================================================
# 12. Prompt builder: build_unified_validation_user_prompt
# ============================================================

class TestBuildUnifiedValidationUserPrompt:

    def _make_text_input(self, original="orig text", calibrated="cal text"):
        """Create a mock validation input for text content type."""
        inp = SimpleNamespace(
            content_type="text",
            original=original,
            calibrated=calibrated,
            length_info={"original_len": len(original), "calibrated_len": len(calibrated)},
        )
        return inp

    def _make_dialog_input(self, original_dialogs, calibrated_dialogs):
        """Create a mock validation input for dialog content type."""
        inp = SimpleNamespace(
            content_type="dialog",
            original=original_dialogs,
            calibrated=calibrated_dialogs,
            length_info={"original_count": len(original_dialogs), "calibrated_count": len(calibrated_dialogs)},
        )
        return inp

    def test_text_content(self):
        inp = self._make_text_input()
        result = build_unified_validation_user_prompt(inp)
        assert "orig text" in result
        assert "cal text" in result

    def test_text_truncation_at_2000(self):
        long_text = "a" * 3000
        inp = self._make_text_input(original=long_text, calibrated=long_text)
        result = build_unified_validation_user_prompt(inp)
        # The prompt should truncate to 2000 chars
        # Verify the full 3000 chars are NOT present as a single block
        assert long_text not in result

    def test_dialog_content(self):
        dialogs = [{"speaker": "A", "text": f"line {i}"} for i in range(5)]
        inp = self._make_dialog_input(dialogs, dialogs)
        result = build_unified_validation_user_prompt(inp)
        assert "line 0" in result

    def test_metadata_injection(self):
        inp = self._make_text_input()
        result = build_unified_validation_user_prompt(
            inp, video_title="VT", author="AU", description="DE"
        )
        assert "VT" in result
        assert "AU" in result
        assert "DE" in result

    def test_length_info_rendered(self):
        inp = self._make_text_input()
        result = build_unified_validation_user_prompt(inp)
        assert "original_len" in result


# ============================================================
# 13. _sample_dialogs helper
# ============================================================

class TestSampleDialogs:

    def test_small_list_returns_all(self):
        orig = [{"text": f"o{i}"} for i in range(10)]
        cal = [{"text": f"c{i}"} for i in range(10)]
        so, sc = _sample_dialogs(orig, cal, max_samples=50)
        assert len(so) == 10
        assert len(sc) == 10

    def test_large_list_is_sampled(self):
        n = 200
        orig = [{"text": f"o{i}"} for i in range(n)]
        cal = [{"text": f"c{i}"} for i in range(n)]
        so, sc = _sample_dialogs(orig, cal, max_samples=50)
        assert len(so) == 50
        assert len(sc) == 50

    def test_head_mid_tail_coverage(self):
        n = 200
        orig = [{"idx": i} for i in range(n)]
        cal = [{"idx": i} for i in range(n)]
        so, sc = _sample_dialogs(orig, cal, max_samples=50)
        # Head items (first few)
        assert so[0]["idx"] == 0
        # Tail items (last few)
        assert so[-1]["idx"] == n - 1

    def test_unequal_lengths_uses_minimum(self):
        orig = [{"text": f"o{i}"} for i in range(100)]
        cal = [{"text": f"c{i}"} for i in range(80)]
        so, sc = _sample_dialogs(orig, cal, max_samples=50)
        assert len(so) == 50
        assert len(sc) == 50


# ============================================================
# 14. System prompt constants sanity checks
# ============================================================

class TestSystemPromptConstants:

    def test_calibrate_system_prompt_not_empty(self):
        assert len(CALIBRATE_SYSTEM_PROMPT) > 100

    def test_calibrate_en_system_prompt_not_empty(self):
        assert len(CALIBRATE_SYSTEM_PROMPT_EN) > 100

    def test_validation_system_prompt_not_empty(self):
        assert len(VALIDATION_SYSTEM_PROMPT) > 50

    def test_unified_validation_system_prompt_not_empty(self):
        assert len(UNIFIED_VALIDATION_SYSTEM_PROMPT) > 50

    def test_speaker_inference_system_prompt_not_empty(self):
        assert len(SPEAKER_INFERENCE_SYSTEM_PROMPT) > 50

    def test_key_info_system_prompt_not_empty(self):
        assert len(KEY_INFO_SYSTEM_PROMPT) > 50


# ============================================================
# 15. QualityValidator
# ============================================================

class TestQualityValidator:
    """Tests for QualityValidator logic (no actual LLM calls)."""

    @pytest.fixture
    def mock_llm_client(self):
        return MagicMock()

    @pytest.fixture
    def validator(self, mock_llm_client):
        return QualityValidator(
            llm_client=mock_llm_client,
            overall_score_threshold=8.0,
            minimum_single_score=7.0,
        )

    # --- validate_by_length ---

    def test_validate_by_length_passes(self, validator):
        original = "a" * 100
        calibrated = "b" * 85
        result = validator.validate_by_length(original, calibrated, min_ratio=0.80)
        assert result == calibrated

    def test_validate_by_length_fails_returns_original(self, validator):
        original = "a" * 100
        calibrated = "b" * 50  # 50% < 80%
        result = validator.validate_by_length(original, calibrated, min_ratio=0.80)
        assert result == original

    def test_validate_by_length_exact_boundary(self, validator):
        original = "a" * 100
        calibrated = "b" * 80  # exactly 80%
        result = validator.validate_by_length(original, calibrated, min_ratio=0.80)
        assert result == calibrated

    def test_validate_by_length_empty_original(self, validator):
        result = validator.validate_by_length("", "", min_ratio=0.80)
        assert result == ""

    # --- _check_threshold ---

    def test_check_threshold_all_pass(self, validator):
        scores = {
            "format_correctness": 9.0,
            "content_fidelity": 8.5,
            "text_quality": 8.0,
            "speaker_consistency": 9.5,
            "time_consistency": 10.0,
        }
        assert validator._check_threshold(8.5, scores) is True

    def test_check_threshold_overall_too_low(self, validator):
        scores = {"dim1": 9.0, "dim2": 9.0}
        assert validator._check_threshold(7.5, scores) is False

    def test_check_threshold_single_score_too_low(self, validator):
        scores = {
            "format_correctness": 9.0,
            "content_fidelity": 6.5,  # below 7.0
        }
        assert validator._check_threshold(8.5, scores) is False

    def test_check_threshold_empty_scores(self, validator):
        # No dimension scores to fail, overall passes
        assert validator._check_threshold(9.0, {}) is True

    def test_check_threshold_boundary_values(self, validator):
        # Exactly at thresholds
        scores = {"dim": 7.0}
        assert validator._check_threshold(8.0, scores) is True

    # --- validate_by_score ---

    def test_validate_by_score_success(self, validator, mock_llm_client):
        mock_result = MagicMock()
        mock_result.structured_output = {
            "overall_score": 9.0,
            "scores": {
                "format_correctness": 9.0,
                "content_fidelity": 9.0,
                "text_quality": 8.5,
                "speaker_consistency": 10.0,
                "time_consistency": 10.0,
            },
            "pass": True,
            "issues": [],
            "recommendation": "Good quality",
        }
        mock_llm_client.call.return_value = mock_result

        original = [{"start_time": "00:00:01", "speaker": "A", "text": "hello"}]
        calibrated = [{"start_time": "00:00:01", "speaker": "A", "text": "Hello"}]

        result = validator.validate_by_score(original, calibrated)
        assert result["passed"] is True
        assert result["overall_score"] == 9.0

    def test_validate_by_score_fails_by_llm(self, validator, mock_llm_client):
        mock_result = MagicMock()
        mock_result.structured_output = {
            "overall_score": 9.0,
            "scores": {"dim": 9.0},
            "pass": False,  # LLM says fail
            "issues": ["Content altered"],
            "recommendation": "Revert",
        }
        mock_llm_client.call.return_value = mock_result

        result = validator.validate_by_score([], [])
        assert result["passed"] is False

    def test_validate_by_score_fails_by_threshold(self, validator, mock_llm_client):
        mock_result = MagicMock()
        mock_result.structured_output = {
            "overall_score": 7.0,  # below threshold
            "scores": {"dim": 9.0},
            "pass": True,
            "issues": [],
            "recommendation": "",
        }
        mock_llm_client.call.return_value = mock_result

        result = validator.validate_by_score([], [])
        assert result["passed"] is False

    def test_validate_by_score_exception_handling(self, validator, mock_llm_client):
        mock_llm_client.call.side_effect = RuntimeError("API error")

        result = validator.validate_by_score([], [])
        assert result["passed"] is False
        assert result["overall_score"] == 0
        assert any("API error" in issue for issue in result["issues"])

    def test_validate_by_score_with_metadata(self, validator, mock_llm_client):
        mock_result = MagicMock()
        mock_result.structured_output = {
            "overall_score": 9.0,
            "scores": {"dim": 9.0},
            "pass": True,
            "issues": [],
            "recommendation": "",
        }
        mock_llm_client.call.return_value = mock_result

        metadata = {"title": "Test", "author": "Auth", "description": "Desc"}
        result = validator.validate_by_score([], [], video_metadata=metadata)
        assert result["passed"] is True

    def test_validate_by_score_uses_selected_models(self, validator, mock_llm_client):
        mock_result = MagicMock()
        mock_result.structured_output = {
            "overall_score": 9.0,
            "scores": {},
            "pass": True,
            "issues": [],
            "recommendation": "",
        }
        mock_llm_client.call.return_value = mock_result

        selected = {
            "validator_model": "gpt-4",
            "validator_reasoning_effort": "high",
        }
        validator.validate_by_score([], [], selected_models=selected)

        call_kwargs = mock_llm_client.call.call_args
        assert call_kwargs.kwargs["model"] == "gpt-4"
        assert call_kwargs.kwargs["reasoning_effort"] == "high"

    # --- init defaults ---

    def test_default_thresholds(self, mock_llm_client):
        v = QualityValidator(llm_client=mock_llm_client)
        assert v.overall_score_threshold == 8.0
        assert v.minimum_single_score == 7.0
        assert v.model == "claude-3-5-sonnet"
        assert v.reasoning_effort is None

    def test_custom_thresholds(self, mock_llm_client):
        v = QualityValidator(
            llm_client=mock_llm_client,
            overall_score_threshold=6.0,
            minimum_single_score=5.0,
        )
        assert v.overall_score_threshold == 6.0
        assert v.minimum_single_score == 5.0


# ============================================================
# 16. llm.prompts.schemas re-export validation
# ============================================================

class TestPromptsSchemaReExports:
    """Verify that llm.prompts.schemas.__init__ re-exports all schemas correctly."""

    def test_all_schemas_importable_from_package(self):
        """All 4 schemas should be importable from llm.prompts.schemas."""
        from src.video_transcript_api.llm.prompts.schemas import (
            KEY_INFO_SCHEMA,
            SPEAKER_MAPPING_SCHEMA,
            VALIDATION_RESULT_SCHEMA,
            UNIFIED_VALIDATION_SCHEMA,
        )
        assert isinstance(KEY_INFO_SCHEMA, dict)
        assert isinstance(SPEAKER_MAPPING_SCHEMA, dict)
        assert isinstance(VALIDATION_RESULT_SCHEMA, dict)
        assert isinstance(UNIFIED_VALIDATION_SCHEMA, dict)

    def test_prompts_speaker_mapping_schema_structure(self):
        """prompts.schemas.speaker_mapping should have correct top-level structure."""
        from src.video_transcript_api.llm.prompts.schemas.speaker_mapping import SPEAKER_MAPPING_SCHEMA
        _assert_json_schema_object(
            SPEAKER_MAPPING_SCHEMA,
            ["speaker_mapping", "confidence", "reasoning"],
        )
        assert SPEAKER_MAPPING_SCHEMA["additionalProperties"] is False

    def test_prompts_validation_schema_structure(self):
        """prompts.schemas.validation should have correct top-level structure."""
        from src.video_transcript_api.llm.prompts.schemas.validation import VALIDATION_RESULT_SCHEMA
        expected_required = [
            "overall_score", "scores", "pass", "issues", "recommendation"
        ]
        _assert_json_schema_object(VALIDATION_RESULT_SCHEMA, expected_required)
        assert VALIDATION_RESULT_SCHEMA["additionalProperties"] is False

    def test_prompts_unified_validation_schema_structure(self):
        """prompts.schemas.unified_validation should have correct top-level structure."""
        from src.video_transcript_api.llm.prompts.schemas.unified_validation import UNIFIED_VALIDATION_SCHEMA
        _assert_json_schema_object(UNIFIED_VALIDATION_SCHEMA, ["scores"])
        assert UNIFIED_VALIDATION_SCHEMA["additionalProperties"] is False

    def test_prompts_schemas_match_llm_schemas(self):
        """prompts.schemas and llm.schemas should export identical objects.

        SPEAKER_MAPPING_SCHEMA is no longer duplicated: it is defined once in
        prompts.schemas and re-exported (not redefined) by llm package __init__,
        so there is nothing left to cross-check here for it.
        """
        from src.video_transcript_api.llm.prompts.schemas import (
            VALIDATION_RESULT_SCHEMA as PS_VR,
            UNIFIED_VALIDATION_SCHEMA as PS_UV,
        )
        from src.video_transcript_api.llm.schemas.validation import VALIDATION_RESULT_SCHEMA as LS_VR
        from src.video_transcript_api.llm.schemas.unified_validation import UNIFIED_VALIDATION_SCHEMA as LS_UV

        # These should be the exact same dict objects (both modules define them identically)
        assert PS_VR == LS_VR
        assert PS_UV == LS_UV

    def test_speaker_mapping_schema_reexported_not_duplicated(self):
        """llm.SPEAKER_MAPPING_SCHEMA must be the same object as prompts.schemas' (single source of truth)."""
        from src.video_transcript_api.llm import SPEAKER_MAPPING_SCHEMA as LLM_SM
        from src.video_transcript_api.llm.prompts.schemas import SPEAKER_MAPPING_SCHEMA as PS_SM

        assert LLM_SM is PS_SM
