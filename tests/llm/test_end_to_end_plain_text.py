"""End-to-end test for plain text LLM processing

Test the complete flow from raw transcript to calibrated text.
Uses real transcript from BV1JkzaBpETo.
"""

import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root / "src"))


def test_plain_text_e2e():
    """Test end-to-end plain text processing"""
    print("=" * 80)
    print("End-to-End Plain Text LLM Processing Test")
    print("=" * 80)

    # Step 1: Load configuration
    print("\n[Step 1] Loading configuration...")
    try:
        from video_transcript_api.api.context import get_config
        config = get_config()
        print("[PASS] Configuration loaded")
        print(f"  - API Key: {config.get('llm', {}).get('api_key', 'NOT_SET')[:10]}...")
        print(f"  - Base URL: {config.get('llm', {}).get('base_url', 'NOT_SET')}")
    except Exception as e:
        print(f"[FAIL] Configuration loading failed: {e}")
        return False

    # Step 2: Load transcript
    print("\n[Step 2] Loading transcript...")
    try:
        transcript_path = project_root / "data/cache/bilibili/2026/202601/BV1JkzaBpETo/transcript_capswriter.txt"
        if not transcript_path.exists():
            print(f"[FAIL] Transcript file not found: {transcript_path}")
            return False

        with open(transcript_path, 'r', encoding='utf-8') as f:
            transcript = f.read().strip()

        print("[PASS] Transcript loaded")
        print(f"  - Length: {len(transcript)} characters")
        print(f"  - Preview: {transcript[:100]}...")
    except Exception as e:
        print(f"[FAIL] Transcript loading failed: {e}")
        import traceback
        traceback.print_exc()
        return False

    # Step 3: Initialize LLM Coordinator
    print("\n[Step 3] Initializing LLM Coordinator...")
    try:
        from video_transcript_api.llm import LLMCoordinator

        cache_dir = config.get("storage", {}).get("cache_dir", "./data/cache")
        coordinator = LLMCoordinator(config_dict=config, cache_dir=cache_dir)

        print("[PASS] LLM Coordinator initialized")
        print(f"  - Cache dir: {cache_dir}")
        print(f"  - Calibrate model: {coordinator.config.calibrate_model}")
        print(f"  - Max retries: {coordinator.config.max_retries}")
    except Exception as e:
        print(f"[FAIL] LLM Coordinator initialization failed: {e}")
        import traceback
        traceback.print_exc()
        return False

    # Step 4: Process transcript (MAIN TEST)
    print("\n[Step 4] Processing transcript with LLM Coordinator...")
    print("  (This may take a while...)")
    try:
        result = coordinator.process(
            content=transcript,  # Plain text mode (no speaker recognition)
            title="特朗普盯上委内瑞拉石油？小心赔了夫人又折兵！",
            author="差评",
            description="",
            platform="bilibili",
            media_id="BV1JkzaBpETo",
            has_risk=False,
        )

        print("[PASS] Transcript processing completed")
        print(f"  - Result type: {type(result)}")
        print(f"  - Result keys: {list(result.keys())}")
    except Exception as e:
        print(f"[FAIL] Transcript processing failed: {e}")
        import traceback
        traceback.print_exc()
        return False

    # Step 5: Validate results
    print("\n[Step 5] Validating results...")
    validation_passed = True

    # Check 5.1: calibrated_text exists
    if "calibrated_text" not in result:
        print("[FAIL] Missing 'calibrated_text' in result")
        validation_passed = False
    else:
        calibrated_text = result["calibrated_text"]
        print(f"[PASS] calibrated_text exists (length: {len(calibrated_text)})")
        print(f"  - Preview: {calibrated_text[:100]}...")

        # Check 5.2: calibrated text is not empty
        if not calibrated_text or len(calibrated_text) < 100:
            print(f"[FAIL] calibrated_text too short: {len(calibrated_text)}")
            validation_passed = False
        else:
            print(f"[PASS] calibrated_text has reasonable length")

    # Check 5.3: key_info exists
    if "key_info" not in result:
        print("[FAIL] Missing 'key_info' in result")
        validation_passed = False
    else:
        key_info = result["key_info"]
        print(f"[PASS] key_info exists")
        print(f"  - Names: {key_info.get('names', [])[:5]}")
        print(f"  - Places: {key_info.get('places', [])[:5]}")
        print(f"  - Terms: {key_info.get('terms', [])[:5]}")

    # Check 5.4: stats exists
    if "stats" not in result:
        print("[FAIL] Missing 'stats' in result")
        validation_passed = False
    else:
        stats = result["stats"]
        print(f"[PASS] stats exists")
        print(f"  - Original length: {stats.get('original_length', 0)}")
        print(f"  - Calibrated length: {stats.get('calibrated_length', 0)}")
        print(f"  - Segment count: {stats.get('segment_count', 0)}")

        # Check 5.5: lengths match expectations
        original_len = stats.get('original_length', 0)
        calibrated_len = stats.get('calibrated_length', 0)

        if original_len != len(transcript):
            print(f"[WARN] Original length mismatch: {original_len} vs {len(transcript)}")

        if calibrated_len != len(result.get('calibrated_text', '')):
            print(f"[WARN] Calibrated length mismatch: {calibrated_len} vs {len(result.get('calibrated_text', ''))}")

        # Check 5.6: calibrated text should not be too short (quality check)
        min_ratio = config.get("llm", {}).get("min_calibrate_ratio", 0.5)
        actual_ratio = calibrated_len / original_len if original_len > 0 else 0

        if actual_ratio < min_ratio:
            print(f"[FAIL] Calibrated text too short: ratio {actual_ratio:.2%} < {min_ratio:.2%}")
            validation_passed = False
        else:
            print(f"[PASS] Calibrated text length ratio OK: {actual_ratio:.2%}")

    # Step 6: Summary
    print("\n" + "=" * 80)
    print("Test Summary")
    print("=" * 80)

    if validation_passed:
        print("\n[SUCCESS] All validations passed!")
        print("\nCalibrated text preview:")
        print("-" * 80)
        print(result['calibrated_text'][:500])
        print("-" * 80)
        return True
    else:
        print("\n[FAILURE] Some validations failed. Please check errors above.")
        return False


def main():
    """Main entry point"""
    try:
        success = test_plain_text_e2e()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\n[FATAL ERROR] Test crashed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
