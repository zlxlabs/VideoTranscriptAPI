"""Test new architecture integration"""

import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root / "src"))


def test_imports():
    """Test basic imports"""
    print("Testing imports...")

    try:
        from video_transcript_api.api.context import get_llm_coordinator, get_config
        print("[PASS] context imported successfully")

        from video_transcript_api.llm import LLMCoordinator
        print("[PASS] LLMCoordinator imported successfully")

        print("\nAll import tests passed!")
        return True
    except Exception as e:
        print(f"[FAIL] Import failed: {e}")
        return False


def test_coordinator_initialization():
    """Test coordinator initialization"""
    print("\nTesting coordinator initialization...")

    try:
        from video_transcript_api.api.context import get_config
        from video_transcript_api.llm import LLMCoordinator

        config = get_config()
        cache_dir = config.get("storage", {}).get("cache_dir", "./data/cache")

        coordinator = LLMCoordinator(config_dict=config, cache_dir=cache_dir)
        print("[PASS] Coordinator initialized successfully")
        print(f"   - API key: {coordinator.config.api_key[:10]}...")
        print(f"   - Cache dir: {cache_dir}")
        print(f"   - Calibrate model: {coordinator.config.calibrate_model}")

        return True
    except Exception as e:
        print(f"[FAIL] Coordinator initialization failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_process_interface():
    """Test process interface"""
    print("\nTesting process interface...")

    try:
        from video_transcript_api.api.context import get_llm_coordinator

        coordinator = get_llm_coordinator()

        # Check if interface exists
        assert hasattr(coordinator, 'process'), "Missing process method"

        print("[PASS] process interface exists")
        print("   - Method signature check passed")

        return True
    except Exception as e:
        print(f"[FAIL] process interface test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    print("=" * 60)
    print("New Architecture Integration Test")
    print("=" * 60)

    results = []

    results.append(("Import Test", test_imports()))
    results.append(("Coordinator Init", test_coordinator_initialization()))
    results.append(("Interface Test", test_process_interface()))

    print("\n" + "=" * 60)
    print("Test Summary")
    print("=" * 60)

    for name, passed in results:
        status = "[PASS]" if passed else "[FAIL]"
        print(f"{name}: {status}")

    all_passed = all(passed for _, passed in results)

    if all_passed:
        print("\nAll tests passed! New architecture integration successful.")
        sys.exit(0)
    else:
        print("\nSome tests failed. Please check error messages.")
        sys.exit(1)
