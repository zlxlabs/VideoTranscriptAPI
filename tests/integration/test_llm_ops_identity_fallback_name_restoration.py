"""Integration tests: V2 + V3 (PR3 review hardening) -- identity_fallback
name restoration must (a) reach every consumer of the round's result, not
just the structured-data artifact, and (b) only fire when the old artifact
it borrows names from is provably describing the SAME diarization input as
this round.

Background: SpeakerInferencer.infer() falls back to an identity mapping
({label: label}) whenever it has no real inference to offer this round
(cache miss, allow_llm=False, no samples, or a transient LLM exception).
_restore_real_names_after_identity_fallback (llm_ops.py) exists to patch
those placeholder labels back to whatever real names an earlier successful
round already established, keyed by speaker_id, so a transient LLM hiccup
does not visibly regress "Speaker1" back onto the view page.

V2 bug: the restoration only ever patched result_dict["structured_data"].
calibrated_text ("校对文本") had already been pulled out of result_dict into
a local variable *before* the restore ran, and both the completion
notification and the task's terminal_snapshot consume the SAME result_dict
after _save_llm_results returns -- all three kept showing the
identity_fallback placeholder labels even though the structured artifact on
disk (and therefore the view page's dialog rendering) showed the restored
real names. Fix: run the restoration before any of those reads happen,
mutate result_dict in place (the single authoritative copy every consumer
shares), and apply the same speaker_id -> real name map to calibrated_text
via a line-leading-label regex replace (calibrated_text is built by
SpeakerAwareProcessor._build_text_from_dialogs as
"{speaker}：{text}"-per-line, joined by blank lines).

V3 bug: the restoration matched old_mapping purely by speaker_id string
equality, with no check that the old artifact it borrows from was produced
from the SAME diarization input as this round. Diarization labels
("SPEAKER_00", ...) are just ordinal cluster ids, not persistent identities
-- if the transcript was reprocessed and the clustering shuffled, the old
"SPEAKER_00 -> 张三" mapping may now describe a different physical person.
Fix: before restoring, recompute this round's true input fingerprint from
the transcript_data that has not changed since the transcription phase
(SpeakerInferencer.extract_speaker_labels/input_fingerprint, the same
primitives transcription.py's own layered-cache pre-check already uses) and
require it to exactly match what is stored in the media's independently
persisted speaker_mapping.json artifact (cache_manager.get_speaker_mapping)
-- llm_processed.json itself carries no fingerprint. A mismatch (or no
comparable fingerprint at all) skips restoration and leaves this round's
raw identity_fallback labels in place, rather than risk mis-attributing a
stranger's name.

All console output must be in English only (no emoji, no Chinese).
"""
from unittest.mock import MagicMock, patch

import pytest

from src.video_transcript_api.cache.cache_manager import CacheManager
from src.video_transcript_api.llm.core.speaker_inferencer import SpeakerInferencer
from src.video_transcript_api.utils.task_status import TaskStatus
from src.video_transcript_api.utils.llm_status import CalibrationStatus, SummaryStatus
from src.video_transcript_api.api.services import llm_ops


PLATFORM = "bilibili"
MEDIA_ID = "vidspk1"

TRANSCRIPT_SEGMENTS_ROUND_1 = [
    {"speaker": "SPEAKER_00", "text": "Hello there", "start": 0, "end": 1},
    {"speaker": "SPEAKER_01", "text": "Hi back", "start": 1, "end": 2},
]
TRANSCRIPT_DATA_ROUND_1 = {
    "speakers": ["SPEAKER_00", "SPEAKER_01"],
    "segments": TRANSCRIPT_SEGMENTS_ROUND_1,
}

# A materially different transcript -- different text content, so its
# input_fingerprint differs from TRANSCRIPT_DATA_ROUND_1's even though the
# raw diarization labels happen to be spelled the same way (labels are just
# ordinal cluster ids, not persistent identities across reprocessing runs).
TRANSCRIPT_SEGMENTS_ROUND_2_DIFFERENT_INPUT = [
    {"speaker": "SPEAKER_00", "text": "Completely different opening line", "start": 0, "end": 1},
    {"speaker": "SPEAKER_01", "text": "And a completely different reply", "start": 1, "end": 2},
]
TRANSCRIPT_DATA_ROUND_2_DIFFERENT_INPUT = {
    "speakers": ["SPEAKER_00", "SPEAKER_01"],
    "segments": TRANSCRIPT_SEGMENTS_ROUND_2_DIFFERENT_INPUT,
}

OLD_REAL_NAMES = {"SPEAKER_00": "张三", "SPEAKER_01": "李四"}


@pytest.fixture
def cm(tmp_path):
    manager = CacheManager(cache_dir=str(tmp_path / "cache"))
    yield manager
    manager.close()


def _seed_prior_successful_speaker_round(cm, *, transcript_data):
    """Simulate a prior fully-successful speaker-recognition round: real
    inferred names persisted both in the structured artifact
    (llm_processed.json, what dialog rendering actually reads) and in the
    authoritative speaker_mapping.json artifact (what carries
    input_fingerprint -- llm_processed.json itself does not)."""
    cm.save_cache(
        platform=PLATFORM,
        url=f"https://example.com/{MEDIA_ID}",
        media_id=MEDIA_ID,
        use_speaker_recognition=True,
        transcript_data=transcript_data,
        transcript_type="funasr",
        title="Demo",
        author="Alice",
    )

    old_dialogs = [
        {
            "speaker": OLD_REAL_NAMES[seg["speaker"]],
            "speaker_id": seg["speaker"],
            "text": seg["text"],
            "start_time": "00:00:00",
            "end_time": "00:00:01",
            "duration": 1,
        }
        for seg in transcript_data["segments"]
    ]
    cm.save_llm_result(
        platform=PLATFORM, media_id=MEDIA_ID, use_speaker_recognition=True,
        llm_type="structured",
        content={"dialogs": old_dialogs, "speaker_mapping": dict(OLD_REAL_NAMES)},
    )
    old_calibrated_text = "\n\n".join(
        f"{OLD_REAL_NAMES[seg['speaker']]}：{seg['text']}" for seg in transcript_data["segments"]
    )
    cm.save_llm_result(
        platform=PLATFORM, media_id=MEDIA_ID, use_speaker_recognition=True,
        llm_type="calibrated", content=old_calibrated_text,
    )
    cm.save_llm_status(
        platform=PLATFORM, media_id=MEDIA_ID, use_speaker_recognition=True,
        calibration_status=CalibrationStatus.FULL, summary_status=SummaryStatus.GENERATED,
    )

    speakers = SpeakerInferencer.extract_speaker_labels(transcript_data["segments"])
    fingerprint = SpeakerInferencer.input_fingerprint(speakers, transcript_data["segments"])
    cm.save_speaker_mapping(
        PLATFORM, MEDIA_ID,
        {
            "mapping": dict(OLD_REAL_NAMES),
            "meta": {
                label: {"name": name, "confidence": 0.9, "applied": True, "sampled": True}
                for label, name in OLD_REAL_NAMES.items()
            },
            "low_confidence": [],
        },
        input_fingerprint=fingerprint,
        speakers=speakers,
        source="llm",
    )
    return fingerprint


def _identity_fallback_task(task_id, *, transcription_data):
    return {
        "task_id": task_id,
        "url": f"https://example.com/{MEDIA_ID}",
        "display_url": f"https://example.com/{MEDIA_ID}",
        "platform": PLATFORM,
        "media_id": MEDIA_ID,
        "video_title": "Demo",
        "author": "Alice",
        "description": "",
        "transcript": "irrelevant -- coordinator is mocked",
        "use_speaker_recognition": True,
        "transcription_data": transcription_data,
        "is_generic": False,
        "wechat_webhook": None,
        "notification_channel": None,
        "notification_webhooks": {},
        "processing_options": {"calibrate": True, "summarize": True, "infer_speaker_names": True},
    }


def _identity_fallback_coordinator_result(*, segments):
    """A coordinator result mimicking a real identity_fallback round: the
    speaker_mapping is the identity ({label: label}), dialogs carry the raw
    labels as their "speaker" display field, and calibrated_text is built
    exactly the way SpeakerAwareProcessor._build_text_from_dialogs does --
    "{speaker}：{text}" per dialog, joined by blank lines."""
    dialogs = [
        {
            "speaker": seg["speaker"],
            "speaker_id": seg["speaker"],
            "text": seg["text"],
            "start_time": "00:00:00",
            "end_time": "00:00:01",
            "duration": 1,
        }
        for seg in segments
    ]
    calibrated_text = "\n\n".join(f"{seg['speaker']}：{seg['text']}" for seg in segments)
    return {
        "calibrated_text": calibrated_text,
        "summary_text": "A short summary of the conversation.",
        "structured_data": {
            "dialogs": dialogs,
            "speaker_mapping": {seg["speaker"]: seg["speaker"] for seg in segments},
        },
        "stats": {
            "calibration_status": CalibrationStatus.FULL,
            "summary_status": SummaryStatus.GENERATED,
            "speaker_inference_source": "identity_fallback",
        },
        "models_used": {},
    }


def _patches(cm, coordinator, notifier_mock):
    """Patch only the true external I/O boundaries -- cache_manager and
    _save_llm_results/_restore_real_names_after_identity_fallback stay REAL
    so the restoration logic actually runs."""
    return [
        patch.object(llm_ops, "cache_manager", cm),
        patch.object(llm_ops, "llm_coordinator", coordinator),
        patch.object(llm_ops, "llm_task_queue", MagicMock()),
        patch.object(llm_ops, "_send_notification", notifier_mock),
        patch.object(llm_ops, "get_notification_router", lambda: MagicMock()),
        patch.object(llm_ops, "_generate_title_if_needed", lambda t, title, tr: title),
        patch.object(llm_ops, "_prepare_llm_content", lambda t, tr, spk: tr),
    ]


class TestIdentityFallbackRestorationReachesAllConsumers:
    """V2: restoration must fire, and EVERY consumer (structured artifact,
    calibrated text on disk, the completion notification, and the task's
    terminal_snapshot) must show the restored real names, not the
    identity_fallback placeholders."""

    def test_calibrated_text_notification_and_snapshot_all_show_restored_names(
        self, cm
    ):
        _seed_prior_successful_speaker_round(cm, transcript_data=TRANSCRIPT_DATA_ROUND_1)

        task_id = cm.create_task(
            url=f"https://example.com/{MEDIA_ID}", platform=PLATFORM, media_id=MEDIA_ID,
            use_speaker_recognition=True,
        )["task_id"]
        cm.update_task_status(task_id, TaskStatus.CALIBRATING)

        coordinator = MagicMock()
        coordinator.process.return_value = _identity_fallback_coordinator_result(
            segments=TRANSCRIPT_SEGMENTS_ROUND_1
        )
        notifier_mock = MagicMock()

        ctxs = _patches(cm, coordinator, notifier_mock)
        for c in ctxs:
            c.start()
        try:
            llm_ops._handle_llm_task(
                _identity_fallback_task(task_id, transcription_data=TRANSCRIPT_DATA_ROUND_1)
            )
        finally:
            for c in ctxs:
                c.stop()

        row = cm.get_task_by_id(task_id)
        assert row["status"] == "success", row

        # 1) Structured artifact on disk (what dialog rendering reads).
        cache_data = cm.get_cache(PLATFORM, MEDIA_ID, use_speaker_recognition=True)
        dialogs = cache_data["llm_processed"]["dialogs"]
        assert dialogs[0]["speaker"] == "张三"
        assert dialogs[1]["speaker"] == "李四"

        # 2) calibrated_text persisted to disk -- the V2 bug: this stayed as
        # the raw "SPEAKER_00：.../SPEAKER_01：..." placeholders even after
        # the structured artifact above was correctly restored.
        assert cache_data["llm_calibrated"] == "张三：Hello there\n\n李四：Hi back"

        # 3) The completion notification consumes the SAME result_dict
        # object _save_llm_results mutated in place.
        assert notifier_mock.called, "completion notification must have fired"
        notified_result = notifier_mock.call_args.kwargs["result_dict"]
        assert notified_result["校对文本"] == "张三：Hello there\n\n李四：Hi back"
        assert notified_result["structured_data"]["dialogs"][0]["speaker"] == "张三"

        # 4) terminal_snapshot persisted alongside the terminal status write
        # -- also the SAME result_dict object.
        snapshot_result = row["terminal_snapshot"]["result"]
        assert snapshot_result["校对文本"] == "张三：Hello there\n\n李四：Hi back"
        assert snapshot_result["structured_data"]["dialogs"][1]["speaker"] == "李四"


class TestIdentityFallbackRestorationRespectsFingerprintBoundary:
    """V3: the old artifact's real names must only be borrowed when they
    are provably describing the SAME diarization input as this round."""

    def test_matching_fingerprint_restores_names(self, cm):
        """Green control for the RED case below: same transcript both
        rounds (matching fingerprint) -- restoration must fire normally,
        proving the fingerprint gate does not just always block."""
        _seed_prior_successful_speaker_round(cm, transcript_data=TRANSCRIPT_DATA_ROUND_1)

        task_id = cm.create_task(
            url=f"https://example.com/{MEDIA_ID}", platform=PLATFORM, media_id=MEDIA_ID,
            use_speaker_recognition=True,
        )["task_id"]
        cm.update_task_status(task_id, TaskStatus.CALIBRATING)

        coordinator = MagicMock()
        coordinator.process.return_value = _identity_fallback_coordinator_result(
            segments=TRANSCRIPT_SEGMENTS_ROUND_1
        )
        ctxs = _patches(cm, coordinator, MagicMock())
        for c in ctxs:
            c.start()
        try:
            llm_ops._handle_llm_task(
                _identity_fallback_task(task_id, transcription_data=TRANSCRIPT_DATA_ROUND_1)
            )
        finally:
            for c in ctxs:
                c.stop()

        cache_data = cm.get_cache(PLATFORM, MEDIA_ID, use_speaker_recognition=True)
        dialogs = cache_data["llm_processed"]["dialogs"]
        assert dialogs[0]["speaker"] == "张三"
        assert dialogs[1]["speaker"] == "李四"

    def test_mismatched_fingerprint_does_not_restore_names(self, cm):
        """RED on the pre-V3 code: the old artifact (speaker_mapping.json +
        llm_processed.json) was produced from TRANSCRIPT_DATA_ROUND_1, but
        this round's actual diarization input is a materially different
        transcript (TRANSCRIPT_DATA_ROUND_2_DIFFERENT_INPUT) -- same raw
        labels ("SPEAKER_00"/"SPEAKER_01"), different underlying content,
        so the two rounds' input fingerprints differ. Restoration must be
        skipped and this round's raw identity_fallback labels must survive
        untouched, rather than mis-attribute 张三/李四's names onto
        speakers who were never verified to be the same people.
        """
        _seed_prior_successful_speaker_round(cm, transcript_data=TRANSCRIPT_DATA_ROUND_1)

        task_id = cm.create_task(
            url=f"https://example.com/{MEDIA_ID}", platform=PLATFORM, media_id=MEDIA_ID,
            use_speaker_recognition=True,
        )["task_id"]
        cm.update_task_status(task_id, TaskStatus.CALIBRATING)

        # This round's transcript (and therefore its transcript_funasr.json
        # on disk) has moved on to different content -- simulating a
        # reprocessing pass where diarization/transcription changed since
        # the prior successful round.
        cm.save_cache(
            platform=PLATFORM,
            url=f"https://example.com/{MEDIA_ID}",
            media_id=MEDIA_ID,
            use_speaker_recognition=True,
            transcript_data=TRANSCRIPT_DATA_ROUND_2_DIFFERENT_INPUT,
            transcript_type="funasr",
            title="Demo",
            author="Alice",
        )

        coordinator = MagicMock()
        coordinator.process.return_value = _identity_fallback_coordinator_result(
            segments=TRANSCRIPT_SEGMENTS_ROUND_2_DIFFERENT_INPUT
        )
        notifier_mock = MagicMock()
        ctxs = _patches(cm, coordinator, notifier_mock)
        for c in ctxs:
            c.start()
        try:
            llm_ops._handle_llm_task(
                _identity_fallback_task(
                    task_id, transcription_data=TRANSCRIPT_DATA_ROUND_2_DIFFERENT_INPUT
                )
            )
        finally:
            for c in ctxs:
                c.stop()

        row = cm.get_task_by_id(task_id)
        assert row["status"] == "success", row

        cache_data = cm.get_cache(PLATFORM, MEDIA_ID, use_speaker_recognition=True)
        dialogs = cache_data["llm_processed"]["dialogs"]
        assert dialogs[0]["speaker"] == "SPEAKER_00", (
            "must NOT borrow 张三's name across a diarization/input change"
        )
        assert dialogs[1]["speaker"] == "SPEAKER_01", (
            "must NOT borrow 李四's name across a diarization/input change"
        )
        assert cache_data["llm_calibrated"] == (
            "SPEAKER_00：Completely different opening line\n\n"
            "SPEAKER_01：And a completely different reply"
        )

        notified_result = notifier_mock.call_args.kwargs["result_dict"]
        assert notified_result["structured_data"]["dialogs"][0]["speaker"] == "SPEAKER_00"


# Names embedded in llm_processed.json's speaker_mapping (old_mapping) --
# deliberately stale/wrong to prove the fix no longer sources restoration
# from this unverified copy.
STALE_EMBEDDED_NAMES = {"SPEAKER_00": "旧张三", "SPEAKER_01": "旧李四"}
# Names in the independently persisted speaker_mapping.json (verified_mapping)
# -- fingerprint-matched to this round's input, therefore authoritative.
VERIFIED_NAMES = {"SPEAKER_00": "张三", "SPEAKER_01": "李四"}


def _seed_prior_round_with_diverging_old_and_verified_mappings(cm):
    """V4 regression fixture: llm_processed.json's embedded speaker_mapping
    (old_mapping) and the independently persisted speaker_mapping.json
    (verified_mapping) intentionally disagree on the real names, while
    sharing the SAME input_fingerprint (both describe TRANSCRIPT_DATA_ROUND_1)
    so the V3 fingerprint gate passes and verified_mapping is not None.

    This reproduces a case the V3 fix alone cannot catch: old_mapping can go
    stale independently of speaker_mapping.json (e.g. a later recalibrate
    rewrote speaker_mapping.json's names via SpeakerInferencer.infer() cache
    hit refresh, but the last llm_processed.json snapshot on disk predates
    that refresh) -- the fingerprint check passing must NOT be read as
    "old_mapping's contents are trustworthy", only "verified_mapping is".
    """
    cm.save_cache(
        platform=PLATFORM,
        url=f"https://example.com/{MEDIA_ID}",
        media_id=MEDIA_ID,
        use_speaker_recognition=True,
        transcript_data=TRANSCRIPT_DATA_ROUND_1,
        transcript_type="funasr",
        title="Demo",
        author="Alice",
    )

    # old_structured / old_mapping: stale names.
    stale_dialogs = [
        {
            "speaker": STALE_EMBEDDED_NAMES[seg["speaker"]],
            "speaker_id": seg["speaker"],
            "text": seg["text"],
            "start_time": "00:00:00",
            "end_time": "00:00:01",
            "duration": 1,
        }
        for seg in TRANSCRIPT_SEGMENTS_ROUND_1
    ]
    cm.save_llm_result(
        platform=PLATFORM, media_id=MEDIA_ID, use_speaker_recognition=True,
        llm_type="structured",
        content={"dialogs": stale_dialogs, "speaker_mapping": dict(STALE_EMBEDDED_NAMES)},
    )
    stale_calibrated_text = "\n\n".join(
        f"{STALE_EMBEDDED_NAMES[seg['speaker']]}：{seg['text']}"
        for seg in TRANSCRIPT_SEGMENTS_ROUND_1
    )
    cm.save_llm_result(
        platform=PLATFORM, media_id=MEDIA_ID, use_speaker_recognition=True,
        llm_type="calibrated", content=stale_calibrated_text,
    )
    cm.save_llm_status(
        platform=PLATFORM, media_id=MEDIA_ID, use_speaker_recognition=True,
        calibration_status=CalibrationStatus.FULL, summary_status=SummaryStatus.GENERATED,
    )

    # speaker_mapping.json / verified_mapping: the authoritative, correct
    # names, fingerprint-matched to this round's actual transcript_data.
    speakers = SpeakerInferencer.extract_speaker_labels(TRANSCRIPT_SEGMENTS_ROUND_1)
    fingerprint = SpeakerInferencer.input_fingerprint(speakers, TRANSCRIPT_SEGMENTS_ROUND_1)
    cm.save_speaker_mapping(
        PLATFORM, MEDIA_ID,
        {
            "mapping": dict(VERIFIED_NAMES),
            "meta": {
                label: {"name": name, "confidence": 0.9, "applied": True, "sampled": True}
                for label, name in VERIFIED_NAMES.items()
            },
            "low_confidence": [],
        },
        input_fingerprint=fingerprint,
        speakers=speakers,
        source="llm",
    )


class TestIdentityFallbackRestorationSourcesFromVerifiedMapping:
    """V4 (PR3 review hardening 二轮): the V3 fingerprint check must not be
    decorative. Restoration must actually READ NAMES from verified_mapping
    (the fingerprint-checked speaker_mapping.json), not from old_mapping
    (the unverified llm_processed.json-embedded copy) -- even when both
    exist and the fingerprint check passes.
    """

    def test_restored_names_come_from_verified_mapping_not_stale_old_mapping(self, cm):
        """RED on the pre-V4 code: old_mapping (STALE_EMBEDDED_NAMES) and
        verified_mapping (VERIFIED_NAMES) diverge on purpose. The buggy code
        reads names from old_mapping once verified_mapping merely gates
        whether to proceed -- so it would restore 旧张三/旧李四. The fix
        must restore 张三/李四 (from verified_mapping) instead, in every
        consumer (structured artifact, calibrated_text, notification,
        terminal_snapshot) exactly like the V2 fix already guarantees.
        """
        _seed_prior_round_with_diverging_old_and_verified_mappings(cm)

        task_id = cm.create_task(
            url=f"https://example.com/{MEDIA_ID}", platform=PLATFORM, media_id=MEDIA_ID,
            use_speaker_recognition=True,
        )["task_id"]
        cm.update_task_status(task_id, TaskStatus.CALIBRATING)

        coordinator = MagicMock()
        coordinator.process.return_value = _identity_fallback_coordinator_result(
            segments=TRANSCRIPT_SEGMENTS_ROUND_1
        )
        notifier_mock = MagicMock()
        ctxs = _patches(cm, coordinator, notifier_mock)
        for c in ctxs:
            c.start()
        try:
            llm_ops._handle_llm_task(
                _identity_fallback_task(task_id, transcription_data=TRANSCRIPT_DATA_ROUND_1)
            )
        finally:
            for c in ctxs:
                c.stop()

        row = cm.get_task_by_id(task_id)
        assert row["status"] == "success", row

        cache_data = cm.get_cache(PLATFORM, MEDIA_ID, use_speaker_recognition=True)
        dialogs = cache_data["llm_processed"]["dialogs"]
        assert dialogs[0]["speaker"] == "张三", (
            "restored name must come from verified_mapping (speaker_mapping.json), "
            f"not stale old_mapping -- got {dialogs[0]['speaker']!r}"
        )
        assert dialogs[1]["speaker"] == "李四", (
            "restored name must come from verified_mapping (speaker_mapping.json), "
            f"not stale old_mapping -- got {dialogs[1]['speaker']!r}"
        )
        assert cache_data["llm_calibrated"] == "张三：Hello there\n\n李四：Hi back"

        notified_result = notifier_mock.call_args.kwargs["result_dict"]
        assert notified_result["校对文本"] == "张三：Hello there\n\n李四：Hi back"
        assert notified_result["structured_data"]["dialogs"][0]["speaker"] == "张三"

        snapshot_result = row["terminal_snapshot"]["result"]
        assert snapshot_result["structured_data"]["dialogs"][1]["speaker"] == "李四"
