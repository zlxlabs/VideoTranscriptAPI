from video_transcript_api.utils.notifications.channel import build_task_status_content


def test_cache_completion_uses_one_shared_task_heading():
    content = build_task_status_content(
        url="https://example.com/test-video",
        status="【任务完成】",
        title="测试视频标题",
        task_id="task_abcdef123456",
        completion_body="总结正文 fixture",
    )

    assert content.splitlines()[0] == "✅ [#abcdef] 测试视频标题"
    assert "总结正文 fixture" in content
