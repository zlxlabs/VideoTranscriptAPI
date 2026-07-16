import datetime
import json
import shutil
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from video_transcript_api.cache.cache_manager import CacheManager
from video_transcript_api.llm.core.speaker_inferencer import SpeakerInferencer
from video_transcript_api.llm.coordinator import LLMCoordinator


def _dialogs():
    return [
        {"speaker": "Speaker1", "text": "Hello", "start_time": 0.0},
        {"speaker": "Speaker2", "text": "World", "start_time": 1.0},
    ]


def _seed_media(cache):
    cache.save_cache(
        platform="youtube",
        url="https://example.com/watch?v=1",
        title="Title",
        author="Author",
        description="",
        media_id="media-1",
        transcript_data={"segments": _dialogs()},
        transcript_type="funasr",
        use_speaker_recognition=True,
    )


def test_artifact_uses_files_loc_and_validates_fingerprint(tmp_path):
    cache = CacheManager(str(tmp_path / "cache"))
    _seed_media(cache)
    speakers = ["Speaker1", "Speaker2"]
    fingerprint = SpeakerInferencer.input_fingerprint(speakers, _dialogs())
    result = {
        "mapping": {"Speaker1": "Alice", "Speaker2": "Bob"},
        "meta": {
            "Speaker1": {"name": "Alice", "confidence": 0.9, "applied": True},
            "Speaker2": {"name": "Bob", "confidence": 0.9, "applied": True},
        },
        "low_confidence": [],
    }

    cache.save_speaker_mapping(
        "youtube", "media-1", result,
        input_fingerprint=fingerprint, speakers=speakers, source="llm",
    )
    record = cache.get_cache("youtube", "media-1", use_speaker_recognition=True)
    artifact_path = cache.cache_dir / record["files_loc"] / "speaker_mapping.json"
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert artifact["schema_version"] == 1
    assert artifact["input_fingerprint"] == fingerprint
    assert artifact["speakers"] == speakers
    assert artifact["source"] == "llm"
    assert cache.get_speaker_mapping(
        "youtube", "media-1", input_fingerprint=fingerprint, speakers=speakers
    ) == result
    assert cache.get_speaker_mapping(
        "youtube", "media-1", input_fingerprint="stale", speakers=speakers
    ) is None


def test_invalidate_speaker_mapping_removes_the_artifact(tmp_path):
    """T5 (local Codex review round 4): the speaker-name-only refresh path
    (api/services/llm_ops.py::_refresh_speaker_names_in_existing_structured_
    artifact) rolls back a just-persisted speaker_mapping.json when
    refreshing the displayed structured artifact fails, so the next request
    treats it as a cache miss and genuinely retries instead of silently
    keeping a mapping that no longer matches what is actually displayed.
    invalidate_speaker_mapping is the primitive that rollback relies on."""
    cache = CacheManager(str(tmp_path / "cache"))
    _seed_media(cache)
    speakers = ["Speaker1", "Speaker2"]
    fingerprint = SpeakerInferencer.input_fingerprint(speakers, _dialogs())
    result = {
        "mapping": {"Speaker1": "Alice", "Speaker2": "Bob"},
        "meta": {
            "Speaker1": {"name": "Alice", "confidence": 0.9, "applied": True},
            "Speaker2": {"name": "Bob", "confidence": 0.9, "applied": True},
        },
        "low_confidence": [],
    }
    cache.save_speaker_mapping(
        "youtube", "media-1", result,
        input_fingerprint=fingerprint, speakers=speakers, source="llm",
    )
    assert cache.get_speaker_mapping(
        "youtube", "media-1", input_fingerprint=fingerprint, speakers=speakers
    ) == result

    cache.invalidate_speaker_mapping("youtube", "media-1")

    assert cache.get_speaker_mapping(
        "youtube", "media-1", input_fingerprint=fingerprint, speakers=speakers
    ) is None
    record = cache.get_cache("youtube", "media-1", use_speaker_recognition=True)
    artifact_path = cache.cache_dir / record["files_loc"] / "speaker_mapping.json"
    assert not artifact_path.exists()


def test_invalidate_speaker_mapping_is_a_no_op_when_nothing_to_clean_up(tmp_path):
    """Both "media never cached at all" and "media cached but no mapping
    file yet" must be silent no-ops -- rollback runs from an error-handling
    path and must never itself raise or require the caller to check
    existence first."""
    cache = CacheManager(str(tmp_path / "cache"))

    cache.invalidate_speaker_mapping("youtube", "no-such-media")  # must not raise

    _seed_media(cache)
    cache.invalidate_speaker_mapping("youtube", "media-1")  # must not raise


def test_artifact_follows_files_loc_across_months(tmp_path):
    """Regression test for the "current-month recompute" bug.

    ``_speaker_artifact_dir`` must resolve the artifact directory strictly
    from the persisted ``video_cache.files_loc`` column. A prior (buggy)
    implementation instead rebuilt the path from ``datetime.now()``, which
    happens to produce the *same* directory as the DB-driven approach right
    after seeding (both point at "this month") -- so a naive test cannot
    tell the two implementations apart. Here we force ``files_loc`` to a
    month far from "now" (and physically relocate the seeded cache files
    there) so the two strategies disagree, and assert save/get follow the
    persisted location rather than recomputing it.
    """
    cache = CacheManager(str(tmp_path / "cache"))
    _seed_media(cache)

    # Grab the just-seeded directory before mutating anything -- the
    # directory still exists on disk at this point, so get_cache() will not
    # trigger its "missing directory -> delete the row" cleanup path.
    seeded_record = cache.get_cache("youtube", "media-1", use_speaker_recognition=True)
    seeded_dir = cache.cache_dir / seeded_record["files_loc"]

    # Pick a month guaranteed to differ from the current one (>1 year back
    # covers any month/leap-year boundary) and relocate the seeded files
    # there, then point video_cache.files_loc at the new location directly
    # via SQL -- mirroring how a video cached long ago would look today.
    past = datetime.datetime.now() - datetime.timedelta(days=400)
    persisted_files_loc = f"youtube/{past.strftime('%Y')}/{past.strftime('%Y%m')}/media-1"
    persisted_dir = cache.cache_dir / persisted_files_loc
    persisted_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(seeded_dir), str(persisted_dir))
    with cache._get_cursor() as cursor:
        cursor.execute(
            "UPDATE video_cache SET files_loc=? WHERE platform=? AND media_id=?",
            (persisted_files_loc, "youtube", "media-1"),
        )

    speakers = ["Speaker1", "Speaker2"]
    fingerprint = SpeakerInferencer.input_fingerprint(speakers, _dialogs())
    result = {
        "mapping": {"Speaker1": "Alice", "Speaker2": "Bob"},
        "meta": {
            "Speaker1": {"name": "Alice", "confidence": 0.9, "applied": True},
            "Speaker2": {"name": "Bob", "confidence": 0.9, "applied": True},
        },
        "low_confidence": [],
    }
    cache.save_speaker_mapping(
        "youtube", "media-1", result,
        input_fingerprint=fingerprint, speakers=speakers, source="llm",
    )

    # The artifact must land under the persisted (non-current-month) dir...
    assert (persisted_dir / "speaker_mapping.json").exists()

    # ...and NOT under whatever directory a current-month recompute would
    # have used instead.
    now = datetime.datetime.now()
    recomputed_dir = (
        cache.cache_dir / "youtube" / now.strftime("%Y") / now.strftime("%Y%m") / "media-1"
    )
    assert recomputed_dir != persisted_dir
    assert not (recomputed_dir / "speaker_mapping.json").exists()

    assert cache.get_speaker_mapping(
        "youtube", "media-1", input_fingerprint=fingerprint, speakers=speakers
    ) == result


def test_artifact_rejects_mapping_that_replaces_an_expected_speaker(tmp_path):
    """R5 (PR3 review hardening) updated this test's expectation: this
    "invalid" payload is missing "Speaker2" from mapping/meta -- before the
    fix, save_speaker_mapping only validated the `source` field, so this
    write silently succeeded on disk and only get_speaker_mapping's
    (read-side) deep validation caught it later, one request too late (and
    only for THIS particular malformed shape -- non-str name / bool
    confidence would have gone through the writer entirely unchecked).
    Reader and writer now share the exact same shape validator
    (_speaker_mapping_result_is_valid); the write itself must refuse to
    persist this payload, matching the same "fail closed" contract
    save_speaker_mapping already applies to `source != 'llm'`."""
    cache = CacheManager(str(tmp_path / "cache"))
    _seed_media(cache)
    speakers = ["Speaker1", "Speaker2"]
    fingerprint = SpeakerInferencer.input_fingerprint(speakers, _dialogs())
    invalid = {
        "mapping": {"Speaker1": "Alice", "Unrelated": "Mallory"},
        "meta": {"Speaker1": {}, "Unrelated": {}},
    }
    with pytest.raises(ValueError):
        cache.save_speaker_mapping(
            "youtube", "media-1", invalid,
            input_fingerprint=fingerprint, speakers=speakers, source="llm",
        )

    record = cache.get_cache("youtube", "media-1", use_speaker_recognition=True)
    artifact_path = cache.cache_dir / record["files_loc"] / "speaker_mapping.json"
    assert not artifact_path.exists(), "rejected payload must never be persisted"

    assert cache.get_speaker_mapping(
        "youtube", "media-1", input_fingerprint=fingerprint, speakers=speakers
    ) is None


def test_artifact_non_object_json_is_a_cache_miss(tmp_path):
    cache = CacheManager(str(tmp_path / "cache"))
    _seed_media(cache)
    record = cache.get_cache("youtube", "media-1", use_speaker_recognition=True)
    artifact_path = cache.cache_dir / record["files_loc"] / "speaker_mapping.json"
    artifact_path.write_text("[]", encoding="utf-8")

    assert cache.get_speaker_mapping(
        "youtube", "media-1", input_fingerprint="any", speakers=["Speaker1"]
    ) is None


@pytest.mark.parametrize(
    "corrupt_speakers_value",
    [42, True, 3.14],
    ids=["int", "bool", "float"],
)
def test_artifact_non_iterable_speakers_field_is_a_cache_miss_not_a_crash(
    tmp_path, corrupt_speakers_value,
):
    """Codex review finding (乙4): get_speaker_mapping's shape validation
    does `set(payload.get("speakers") or [])`, which raises an uncaught
    TypeError when the persisted "speakers" field is a truthy non-iterable
    scalar (int/bool/float -- all valid JSON, just corrupted data). This
    exception previously propagated straight out of get_speaker_mapping,
    through SpeakerInferencer.infer() (which does not wrap the call in
    try/except), turning one damaged artifact into a hard task failure
    instead of a harmless cache miss / re-inference. Design requirement:
    corrupted artifacts must degrade to None, never raise.
    """
    cache = CacheManager(str(tmp_path / "cache"))
    _seed_media(cache)
    record = cache.get_cache("youtube", "media-1", use_speaker_recognition=True)
    artifact_path = cache.cache_dir / record["files_loc"] / "speaker_mapping.json"
    artifact_path.write_text(
        json.dumps({
            "schema_version": 1,
            "input_fingerprint": "fp",
            "speakers": corrupt_speakers_value,
            "source": "llm",
            "result": {
                "mapping": {"Speaker1": "Alice"},
                "meta": {"Speaker1": {}},
            },
        }),
        encoding="utf-8",
    )

    assert cache.get_speaker_mapping(
        "youtube", "media-1", input_fingerprint="fp", speakers=["Speaker1"]
    ) is None


@pytest.mark.parametrize(
    "corrupt_result",
    [
        # meta[speaker] itself is not a dict at all.
        {"mapping": {"Speaker1": "Alice"}, "meta": {"Speaker1": "not-a-dict"}},
        # meta[speaker] is a dict but missing "name".
        {"mapping": {"Speaker1": "Alice"}, "meta": {"Speaker1": {"confidence": 0.9}}},
        # meta[speaker]["name"] has the wrong type.
        {"mapping": {"Speaker1": "Alice"},
         "meta": {"Speaker1": {"name": 123, "confidence": 0.9}}},
        # meta[speaker] is a dict but missing "confidence".
        {"mapping": {"Speaker1": "Alice"}, "meta": {"Speaker1": {"name": "Alice"}}},
        # meta[speaker]["confidence"] has the wrong type.
        {"mapping": {"Speaker1": "Alice"},
         "meta": {"Speaker1": {"name": "Alice", "confidence": "high"}}},
        # confidence is a bool -- a valid int subclass in Python, but not a
        # meaningful confidence value; must still be rejected.
        {"mapping": {"Speaker1": "Alice"},
         "meta": {"Speaker1": {"name": "Alice", "confidence": True}}},
        # low_confidence is a non-iterable scalar.
        {"mapping": {"Speaker1": "Alice"},
         "meta": {"Speaker1": {"name": "Alice", "confidence": 0.9}},
         "low_confidence": 42},
        # low_confidence is a bare string -- technically iterable
        # (char-by-char), which must NOT be mistaken for a valid list of
        # speaker labels.
        {"mapping": {"Speaker1": "Alice"},
         "meta": {"Speaker1": {"name": "Alice", "confidence": 0.9}},
         "low_confidence": "Speaker1"},
        # low_confidence is a list but its items are not strings.
        {"mapping": {"Speaker1": "Alice"},
         "meta": {"Speaker1": {"name": "Alice", "confidence": 0.9}},
         "low_confidence": [1, 2]},
        # Y3 (PR3 review hardening 加固轮): mapping[speaker] itself (the
        # display name actually rendered/substituted downstream) has the
        # wrong type -- meta[speaker]["name"] being a valid str does NOT
        # guarantee mapping[speaker] is, since the two fields are written
        # independently.
        {"mapping": {"Speaker1": 123},
         "meta": {"Speaker1": {"name": "Alice", "confidence": 0.9}}},
        {"mapping": {"Speaker1": None},
         "meta": {"Speaker1": {"name": "Alice", "confidence": 0.9}}},
        {"mapping": {"Speaker1": ["Alice"]},
         "meta": {"Speaker1": {"name": "Alice", "confidence": 0.9}}},
        # mapping[speaker] is an empty string -- not a meaningful display name.
        {"mapping": {"Speaker1": ""},
         "meta": {"Speaker1": {"name": "Alice", "confidence": 0.9}}},
    ],
    ids=[
        "meta_entry_not_dict",
        "meta_entry_missing_name",
        "meta_entry_name_wrong_type",
        "meta_entry_missing_confidence",
        "meta_entry_confidence_wrong_type",
        "meta_entry_confidence_is_bool",
        "low_confidence_not_iterable",
        "low_confidence_is_bare_string",
        "low_confidence_contains_non_strings",
        "mapping_value_wrong_type_int",
        "mapping_value_wrong_type_none",
        "mapping_value_wrong_type_list",
        "mapping_value_empty_string",
    ],
)
def test_artifact_deep_malformed_meta_or_low_confidence_is_a_cache_miss_not_a_crash(
    tmp_path, corrupt_result,
):
    """Local codex review round 5, F5: get_speaker_mapping's shape
    validation previously only checked that mapping/meta are dicts whose
    keys cover the expected speakers set -- it never looked *inside* each
    meta entry. Legitimate JSON with deep structural corruption (a
    meta[speaker] entry that isn't a dict, is missing name/confidence, or
    a low_confidence field that isn't an iterable of strings) used to sail
    straight through this check and only blow up downstream, before any
    LLM fallback ever runs: SpeakerInferencer.infer()'s cache-hit path does
    direct meta[speaker]["name"]/["confidence"] subscripting (TypeError for
    a non-dict entry, KeyError for a missing key), and
    _normalize_cached_result's list(low_confidence) call raises TypeError
    for a non-iterable. Neither call site wraps this in try/except, so one
    corrupted artifact turned a harmless cache-miss-and-re-infer into a
    hard task failure. Every shape of corruption here must degrade to None
    at this single choke point instead."""
    cache = CacheManager(str(tmp_path / "cache"))
    _seed_media(cache)
    speakers = ["Speaker1"]
    fingerprint = SpeakerInferencer.input_fingerprint(speakers, _dialogs())
    record = cache.get_cache("youtube", "media-1", use_speaker_recognition=True)
    artifact_path = cache.cache_dir / record["files_loc"] / "speaker_mapping.json"
    artifact_path.write_text(
        json.dumps({
            "schema_version": 1,
            "input_fingerprint": fingerprint,
            "speakers": speakers,
            "source": "llm",
            "result": corrupt_result,
        }),
        encoding="utf-8",
    )

    assert cache.get_speaker_mapping(
        "youtube", "media-1", input_fingerprint=fingerprint, speakers=speakers
    ) is None


@pytest.mark.parametrize(
    "corrupt_result",
    [
        {"mapping": {"Speaker1": "Alice"}, "meta": {"Speaker1": "not-a-dict"}},
        {"mapping": {"Speaker1": "Alice"}, "meta": {"Speaker1": {"confidence": 0.9}}},
        {"mapping": {"Speaker1": "Alice"},
         "meta": {"Speaker1": {"name": 123, "confidence": 0.9}}},
        {"mapping": {"Speaker1": "Alice"}, "meta": {"Speaker1": {"name": "Alice"}}},
        {"mapping": {"Speaker1": "Alice"},
         "meta": {"Speaker1": {"name": "Alice", "confidence": "high"}}},
        # confidence is a bool -- a valid int subclass in Python, but not a
        # meaningful confidence value; must still be rejected.
        {"mapping": {"Speaker1": "Alice"},
         "meta": {"Speaker1": {"name": "Alice", "confidence": True}}},
        {"mapping": {"Speaker1": "Alice"},
         "meta": {"Speaker1": {"name": "Alice", "confidence": 0.9}},
         "low_confidence": 42},
        {"mapping": {"Speaker1": "Alice"},
         "meta": {"Speaker1": {"name": "Alice", "confidence": 0.9}},
         "low_confidence": "Speaker1"},
        {"mapping": {"Speaker1": "Alice"},
         "meta": {"Speaker1": {"name": "Alice", "confidence": 0.9}},
         "low_confidence": [1, 2]},
        {"mapping": {"Speaker1": 123},
         "meta": {"Speaker1": {"name": "Alice", "confidence": 0.9}}},
        {"mapping": {"Speaker1": None},
         "meta": {"Speaker1": {"name": "Alice", "confidence": 0.9}}},
        {"mapping": {"Speaker1": ["Alice"]},
         "meta": {"Speaker1": {"name": "Alice", "confidence": 0.9}}},
        {"mapping": {"Speaker1": ""},
         "meta": {"Speaker1": {"name": "Alice", "confidence": 0.9}}},
    ],
    ids=[
        "meta_entry_not_dict",
        "meta_entry_missing_name",
        "meta_entry_name_wrong_type",
        "meta_entry_missing_confidence",
        "meta_entry_confidence_wrong_type",
        "meta_entry_confidence_is_bool",
        "low_confidence_not_iterable",
        "low_confidence_is_bare_string",
        "low_confidence_contains_non_strings",
        "mapping_value_wrong_type_int",
        "mapping_value_wrong_type_none",
        "mapping_value_wrong_type_list",
        "mapping_value_empty_string",
    ],
)
def test_save_speaker_mapping_rejects_shapes_the_reader_would_reject(
    tmp_path, corrupt_result,
):
    """R5 (PR3 review hardening): the write side (save_speaker_mapping)
    must reject every one of the same deeply-malformed shapes the read side
    (get_speaker_mapping, exercised by the parametrized test above using
    the identical corrupt_result fixtures) already rejects -- reader and
    writer now share one validator (_speaker_mapping_result_is_valid), so
    there is no longer a gap where a write-time bug (e.g. an LLM response
    with a non-str name or a bool confidence) can persist a payload that
    the reader would immediately treat as a cache miss on the very next
    request, silently burning another LLM call every time. The persisted
    file must never even be created."""
    cache = CacheManager(str(tmp_path / "cache"))
    _seed_media(cache)
    speakers = ["Speaker1"]
    fingerprint = SpeakerInferencer.input_fingerprint(speakers, _dialogs())

    with pytest.raises(ValueError):
        cache.save_speaker_mapping(
            "youtube", "media-1", corrupt_result,
            input_fingerprint=fingerprint, speakers=speakers, source="llm",
        )

    record = cache.get_cache("youtube", "media-1", use_speaker_recognition=True)
    artifact_path = cache.cache_dir / record["files_loc"] / "speaker_mapping.json"
    assert not artifact_path.exists(), "rejected payload must never be persisted"


def test_deep_malformed_artifact_falls_back_to_real_inference_not_a_task_failure(
    tmp_path,
):
    """Full-chain companion to the parametrized test above: proves the fix
    is effective at the actual consumption site, not just at
    get_speaker_mapping's own return value. Uses the REAL CacheManager
    (with the fix) and the REAL SpeakerInferencer (only the LLM boundary is
    mocked) against a deeply corrupted on-disk artifact -- before the fix,
    this exact setup raised TypeError out of infer()'s cache-hit block
    (meta["Speaker1"] is a plain string, so ["name"] subscripting fails)
    and the whole task would have failed instead of quietly re-inferring
    via the LLM, which is the designed self-healing behavior for any
    "this cached artifact happens to be damaged" scenario."""
    cache = CacheManager(str(tmp_path / "cache"))
    _seed_media(cache)
    speakers = ["Speaker1", "Speaker2"]
    dialogs = _dialogs()
    fingerprint = SpeakerInferencer.input_fingerprint(speakers, dialogs)
    record = cache.get_cache("youtube", "media-1", use_speaker_recognition=True)
    artifact_path = cache.cache_dir / record["files_loc"] / "speaker_mapping.json"
    artifact_path.write_text(
        json.dumps({
            "schema_version": 1,
            "input_fingerprint": fingerprint,
            "speakers": speakers,
            "source": "llm",
            "result": {
                # Speaker1's meta entry is a bare string, not a dict --
                # exactly the shape that used to crash the cache-hit path.
                "mapping": {"Speaker1": "Alice", "Speaker2": "Bob"},
                "meta": {"Speaker1": "corrupted", "Speaker2": {"name": "Bob", "confidence": 0.9}},
                "low_confidence": [],
            },
        }),
        encoding="utf-8",
    )

    llm = MagicMock()
    llm.call.return_value = SimpleNamespace(structured_output={
        "speaker_mapping": {"Speaker1": "Alice", "Speaker2": "Bob"},
        "confidence": {"Speaker1": 0.95, "Speaker2": 0.95},
    })
    inferencer = SpeakerInferencer(llm, cache_manager=cache)

    result = inferencer.infer(
        speakers, dialogs, "Title",
        platform="youtube", media_id="media-1", allow_llm=True,
    )

    # The corrupted cache must not have been treated as a hit -- the real
    # inference path (LLM call) must have taken over instead of crashing.
    llm.call.assert_called_once()
    assert result["mapping"] == {"Speaker1": "Alice", "Speaker2": "Bob"}


def test_legacy_cache_reader_unwraps_versioned_artifact(tmp_path, monkeypatch):
    from video_transcript_api.llm.core.cache_manager import CacheManager as LLMCacheManager

    cache = LLMCacheManager(str(tmp_path))
    artifact_dir = tmp_path / "youtube" / "2026" / "202607" / "media-1"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "speaker_mapping.json").write_text(
        json.dumps({"schema_version": 1, "result": {"Speaker1": "Alice"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cache, "_get_video_cache_dir", lambda *args: artifact_dir)

    assert cache.get_speaker_mapping("youtube", "media-1") == {"Speaker1": "Alice"}


def test_numeric_zero_is_a_valid_speaker_id():
    from video_transcript_api.api.services.transcription import _extract_speaker_labels

    assert _extract_speaker_labels([
        {"speaker_id": 0, "text": "zero"},
        {"speaker_id": 1, "text": "one"},
    ]) == ["0", "1"]

    fingerprint_zero = SpeakerInferencer.input_fingerprint(
        ["0"], [{"speaker_id": 0, "text": "same"}]
    )
    fingerprint_unknown = SpeakerInferencer.input_fingerprint(
        ["0"], [{"text": "same"}]
    )
    assert fingerprint_zero != fingerprint_unknown


def test_coerce_dialogs_preserves_numeric_zero_speaker_fingerprint():
    """_coerce_dialogs is the save-side normalization whose output feeds the
    persisted speaker_mapping fingerprint. It must agree with the precheck-side
    fingerprint (computed straight from raw dialogs) for speaker id 0, or the
    layered cache precheck never matches what got saved -- permanently
    invalidating the cache for any dialog using numeric speaker id 0.
    """
    from video_transcript_api.llm.core.config import LLMConfig
    from video_transcript_api.llm.processors.speaker_aware_processor import (
        SpeakerAwareProcessor,
    )

    processor = SpeakerAwareProcessor(
        config=LLMConfig(
            api_key="k", base_url="http://test",
            calibrate_model="test-model", summary_model="test-model",
        ),
        llm_client=MagicMock(),
        key_info_extractor=MagicMock(),
        speaker_inferencer=MagicMock(),
        quality_validator=MagicMock(),
    )

    raw_dialogs = [{"spk": 0, "text": "zero"}, {"spk": 1, "text": "one"}]
    coerced = processor._coerce_dialogs(raw_dialogs)

    assert coerced[0]["speaker"] == "0"
    assert coerced[1]["speaker"] == "1"

    fingerprint_raw = SpeakerInferencer.input_fingerprint(["0", "1"], raw_dialogs)
    fingerprint_coerced = SpeakerInferencer.input_fingerprint(["0", "1"], coerced)
    assert fingerprint_coerced == fingerprint_raw


def test_extract_speaker_labels_skips_empty_text_dialogs():
    """H7 (local codex review round 7): a speaker whose only appearance in
    the raw dialog list is an empty-text entry must not show up in the
    read-side speaker label list -- _coerce_dialogs (the save-side
    normalization) drops empty-text dialogs before deriving its speakers
    list, so a speaker that only exists via empty-text lines is invisible
    to the save side. If the read-side precheck (this function) still
    counted it, the two sides would permanently disagree about which
    speakers exist for the same input."""
    from video_transcript_api.api.services.transcription import _extract_speaker_labels

    labels = _extract_speaker_labels([
        {"speaker_id": "S1", "text": "hello"},
        {"speaker_id": "S2", "text": ""},
        {"speaker_id": "S3", "text": None},
    ])

    assert labels == ["S1"], (
        "speakers that only ever appear in empty-text dialogs must be "
        "excluded, matching _coerce_dialogs' filtering"
    )


def test_coerce_dialogs_and_extract_speaker_labels_agree_on_empty_text_dialogs():
    """H7 (local codex review round 7): the fingerprint bug class this round
    fixes -- same shape as test_coerce_dialogs_preserves_numeric_zero_
    speaker_fingerprint above, but for a speaker that only ever appears in
    an empty-text dialog rather than a falsy-but-valid speaker id.

    Before the fix: transcription.py's precheck-side _extract_speaker_labels
    did not filter by text at all, so it counted "S2" (empty-text-only)
    as a real speaker -- but the save-side path (SpeakerAwareProcessor.
    _coerce_dialogs filters out empty-text dialogs first, then derives its
    speakers list from what remains) never sees "S2" at all. The two sides'
    speaker lists therefore permanently disagreed for any input containing
    a speaker with only empty-text lines, so input_fingerprint(speakers,
    dialogs) never matched between precheck and save -- the layered cache
    always missed for this shape of input, silently re-running speaker
    inference (and rewriting the artifact) on every single request.
    """
    from video_transcript_api.llm.core.config import LLMConfig
    from video_transcript_api.llm.processors.speaker_aware_processor import (
        SpeakerAwareProcessor,
    )
    from video_transcript_api.api.services.transcription import _extract_speaker_labels

    processor = SpeakerAwareProcessor(
        config=LLMConfig(
            api_key="k", base_url="http://test",
            calibrate_model="test-model", summary_model="test-model",
        ),
        llm_client=MagicMock(),
        key_info_extractor=MagicMock(),
        speaker_inferencer=MagicMock(),
        quality_validator=MagicMock(),
    )

    # S2 only ever appears in an empty-text dialog.
    raw_dialogs = [
        {"speaker_id": "S1", "text": "hello"},
        {"speaker_id": "S2", "text": ""},
    ]

    read_side_speakers = _extract_speaker_labels(raw_dialogs)
    coerced = processor._coerce_dialogs(raw_dialogs)
    write_side_speakers = [d["speaker"] for d in coerced]

    assert read_side_speakers == write_side_speakers == ["S1"], (
        "read side (precheck) and write side (save) must agree on the "
        "speaker set for the same raw dialogs"
    )

    fingerprint_read_side = SpeakerInferencer.input_fingerprint(
        read_side_speakers, raw_dialogs
    )
    fingerprint_write_side = SpeakerInferencer.input_fingerprint(
        write_side_speakers, coerced
    )
    assert fingerprint_read_side == fingerprint_write_side, (
        "a real request's precheck fingerprint must match what actually "
        "gets persisted, or the cache can never hit for inputs shaped "
        "like this"
    )


def test_disabled_inference_reuses_valid_mapping_without_llm():
    cache = MagicMock()
    cache.get_speaker_mapping.return_value = {
        "mapping": {"Speaker1": "Alice", "Speaker2": "Bob"},
        "meta": {
            "Speaker1": {"name": "Alice", "confidence": 0.9, "applied": True},
            "Speaker2": {"name": "Bob", "confidence": 0.9, "applied": True},
        },
        "low_confidence": [],
    }
    llm = MagicMock()
    inferencer = SpeakerInferencer(llm, cache_manager=cache)
    result = inferencer.infer(
        ["Speaker1", "Speaker2"], _dialogs(), "Title",
        platform="youtube", media_id="media-1", allow_llm=False,
    )
    assert result["mapping"] == {"Speaker1": "Alice", "Speaker2": "Bob"}
    llm.call.assert_not_called()
    cache.save_speaker_mapping.assert_not_called()


def test_disabled_inference_cache_miss_returns_identity_without_persisting():
    cache = MagicMock()
    cache.get_speaker_mapping.return_value = None
    llm = MagicMock()
    inferencer = SpeakerInferencer(llm, cache_manager=cache)
    result = inferencer.infer(
        ["Speaker1", "Speaker2"], _dialogs(), "Title",
        platform="youtube", media_id="media-1", allow_llm=False,
    )
    assert result["mapping"] == {"Speaker1": "Speaker1", "Speaker2": "Speaker2"}
    llm.call.assert_not_called()
    cache.save_speaker_mapping.assert_not_called()


def test_all_processing_features_disabled_make_zero_llm_calls(tmp_path):
    coordinator = LLMCoordinator(
        config_dict={
            "llm": {
                "api_key": "test",
                "base_url": "https://example.invalid",
                "calibrate_model": "test-model",
                "summary_model": "test-model",
            }
        },
        cache_dir=str(tmp_path),
    )
    coordinator.llm_client.call = MagicMock()

    result = coordinator.process(
        content=_dialogs(),
        title="Title",
        platform="youtube",
        media_id="media-1",
        skip_calibration=True,
        skip_summary=True,
        infer_speaker_names=False,
    )

    coordinator.llm_client.call.assert_not_called()
    assert result["structured_data"]["speaker_mapping"] == {
        "Speaker1": "Speaker1",
        "Speaker2": "Speaker2",
    }


def test_infer_speaker_names_alone_still_calls_llm_for_name_inference(tmp_path):
    """Matrix case locking in already-decided semantics: calibrate=False +
    summarize=False must NOT silence infer_speaker_names=True. Name inference
    is a "transcription" deliverable, independent from calibration/summary,
    so it must still reach the LLM while calibration and summary make zero
    calls. Real coordinator + real processors; only the LLM boundary is
    mocked (same style as test_all_processing_features_disabled_make_zero_llm_calls
    above, which locks the opposite corner of this matrix).
    """
    coordinator = LLMCoordinator(
        config_dict={
            "llm": {
                "api_key": "test",
                "base_url": "https://example.invalid",
                "calibrate_model": "test-model",
                "summary_model": "test-model",
            }
        },
        cache_dir=str(tmp_path),
    )

    def _fake_llm_call(**kwargs):
        task_type = kwargs.get("task_type")
        if task_type == "key_info":
            return SimpleNamespace(structured_output={
                "names": [], "places": [], "technical_terms": [], "brands": [],
                "abbreviations": [], "foreign_terms": [], "other_entities": [],
            })
        if task_type == "speaker_inference":
            return SimpleNamespace(structured_output={
                "speaker_mapping": {"Speaker1": "Alice", "Speaker2": "Bob"},
                "confidence": {"Speaker1": 0.95, "Speaker2": 0.95},
            })
        raise AssertionError(
            f"unexpected LLM call while calibration/summary are disabled: "
            f"task_type={task_type!r}"
        )

    coordinator.llm_client.call = MagicMock(side_effect=_fake_llm_call)

    result = coordinator.process(
        content=_dialogs(),
        title="Title",
        platform="youtube",
        media_id="media-1",
        skip_calibration=True,
        skip_summary=True,
        infer_speaker_names=True,
    )

    # Name inference did reach the LLM ...
    assert coordinator.llm_client.call.call_count >= 1
    called_task_types = {
        call.kwargs.get("task_type")
        for call in coordinator.llm_client.call.call_args_list
    }
    assert called_task_types <= {"key_info", "speaker_inference"}
    # ... and calibration/summary made zero calls (would have raised above
    # via _fake_llm_call's else branch if they had).
    disabled_task_types = {
        "calibrate_segment", "calibrate_segment_retry", "calibrate_chunk",
        "summary", "quality_validation", "unified_quality_validation",
    }
    assert not (called_task_types & disabled_task_types)
    assert result["structured_data"]["speaker_mapping"] == {
        "Speaker1": "Alice", "Speaker2": "Bob",
    }
