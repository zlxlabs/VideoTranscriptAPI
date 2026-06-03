"""Unit tests for the TaskStatus state constants module.

All console output must be in English only (no emoji, no Chinese).
"""

from video_transcript_api.utils.task_status import (
    TaskStatus,
    TERMINAL_STATUSES,
    NON_TERMINAL_STATUSES,
    http_code_for_status,
)


class TestTaskStatusValues:
    """The enum members must carry the exact string values stored in SQLite."""

    def test_member_string_values(self):
        assert TaskStatus.QUEUED == "queued"
        assert TaskStatus.PROCESSING == "processing"
        assert TaskStatus.CALIBRATING == "calibrating"
        assert TaskStatus.SUCCESS == "success"
        assert TaskStatus.FAILED == "failed"

    def test_str_renders_plain_value(self):
        # StrEnum must serialize as the bare value (not "TaskStatus.SUCCESS"),
        # otherwise SQLite would store the wrong text.
        assert str(TaskStatus.CALIBRATING) == "calibrating"
        assert f"{TaskStatus.SUCCESS}" == "success"


class TestStatusGroups:
    """Terminal / non-terminal partition must be complete and disjoint."""

    def test_terminal_set(self):
        assert TaskStatus.SUCCESS in TERMINAL_STATUSES
        assert TaskStatus.FAILED in TERMINAL_STATUSES
        assert TaskStatus.CALIBRATING not in TERMINAL_STATUSES

    def test_non_terminal_set(self):
        assert TaskStatus.QUEUED in NON_TERMINAL_STATUSES
        assert TaskStatus.PROCESSING in NON_TERMINAL_STATUSES
        assert TaskStatus.CALIBRATING in NON_TERMINAL_STATUSES

    def test_partition_is_disjoint_and_complete(self):
        assert TERMINAL_STATUSES.isdisjoint(NON_TERMINAL_STATUSES)
        assert TERMINAL_STATUSES | NON_TERMINAL_STATUSES == set(TaskStatus)

    def test_plain_string_membership(self):
        # Callers compare bare DB strings against the sets.
        assert "success" in TERMINAL_STATUSES
        assert "calibrating" in NON_TERMINAL_STATUSES


class TestHttpCodeMapping:
    """HTTP code derived from status: 202 in-flight, 200 done, 500 failed."""

    def test_in_flight_states_map_to_202(self):
        assert http_code_for_status(TaskStatus.QUEUED) == 202
        assert http_code_for_status(TaskStatus.PROCESSING) == 202
        assert http_code_for_status(TaskStatus.CALIBRATING) == 202

    def test_success_maps_to_200(self):
        assert http_code_for_status(TaskStatus.SUCCESS) == 200

    def test_failed_maps_to_500(self):
        assert http_code_for_status(TaskStatus.FAILED) == 500

    def test_accepts_plain_strings(self):
        assert http_code_for_status("calibrating") == 202
        assert http_code_for_status("success") == 200
        assert http_code_for_status("failed") == 500

    def test_unknown_status_defaults_to_200(self):
        assert http_code_for_status("weird-unknown") == 200
