"""#147: ``main.py --check-config`` 输出 9 个白名单 LLM 参数的生效值与来源。

背景：#147 记录了「分段阈值有三套取值」（dataclass 默认 / from_dict 字面量 /
示例推荐值 / 生产实测值），注释当契约已经骗不到人。本模块锁的是
``--check-config`` 末行 JSON 的**行为**：

1. 输出恰好 9 个白名单键，每个带 ``value`` + ``source``；
2. ``value`` 与真实解析器 ``LLMConfig.from_dict`` 的取值逐字段相等
   （不允许第二套手算默认值）；
3. ``source`` 复刻 ``from_dict`` 的真实分支——裸 ``dict.get(key, default)``
   的 8 个键「键存在即 config」：显式 ``null`` 也算 config 且 value 原样为
   ``null``；``structured_fallback_strategy`` 走真值派生，命中派生分支才标
   ``derived``；
4. 凭据 sentinel（api_key / base_url / webhook / token）不出现在 stdout；
5. 进程边界用真实 subprocess + 真实 stdout JSON 断言，不用 mock 冒充 CLI。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from video_transcript_api.llm.core.config import LLMConfig


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAIN_PY = PROJECT_ROOT / "main.py"

# 白名单 9 项，顺序按 from_dict 里的分组，不为凑数增删。
WHITELIST_FIELDS = (
    "enable_threshold",
    "segment_size",
    "max_segment_size",
    "min_chunk_length",
    "max_chunk_length",
    "preferred_chunk_length",
    "calibration_concurrent_limit",
    "structured_calibration_for_plain",
    "structured_fallback_strategy",
)

# 裸 dict.get(key, default) 的 8 项：键存在（含显式 null）即为 config。
PLAIN_GET_FIELDS = WHITELIST_FIELDS[:-1]

SENTINEL_API_KEY = "SENTINEL-LLM-API-KEY-4f1c9a"
SENTINEL_BASE_URL = "http://127.0.0.1:1/SENTINEL-BASE-URL-4f1c9a/v1"
SENTINEL_WEBHOOK = "SENTINEL-WECOM-WEBHOOK-4f1c9a"
SENTINEL_TIKHUB_KEY = "SENTINEL-TIKHUB-KEY-4f1c9a"
SENTINEL_AUTH_TOKEN = "SENTINEL-API-AUTH-TOKEN-4f1c9a"


def _base_config(tmp_path: Path) -> dict:
    """最小可通过校验的配置。storage/log 路径全部落在 tmp_path，避免任何
    真实运行期目录/日志被写；base_url 指向关闭端口，任何误发的请求都会失败
    而不是打到外部。"""
    return {
        "api": {
            "host": "127.0.0.1",
            "port": 8000,
            "auth_token": SENTINEL_AUTH_TOKEN,
        },
        "concurrent": {"max_workers": 1, "queue_size": 2, "llm_max_workers": 1},
        "storage": {
            "cache_dir": str(tmp_path / "cache"),
            "workspace_dir": str(tmp_path / "workspace"),
            "temp_dir": str(tmp_path / "temp"),
            "audit_db": str(tmp_path / "audit.db"),
        },
        "web": {"base_url": "http://localhost:8000"},
        "llm": {
            "api_key": SENTINEL_API_KEY,
            "base_url": SENTINEL_BASE_URL,
            "calibrate_model": "calibrate-model",
            "summary_model": "summary-model",
        },
        "wechat": {"webhook": SENTINEL_WEBHOOK},
        "tikhub": {"enabled": False, "api_key": SENTINEL_TIKHUB_KEY},
        "log": {"file": str(tmp_path / "app.log")},
    }


def _clean_env() -> dict:
    """无会话身份的最小环境：不给任何 PI_LEAD_SESSION / 代理变量，
    --check-config 的输出不能依赖操作者身份。"""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(Path(os.environ.get("HOME", "/tmp"))),
        "LANG": "C.UTF-8",
    }


def _run_check_config(tmp_path: Path, llm_section: dict):
    """真实 subprocess 跑 main.py --check-config，返回 CompletedProcess。
    argv 用绝对路径（main.py 与 Python 入口都是绝对路径），cwd 固定项目根，
    env 是干净最小集。"""
    config = _base_config(tmp_path)
    config["llm"] = {**config["llm"], **llm_section}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    env = _clean_env()
    assert "PI_LEAD_SESSION" not in env
    argv = [
        sys.executable,
        str(MAIN_PY),
        "--check-config",
        "--config",
        str(config_path),
    ]
    assert os.path.isabs(argv[0]) and os.path.isabs(argv[1]), argv
    assert os.path.isabs(argv[-1]), argv

    return subprocess.run(
        argv,
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    ), config


def _effective(result) -> dict:
    """从真实 stdout 解析末行 JSON，返回 llm_effective 段。

    stdout 上还有 loguru 的 INFO 行（setup_logger 把日志接到 stdout），
    所以只要求 ``Configuration OK`` 那一行原样存在、且 JSON 是最后一行。
    """
    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert "Configuration OK" in lines, result.stdout
    payload = json.loads(lines[-1])
    return payload["llm_effective"]


def _assert_matches_real_parser(effective: dict, config: dict) -> None:
    """与真实解析器逐字段相等——value 不是另一套手算默认值。"""
    real = LLMConfig.from_dict(config)
    for field in WHITELIST_FIELDS:
        assert effective[field]["value"] == getattr(real, field), field


def test_check_config_prints_exactly_nine_whitelisted_fields(tmp_path):
    result, config = _run_check_config(tmp_path, {})
    effective = _effective(result)

    assert tuple(sorted(effective)) == tuple(sorted(WHITELIST_FIELDS))
    for field in WHITELIST_FIELDS:
        assert set(effective[field]) == {"value", "source"}, field
    _assert_matches_real_parser(effective, config)


def test_missing_optional_sections_report_defaults_and_derived(tmp_path):
    """省略 segmentation / structured_calibration 段：8 项 default，
    structured_fallback_strategy 走真值派生 -> derived。"""
    result, _ = _run_check_config(tmp_path, {})
    effective = _effective(result)

    for field in PLAIN_GET_FIELDS:
        assert effective[field]["source"] == "default", field
    assert effective["structured_fallback_strategy"] == {
        "value": "formatted_original",
        "source": "derived",
    }


def test_explicit_values_report_config_source(tmp_path):
    result, config = _run_check_config(
        tmp_path,
        {
            "segmentation": {
                "enable_threshold": 3000,
                "segment_size": 1500,
                "max_segment_size": 3000,
            },
            "structured_calibration": {
                "min_chunk_length": 111,
                "max_chunk_length": 2222,
                "preferred_chunk_length": 333,
                "calibration_concurrent_limit": 9,
            },
            "structured_calibration_for_plain": True,
        },
    )
    effective = _effective(result)

    for field in PLAIN_GET_FIELDS:
        assert effective[field]["source"] == "config", field
    # structured_calibration 段没写 quality_validation.fallback_strategy，
    # 仍走真值派生分支——不能因为「本段其他键都是显式值」就连它一起标 config。
    assert effective["structured_fallback_strategy"]["source"] == "derived"
    assert effective["enable_threshold"]["value"] == 3000
    assert effective["preferred_chunk_length"]["value"] == 333
    assert effective["calibration_concurrent_limit"]["value"] == 9
    _assert_matches_real_parser(effective, config)


def test_explicit_null_scalar_stays_config_source(tmp_path):
    """裸 .get(key, default) 会保留显式 None，不回退 default——来源必须是
    config 而不是 default，value 原样为 null。"""
    result, config = _run_check_config(
        tmp_path,
        {"segmentation": {"segment_size": None}, "structured_calibration": {"max_chunk_length": None}},
    )
    effective = _effective(result)

    assert effective["segment_size"] == {"value": None, "source": "config"}
    assert effective["max_chunk_length"] == {"value": None, "source": "config"}
    # 同段其余缺省键仍走 default，证明判定看的是键在不在，不是该段整体。
    assert effective["enable_threshold"]["source"] == "default"
    _assert_matches_real_parser(effective, config)


def test_explicit_false_scalar_stays_config_source(tmp_path):
    result, config = _run_check_config(
        tmp_path, {"structured_calibration_for_plain": False}
    )
    effective = _effective(result)

    assert effective["structured_calibration_for_plain"] == {
        "value": False,
        "source": "config",
    }
    _assert_matches_real_parser(effective, config)


@pytest.mark.parametrize(
    "calibration_section, expected",
    [
        ({"fallback_to_original": False}, "best_quality"),
        ({"fallback_to_original": None}, "best_quality"),
        ({"fallback_to_original": ""}, "best_quality"),
        ({"fallback_to_original": 0}, "best_quality"),
        ({"fallback_to_original": True}, "formatted_original"),
        ({}, "formatted_original"),
        ({"quality_validation": {"fallback_strategy": ""}}, "formatted_original"),
        ({"quality_validation": {"fallback_strategy": None}}, "formatted_original"),
        (
            {"fallback_to_original": False, "quality_validation": {"fallback_strategy": "keep"}},
            "keep",
        ),
    ],
)
def test_structured_fallback_strategy_follows_truthiness_branch(
    tmp_path, calibration_section, expected
):
    result, config = _run_check_config(
        tmp_path, {"structured_calibration": calibration_section}
    )
    effective = _effective(result)

    entry = effective["structured_fallback_strategy"]
    assert entry["value"] == expected
    configured = calibration_section.get("quality_validation", {}).get("fallback_strategy")
    assert entry["source"] == ("config" if configured else "derived")
    _assert_matches_real_parser(effective, config)


def test_output_never_leaks_credentials(tmp_path):
    result, _ = _run_check_config(
        tmp_path,
        {
            "segmentation": {"enable_threshold": 3000},
            "structured_calibration": {"min_chunk_length": 111},
        },
    )
    assert result.returncode == 0, result.stderr

    for secret in (
        SENTINEL_API_KEY,
        SENTINEL_BASE_URL,
        SENTINEL_WEBHOOK,
        SENTINEL_TIKHUB_KEY,
        SENTINEL_AUTH_TOKEN,
    ):
        assert secret not in result.stdout, secret
        assert secret not in result.stderr, secret
    # 连字段名也不该顺手带出凭据通道。
    for leaked_key in ("api_key", "base_url", "webhook", "auth_token"):
        assert leaked_key not in result.stdout, leaked_key


def test_disabled_backends_and_omitted_sections_still_report(tmp_path):
    """llm 只填 4 个硬键、下游后端显式关闭：--check-config 仍成功，仍给出
    9 项生效值，且不创建任何运行期目录/文件。"""
    result, config = _run_check_config(tmp_path, {})
    assert result.returncode == 0, result.stderr
    effective = _effective(result)
    assert set(effective) == set(WHITELIST_FIELDS)

    for created in ("cache", "workspace", "temp", "audit.db", "app.log"):
        assert not (tmp_path / created).exists(), created
    _assert_matches_real_parser(effective, config)


def test_missing_llm_section_still_fails_through_original_error_path(tmp_path):
    """诊断只在成功路径追加输出；缺必填 llm 段继续沿原错误路径，不新增
    catch/fallback。"""
    config = _base_config(tmp_path)
    config.pop("llm")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(MAIN_PY), "--check-config", "--config", str(config_path)],
        cwd=str(PROJECT_ROOT),
        env=_clean_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "Configuration OK" not in result.stdout
    assert "llm" in result.stderr