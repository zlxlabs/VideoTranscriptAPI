"""Test SummaryProcessor unit tests"""

import unittest
from unittest.mock import Mock, patch
from video_transcript_api.utils.llm.processors.summary_processor import SummaryProcessor
from video_transcript_api.utils.llm.core.config import LLMConfig


class TestSummaryProcessor(unittest.TestCase):
    """Test SummaryProcessor functionality"""

    def setUp(self):
        """Set up test configuration"""
        # Create minimal config
        self.config = LLMConfig(
            api_key="test_key",
            base_url="http://test.api.com",
            calibrate_model="test-model",
            summary_model="test-summary-model",
            min_summary_threshold=500,
        )

        self.llm_client = Mock()

        self.processor = SummaryProcessor(
            llm_client=self.llm_client,
            config=self.config,
        )

    def test_short_text_returns_none(self):
        """Test short text skips summary generation"""
        result = self.processor.process(
            text="This is a very short text.",  # < 500 chars
            title="Test Title",
        )
        self.assertIsNone(result)

    @patch('video_transcript_api.utils.llm.core.llm_client.LLMClient.call')
    def test_long_text_generates_summary(self, mock_call):
        """Test long text generates summary"""
        # Mock LLM response
        mock_response = Mock()
        mock_response.text = "This is the generated summary. " * 10  # > 50 chars
        self.llm_client.call = Mock(return_value=mock_response)

        result = self.processor.process(
            text="This is a very long text... " * 100,  # > 500 chars
            title="Test Title",
        )

        self.assertIsNotNone(result)
        self.assertIn("summary", result.lower())

    def test_single_speaker_prompt_selection(self):
        """Test single speaker prompt selection"""
        single_prompt = self.processor._select_system_prompt(speaker_count=0)
        multi_prompt = self.processor._select_system_prompt(speaker_count=2)

        # Verify prompt is not empty
        self.assertTrue(len(single_prompt) > 100)

        # Verify single and multi prompts are different
        self.assertNotEqual(single_prompt, multi_prompt)

    def test_multi_speaker_prompt_selection(self):
        """Test multi-speaker prompt selection"""
        single_prompt = self.processor._select_system_prompt(speaker_count=0)
        multi_prompt = self.processor._select_system_prompt(speaker_count=2)

        # Verify prompt is not empty
        self.assertTrue(len(multi_prompt) > 100)

        # Verify single and multi prompts are different
        self.assertNotEqual(single_prompt, multi_prompt)

    @patch('video_transcript_api.utils.llm.core.llm_client.LLMClient.call')
    def test_task_type_parameter(self, mock_call):
        """Test task_type parameter is correctly passed"""
        # Mock LLM response
        mock_response = Mock()
        mock_response.text = "This is the generated summary. " * 10
        self.llm_client.call = Mock(return_value=mock_response)

        # Call processor
        self.processor.process(
            text="This is a very long text... " * 100,
            title="Test Title",
        )

        # Verify task_type parameter
        self.llm_client.call.assert_called_once()
        call_kwargs = self.llm_client.call.call_args[1]
        self.assertEqual(call_kwargs.get("task_type"), "summary")

    @patch('video_transcript_api.utils.llm.core.llm_client.LLMClient.call')
    def test_summary_too_short_returns_none(self, mock_call):
        """Test summary generation returns None if result too short"""
        # Mock LLM response with very short text
        mock_response = Mock()
        mock_response.text = "Short"  # < 50 chars
        self.llm_client.call = Mock(return_value=mock_response)

        result = self.processor.process(
            text="This is a very long text... " * 100,
            title="Test Title",
        )

        self.assertIsNone(result)

    @patch('video_transcript_api.utils.llm.core.llm_client.LLMClient.call')
    def test_exception_handling(self, mock_call):
        """Test exception handling returns None gracefully"""
        # Mock LLM call to raise exception
        self.llm_client.call = Mock(side_effect=Exception("Test error"))

        result = self.processor.process(
            text="This is a very long text... " * 100,
            title="Test Title",
        )

        # Should return None instead of raising exception
        self.assertIsNone(result)

    @patch('video_transcript_api.utils.llm.core.llm_client.LLMClient.call')
    def test_selected_models_parameter(self, mock_call):
        """Test selected_models parameter overrides config"""
        # Mock LLM response
        mock_response = Mock()
        mock_response.text = "This is the generated summary. " * 10
        self.llm_client.call = Mock(return_value=mock_response)

        # Call with selected_models
        selected_models = {
            "summary_model": "risk-model",
            "summary_reasoning_effort": "high",
        }

        self.processor.process(
            text="This is a very long text... " * 100,
            title="Test Title",
            selected_models=selected_models,
        )

        # Verify model parameter
        call_kwargs = self.llm_client.call.call_args[1]
        self.assertEqual(call_kwargs.get("model"), "risk-model")
        self.assertEqual(call_kwargs.get("reasoning_effort"), "high")


if __name__ == "__main__":
    unittest.main()
