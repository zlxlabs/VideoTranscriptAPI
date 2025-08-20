#!/usr/bin/env python
"""
缓存清理脚本
可以手动运行或通过计划任务定期执行
"""
import os
import sys
from utils import setup_logger, load_config, CacheManager

logger = setup_logger("cache_cleanup")


def main():
    """主函数"""
    # 加载配置
    config = load_config()
    
    # 获取缓存配置
    cache_dir = config.get("storage", {}).get("cache_dir", "./cache_dir")
    retention_days = config.get("storage", {}).get("cache_retention_days", 180)  # 默认6个月
    
    logger.info(f"开始清理缓存，保留最近 {retention_days} 天的数据")
    logger.info(f"缓存目录: {cache_dir}")
    
    # 创建缓存管理器
    cache_manager = CacheManager(cache_dir)
    
    try:
        # 获取清理前的统计信息
        stats_before = cache_manager.get_cache_stats()
        logger.info(f"清理前 - 总记录数: {stats_before.get('total_records', 0)}, "
                   f"缓存大小: {stats_before.get('cache_size_mb', 0)} MB")
        
        # 首先验证缓存完整性，删除无效记录
        logger.info("验证缓存完整性...")
        invalid_count = cache_manager.validate_cache_integrity()
        if invalid_count > 0:
            logger.info(f"删除了 {invalid_count} 条无效缓存记录")
        else:
            logger.info("所有缓存记录都有效")
        
        # 执行时间清理
        logger.info(f"清理 {retention_days} 天前的旧缓存...")
        deleted_count = cache_manager.cleanup_old_cache(days=retention_days)
        
        # 获取清理后的统计信息
        stats_after = cache_manager.get_cache_stats()
        logger.info(f"清理后 - 总记录数: {stats_after.get('total_records', 0)}, "
                   f"缓存大小: {stats_after.get('cache_size_mb', 0)} MB")
        
        # 计算清理效果
        records_cleaned = stats_before.get('total_records', 0) - stats_after.get('total_records', 0)
        space_freed = stats_before.get('cache_size_mb', 0) - stats_after.get('cache_size_mb', 0)
        
        logger.info(f"清理完成: 删除 {deleted_count} 条记录，释放 {space_freed:.2f} MB 空间")
        
        # 显示详细的平台统计
        logger.info("各平台缓存分布:")
        for platform, count in stats_after.get('platform_stats', {}).items():
            logger.info(f"  - {platform}: {count} 条")
            
    except Exception as e:
        logger.error(f"清理缓存时发生错误: {e}")
        return 1
    finally:
        # 关闭数据库连接
        cache_manager.close()
    
    return 0


if __name__ == "__main__":
    sys.exit(main())