"""T8: SpeakerAwareProcessor has_speaker=False（无说话人逐段校对）模式测试。

无 speaker 的 segments（plain 源：CapsWriter/YouTube 字幕）走「无说话人逐段
校对」：结构与 FunASR 路径同构（id 锚点映射、禁止合并/拆分/重排），但

- SpeakerInferencer.infer 整步跳过（零 LLM 调用）；
- 不做「连续同说话人合并」（两边 speaker_id 都是 None 会被误判成同一人，
  全文塌缩成一条）；
- speaker/speaker_id 保留缺省（不落 "unknown"）、None 时间保留 None
  （不落 "00:00:00"）；
- 最终 dialogs 是确定性段落化后的段落（无 speaker 键），可直接落盘
  llm_processed.json 并支撑 dlg-{i} 锚点渲染。

规格来源：docs/sessions/260719-0513-chapters/TASKS.md T8。
"""

import json
import re
from unittest.mock import MagicMock

import pytest

from video_transcript_api.llm.core.config import LLMConfig
from video_transcript_api.llm.core.key_info_extractor import KeyInfo
from video_transcript_api.llm.processors.speaker_aware_processor import (
    SpeakerAwareProcessor,
)
from video_transcript_api.llm.prompts import (
    STRUCTURED_CALIBRATE_NO_SPEAKER_SYSTEM_PROMPT,
    STRUCTURED_CALIBRATE_SYSTEM_PROMPT,
)
from video_transcript_api.utils.llm_status import CalibrationStatus


def _make_config(**overrides):
    """真实 LLMConfig（非 MagicMock），DialogSegmenter/段落化的算术才能跑。

    默认把段落化参数调小，方便用短文本构造断点场景。
    """
    kwargs = dict(
        api_key="k",
        base_url="http://test",
        calibrate_model="test-model",
        summary_model="test-model",
        paragraphization_target_chars=10,
        paragraphization_hard_max_chars=100,
        paragraphization_pause_threshold_seconds=2.0,
    )
    kwargs.update(overrides)
    return LLMConfig(**kwargs)


def _empty_key_info():
    return KeyInfo(
        names=[],
        places=[],
        technical_terms=[],
        brands=[],
        abbreviations=[],
        foreign_terms=[],
        other_entities=[],
    )


def _calibrate_side_effect(**kwargs):
    """按 user_prompt 里的 [id] 行生成全覆盖 corrections（合法非空文本）。

    coverage=1.0 → 每个 chunk 干净成功，不走重试/降级，产物结构完全由
    _apply_corrections_by_id 决定。
    """
    user_prompt = kwargs["user_prompt"]
    corrections = []
    for line in user_prompt.splitlines():
        m = re.match(r"^\[(\d+)\]", line.strip())
        if m:
            corrections.append(
                {"id": int(m.group(1)), "text": f"校准文本{m.group(1)}。"}
            )
    response = MagicMock()
    response.structured_output = {"corrections": corrections}
    return response


def _make_processor(config=None, llm_client=None, speaker_inferencer=None):
    """组装 processor：llm_client/key_info_extractor/speaker_inferencer/
    quality_validator 全部 mock（参照 test_speaker_aware_processor_stats.py）。"""
    if config is None:
        config = _make_config()
    if llm_client is None:
        llm_client = MagicMock()
        llm_client.call = MagicMock(side_effect=_calibrate_side_effect)
    key_info_extractor = MagicMock()
    key_info_extractor.extract = MagicMock(return_value=_empty_key_info())
    if speaker_inferencer is None:
        speaker_inferencer = MagicMock()
        speaker_inferencer.infer = MagicMock(
            return_value={"mapping": {}, "meta": {}, "source": "identity_fallback"}
        )
    quality_validator = MagicMock()
    processor = SpeakerAwareProcessor(
        config=config,
        llm_client=llm_client,
        key_info_extractor=key_info_extractor,
        speaker_inferencer=speaker_inferencer,
        quality_validator=quality_validator,
    )
    return processor, llm_client, key_info_extractor, speaker_inferencer


def _prompt_id_lines(user_prompt):
    """提取 user_prompt 里以 [id] 开头的数据行。"""
    return [
        line
        for line in user_prompt.splitlines()
        if re.match(r"^\[\d+\]", line.strip())
    ]


# ----------------------------------------------------------------------
# 1. has_speaker 自动判定
# ----------------------------------------------------------------------


class TestHasSpeakerAutoDetection:
    def test_no_speaker_input_detected_as_false(self):
        """全无 speaker 的输入自动走无说话人模式：infer 零调用。"""
        processor, llm_client, _, speaker_inferencer = _make_processor()
        dialogs = [
            {"text": "第一句话。", "start_time": 0.0, "end_time": 1.0},
            {"text": "第二句话。", "start_time": 1.0, "end_time": 2.0},
        ]

        processor.process(dialogs=dialogs, title="Test", skip_calibration=True)

        speaker_inferencer.infer.assert_not_called()

    def test_mixed_input_detected_as_true(self):
        """混合输入（部分有 speaker）维持现状 has_speaker=True 路径：
        infer 被调用、产物保留 speaker 键、不段落化。"""
        processor, _, _, speaker_inferencer = _make_processor()
        speaker_inferencer.infer.return_value = {
            "mapping": {"S1": "Alice"},
            "meta": {},
            "source": "llm",
        }
        dialogs = [
            {"speaker": "S1", "text": "有说话人的一段。", "start_time": 0.0, "end_time": 1.0},
            {"text": "缺说话人的一段。", "start_time": 1.0, "end_time": 2.0},
        ]

        result = processor.process(dialogs=dialogs, title="Test", skip_calibration=True)

        speaker_inferencer.infer.assert_called_once()
        out_dialogs = result["structured_data"]["dialogs"]
        # 现状路径：每条都有 speaker 键（缺省段按现状塞 "unknown"），且不段落化
        assert len(out_dialogs) == 2
        assert all("speaker" in d for d in out_dialogs)
        assert out_dialogs[0]["speaker"] == "Alice"

    def test_explicit_has_speaker_overrides_auto_detection(self):
        """显式传 has_speaker=True 覆盖自动判定：全无 speaker 的输入也走
        现状路径（coerce 塞 "unknown"、按同说话人合并塌缩成一条）。"""
        processor, _, _, speaker_inferencer = _make_processor()
        dialogs = [
            {"text": "第一段。", "start_time": 0.0, "end_time": 1.0},
            {"text": "第二段。", "start_time": 1.0, "end_time": 2.0},
        ]

        result = processor.process(
            dialogs=dialogs, title="Test", skip_calibration=True, has_speaker=True
        )

        speaker_inferencer.infer.assert_called_once()
        out_dialogs = result["structured_data"]["dialogs"]
        # 现状合并行为：speaker_id 都是 "unknown" → 塌缩成一条
        assert len(out_dialogs) == 1
        assert out_dialogs[0]["speaker"] == "unknown"


# ----------------------------------------------------------------------
# 2. 序列化硬断言（落盘契约）
# ----------------------------------------------------------------------


class TestNoSpeakerSerialization:
    def test_serialized_structured_data_has_no_unknown_no_speaker_keys(self):
        """json.dumps(structured_data) 全文不含 "unknown"；dialogs 无
        speaker/speaker_id 键；保留 start/end/text 字段。"""
        processor, _, _, _ = _make_processor()
        dialogs = [
            {"text": "第一段内容。", "start_time": 0.0, "end_time": 5.0},
            {"text": "第二段内容。", "start_time": 5.0, "end_time": 10.0},
        ]

        result = processor.process(dialogs=dialogs, title="Test")

        serialized = json.dumps(result["structured_data"], ensure_ascii=False)
        assert "unknown" not in serialized
        for dialog in result["structured_data"]["dialogs"]:
            assert "speaker" not in dialog
            assert "speaker_id" not in dialog
            assert "start_time" in dialog
            assert "end_time" in dialog
            assert "text" in dialog

    def test_calibrated_text_has_no_speaker_prefix(self):
        """calibrated_text 无 "None："/"unknown：" 前缀，只是段落文本的
        \\n\\n 连接。"""
        processor, _, _, _ = _make_processor()
        dialogs = [
            {"text": "第一段内容。", "start_time": 0.0, "end_time": 5.0},
            {"text": "第二段内容。", "start_time": 5.0, "end_time": 10.0},
        ]

        result = processor.process(dialogs=dialogs, title="Test")

        calibrated_text = result["calibrated_text"]
        assert "None：" not in calibrated_text
        assert "unknown：" not in calibrated_text
        paragraphs = result["structured_data"]["dialogs"]
        assert calibrated_text == "\n\n".join(p["text"] for p in paragraphs)

    def test_stats_dialog_count_is_paragraph_count(self):
        """无说话人模式 stats.dialog_count = 段落数（与落盘 dialogs 一致）。"""
        processor, _, _, _ = _make_processor()
        dialogs = [
            {"text": "第一段内容。", "start_time": 0.0, "end_time": 5.0},
            {"text": "第二段内容。", "start_time": 5.0, "end_time": 10.0},
        ]

        result = processor.process(dialogs=dialogs, title="Test")

        assert result["stats"]["dialog_count"] == len(
            result["structured_data"]["dialogs"]
        )


# ----------------------------------------------------------------------
# 3. infer 零调用 / key_info 提取规则
# ----------------------------------------------------------------------


class TestInferAndKeyInfo:
    def test_infer_not_called_and_key_info_extracted_when_calibrating(self):
        """校准开启：SpeakerInferencer.infer 零调用，key_info 仍提取
        （喂养校对 prompt，与 speaker 无关）。"""
        processor, _, key_info_extractor, speaker_inferencer = _make_processor()
        dialogs = [{"text": "内容。", "start_time": 0.0, "end_time": 1.0}]

        result = processor.process(dialogs=dialogs, title="Test")

        speaker_inferencer.infer.assert_not_called()
        key_info_extractor.extract.assert_called_once()
        # identity_fallback source 保持下游 llm_ops 刷新判断安全
        assert result["stats"]["speaker_inference_source"] == "identity_fallback"
        assert result["structured_data"]["speaker_mapping"] == {}

    def test_key_info_not_extracted_when_skip_calibration(self):
        """skip_calibration=True 且无 speaker（推断步骤也不存在）时，
        key_info 无消费者，整步不提取（无隐藏 LLM 调用）。"""
        processor, llm_client, key_info_extractor, speaker_inferencer = _make_processor()
        dialogs = [{"text": "内容。", "start_time": 0.0, "end_time": 1.0}]

        processor.process(dialogs=dialogs, title="Test", skip_calibration=True)

        speaker_inferencer.infer.assert_not_called()
        key_info_extractor.extract.assert_not_called()
        llm_client.call.assert_not_called()


# ----------------------------------------------------------------------
# 4. 不合并：校准前保持原始 segments 粒度
# ----------------------------------------------------------------------


class TestNoMergingBeforeCalibration:
    def test_dialogs_not_collapsed_into_one_before_calibration(self):
        """无 speaker 的多条输入在校准 prompt 里必须各占一行（[0]/[1]/[2]）。
        若 _normalize_and_merge_dialogs 按 speaker_id==None 合并，全文会
        塌缩成一条，prompt 里只剩 [0] 一行。"""
        processor, llm_client, _, _ = _make_processor()
        dialogs = [
            {"text": "第一段。", "start_time": 0.0, "end_time": 1.0},
            {"text": "第二段。", "start_time": 1.0, "end_time": 2.0},
            {"text": "第三段。", "start_time": 2.0, "end_time": 3.0},
        ]

        processor.process(dialogs=dialogs, title="Test")

        llm_client.call.assert_called_once()
        user_prompt = llm_client.call.call_args.kwargs["user_prompt"]
        id_lines = _prompt_id_lines(user_prompt)
        assert len(id_lines) == 3
        assert id_lines[0].startswith("[0]")
        assert id_lines[1].startswith("[1]")
        assert id_lines[2].startswith("[2]")


# ----------------------------------------------------------------------
# 5. None 时间保留 None
# ----------------------------------------------------------------------


class TestNoneTimesPreserved:
    def test_none_times_stay_none_no_zero_fallback(self):
        """无时间输入：段落 start_time/end_time 为 None，序列化产物里
        不出现 "00:00:00" 兜底标签。"""
        processor, _, _, _ = _make_processor()
        dialogs = [
            {"text": "第一段内容。"},
            {"text": "第二段内容。"},
        ]

        result = processor.process(dialogs=dialogs, title="Test", skip_calibration=True)

        out_dialogs = result["structured_data"]["dialogs"]
        assert len(out_dialogs) >= 1
        for dialog in out_dialogs:
            assert dialog["start_time"] is None
            assert dialog["end_time"] is None
        serialized = json.dumps(result["structured_data"], ensure_ascii=False)
        assert "00:00:00" not in serialized


# ----------------------------------------------------------------------
# 6. 确定性段落化集成
# ----------------------------------------------------------------------


class TestParagraphizationIntegration:
    def test_output_dialogs_are_paragraphs_broken_at_authorized_points(self):
        """输出 dialogs 是段落：长度到预算后在句末授权点断段（不腰斩句子）。"""
        processor, _, _, _ = _make_processor()
        dialogs = [
            {"text": "啊啊啊啊啊", "start_time": 0.0, "end_time": 5.0},
            {"text": "啊啊啊啊啊啊。", "start_time": 5.5, "end_time": 10.0},
            {"text": "呜呜呜呜呜", "start_time": 10.5, "end_time": 15.0},
            {"text": "呜呜呜呜呜呜。", "start_time": 15.5, "end_time": 20.0},
        ]

        result = processor.process(dialogs=dialogs, title="Test", skip_calibration=True)

        out = result["structured_data"]["dialogs"]
        # 4 条输入 → 2 段：断点落在 seg1/seg3（句末标点）之后
        assert len(out) == 2
        assert out[0]["text"] == "啊啊啊啊啊" + "啊啊啊啊啊啊。"
        assert out[1]["text"] == "呜呜呜呜呜" + "呜呜呜呜呜呜。"
        # 段落时间 = 首成员 start / 末成员 end，格式化 HH:MM:SS
        assert out[0]["start_time"] == "00:00:00"
        assert out[0]["end_time"] == "00:00:10"
        assert out[1]["start_time"] == "00:00:10"
        assert out[1]["end_time"] == "00:00:20"

    def test_pause_authorization_consumes_float_second_snapshot(self):
        """停顿授权消费 normalize 前的 float 秒快照，不被 HH:MM:SS 截断
        误差带偏：gap=1.5s（12.1-10.6）< 2.0 不断；gap=2.5s（13.1-10.6）
        >= 2.0 断。若误用截断值（10 vs 12），前者会被误判成 gap=2.0 断开。"""
        processor, _, _, _ = _make_processor()

        base = [
            {"text": "AAAAAAAAAA", "start_time": 0.0, "end_time": 10.6},
            {"text": "BBBBBBBBBB", "start_time": 12.1, "end_time": 20.0},
        ]
        result = processor.process(dialogs=base, title="Test", skip_calibration=True)
        assert len(result["structured_data"]["dialogs"]) == 1

        wider = [
            {"text": "AAAAAAAAAA", "start_time": 0.0, "end_time": 10.6},
            {"text": "BBBBBBBBBB", "start_time": 13.1, "end_time": 20.0},
        ]
        result = processor.process(dialogs=wider, title="Test", skip_calibration=True)
        assert len(result["structured_data"]["dialogs"]) == 2

    def test_calibrated_branch_also_paragraphizes(self):
        """校准分支同样段落化：段落 text 来自校准后文本，并携带
        original_text（原文拼接）与 duration。"""
        processor, llm_client, _, _ = _make_processor()
        dialogs = [
            {"text": "第一段内容。", "start_time": 0.0, "end_time": 5.0, "duration": 5.0},
            {"text": "第二段内容。", "start_time": 5.0, "end_time": 10.0, "duration": 5.0},
        ]

        result = processor.process(dialogs=dialogs, title="Test")

        llm_client.call.assert_called_once()
        out = result["structured_data"]["dialogs"]
        assert len(out) == 1  # 总长度未到 target=10 之后的授权点之外，合成一段
        assert out[0]["text"] == "校准文本0。校准文本1。"
        assert out[0]["original_text"] == "第一段内容。第二段内容。"
        assert out[0]["duration"] == 10.0


# ----------------------------------------------------------------------
# 7. skip_calibration 分支
# ----------------------------------------------------------------------


class TestSkipCalibration:
    def test_skip_calibration_still_paragraphizes_with_zero_llm_calls(self):
        """skip_calibration=True：仍做确定性段落化、零 chunk LLM 调用、
        calibration_status=DISABLED（与 FunASR calibrate=false 形态一致）。"""
        processor, llm_client, _, _ = _make_processor()
        dialogs = [
            {"text": "啊啊啊啊啊", "start_time": 0.0, "end_time": 5.0},
            {"text": "啊啊啊啊啊啊。", "start_time": 5.5, "end_time": 10.0},
            {"text": "呜呜呜呜呜", "start_time": 10.5, "end_time": 15.0},
            {"text": "呜呜呜呜呜呜。", "start_time": 15.5, "end_time": 20.0},
        ]

        result = processor.process(dialogs=dialogs, title="Test", skip_calibration=True)

        llm_client.call.assert_not_called()
        assert (
            result["stats"]["calibration_stats"]["calibration_status"]
            == CalibrationStatus.DISABLED
        )
        out = result["structured_data"]["dialogs"]
        assert len(out) == 2
        # 未校准：段落 text 即原文拼接
        assert out[0]["text"] == "啊啊啊啊啊" + "啊啊啊啊啊啊。"


# ----------------------------------------------------------------------
# 8. prompt 行格式与 echo 拒绝
# ----------------------------------------------------------------------


class TestPromptFormat:
    def test_format_chunk_no_speaker_line_format(self):
        """无说话人行格式：[{idx}]{time_tag}: {text}（去掉 speaker 括号；
        time_tag 为空时为 [{idx}]: {text}），f-string 不得输出字面 [None]。"""
        processor, _, _, _ = _make_processor()
        chunk = [
            {"text": "你好", "start_time": "00:00:01"},
            {"text": "世界", "start_time": None},
        ]

        out = processor._format_chunk_for_prompt(chunk, {}, has_speaker=False)

        assert out == "[0][00:00:01]: 你好\n[1]: 世界"

    def test_format_chunk_with_speaker_line_format_unchanged(self):
        """has_speaker=True 行格式现状不变：[{idx}]{time_tag}[{speaker}]: {text}。"""
        processor, _, _, _ = _make_processor()
        chunk = [{"text": "你好", "start_time": "00:00:01", "speaker": "S0"}]

        out = processor._format_chunk_for_prompt(chunk, {}, has_speaker=True)

        assert out == "[0][00:00:01][S0]: 你好"

    def test_calibrate_uses_no_speaker_system_prompt(self):
        """无说话人模式校准选用 NO_SPEAKER system prompt（行格式描述与
        实际行生成一致）。"""
        processor, llm_client, _, _ = _make_processor()
        dialogs = [{"text": "内容。", "start_time": 0.0, "end_time": 1.0}]

        processor.process(dialogs=dialogs, title="Test")

        system_prompt = llm_client.call.call_args.kwargs["system_prompt"]
        assert system_prompt == STRUCTURED_CALIBRATE_NO_SPEAKER_SYSTEM_PROMPT
        assert system_prompt != STRUCTURED_CALIBRATE_SYSTEM_PROMPT

    def test_echo_rejection_covers_no_speaker_line_format(self):
        """echo 拒绝正则同时覆盖两种行格式的回吐：
        `[0]: fake text`（无 speaker 新格式）与
        `[0][00:00:01][S0]: fake`（有 speaker 旧格式）。"""
        assert SpeakerAwareProcessor._valid_correction_text("[0]: fake text") is False
        assert (
            SpeakerAwareProcessor._valid_correction_text("[0][00:00:01][S0]: fake")
            is False
        )
        assert SpeakerAwareProcessor._valid_correction_text("正常校对文本") is True

    def test_build_text_no_speaker_variant(self):
        """无 speaker 变体只输出 text（\\n\\n 连接），不输出 `speaker：` 前缀。"""
        processor, _, _, _ = _make_processor()

        out = processor._build_text_from_dialogs(
            [{"text": "第一段"}, {"text": "第二段"}], has_speaker=False
        )

        assert out == "第一段\n\n第二段"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
