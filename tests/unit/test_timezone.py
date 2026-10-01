#!/usr/bin/env python3
"""
测试时区转换功能
"""
import os
import sys
from datetime import datetime, timezone, timedelta

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def test_timezone_functionality():
    """测试时区功能（pytest 入口）"""
    assert _run_timezone_cases()


def _run_timezone_cases():
    """实际断言时区解析/格式化/配置读取（__main__ 脚本入口需要 bool 算退出码）"""
    print("开始测试时区转换功能...")

    project_root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(project_root)

    from video_transcript_api.utils.timeutil import (
        parse_timezone_offset,
        get_configured_timezone,
        format_datetime_with_timezone,
        format_datetime_for_display,
    )

    # 1. 测试时区解析：期望小时偏移逐条对照
    print("\n步骤1: 测试时区字符串解析...")

    expected_offsets = {
        "UTC+8": 8,
        "UTC-5": -5,
        "UTC+08:30": 8.5,
        "UTC-05:30": -5.5,
        "UTC": 0,
        "utc+8": 8,          # 大小写不敏感
        "UTC+0": 0,
        "UTC+12": 12,
        "UTC-12": -12,
    }

    for tz_str, expected_hours in expected_offsets.items():
        result = parse_timezone_offset(tz_str)
        assert result is not None, f"{tz_str} 解析失败"
        offset = result.utcoffset(datetime.now())
        hours = offset.total_seconds() / 3600
        assert hours == expected_hours, f"{tz_str} -> {hours}h，期望 {expected_hours}h"
        print(f"  [OK] {tz_str} -> {hours:+.1f}小时偏移")

    # 无效格式必须解析失败（返回 None），而不是悄悄给个默认时区
    for bad in ("INVALID", "GMT+8", "", "UTC+abc"):
        assert parse_timezone_offset(bad) is None, f"{bad!r} 应解析失败"
    print("  [OK] 无效格式均返回 None")

    # 2. 测试配置获取
    print("\n步骤2: 测试配置的时区获取...")

    configured_tz = get_configured_timezone()
    assert configured_tz is not None, "get_configured_timezone 返回 None"
    offset = configured_tz.utcoffset(datetime.now())
    hours = offset.total_seconds() / 3600
    print(f"[OK] 配置的时区偏移: {hours:+.1f}小时")

    # 3. 测试时间格式转换
    print("\n步骤3: 测试时间格式转换...")

    # 无时区后缀的输入按 UTC 解析，再转到配置时区
    expected_output = (
        datetime(2025, 8, 20, 12, 34, 56, tzinfo=timezone.utc)
        .astimezone(configured_tz)
        .strftime("%Y-%m-%d %H:%M:%S")
    )
    test_times = [
        "2025-08-20 12:34:56",
        "2025-08-20T12:34:56",
        "2025-08-20T12:34:56Z",
        "2025-08-20 12:34:56.123456",
    ]

    for test_time in test_times:
        formatted_time = format_datetime_with_timezone(test_time)
        assert formatted_time == expected_output, \
            f"{test_time} -> {formatted_time}，期望 {expected_output}"
        print(f"  [OK] {test_time} -> {formatted_time}")

    # 4. 测试显示格式
    print("\n步骤4: 测试用户显示格式...")

    current_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    display_format = format_datetime_for_display(current_utc)
    assert "年" in display_format and "月" in display_format and "日" in display_format, \
        f"显示格式缺少年月日: {display_format}"
    assert "(UTC" in display_format, f"显示格式缺少时区标注: {display_format}"
    print(f"[OK] 当前UTC时间 {current_utc} -> 显示为: {display_format}")

    # 5. 测试不同时区配置
    print("\n步骤5: 测试不同时区配置...")

    import importlib
    import video_transcript_api.utils.timeutil.timezone_helper as timezone_helper_module

    test_time = "2025-08-20 12:00:00"
    for tz in ["UTC+0", "UTC-5", "UTC+9", "UTC+05:30"]:
        # 直接改配置，再 reload 模块让 timezone_helper 读到新值
        original_config = timezone_helper_module.load_config()
        original_tz = original_config["web"]["timezone"]
        original_config["web"]["timezone"] = tz

        importlib.reload(timezone_helper_module)

        from video_transcript_api.utils.timeutil import (
            get_configured_timezone as get_configured_timezone_reloaded,
            format_datetime_for_display as format_display_reloaded,
        )

        offset_hours = get_configured_timezone_reloaded().utcoffset(
            datetime.now()
        ).total_seconds() / 3600
        assert offset_hours == tz_to_hours(tz), \
            f"{tz} 生效偏移 {offset_hours}h，期望 {tz_to_hours(tz)}h"

        display_time = format_display_reloaded(test_time)
        expected_display_time = (
            datetime(2025, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
            .astimezone(get_configured_timezone_reloaded())
            .strftime("%Y年%m月%d日 %H:%M")
        )
        assert display_time.startswith(expected_display_time), \
            f"{tz}: {display_time} 期望以 {expected_display_time} 开头"
        print(f"  [OK] {tz}: {test_time} UTC -> {display_time}")

    # 恢复原始配置
    timezone_helper_module.load_config()["web"]["timezone"] = "UTC+8"

    print("\n[SUCCESS] 时区转换功能测试完成！")
    print("功能总结:")
    print("  [OK] 时区字符串解析")
    print("  [OK] 配置文件时区读取")
    print("  [OK] UTC时间转本地时间")
    print("  [OK] 用户友好的时间显示格式")
    print("  [OK] 多种时区格式支持")

    return True


def tz_to_hours(tz_str):
    """把 'UTC+05:30' 这类配置串换算成小时偏移（测试侧期望值）"""
    offset_str = tz_str[3:]
    sign = 1 if offset_str[0] == "+" else -1
    body = offset_str[1:]
    if ":" in body:
        hours, minutes = body.split(":")
        return sign * (int(hours) + int(minutes) / 60)
    return sign * int(body)


def test_edge_cases():
    """测试边界情况（pytest 入口）"""
    assert _run_edge_cases()


def _run_edge_cases():
    """实际断言边界输入的处理（__main__ 脚本入口需要 bool 算退出码）"""
    print("\n测试边界情况...")

    from video_transcript_api.utils.timeutil import (
        format_datetime_with_timezone,
        format_datetime_for_display,
    )

    # 空串：两个函数都不抛异常。注意 format_datetime_for_display("") 实际返回
# ' (UTC+8)'（空正文 + 时区标注）——这里锁住现状，不在本卡改 src 行为。
    assert format_datetime_with_timezone("") == ""
    assert format_datetime_for_display("") == " (UTC+8)"

    # 无法解析的输入：format_datetime_with_timezone 原样返回；display 版本在其后
    # 追加 ' (UTC±H)' 标注（现状如此，本卡不改 src）
    for bad in ("invalid", "2025-13-45 25:61:99"):
        assert format_datetime_with_timezone(bad) == bad, \
            f"不可解析输入 {bad!r} 应原样返回"
        assert format_datetime_for_display(bad).startswith(bad), \
            f"不可解析输入 {bad!r} 应以原文开头: {format_datetime_for_display(bad)!r}"
        print(f"  [OK] 边界情况 '{bad}' 处理正常")

    # None 不该把格式化炸掉（调用方可能直接透传数据库字段）
    assert format_datetime_with_timezone(None) == "", \
        "None 输入应被安全处理为空串"

    print("[OK] 边界情况测试完成")
    return True


if __name__ == "__main__":
    try:
        success = _run_timezone_cases()
        edge_success = _run_edge_cases()
    except AssertionError as e:
        print(f"\n❌ 断言失败: {e}")
        sys.exit(1)

    if success and edge_success:
        print("\n✅ 所有时区功能测试通过！")
        sys.exit(0)
    else:
        print("\n❌ 部分测试失败")
        sys.exit(1)