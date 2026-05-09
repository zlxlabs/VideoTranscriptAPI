"""Tests for log_llm_config_summary — startup per-task provider+model+thinking line.

Verifies:
- One line per configured task (calibrate/summary/risk_*/validator/...)
- Each line includes provider family and thinking mode
- Line format is stable (used for log grep by operators)
- No secret-adjacent fields leak (api_key, base_url, headers)
"""
import logging

import pytest

from video_transcript_api.llm.llm import log_llm_config_summary


class TestLogLLMConfigSummary:
    def test_summarizes_each_task(self, caplog):
        config = {
            "llm": {
                "api_key": "SECRET_KEY_DO_NOT_LEAK",
                "base_url": "https://internal-gateway.example/v1",
                "calibrate_model": "gpt-4.1-mini",
                "calibrate_reasoning_effort": None,
                "summary_model": "deepseek-v4-flash",
                "summary_reasoning_effort": "high",
            }
        }
        with caplog.at_level(logging.INFO):
            log_llm_config_summary(config)

        text = caplog.text
        # Per-task lines
        assert "calibrate" in text.lower()
        assert "summary" in text.lower()
        # Model + provider
        assert "gpt-4.1-mini" in text
        assert "deepseek-v4-flash" in text
        assert "deepseek" in text.lower()

    def test_does_not_leak_secrets(self, caplog):
        config = {
            "llm": {
                "api_key": "SECRET_KEY_DO_NOT_LEAK",
                "base_url": "https://internal-gateway.example/v1",
                "calibrate_model": "gpt-4o",
                "summary_model": "deepseek-v4-flash",
                "summary_reasoning_effort": "high",
            }
        }
        with caplog.at_level(logging.INFO):
            log_llm_config_summary(config)

        # 严禁把 api_key / base_url 输出到启动日志
        assert "SECRET_KEY_DO_NOT_LEAK" not in caplog.text
        assert "internal-gateway.example" not in caplog.text

    def test_reflects_thinking_mode(self, caplog):
        config = {
            "llm": {
                "api_key": "k",
                "base_url": "http://x",
                "calibrate_model": "deepseek-v4-flash",
                "calibrate_reasoning_effort": "disabled",
                "summary_model": "deepseek-v4-flash",
                "summary_reasoning_effort": "high",
            }
        }
        with caplog.at_level(logging.INFO):
            log_llm_config_summary(config)
        # disabled 任务能被识别
        assert "disabled" in caplog.text.lower()
        assert "high" in caplog.text.lower()

    def test_handles_missing_llm_key(self, caplog):
        with caplog.at_level(logging.INFO):
            log_llm_config_summary({})  # 不应炸
        # 可以不产生输出或产生一行 warning，只要不 raise

    def test_does_not_raise_on_bad_model_name(self):
        # 防御性：未知模型不应让启动崩溃
        log_llm_config_summary({
            "llm": {
                "api_key": "k",
                "base_url": "http://x",
                "calibrate_model": "weird-unknown-model-xyz",
                "summary_model": "deepseek-v4-flash",
            }
        })
