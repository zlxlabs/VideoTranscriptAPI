from video_transcript_api.utils.notifications.channel import build_task_status_content


def test_failure_webhook_message_starts_with_task_heading():
    content = build_task_status_content(
        url="https://example.com/test-video",
        status="下载失败",
        error="connection refused",
        title="测试视频标题",
        task_id="task_abcdef123456",
    )

    assert content.splitlines()[0] == "❌ [#abcdef] 测试视频标题"
    assert "下载失败" in content
    assert "connection refused" in content
