#!/usr/bin/env python3
"""测试元数据查找逻辑"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import MetadataCache, setup_logger

logger = setup_logger("test_metadata_lookup")

def test_metadata_lookup():
    """测试元数据查找功能"""
    
    metadata_cache = MetadataCache()
    
    test_cases = [
        {
            "cached_file": "./output/bilibili_BV1JXt3z9EUX_1754310407.txt",
            "expected_metadata_file": "250804-202648_bilibili_BV1JXt3z9EUX_盘点一下我每天都补的啥.metadata.json",
            "platform": "bilibili",
            "video_id": "BV1JXt3z9EUX"
        },
        {
            "cached_file": "./output/xiaohongshu_6730aa31000000001b029679_1754071613.txt",
            "expected_metadata_file": None,  # 假设没有对应的元数据文件
            "platform": "xiaohongshu",
            "video_id": "6730aa31000000001b029679"
        }
    ]
    
    for case in test_cases:
        cached_file = case["cached_file"]
        logger.info(f"\n测试查找元数据: {cached_file}")
        
        metadata = metadata_cache.find_metadata_for_cached_file(cached_file)
        
        if metadata:
            logger.info(f"✓ 找到元数据:")
            logger.info(f"  - 标题: {metadata.get('video_title', '')}")
            logger.info(f"  - 作者: {metadata.get('author', '')}")
            logger.info(f"  - 平台: {metadata.get('platform', '')}")
            logger.info(f"  - 视频ID: {metadata.get('video_id', '')}")
        else:
            logger.warning(f"✗ 未找到元数据")

if __name__ == "__main__":
    test_metadata_lookup()