"""Unit tests for utils/llm_status.py constants.

Locks the exact string values of CalibrationStatus / SummaryStatus since they
are persisted verbatim into llm_status.json and the task_status DB columns.
Silent renames would break backward compatibility with existing cache data.

All console output must be in English only (no emoji, no Chinese).
"""

from video_transcript_api.utils.llm_status import CalibrationStatus, ChaptersStatus, SummaryStatus


class TestCalibrationStatus:
    def test_values(self):
        assert CalibrationStatus.FULL == "full"
        assert CalibrationStatus.PARTIAL == "partial"
        assert CalibrationStatus.NONE == "none"
        assert CalibrationStatus.DISABLED == "disabled"

    def test_is_str_subclass(self):
        """Must behave as plain str for JSON/SQLite round-tripping."""
        assert isinstance(CalibrationStatus.FULL, str)
        assert str(CalibrationStatus.FULL) == "full"


class TestSummaryStatus:
    def test_values(self):
        assert SummaryStatus.GENERATED == "generated"
        assert SummaryStatus.SKIPPED_SHORT == "skipped_short"
        assert SummaryStatus.FAILED == "failed"
        assert SummaryStatus.PENDING == "pending"
        assert SummaryStatus.DISABLED == "disabled"

    def test_is_str_subclass(self):
        assert isinstance(SummaryStatus.GENERATED, str)
        assert str(SummaryStatus.GENERATED) == "generated"


class TestChaptersStatus:
    def test_values(self):
        assert ChaptersStatus.GENERATED == "generated"
        assert ChaptersStatus.SKIPPED_SHORT == "skipped_short"
        assert ChaptersStatus.SKIPPED_NO_TIMELINE == "skipped_no_timeline"
        assert ChaptersStatus.FAILED == "failed"
        assert ChaptersStatus.PENDING == "pending"
        assert ChaptersStatus.DISABLED == "disabled"

    def test_is_str_subclass(self):
        assert isinstance(ChaptersStatus.GENERATED, str)
        assert str(ChaptersStatus.GENERATED) == "generated"
