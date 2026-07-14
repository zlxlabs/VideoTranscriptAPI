"""Simple end-to-end test for summary feature (requires real API)

This test requires:
1. Valid config file with API credentials
2. Real LLM API access

Run manually: uv run pytest tests/manual/test_summary_e2e_simple.py -v -s
"""

import unittest
from pathlib import Path
from video_transcript_api.api.context import get_config
from video_transcript_api.llm import LLMCoordinator


class TestSummaryE2ESimple(unittest.TestCase):
    """Simple end-to-end test for summary feature"""

    @classmethod
    def setUpClass(cls):
        """Set up test class - load config and initialize LLM client"""
        try:
            cls.config = get_config()
            cls.cache_dir = "./data/cache"
        except Exception as e:
            raise unittest.SkipTest(f"Failed to load config: {e}")

        from video_transcript_api.llm import set_default_config
        try:
            set_default_config(cls.config)
        except Exception as e:
            raise unittest.SkipTest(f"Failed to initialize LLM client: {e}")

        from video_transcript_api.llm.llm import get_sync_client
        try:
            get_sync_client()
        except RuntimeError:
            raise unittest.SkipTest("SyncLLMClient not initialized (missing API credentials)")

    def test_short_text_summary(self):
        """Test summary generation with short text"""
        # Create coordinator
        coordinator = LLMCoordinator(
            config_dict=self.config,
            cache_dir=self.cache_dir,
        )

        # Short text (< 500 chars)
        short_text = "This is a short test text. It should not generate a summary."

        result = coordinator.process(
            content=short_text,
            title="Short Test",
            author="Test Author",
        )

        # Verify results
        self.assertIsNotNone(result)
        self.assertIn("calibrated_text", result)
        self.assertIn("summary_text", result)

        # Short text should skip summary
        self.assertIsNone(result["summary_text"])
        self.assertEqual(result["stats"]["summary_length"], 0)

        print("\nShort text test passed:")
        print(f"  Calibrated length: {len(result['calibrated_text'])}")
        print(f"  Summary: {result['summary_text']}")

    def test_long_text_summary(self):
        """Test summary generation with long text"""
        # Create coordinator
        coordinator = LLMCoordinator(
            config_dict=self.config,
            cache_dir=self.cache_dir,
        )

        # Long text (> 500 chars)
        long_text = """
        Artificial intelligence has made remarkable progress in recent years,
        particularly in the field of natural language processing. Models like GPT-3,
        BERT, and their successors have demonstrated impressive capabilities in
        understanding and generating human-like text. These models are trained on
        vast amounts of text data and can perform a wide variety of language tasks.

        The development of large language models has opened up new possibilities
        for applications in areas such as automated content generation, translation,
        summarization, and question answering. However, these advancements also raise
        important questions about ethics, bias, and the responsible use of AI technology.

        As we move forward, it will be crucial to address these challenges while
        continuing to push the boundaries of what's possible with AI. The future
        of natural language processing looks promising, with potential applications
        across education, healthcare, business, and many other domains.
        """ * 2  # Repeat to ensure > 500 chars

        result = coordinator.process(
            content=long_text,
            title="AI and NLP Progress",
            author="Test Author",
            description="A discussion about recent advances in AI and NLP",
        )

        # Verify results
        self.assertIsNotNone(result)
        self.assertIn("calibrated_text", result)
        self.assertIn("summary_text", result)

        # Long text should generate summary
        self.assertIsNotNone(result["summary_text"])
        self.assertGreater(result["stats"]["summary_length"], 0)
        self.assertGreater(len(result["summary_text"]), 50)

        print("\nLong text test passed:")
        print(f"  Original length: {len(long_text)}")
        print(f"  Calibrated length: {len(result['calibrated_text'])}")
        print(f"  Summary length: {result['stats']['summary_length']}")
        print(f"\nSummary preview:")
        print(f"  {result['summary_text'][:200]}...")


if __name__ == "__main__":
    # Run with verbose output
    unittest.main(verbosity=2)
