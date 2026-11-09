"""
Transcriber unit tests.

Covers:
- Successful transcription flow
- Error handling when transcription fails
- File not found handling

All console output must be in English only (no emoji, no Chinese).
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


from video_transcript_api.errors import EmptyTranscriptError
from video_transcript_api.transcriber import Transcriber


class TestTranscriber(unittest.TestCase):
    """Test transcriber core flow."""

    def setUp(self):
        """Set up test environment."""
        self.temp_dir = tempfile.mkdtemp()
        self.test_config = {
            "capswriter": {
                "server_url": "ws://localhost:6006"
            },
            "storage": {
                "output_dir": self.temp_dir
            }
        }

        # Create a fake audio file
        self.test_audio_file = os.path.join(self.temp_dir, "test_audio.mp3")
        with open(self.test_audio_file, "w", encoding="utf-8") as f:
            f.write("fake audio")

    def tearDown(self):
        """Clean up test files."""
        import shutil
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    @patch("video_transcript_api.transcriber.transcriber.CapsWriterClient")
    def test_transcribe_success(self, mock_client_cls):
        """Successful transcription should return transcript text."""
        # Create a fake .txt output file
        txt_path = os.path.join(self.temp_dir, "test_output.txt")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("This is the transcribed text.")

        # Mock client
        mock_client = MagicMock()
        mock_client.transcribe_file.return_value = (True, [Path(txt_path)])
        mock_client_cls.return_value = mock_client

        transcriber = Transcriber(config=self.test_config)
        result = transcriber.transcribe(self.test_audio_file, "test_output")

        mock_client.transcribe_file.assert_called_once_with(self.test_audio_file)
        self.assertIn("transcript", result)
        self.assertEqual(result["transcript"], "This is the transcribed text.")

    @patch("video_transcript_api.transcriber.transcriber.logger")
    @patch("video_transcript_api.transcriber.transcriber.CapsWriterClient")
    def test_transcript_read_failure_raises_with_grep_marker(
        self, mock_client_cls, mock_logger
    ):
        """A transcript file read failure must not turn into an empty success."""
        txt_path = os.path.join(self.temp_dir, "unreadable.txt")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("transcript")

        mock_client = MagicMock()
        mock_client.transcribe_file.return_value = (True, [Path(txt_path)])
        mock_client_cls.return_value = mock_client
        transcriber = Transcriber(config=self.test_config)

        with patch("builtins.open", side_effect=PermissionError("read denied")):
            with self.assertRaises(PermissionError):
                transcriber.transcribe(self.test_audio_file, "unreadable")

        self.assertTrue(
            any("TRANSCRIPT_READ_FAILED" in str(call) for call in mock_logger.error.call_args_list)
        )

    @patch("video_transcript_api.transcriber.transcriber.CapsWriterClient")
    def test_transcribe_whitespace_only_text_raises(self, mock_client_cls):
        """A successfully read but blank transcript must remain an error."""
        txt_path = os.path.join(self.temp_dir, "blank.txt")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(" \n\t ")
        mock_client = MagicMock()
        mock_client.transcribe_file.return_value = (True, [Path(txt_path)])
        mock_client_cls.return_value = mock_client

        transcriber = Transcriber(config=self.test_config)

        with self.assertRaises(EmptyTranscriptError):
            transcriber.transcribe(self.test_audio_file, "blank")

    @patch("video_transcript_api.transcriber.transcriber.CapsWriterClient")
    def test_transcribe_failure(self, mock_client_cls):
        """Failed transcription should raise RuntimeError."""
        mock_client = MagicMock()
        mock_client.transcribe_file.return_value = (False, [])
        mock_client_cls.return_value = mock_client

        transcriber = Transcriber(config=self.test_config)

        with self.assertRaises(RuntimeError):
            transcriber.transcribe(self.test_audio_file, "test_output")

    @patch("video_transcript_api.transcriber.transcriber.CapsWriterClient")
    def test_transcribe_file_not_found(self, mock_client_cls):
        """Non-existent audio file should raise FileNotFoundError."""
        mock_client_cls.return_value = MagicMock()

        transcriber = Transcriber(config=self.test_config)

        with self.assertRaises(FileNotFoundError):
            transcriber.transcribe("/nonexistent/audio.mp3", "test_output")


if __name__ == '__main__':
    unittest.main()
