from unittest.mock import MagicMock

from video_transcript_api.llm.core.speaker_inferencer import SpeakerInferencer


def _pad_dialogs(dialogs):
    """在待测对话前后插入两个发言量更高的说话人，使待测说话人落在发言量
    top2 之外——top2 采样加额（8 条/1200 字符，card-vta-261015-speaker-evidence
    锁定决策 6）生效后，单说话人场景永远命中加额配额，这些测试针对的
    「默认配额 3 条/400 字符的分层采样行为」需要非 top2 说话人来验证。"""
    pad_a = [
        {"speaker": "PadderA", "text": f"pad-a-{i}-{'x' * 40}", "start_time": -100 + i}
        for i in range(12)
    ]
    pad_b = [
        {"speaker": "PadderB", "text": f"pad-b-{i}-{'y' * 40}", "start_time": 10000 + i}
        for i in range(12)
    ]
    return pad_a + dialogs + pad_b


def test_sampling_uses_head_middle_tail_and_sees_late_pollution():
    inferencer = SpeakerInferencer(MagicMock(), samples_per_speaker=3)
    dialogs = _pad_dialogs(
        [
            {"speaker": "Speaker4", "text": f"head-{i}", "start_time": i}
            for i in range(9)
        ]
    )
    samples = inferencer._extract_sample_dialogs(
        dialogs, ["Speaker4", "PadderA", "PadderB"]
    )
    assert samples["Speaker4"]["own_samples"] == ["head-0", "head-4", "head-8"]


def test_sampling_fewer_than_k_keeps_original_time_order():
    inferencer = SpeakerInferencer(MagicMock(), samples_per_speaker=3)
    dialogs = [
        {"speaker": "Speaker4", "text": "first", "start_time": 10},
        {"speaker": "Speaker4", "text": "second", "start_time": 11},
    ]
    samples = inferencer._extract_sample_dialogs(
        dialogs, ["Speaker4", "PadderA", "PadderB"]
    )
    assert samples["Speaker4"]["own_samples"] == ["first", "second"]


def test_sampling_default_budget_keeps_head_middle_tail_samples():
    inferencer = SpeakerInferencer(MagicMock())
    dialogs = _pad_dialogs(
        [
            {"speaker": "Speaker4", "text": "HEAD-" + "h" * 500, "start_time": 0},
            {"speaker": "Speaker4", "text": "early", "start_time": 1},
            {"speaker": "Speaker4", "text": "MIDDLE-" + "m" * 500, "start_time": 2},
            {"speaker": "Speaker4", "text": "late", "start_time": 3},
            {"speaker": "Speaker4", "text": "TAIL_MARKER", "start_time": 4},
        ]
    )

    own_samples = inferencer._extract_sample_dialogs(
        dialogs, ["Speaker4", "PadderA", "PadderB"]
    )["Speaker4"][
        "own_samples"
    ]

    assert len(own_samples) == 3
    assert all(own_samples)
    assert any("TAIL_MARKER" in sample for sample in own_samples)
    assert sum(map(len, own_samples)) <= 400


def test_sampling_per_sample_budget_preserves_tail_when_budget_is_tight():
    inferencer = SpeakerInferencer(
        MagicMock(), samples_per_speaker=3, max_chars_per_speaker=200
    )
    dialogs = _pad_dialogs(
        [
            {"speaker": "Speaker4", "text": "HEAD-" + "h" * 500, "start_time": 0},
            {"speaker": "Speaker4", "text": "early", "start_time": 1},
            {"speaker": "Speaker4", "text": "MIDDLE-" + "m" * 500, "start_time": 2},
            {"speaker": "Speaker4", "text": "late", "start_time": 3},
            {"speaker": "Speaker4", "text": "TAIL_MARKER", "start_time": 4},
        ]
    )

    own_samples = inferencer._extract_sample_dialogs(
        dialogs, ["Speaker4", "PadderA", "PadderB"]
    )["Speaker4"][
        "own_samples"
    ]

    assert len(own_samples) == 3
    assert all(own_samples)
    assert any("TAIL_MARKER" in sample for sample in own_samples)
    assert sum(map(len, own_samples)) <= 200
