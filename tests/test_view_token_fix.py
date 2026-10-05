"""
Test script to verify view_token query fix

原实现硬编码了一个生产 view_token（view_FApVb...），依赖本机 ./data/cache 里恰好
存在那条记录；在干净检出/CI 上查不到 → 走 return False 分支被 pytest 静默判绿。
这里改成自建临时 CacheManager、造一条 success 任务，再断言按 view_token 能查回
同一任务与同一查看页数据——验证的行为不变，但不再依赖外部状态。
"""

import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from video_transcript_api.cache import CacheManager


def test_view_token_query():
    """Test that view_token returns the correct task (success status)"""
    assert _run_view_token_query_cases()


def _run_view_token_query_cases():
    """实际断言 view_token 查询行为（__main__ 脚本入口需要 bool 算退出码）"""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        cache_manager = CacheManager(cache_dir=tmp_dir)

        task = cache_manager.create_task(
            "https://www.youtube.com/watch?v=rOQJq7qXIcs", False
        )
        task_id = task["task_id"]
        view_token = task["view_token"]

        cache_manager.save_cache(
            platform="youtube",
            url="https://www.youtube.com/watch?v=rOQJq7qXIcs",
            media_id="rOQJq7qXIcs",
            use_speaker_recognition=False,
            transcript_data="这是一段用于测试的转写文本。",
            transcript_type="capswriter",
            title="测试视频",
            author="测试作者",
        )

        cache_manager.update_task_status(
            task_id,
            "success",
            platform="youtube",
            media_id="rOQJq7qXIcs",
            title="测试视频",
            author="测试作者",
        )

        print(f"Testing view_token: {view_token}")
        print("-" * 60)

        task_info = cache_manager.get_task_by_view_token(view_token)
        assert task_info is not None, "view_token 查不到任务"
        print(f"Task ID: {task_info['task_id']}")
        print(f"Status: {task_info['status']}")

        assert task_info["task_id"] == task_id, \
            f"Expected '{task_id}', got '{task_info['task_id']}'"
        assert task_info['status'] == 'success', \
            f"Expected 'success', got '{task_info['status']}'"
        assert task_info.get('platform') == 'youtube', \
            f"Expected 'youtube', got '{task_info.get('platform')}'"
        assert task_info.get('media_id') == 'rOQJq7qXIcs', \
            f"Expected 'rOQJq7qXIcs', got '{task_info.get('media_id')}'"
        print("[PASS] Task query returned correct result (success status)")

        # 未知 view_token 必须返回 None，而不是随便挑一条任务
        assert cache_manager.get_task_by_view_token("view_does_not_exist") is None, \
            "未知 view_token 不应返回任务"

        print("\nTesting get_view_data_by_token...")
        print("-" * 60)

        view_data = cache_manager.get_view_data_by_token(view_token)
        assert view_data is not None, "view_token 查不到查看页数据"
        print(f"Status: {view_data.get('status')}")

        assert view_data.get('status') == 'success', \
            f"Expected 'success', got '{view_data.get('status')}'"
        assert '这是一段用于测试的转写文本' in view_data.get('transcript', ''), \
            f"查看页未带出转写正文: {view_data.get('transcript')!r}"
        print("[PASS] View data query returned correct result")

        print("\n" + "=" * 60)
        print("All tests passed!")
        print("=" * 60)

        cache_manager.close()
        return True


if __name__ == "__main__":
    try:
        success = _run_view_token_query_cases()
    except AssertionError as e:
        print(f"\n[FAIL] Assertion failed: {e}")
        sys.exit(1)
    sys.exit(0 if success else 1)