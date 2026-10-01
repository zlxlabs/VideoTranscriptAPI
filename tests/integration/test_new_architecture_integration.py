"""Test new architecture integration"""

import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root / "src"))


def test_imports():
    """Test basic imports"""
    assert _run_import_cases()


def _run_import_cases():
    """实际执行导入检查（__main__ 脚本入口需要 bool 来算 sys.exit 退出码）"""
    print("Testing imports...")

    try:
        from video_transcript_api.api.context import get_llm_coordinator, get_config
        assert callable(get_llm_coordinator), "get_llm_coordinator 不可调用"
        assert callable(get_config), "get_config 不可调用"
        print("[PASS] context imported successfully")

        from video_transcript_api.llm import LLMCoordinator
        assert callable(LLMCoordinator), "LLMCoordinator 不可调用"
        print("[PASS] LLMCoordinator imported successfully")

        print("\nAll import tests passed!")
        return True
    except Exception as e:
        print(f"[FAIL] Import failed: {e}")
        return False


def test_coordinator_initialization():
    """Test coordinator initialization"""
    assert _run_coordinator_init_cases()


def _run_coordinator_init_cases():
    """构造 coordinator 并断言配置确实被读入（__main__ 入口需要 bool）"""
    print("\nTesting coordinator initialization...")

    try:
        from video_transcript_api.api.context import get_config
        from video_transcript_api.llm import LLMCoordinator

        config = get_config()
        cache_dir = config.get("storage", {}).get("cache_dir", "./data/cache")

        coordinator = LLMCoordinator(config_dict=config, cache_dir=cache_dir)
        # 真断言：配置字段真的到位，而不是「构造过程恰好没抛异常」
        assert coordinator.config is not None, "coordinator.config 为空"
        assert coordinator.config.calibrate_model, "calibrate_model 未从配置读入"
        print("[PASS] Coordinator initialized successfully")
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
    assert _run_process_interface_cases()


def _run_process_interface_cases():
    """检查 process 接口存在且可调用（__main__ 入口需要 bool）"""
    print("\nTesting process interface...")

    try:
        from video_transcript_api.api.context import get_llm_coordinator

        coordinator = get_llm_coordinator()

        # Check if interface exists
        assert hasattr(coordinator, 'process'), "Missing process method"
        assert callable(coordinator.process), "process 不可调用"

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

    results.append(("Import Test", _run_import_cases()))
    results.append(("Coordinator Init", _run_coordinator_init_cases()))
    results.append(("Interface Test", _run_process_interface_cases()))

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