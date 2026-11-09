"""Regression tests for atomic publication of transcript bodies and sidecars.

Console output: English only, no emoji.
"""
import threading

import pytest

from src.video_transcript_api.cache import cache_manager as cache_manager_module
from src.video_transcript_api.cache.cache_manager import CacheManager
from src.video_transcript_api.transcriber.segments import load_segments


@pytest.fixture
def cm(tmp_path):
    manager = CacheManager(cache_dir=str(tmp_path / "cache"))
    yield manager
    manager.close()


def _save_group(cm, body, segment_text):
    return cm.save_cache(
        platform="youtube",
        url="https://example.com/generation-test",
        media_id="generation-test",
        use_speaker_recognition=False,
        transcript_data=body,
        transcript_type="capswriter",
        extra_json_data={
            "segments": [
                {"start_time": 0, "end_time": 1, "text": segment_text}
            ]
        },
    )


def _assert_complete_group(cache_data, allowed_groups):
    assert cache_data is None or any(
        cache_data["transcript_data"] == body
        and cache_data.get("segments", [{}])[0].get("text") == segment
        for body, segment in allowed_groups
    ), "cache read returned a mixed or partial transcript generation"


def _assert_segments_are_one_known_generation(segments, allowed_texts):
    assert segments is None or (
        len(segments) == 1 and segments[0]["text"] in allowed_texts
    ), "segment reader returned data outside the published generations"


def test_second_file_replace_failure_never_exposes_mixed_group(
    cm, monkeypatch,
):
    old_group = ("OLD BODY", "OLD SEGMENT")
    new_group = ("NEW BODY", "NEW SEGMENT")
    initial = _save_group(cm, *old_group)
    assert initial is not None

    real_replace = cache_manager_module.os.replace
    replace_count = 0

    def fail_second_replace(source, target):
        nonlocal replace_count
        replace_count += 1
        if replace_count == 2:
            raise OSError("injected second artifact replacement failure")
        return real_replace(source, target)

    monkeypatch.setattr(cache_manager_module.os, "replace", fail_second_replace)
    assert _save_group(cm, *new_group) is None
    assert replace_count == 2, "the failure must occur at the second artifact replacement"

    cache_data = cm.get_cache(
        platform="youtube", media_id="generation-test", use_speaker_recognition=False
    )
    _assert_complete_group(cache_data, [old_group, new_group])

    media_dir = (
        cm.cache_dir / cache_data["files_loc"]
        if cache_data is not None
        else cm.cache_dir / initial["files_loc"]
    )
    _assert_segments_are_one_known_generation(
        load_segments(media_dir), {old_group[1], new_group[1]}
    )


def test_reader_between_artifact_replacements_never_sees_mixed_group(
    cm, monkeypatch,
):
    old_group = ("OLD BODY", "OLD SEGMENT")
    new_group = ("NEW BODY", "NEW SEGMENT")
    initial = _save_group(cm, *old_group)
    assert initial is not None

    first_replace_done = threading.Event()
    continue_writer = threading.Event()
    save_result = {}
    real_replace = cache_manager_module.os.replace
    replace_count = 0

    def pause_after_first_replace(source, target):
        nonlocal replace_count
        replace_count += 1
        result = real_replace(source, target)
        if replace_count == 1:
            first_replace_done.set()
            assert continue_writer.wait(timeout=5), "writer release timed out"
        return result

    monkeypatch.setattr(cache_manager_module.os, "replace", pause_after_first_replace)

    def save_new_group():
        save_result["value"] = _save_group(cm, *new_group)

    writer = threading.Thread(target=save_new_group)
    writer.start()
    try:
        assert first_replace_done.wait(timeout=3), "writer never reached first replacement"
        cache_data = cm.get_cache(
            platform="youtube", media_id="generation-test", use_speaker_recognition=False
        )
        _assert_complete_group(cache_data, [old_group, new_group])

        media_dir = (
            cm.cache_dir / cache_data["files_loc"]
            if cache_data is not None
            else cm.cache_dir / initial["files_loc"]
        )
        _assert_segments_are_one_known_generation(
            load_segments(media_dir), {old_group[1], new_group[1]}
        )
    finally:
        continue_writer.set()
        writer.join(timeout=5)

    assert not writer.is_alive(), "writer did not finish after release"
    assert save_result.get("value") is not None
    final_data = cm.get_cache(
        platform="youtube", media_id="generation-test", use_speaker_recognition=False
    )
    _assert_complete_group(final_data, [new_group])
