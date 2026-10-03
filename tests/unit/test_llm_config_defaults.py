"""漂移锁：``LLMConfig`` 的 dataclass 默认值必须与 ``from_dict`` 缺键时的取值逐字段一致。

背景（#147）：配置真值来源曾有四份——dataclass 默认值、``from_dict`` 里的字面量、
``config/config.example.jsonc``、生产 ``config/config.jsonc``。``structured_fallback_strategy``
已实测分叉：直接构造得 ``best_quality``，生产走的 ``from_dict`` 缺键时得
``formatted_original``。真值方向以 ``from_dict``（生产路径）为准，dataclass 向它对齐。

本模块只锁「dataclass 默认值 vs from_dict 缺键」这一条不变式，锁的是**行为**（比较两个实例
的实际取值），不是源码字符串。``config.example.jsonc`` 与生产文件的取值不在本模块范围内。

派生字段豁免清单
--------------
下列字段**有意**不同：dataclass 默认为 ``None``（表达「未显式指定」），
``from_dict`` 在缺键时把主模型名回填进去（``calibrate_model`` / ``summary_model``），
真正的回退发生在消费端（如 ``get_models()`` 的 ``notes_model or self.summary_model``）。
这是刻意的「None 表示继承」语义，不是漂移，因此豁免。

清单非空时才通过：把全部字段都豁免掉同样视为失败。
"""

import dataclasses

import pytest

from video_transcript_api.llm.core.config import LLMConfig

# ``from_dict`` 会强依赖这四个键的**存在**（不是取值），其余键一律允许缺失。
MINIMAL_REQUIRED = {
    "api_key": "k",
    "base_url": "u",
    "calibrate_model": "calibrate-model",
    "summary_model": "summary-model",
}

# 派生字段豁免清单：字段名 -> 为何有意不同（必须逐个写明理由）。
# 新增条目必须同时给出理由，否则本测试的意图无从审查。
DERIVED_FIELD_EXEMPTIONS = {
    "key_info_model": (
        "dataclass 默认 None 表示「继承 calibrate_model」，from_dict 缺键时直接回填 "
        "calibrate_model 的字面值；回退语义由消费端承担。"
    ),
    "speaker_model": (
        "dataclass 默认 None 表示「继承 calibrate_model」，from_dict 缺键时直接回填 "
        "calibrate_model 的字面值；回退语义由消费端承担。"
    ),
    "validator_model": (
        "dataclass 默认 None 表示「继承 calibrate_model」，from_dict 缺键时直接回填 "
        "calibrate_model 的字面值；回退语义由消费端承担。"
    ),
    "chapters_model": (
        "dataclass 默认 None 表示「继承 calibrate_model」，from_dict 缺键时直接回填 "
        "calibrate_model 的字面值；回退语义由消费端承担。"
    ),
    "notes_model": (
        "dataclass 默认 None 表示「继承 summary_model」，from_dict 缺键时直接回填 "
        "summary_model 的字面值；get_models() 用 notes_model or summary_model 做回退。"
    ),
}


def _minimal_instances():
    direct = LLMConfig(**MINIMAL_REQUIRED)
    from_dict = LLMConfig.from_dict({"llm": dict(MINIMAL_REQUIRED)})
    return direct, from_dict


def test_derivation_exemption_list_is_not_empty():
    """豁免清单为空 => 全字段恒绿，等于没锁。"""
    assert DERIVED_FIELD_EXEMPTIONS, (
        "派生字段豁免清单为空：所有字段都被豁免时本模块的漂移锁会恒绿通过。"
        "若确实不再有派生字段，应连同本测试一起删除，而不是把清单留空。"
    )


def test_exemption_list_only_names_real_fields():
    """豁免清单不得引用已删除/拼错的字段名，否则会悄悄放过真实漂移。"""
    real = {f.name for f in dataclasses.fields(LLMConfig)}
    unknown = set(DERIVED_FIELD_EXEMPTIONS) - real
    assert not unknown, f"豁免清单含 LLMConfig 上不存在的字段：{sorted(unknown)}"
    missing_reason = [name for name, reason in DERIVED_FIELD_EXEMPTIONS.items() if not reason]
    assert not missing_reason, f"豁免清单中这些字段没写明为何有意不同：{sorted(missing_reason)}"


def test_direct_construction_matches_from_dict_defaults():
    """核心漂移锁：dataclass 默认值 == from_dict 缺键时的取值（逐字段比较实际取值）。"""
    direct, from_dict = _minimal_instances()

    mismatches = {}
    for f in dataclasses.fields(LLMConfig):
        if f.name in DERIVED_FIELD_EXEMPTIONS:
            continue
        direct_value = getattr(direct, f.name)
        from_dict_value = getattr(from_dict, f.name)
        if direct_value != from_dict_value:
            mismatches[f.name] = (direct_value, from_dict_value)

    assert not mismatches, (
        "LLMConfig 的 dataclass 默认值与 from_dict 缺键时的取值分叉，"
        "真值方向以 from_dict（生产路径）为准，把 dataclass 对齐过去：\n"
        + "\n".join(
            f"  {name}: 直接构造={direct_value!r} vs from_dict={from_dict_value!r}"
            for name, (direct_value, from_dict_value) in sorted(mismatches.items())
        )
    )


def test_structured_fallback_strategy_agrees_on_both_paths():
    """#147 的具体分叉点单独钉一道，避免被上面的逐字段锁「平均掉」而无人察觉。"""
    direct, from_dict = _minimal_instances()
    assert direct.structured_fallback_strategy == "formatted_original"
    assert from_dict.structured_fallback_strategy == "formatted_original"


@pytest.mark.parametrize("field_name", ["key_info_model", "speaker_model",
                                        "validator_model", "chapters_model", "notes_model"])
def test_derived_model_fields_still_fall_back_through_consumers(field_name):
    """豁免字段本身也要有行为保证：None 必须真的能被消费端回退，而不是躺平。"""
    direct, from_dict = _minimal_instances()
    assert getattr(direct, field_name) is None
    assert getattr(from_dict, field_name) in {
        MINIMAL_REQUIRED["calibrate_model"],
        MINIMAL_REQUIRED["summary_model"],
    }


def test_from_dict_is_unaffected_by_the_dataclass_default():
    """dataclass 默认值不是生产路径：显式写 ``fallback_to_original`` 仍决定 from_dict 取值。"""
    enabled = LLMConfig.from_dict(
        {"llm": {**MINIMAL_REQUIRED, "structured_calibration": {"fallback_to_original": True}}}
    )
    disabled = LLMConfig.from_dict(
        {"llm": {**MINIMAL_REQUIRED, "structured_calibration": {"fallback_to_original": False}}}
    )
    explicit = LLMConfig.from_dict(
        {
            "llm": {
                **MINIMAL_REQUIRED,
                "structured_calibration": {
                    "quality_validation": {"fallback_strategy": "best_quality"}
                },
            }
        }
    )
    assert enabled.structured_fallback_strategy == "formatted_original"
    assert disabled.structured_fallback_strategy == "best_quality"
    assert explicit.structured_fallback_strategy == "best_quality"