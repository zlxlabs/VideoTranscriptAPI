"""
企业微信通知发送流程完整测试

模拟从任务创建到完成的所有企业微信通知发送点
测试风控模块对敏感词的处理效果
"""

import sys
import os
import time

# 添加项目根目录到路径
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, project_root)

from src.video_transcript_api.utils.notifications import (
    WechatNotifier,
    send_long_text_wechat,
    send_view_link_wechat,
)
from src.video_transcript_api.utils.logging import load_config
from src.video_transcript_api.risk_control import init_risk_control, is_enabled


def load_test_summary():
    """加载测试用的总结文本"""
    summary_file = os.path.join(project_root, "data", "temp", "敏感词内容校验.txt")

    if not os.path.exists(summary_file):
        print(f"ERROR: Test summary file not found: {summary_file}")
        return None

    with open(summary_file, 'r', encoding='utf-8') as f:
        return f.read()


def test_notification_flow():
    """测试完整的企业微信通知发送流程"""

    print("=" * 80)
    print("Enterprise WeChat Notification Flow Test")
    print("=" * 80)

    # 加载配置
    print("\n[Step 1] Loading configuration...")
    config = load_config()

    # 初始化风控模块
    print("\n[Step 2] Initializing risk control module...")
    risk_config = config.get("risk_control", {})
    if risk_config.get("enabled", False):
        init_risk_control(config)
        if is_enabled():
            print("[OK] Risk control module initialized successfully")
        else:
            print("[FAIL] Risk control module failed to initialize")
    else:
        print("[WARN] Risk control is disabled in config")

    # 测试数据
    test_url = "https://www.youtube.com/watch?v=crMrVozp_h8"
    test_title = "关键时刻必有关键抉择——习近平经济思想引领新时代经济工作述评之四"
    test_author = "新华社"
    test_view_token = "test_token_" + str(int(time.time()))

    # 加载测试总结文本
    print("\n[Step 3] Loading test summary content...")
    test_summary = load_test_summary()
    if not test_summary:
        print("[FAIL] Failed to load summary, using fallback text")
        test_summary = "这是一段测试总结文本，包含敏感词用于测试风控功能。"
    else:
        print(f"[OK] Loaded summary ({len(test_summary)} characters)")

    # 获取webhook配置
    webhook = config.get("wechat", {}).get("webhook")
    if not webhook:
        print("\n[ERROR] No webhook configured in config.json")
        return

    print(f"\n[Step 4] Using webhook: {webhook[:50]}...")

    # 创建通知器（自动使用全局 WeComNotifier，已内置频率控制）
    notifier = WechatNotifier(webhook)

    print("\n" + "=" * 80)
    print("Starting notification sequence...")
    print("=" * 80)

    # ===== 通知1: 任务创建通知 =====
    print("\n[Notification 1/8] Task Creation - View Link")
    print("-" * 80)
    try:
        success = send_view_link_wechat(
            title=f"YouTube Video Transcription",
            view_token=test_view_token,
            webhook=webhook,
            original_url=test_url
        )
        print(f"Status: {'[SENT]' if success else '[FAILED]'}")
        time.sleep(1)  # 延迟，避免发送过快
    except Exception as e:
        print(f"[ERROR] {e}")

    # ===== 通知2: 开始处理 =====
    print("\n[Notification 2/8] Task Start Processing")
    print("-" * 80)
    try:
        success = notifier.notify_task_status(
            url=test_url,
            status="开始处理 - 普通转录(CapsWriter)",
            title=test_title,
            author=test_author
        )
        print(f"Status: {'[SENT]' if success else '[FAILED]'}")
        time.sleep(1)
    except Exception as e:
        print(f"[ERROR] {e}")

    # ===== 通知3: 正在下载 =====
    print("\n[Notification 3/8] Downloading Video")
    print("-" * 80)
    try:
        success = notifier.notify_task_status(
            url=test_url,
            status="正在下载视频 - 普通转录(CapsWriter)",
            title=test_title,
            author=test_author
        )
        print(f"Status: {'[SENT]' if success else '[FAILED]'}")
        time.sleep(1)
    except Exception as e:
        print(f"[ERROR] {e}")

    # ===== 通知4: 正在转录 =====
    print("\n[Notification 4/8] Transcribing Audio")
    print("-" * 80)
    try:
        success = notifier.notify_task_status(
            url=test_url,
            status="正在转录音视频 - 普通转录(CapsWriter)",
            title=test_title,
            author=test_author
        )
        print(f"Status: {'[SENT]' if success else '[FAILED]'}")
        time.sleep(1)
    except Exception as e:
        print(f"[ERROR] {e}")

    # ===== 通知5: 转录完成 =====
    print("\n[Notification 5/8] Transcription Complete")
    print("-" * 80)
    test_transcript_preview = "党的十八大以来，以习近平同志为核心的党中央科学研判发展大势..."
    try:
        success = notifier.notify_task_status(
            url=test_url,
            status="转录完成 - 普通转录(CapsWriter)",
            title=test_title,
            author=test_author,
            transcript=test_transcript_preview
        )
        print(f"Status: {'[SENT]' if success else '[FAILED]'}")
        time.sleep(1)
    except Exception as e:
        print(f"[ERROR] {e}")

    # ===== 通知6: 发送总结文本（分段） =====
    print("\n[Notification 6/8] Sending Summary Text (may be split into multiple parts)")
    print("-" * 80)
    try:
        send_long_text_wechat(
            title=test_title,
            url=test_url,
            text=test_summary,
            is_summary=True,
            webhook=webhook,
            has_speaker_recognition=False,
            use_rate_limit=True
        )
        print("[OK] Summary text sent (check logs for details)")
        time.sleep(2)  # 给分段发送留出时间
    except Exception as e:
        print(f"[ERROR] {e}")

    # ===== 通知7: 任务完成通知 =====
    print("\n[Notification 7/8] Task Completion with View Link")
    print("-" * 80)
    try:
        from src.video_transcript_api.utils.rendering import get_base_url

        base_url = get_base_url()
        view_url = f"{base_url}/view/{test_view_token}"
        clean_url = notifier._clean_url(test_url)

        completion_message = f"# {test_title}\n\n{clean_url}\n\n🔗 总结和校对：\n{view_url}\n\n✅ **【任务完成】**"

        success = notifier.send_text(completion_message)
        print(f"Status: {'[SENT]' if success else '[FAILED]'}")
        time.sleep(1)
    except Exception as e:
        print(f"[ERROR] {e}")

    # ===== 通知8: 错误通知测试（可选） =====
    print("\n[Notification 8/8] Error Notification (optional test)")
    print("-" * 80)
    try:
        success = notifier.notify_task_status(
            url=test_url,
            status="转录异常",
            error="这是一个测试错误信息",
            title=test_title,
            author=test_author
        )
        print(f"Status: {'[SENT]' if success else '[FAILED]'}")
    except Exception as e:
        print(f"[ERROR] {e}")

    print("\n" + "=" * 80)
    print("Notification Flow Test Completed!")
    print("=" * 80)

    # 风控统计
    if is_enabled():
        print("\n[Risk Control Summary]")
        print("Check the logs above for 'WARNING' messages about sensitive words")
        print("Sensitive words should be sanitized with random characters")


if __name__ == "__main__":
    print("\n" + "=" * 80)
    print("Enterprise WeChat Notification Flow Complete Test")
    print("Testing URL: https://www.youtube.com/watch?v=crMrVozp_h8")
    print("Testing Title: 关键时刻必有关键抉择——习近平经济思想引领新时代经济工作述评之四")
    print("Testing Content: From敏感词内容校验.txt")
    print("=" * 80)

    try:
        test_notification_flow()
    except KeyboardInterrupt:
        print("\n\nTest interrupted by user")
    except Exception as e:
        print(f"\n\nTest failed with error: {e}")
        import traceback
        traceback.print_exc()
