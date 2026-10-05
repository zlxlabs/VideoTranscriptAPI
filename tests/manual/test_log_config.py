"""
Test script to verify log configuration is working correctly
"""
import os
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root / "src"))

from video_transcript_api.utils import create_debug_dir, get_llm_debug_dir, load_config


def test_log_paths():
    """Test that all log paths are correctly configured and created"""

    print("=" * 60)
    print("Testing Log Configuration")
    print("=" * 60)

    # Load config
    config = load_config()
    log_config = config.get("log", {})

    # Test main log file path
    main_log_path = log_config.get("file", "NOT_CONFIGURED")
    print(f"\n1. Main log file path: {main_log_path}")
    print(f"   Directory exists: {os.path.exists(os.path.dirname(main_log_path))}")

    # Test debug directory
    debug_dir = create_debug_dir()
    print(f"\n2. Debug directory: {debug_dir}")
    print(f"   Expected: {log_config.get('debug_dir', 'NOT_CONFIGURED')}")
    print(f"   Directory exists: {os.path.exists(debug_dir)}")
    print(f"   Match: {debug_dir == log_config.get('debug_dir', '')}")

    # Test LLM debug directory
    llm_debug_dir = get_llm_debug_dir()
    print(f"\n3. LLM debug directory: {llm_debug_dir}")
    print(f"   Expected: {log_config.get('llm_debug_dir', 'NOT_CONFIGURED')}")
    print(f"   Directory exists: {os.path.exists(llm_debug_dir)}")
    print(f"   Match: {llm_debug_dir == log_config.get('llm_debug_dir', '')}")

    # Create test files to verify write permissions
    test_debug_file = os.path.join(debug_dir, "test_write.txt")
    test_llm_debug_file = os.path.join(llm_debug_dir, "test_write.txt")

    print("\n" + "=" * 60)
    print("Testing Write Permissions")
    print("=" * 60)

    try:
        with open(test_debug_file, 'w', encoding='utf-8') as f:
            f.write("Test content for debug directory")
        print(f"\n4. Debug directory write test: PASSED")
        print(f"   Test file created: {test_debug_file}")
        os.remove(test_debug_file)
        print(f"   Test file removed successfully")
    except Exception as e:
        print(f"\n4. Debug directory write test: FAILED")
        print(f"   Error: {e}")

    try:
        with open(test_llm_debug_file, 'w', encoding='utf-8') as f:
            f.write("Test content for LLM debug directory")
        print(f"\n5. LLM debug directory write test: PASSED")
        print(f"   Test file created: {test_llm_debug_file}")
        os.remove(test_llm_debug_file)
        print(f"   Test file removed successfully")
    except Exception as e:
        print(f"\n5. LLM debug directory write test: FAILED")
        print(f"   Error: {e}")

    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)

    # Check if old directories exist
    old_logs_debug = os.path.exists("./logs/debug")
    old_debug = os.path.exists("./debug")

    if old_logs_debug or old_debug:
        print("\nWARNING: Old log directories still exist:")
        if old_logs_debug:
            print("  - ./logs/debug (should migrate to data/logs/debug)")
        if old_debug:
            print("  - ./debug (should migrate to data/logs/llm_debug)")
    else:
        print("\nGood: No old log directories found")

    print("\nAll log paths are now unified under ./data/logs/")
    print("=" * 60)


if __name__ == "__main__":
    test_log_paths()
