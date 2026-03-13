"""
Test script to verify view_token query fix
"""

import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from video_transcript_api.cache import CacheManager

def test_view_token_query():
    """Test that view_token returns the correct task (success status)"""

    # Initialize cache manager
    cache_manager = CacheManager(
        cache_dir="./data/cache"
    )

    # Test view_token that has multiple tasks
    view_token = "view_FApVbBfNbjkelp6xCJxrL2y1p4QVCKoepHx9kksS2O4"

    print(f"Testing view_token: {view_token}")
    print("-" * 60)

    # Get task by view_token
    task_info = cache_manager.get_task_by_view_token(view_token)

    if task_info:
        print(f"Task ID: {task_info['task_id']}")
        print(f"Status: {task_info['status']}")
        print(f"Platform: {task_info.get('platform', 'N/A')}")
        print(f"Media ID: {task_info.get('media_id', 'N/A')}")
        print(f"Title: {task_info.get('title', 'N/A')}")
        print(f"Author: {task_info.get('author', 'N/A')}")
        print("-" * 60)

        # Verify it returns success status
        assert task_info['status'] == 'success', f"Expected 'success', got '{task_info['status']}'"
        assert task_info.get('platform') == 'youtube', f"Expected 'youtube', got '{task_info.get('platform')}'"
        assert task_info.get('media_id') == 'rOQJq7qXIcs', f"Expected 'rOQJq7qXIcs', got '{task_info.get('media_id')}'"

        print("[PASS] Task query returned correct result (success status)")

    else:
        print("[FAIL] No task found for view_token")
        return False

    # Test get_view_data_by_token
    print("\nTesting get_view_data_by_token...")
    print("-" * 60)

    view_data = cache_manager.get_view_data_by_token(view_token)

    if view_data:
        print(f"Status: {view_data.get('status')}")
        print(f"Title: {view_data.get('title', 'N/A')}")
        print(f"Author: {view_data.get('author', 'N/A')}")
        print(f"Has transcript: {'transcript' in view_data}")
        print(f"Has summary: {'summary' in view_data}")
        print("-" * 60)

        # Verify it returns success status
        assert view_data.get('status') == 'success', f"Expected 'success', got '{view_data.get('status')}'"

        print("[PASS] View data query returned correct result")

    else:
        print("[FAIL] No view data found for view_token")
        return False

    print("\n" + "=" * 60)
    print("All tests passed!")
    print("=" * 60)

    cache_manager.close()
    return True

if __name__ == "__main__":
    try:
        success = test_view_token_query()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\n[FAIL] Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
