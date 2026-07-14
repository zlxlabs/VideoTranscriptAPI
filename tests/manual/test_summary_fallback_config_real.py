"""
Manual/integration checks for the REAL config/config.jsonc deployment file.

Split out of tests/unit/test_summary_fallback_config.py (2026-07 CI sweep):
these two checks assert against the developer's real, gitignored
config/config.jsonc (not the tracked config.example.jsonc placeholder), so
they only make sense to run manually against a real local deployment config.
They must not run in the default `pytest -q` discovery (no config.jsonc on
CI runners / clean checkouts).

Run: VTAPI_TESTS_MANUAL=1 uv run pytest tests/manual/test_summary_fallback_config_real.py -v

All console output must be in English only (no emoji, no Chinese).
"""

import json
import re


class TestRealContentFallbacksConfig:
    """Verify content_fallbacks configuration against the real config.jsonc."""

    def _load_config_jsonc(self, path: str) -> dict:
        """Load JSONC file (strip comments and handle control chars)."""
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        # Remove single-line comments (but not inside strings)
        content = re.sub(r'(?<!:)//[^\n]*', '', content)
        # Remove multi-line comments
        content = re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)
        # Remove trailing commas before } or ]
        content = re.sub(r',\s*([}\]])', r'\1', content)
        return json.loads(content, strict=False)

    def test_config_has_deepseek_v4_pro_fallback(self):
        """config.jsonc should have deepseek-v4-pro fallback with deepseek-v4-flash first."""
        config = self._load_config_jsonc("config/config.jsonc")
        llm = config["llm"]
        fallbacks = llm["content_fallbacks"]
        assert "deepseek-v4-pro" in fallbacks
        # deepseek-v4-flash should be the first fallback (same provider, fastest)
        assert fallbacks["deepseek-v4-pro"][0] == "deepseek-v4-flash"

    def test_total_timeout_sufficient_for_fallback(self):
        """total_timeout should be >= 300s to leave room for fallback after primary timeout."""
        config = self._load_config_jsonc("config/config.jsonc")
        llm = config["llm"]
        total_timeout = llm.get("total_timeout", 180)
        assert total_timeout >= 300, (
            f"total_timeout={total_timeout}s is too short for fallback execution. "
            f"Primary model may consume ~180s, leaving no time for fallbacks."
        )
