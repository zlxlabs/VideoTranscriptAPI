"""Test Coordinator integration with summary functionality"""

import unittest
from unittest.mock import Mock, patch
from video_transcript_api.llm import LLMCoordinator


class TestCoordinatorWithSummary(unittest.TestCase):
    """Test Coordinator summary integration"""

    def setUp(self):
        """Set up test configuration"""
        self.config_dict = {
            "llm": {
                "api_key": "test_key",
                "base_url": "http://test.api.com",
                "calibrate_model": "test-model",
                "summary_model": "test-summary-model",
                "key_info_model": "test-model",
                "speaker_model": "test-model",
                "min_summary_threshold": 500,
                "segmentation": {},
                "structured_calibration": {},
            }
        }
        self.cache_dir = "./test_cache_dir"

    def test_coordinator_has_summary_processor(self):
        """Test coordinator initializes with summary processor"""
        coordinator = LLMCoordinator(
            config_dict=self.config_dict,
            cache_dir=self.cache_dir,
        )

        self.assertIsNotNone(coordinator.summary_processor)

    @patch('video_transcript_api.llm.core.llm_client.LLMClient.call')
    def test_short_text_skips_summary(self, mock_call):
        """Test short text skips summary generation"""
        # Mock calibration response (key info extraction + calibration)
        def mock_llm_response(*args, **kwargs):
            response = Mock()
            task_type = kwargs.get("task_type", "")

            if task_type == "key_info_extraction":
                # Key info extraction response
                response.structured_output = {
                    "names": [],
                    "places": [],
                    "technical_terms": [],
                    "brands": [],
                    "abbreviations": {},
                    "foreign_terms": [],
                    "other_entities": []
                }
                response.text = ""
            else:
                # Calibration response
                response.text = "This is calibrated short text."
                response.structured_output = None

            return response

        mock_call.side_effect = mock_llm_response

        coordinator = LLMCoordinator(
            config_dict=self.config_dict,
            cache_dir=self.cache_dir,
        )

        result = coordinator.process(
            content="This is a very short text.",  # < 500 chars
            title="Test Title",
        )

        # Verify summary is None
        self.assertIsNone(result["summary_text"])
        self.assertEqual(result["stats"]["summary_length"], 0)

    @patch('video_transcript_api.llm.core.llm_client.LLMClient.call')
    def test_long_text_generates_summary(self, mock_call):
        """Test long text generates summary"""
        # Mock responses for different tasks
        def mock_llm_response(*args, **kwargs):
            response = Mock()
            task_type = kwargs.get("task_type", "")

            if task_type == "key_info_extraction":
                # Key info extraction response
                response.structured_output = {
                    "names": ["Alice"],
                    "places": [],
                    "technical_terms": [],
                    "brands": [],
                    "abbreviations": {},
                    "foreign_terms": [],
                    "other_entities": []
                }
                response.text = ""
            elif task_type == "summary":
                # Summary generation response
                response.text = "## Overview\nThis is a comprehensive summary..." * 10
                response.structured_output = None
            else:
                # Calibration response
                response.text = "This is calibrated long text... " * 100
                response.structured_output = None

            return response

        mock_call.side_effect = mock_llm_response

        coordinator = LLMCoordinator(
            config_dict=self.config_dict,
            cache_dir=self.cache_dir,
        )

        result = coordinator.process(
            content="This is a very long text... " * 100,  # > 500 chars
            title="Test Title",
        )

        # Verify summary is generated
        self.assertIsNotNone(result["summary_text"])
        self.assertIn("Overview", result["summary_text"])
        self.assertGreater(result["stats"]["summary_length"], 0)

    @patch('video_transcript_api.llm.core.llm_client.LLMClient.call')
    def test_result_structure(self, mock_call):
        """Test result structure includes all expected fields"""
        # Mock calibration response
        def mock_llm_response(*args, **kwargs):
            response = Mock()
            task_type = kwargs.get("task_type", "")

            if task_type == "key_info_extraction":
                response.structured_output = {
                    "names": [],
                    "places": [],
                    "technical_terms": [],
                    "brands": [],
                    "abbreviations": {},
                    "foreign_terms": [],
                    "other_entities": []
                }
                response.text = ""
            else:
                response.text = "Calibrated text."
                response.structured_output = None

            return response

        mock_call.side_effect = mock_llm_response

        coordinator = LLMCoordinator(
            config_dict=self.config_dict,
            cache_dir=self.cache_dir,
        )

        result = coordinator.process(
            content="Short text",
            title="Test Title",
        )

        # Verify all expected fields are present
        self.assertIn("calibrated_text", result)
        self.assertIn("summary_text", result)
        self.assertIn("key_info", result)
        self.assertIn("stats", result)
        self.assertIn("summary_length", result["stats"])

    @patch('video_transcript_api.llm.core.llm_client.LLMClient.call')
    def test_speaker_count_extraction_plain_text(self, mock_call):
        """Test speaker count extraction for plain text (should be 0)"""
        # Mock calibration response
        def mock_llm_response(*args, **kwargs):
            response = Mock()
            task_type = kwargs.get("task_type", "")

            if task_type == "key_info_extraction":
                response.structured_output = {
                    "names": [],
                    "places": [],
                    "technical_terms": [],
                    "brands": [],
                    "abbreviations": {},
                    "foreign_terms": [],
                    "other_entities": []
                }
                response.text = ""
            else:
                response.text = "Calibrated text."
                response.structured_output = None

            return response

        mock_call.side_effect = mock_llm_response

        coordinator = LLMCoordinator(
            config_dict=self.config_dict,
            cache_dir=self.cache_dir,
        )

        # Test plain text input
        content = "This is plain text content."
        speaker_count = coordinator._extract_speaker_count(
            content, {"calibrated_text": content}
        )

        self.assertEqual(speaker_count, 0)

    def test_speaker_count_extraction_dialog_list(self):
        """Test speaker count extraction for dialog list"""
        coordinator = LLMCoordinator(
            config_dict=self.config_dict,
            cache_dir=self.cache_dir,
        )

        # Test dialog list input
        content = [
            {"speaker": "spk_0", "text": "Hello"},
            {"speaker": "spk_1", "text": "Hi"},
        ]

        calibration_result = {
            "structured_data": {
                "speaker_mapping": {
                    "spk_0": "Alice",
                    "spk_1": "Bob"
                }
            }
        }

        speaker_count = coordinator._extract_speaker_count(content, calibration_result)

        self.assertEqual(speaker_count, 2)


if __name__ == "__main__":
    unittest.main()
