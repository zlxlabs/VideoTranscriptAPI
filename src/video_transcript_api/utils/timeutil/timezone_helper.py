from datetime import datetime, timezone, timedelta
from typing import Optional
from ..logging import setup_logger, load_config

logger = setup_logger("timezone_helper")

def parse_timezone_offset(timezone_str: str) -> Optional[timezone]:
    """
    解析时区字符串并返回timezone对象
    
    支持格式：
    - UTC+8, UTC-5 (小时偏移)
    - UTC+08:30, UTC-05:30 (小时:分钟偏移)
    - UTC (UTC时间)
    
    Args:
        timezone_str: 时区字符串
        
    Returns:
        timezone对象，解析失败返回None
    """
    try:
        timezone_str = timezone_str.strip().upper()
        
        if timezone_str == "UTC":
            return timezone.utc
            
        if not timezone_str.startswith("UTC"):
            logger.warning(f"不支持的时区格式: {timezone_str}")
            return None
            
        # 移除UTC前缀
        offset_str = timezone_str[3:]
        
        if not offset_str:
            return timezone.utc
            
        # 判断正负号
        if offset_str[0] not in ['+', '-']:
            logger.warning(f"时区偏移格式错误: {timezone_str}")
            return None
            
        sign = 1 if offset_str[0] == '+' else -1
        offset_str = offset_str[1:]
        
        # 解析时间偏移
        if ':' in offset_str:
            # 格式：HH:MM
            parts = offset_str.split(':')
            if len(parts) != 2:
                logger.warning(f"时区偏移格式错误: {timezone_str}")
                return None
            hours = int(parts[0])
            minutes = int(parts[1])
        else:
            # 格式：H 或 HH
            hours = int(offset_str)
            minutes = 0
        
        # 创建timedelta对象
        total_minutes = sign * (hours * 60 + minutes)
        offset = timedelta(minutes=total_minutes)
        
        return timezone(offset)
        
    except (ValueError, IndexError) as e:
        logger.error(f"解析时区失败 {timezone_str}: {e}")
        return None

def get_configured_timezone() -> timezone:
    """
    获取配置的时区对象
    
    Returns:
        配置的timezone对象，默认为UTC+8
    """
    try:
        config = load_config()
        timezone_str = config.get("web", {}).get("timezone", "UTC+8")
        
        parsed_tz = parse_timezone_offset(timezone_str)
        if parsed_tz is None:
            logger.warning(f"使用默认时区 UTC+8，因为配置的时区无效: {timezone_str}")
            return timezone(timedelta(hours=8))  # 默认UTC+8
            
        return parsed_tz
        
    except Exception as e:
        logger.error(f"获取时区配置失败: {e}")
        return timezone(timedelta(hours=8))  # 默认UTC+8

def format_datetime_with_timezone(dt_str: str, output_format: str = "%Y-%m-%d %H:%M:%S") -> str:
    """
    将数据库中的UTC时间字符串转换为配置时区的格式化字符串
    
    Args:
        dt_str: 数据库中的时间字符串 (SQLite默认格式)
        output_format: 输出格式，默认为 YYYY-MM-DD HH:MM:SS
        
    Returns:
        格式化后的本地化时间字符串
    """
    try:
        if not dt_str:
            return ""
            
        # SQLite的默认时间格式：YYYY-MM-DD HH:MM:SS
        # 尝试解析不同的时间格式
        dt_formats = [
            "%Y-%m-%d %H:%M:%S",      # 2025-08-20 12:34:56
            "%Y-%m-%d %H:%M:%S.%f",   # 2025-08-20 12:34:56.123456
            "%Y-%m-%dT%H:%M:%S",      # 2025-08-20T12:34:56
            "%Y-%m-%dT%H:%M:%S.%f",   # 2025-08-20T12:34:56.123456
            "%Y-%m-%dT%H:%M:%SZ",     # 2025-08-20T12:34:56Z
            "%Y-%m-%dT%H:%M:%S.%fZ",  # 2025-08-20T12:34:56.123456Z
        ]
        
        utc_dt = None
        for fmt in dt_formats:
            try:
                utc_dt = datetime.strptime(dt_str, fmt)
                break
            except ValueError:
                continue
        
        if utc_dt is None:
            logger.warning(f"无法解析时间格式: {dt_str}")
            return dt_str  # 返回原始字符串
        
        # 设置为UTC时区
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
        
        # 转换到配置的时区
        target_tz = get_configured_timezone()
        local_dt = utc_dt.astimezone(target_tz)
        
        # 格式化输出
        return local_dt.strftime(output_format)
        
    except Exception as e:
        logger.error(f"时间转换失败 {dt_str}: {e}")
        return dt_str  # 出错时返回原始字符串

def format_datetime_for_display(dt_str: str) -> str:
    """
    为web页面显示格式化时间
    
    Args:
        dt_str: 数据库中的时间字符串
        
    Returns:
        用户友好的时间显示格式
    """
    try:
        formatted_time = format_datetime_with_timezone(dt_str, "%Y年%m月%d日 %H:%M")
        
        # 获取时区信息用于显示
        target_tz = get_configured_timezone()
        
        # 计算时区偏移用于显示
        offset_seconds = target_tz.utcoffset(datetime.now()).total_seconds()
        offset_hours = int(offset_seconds / 3600)
        offset_minutes = int((abs(offset_seconds) % 3600) / 60)
        
        if offset_minutes == 0:
            tz_display = f"UTC{offset_hours:+d}"
        else:
            sign = "+" if offset_hours >= 0 else "-"
            tz_display = f"UTC{sign}{abs(offset_hours):02d}:{offset_minutes:02d}"
        
        return f"{formatted_time} ({tz_display})"
        
    except Exception as e:
        logger.error(f"格式化显示时间失败 {dt_str}: {e}")
        return dt_str

def get_current_time_display() -> str:
    """
    获取当前时间的显示格式
    
    Returns:
        当前时间的格式化字符串
    """
    try:
        target_tz = get_configured_timezone()
        current_time = datetime.now(target_tz)
        return current_time.strftime("%Y年%m月%d日 %H:%M")
    except Exception as e:
        logger.error(f"获取当前时间失败: {e}")
        return datetime.now().strftime("%Y年%m月%d日 %H:%M")
