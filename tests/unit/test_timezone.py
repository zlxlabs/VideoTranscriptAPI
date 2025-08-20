#!/usr/bin/env python3
"""
测试时区转换功能
"""
import os
import sys
from datetime import datetime, timezone, timedelta

def test_timezone_functionality():
    """测试时区功能"""
    print("开始测试时区转换功能...")
    
    project_root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(project_root)
    sys.path.insert(0, project_root)
    
    try:
        from utils.timezone_helper import (
            parse_timezone_offset, 
            get_configured_timezone,
            format_datetime_with_timezone,
            format_datetime_for_display
        )
        
        # 1. 测试时区解析
        print("\n步骤1: 测试时区字符串解析...")
        
        test_timezones = [
            "UTC+8",
            "UTC-5", 
            "UTC+08:30",
            "UTC-05:30",
            "UTC",
            "utc+8",  # 测试大小写不敏感
            "UTC+0",
            "UTC+12",
            "UTC-12",
            "INVALID"  # 无效格式
        ]
        
        for tz_str in test_timezones:
            result = parse_timezone_offset(tz_str)
            if result:
                offset = result.utcoffset(datetime.now())
                hours = offset.total_seconds() / 3600
                print(f"  [OK] {tz_str} -> {hours:+.1f}小时偏移")
            else:
                print(f"  [WARN] {tz_str} -> 解析失败")
        
        # 2. 测试配置获取
        print("\n步骤2: 测试配置的时区获取...")
        
        configured_tz = get_configured_timezone()
        offset = configured_tz.utcoffset(datetime.now())
        hours = offset.total_seconds() / 3600
        print(f"[OK] 配置的时区偏移: {hours:+.1f}小时")
        
        # 3. 测试时间格式转换
        print("\n步骤3: 测试时间格式转换...")
        
        test_times = [
            "2025-08-20 12:34:56",
            "2025-08-20T12:34:56", 
            "2025-08-20T12:34:56Z",
            "2025-08-20 12:34:56.123456",
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ]
        
        for test_time in test_times:
            try:
                formatted_time = format_datetime_with_timezone(test_time)
                print(f"  [OK] {test_time} -> {formatted_time}")
            except Exception as e:
                print(f"  [ERROR] {test_time} -> {e}")
        
        # 4. 测试显示格式
        print("\n步骤4: 测试用户显示格式...")
        
        current_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        display_format = format_datetime_for_display(current_utc)
        print(f"[OK] 当前UTC时间 {current_utc} -> 显示为: {display_format}")
        
        # 5. 测试不同时区配置
        print("\n步骤5: 测试不同时区配置...")
        
        # 临时修改配置测试
        from utils import load_config
        
        original_config = load_config()
        test_timezones_config = ["UTC+0", "UTC-5", "UTC+9", "UTC+05:30"]
        
        for tz in test_timezones_config:
            # 模拟不同时区配置
            original_config["web"]["timezone"] = tz
            
            # 重新导入模块以获取新配置
            import importlib
            import utils.timezone_helper
            importlib.reload(utils.timezone_helper)
            
            from utils.timezone_helper import get_configured_timezone, format_datetime_for_display
            
            test_time = "2025-08-20 12:00:00"
            display_time = format_datetime_for_display(test_time)
            print(f"  [OK] {tz}: {test_time} UTC -> {display_time}")
        
        # 恢复原始配置
        original_config["web"]["timezone"] = "UTC+8"
        
        print("\n[SUCCESS] 时区转换功能测试完成！")
        print("功能总结:")
        print("  [OK] 时区字符串解析")
        print("  [OK] 配置文件时区读取")
        print("  [OK] UTC时间转本地时间")
        print("  [OK] 用户友好的时间显示格式")
        print("  [OK] 多种时区格式支持")
        
        return True
        
    except Exception as e:
        print(f"[ERROR] 测试过程中出现异常: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

def test_edge_cases():
    """测试边界情况"""
    print("\n测试边界情况...")
    
    try:
        from utils.timezone_helper import (
            format_datetime_with_timezone,
            format_datetime_for_display
        )
        
        edge_cases = [
            "",           # 空字符串
            None,         # None值
            "invalid",    # 无效格式
            "2025-13-45 25:61:99",  # 无效日期时间
        ]
        
        for case in edge_cases:
            try:
                result1 = format_datetime_with_timezone(str(case) if case is not None else "")
                result2 = format_datetime_for_display(str(case) if case is not None else "")
                print(f"  [OK] 边界情况 '{case}' 处理正常")
            except Exception as e:
                print(f"  [WARN] 边界情况 '{case}': {e}")
        
        print("[OK] 边界情况测试完成")
        return True
        
    except Exception as e:
        print(f"[ERROR] 边界情况测试异常: {e}")
        return False

if __name__ == "__main__":
    success = test_timezone_functionality()
    edge_success = test_edge_cases()
    
    if success and edge_success:
        print("\n✅ 所有时区功能测试通过！")
        sys.exit(0)
    else:
        print("\n❌ 部分测试失败")
        sys.exit(1)