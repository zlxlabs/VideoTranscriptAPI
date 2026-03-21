"""Unit tests for the errors module hierarchy and behavior."""
import pytest

from src.video_transcript_api.errors import (
    TranscriptAPIError,
    DownloadFailedError,
    InvalidMediaError,
    NetworkError,
    DownloadTimeoutError,
    HTTPForbiddenError,
    ASRConnectionError,
    EmptyTranscriptError,
)


class TestTranscriptAPIError:
    """Tests for the base error class."""

    def test_default_message(self):
        err = TranscriptAPIError()
        assert err.message == ""

    def test_custom_message(self):
        err = TranscriptAPIError("something went wrong")
        assert err.message == "something went wrong"

    def test_retryable_default_false(self):
        err = TranscriptAPIError("err")
        assert err.retryable is False

    def test_retryable_explicit_true(self):
        err = TranscriptAPIError("err", retryable=True)
        assert err.retryable is True

    def test_str_representation(self):
        err = TranscriptAPIError("display this")
        assert str(err) == "display this"

    def test_inherits_exception(self):
        err = TranscriptAPIError("test")
        assert isinstance(err, Exception)


class TestDownloadErrors:
    """Tests for download-related error classes."""

    def test_download_failed_default_message(self):
        err = DownloadFailedError()
        assert err.message == "File download failed"

    def test_download_failed_retryable(self):
        err = DownloadFailedError()
        assert err.retryable is True

    def test_download_failed_custom_message(self):
        err = DownloadFailedError("custom msg")
        assert err.message == "custom msg"
        assert err.retryable is True

    def test_download_failed_inherits_base(self):
        err = DownloadFailedError()
        assert isinstance(err, TranscriptAPIError)

    def test_invalid_media_default_message(self):
        err = InvalidMediaError()
        assert err.message == "Invalid media file"

    def test_invalid_media_not_retryable(self):
        err = InvalidMediaError()
        assert err.retryable is False

    def test_invalid_media_inherits_base(self):
        err = InvalidMediaError()
        assert isinstance(err, TranscriptAPIError)


class TestNetworkErrors:
    """Tests for network-related error classes."""

    def test_network_error_default_message(self):
        err = NetworkError()
        assert err.message == "Network error"

    def test_network_error_retryable(self):
        err = NetworkError()
        assert err.retryable is True

    def test_network_error_inherits_base(self):
        err = NetworkError()
        assert isinstance(err, TranscriptAPIError)

    def test_download_timeout_default_message(self):
        err = DownloadTimeoutError()
        assert err.message == "Download timed out"

    def test_download_timeout_retryable(self):
        err = DownloadTimeoutError()
        assert err.retryable is True

    def test_download_timeout_inherits_network_error(self):
        err = DownloadTimeoutError()
        assert isinstance(err, NetworkError)

    def test_download_timeout_inherits_base(self):
        err = DownloadTimeoutError()
        assert isinstance(err, TranscriptAPIError)

    def test_http_forbidden_default_message(self):
        err = HTTPForbiddenError()
        assert err.message == "HTTP 403 Forbidden"

    def test_http_forbidden_not_retryable(self):
        err = HTTPForbiddenError()
        assert err.retryable is False

    def test_http_forbidden_inherits_base(self):
        err = HTTPForbiddenError()
        assert isinstance(err, TranscriptAPIError)

    def test_http_forbidden_not_network_error(self):
        """HTTPForbiddenError inherits TranscriptAPIError, not NetworkError."""
        err = HTTPForbiddenError()
        assert not isinstance(err, NetworkError)


class TestTranscriptionErrors:
    """Tests for transcription-related error classes."""

    def test_asr_connection_default_message(self):
        err = ASRConnectionError()
        assert err.message == "ASR service connection failed"

    def test_asr_connection_retryable(self):
        err = ASRConnectionError()
        assert err.retryable is True

    def test_asr_connection_inherits_base(self):
        err = ASRConnectionError()
        assert isinstance(err, TranscriptAPIError)

    def test_empty_transcript_default_message(self):
        err = EmptyTranscriptError()
        assert err.message == "Transcript is empty"

    def test_empty_transcript_not_retryable(self):
        err = EmptyTranscriptError()
        assert err.retryable is False

    def test_empty_transcript_inherits_base(self):
        err = EmptyTranscriptError()
        assert isinstance(err, TranscriptAPIError)


class TestCatchingBaseClassCatchesSubclass:
    """Test that catching base class catches all subclasses."""

    @pytest.mark.parametrize("error_cls,args", [
        (DownloadFailedError, ()),
        (InvalidMediaError, ()),
        (NetworkError, ()),
        (DownloadTimeoutError, ()),
        (HTTPForbiddenError, ()),
        (ASRConnectionError, ()),
        (EmptyTranscriptError, ()),
    ])
    def test_base_class_catches_subclass(self, error_cls, args):
        with pytest.raises(TranscriptAPIError):
            raise error_cls(*args)

    def test_network_error_catches_download_timeout(self):
        with pytest.raises(NetworkError):
            raise DownloadTimeoutError()

    def test_exception_catches_all(self):
        with pytest.raises(Exception):
            raise DownloadFailedError("test")


class TestIsinstanceChains:
    """Verify full isinstance chains for multi-level inheritance."""

    def test_download_timeout_full_chain(self):
        err = DownloadTimeoutError("timeout")
        assert isinstance(err, DownloadTimeoutError)
        assert isinstance(err, NetworkError)
        assert isinstance(err, TranscriptAPIError)
        assert isinstance(err, Exception)

    def test_download_failed_is_not_network_error(self):
        err = DownloadFailedError()
        assert not isinstance(err, NetworkError)

    def test_asr_connection_is_not_network_error(self):
        err = ASRConnectionError()
        assert not isinstance(err, NetworkError)
