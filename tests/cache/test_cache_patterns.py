#!/usr/bin/env python3
"""测试各种缓存文件命名模式"""

import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(project_root, "src"))

from video_transcript_api.utils.logging import setup_logger

logger = setup_logger("test_cache_patterns")

def test_cache_patterns():
    """测试不同的缓存文件命名模式"""
    
    test_cases = [
        {
            "platform": "bilibili",
            "video_id": "BV1JXt3z9EUX",
            "files": [
                "250804-202648_bilibili_BV1JXt3z9EUX_盘点一下我每天都补的啥.metadata.json",
                "bilibili_BV1JXt3z9EUX_1754310407.txt"
            ]
        },
        {
            "platform": "xiaohongshu", 
            "video_id": "6730aa31000000001b029679",
            "files": [
                "xiaohongshu_6730aa31000000001b029679_1754071613.txt",
                "250805-120000_xiaohongshu_6730aa31000000001b029679_测试标题.json"
            ]
        }
    ]
    
    for case in test_cases:
        platform = case["platform"]
        video_id = case["video_id"]
        files = case["files"]
        
        logger.info(f"\n测试平台: {platform}, 视频ID: {video_id}")
        logger.info(f"测试文件列表: {files}")
        
        json_files = []
        txt_files = []
        metadata_files = []
        
        for file in files:
            # 检查两种命名模式：带时间戳前缀的和不带的
            if f"_{platform}_{video_id}" in file or f"{platform}_{video_id}" in file:
                logger.info(f"  ✓ 匹配到文件: {file}")
                
                # 排除元数据文件
                if file.endswith(".metadata.json"):
                    metadata_files.append(file)
                    logger.info(f"    -> 识别为元数据文件")
                elif file.endswith(".json"):
                    json_files.append(file)
                    logger.info(f"    -> 识别为JSON转录文件")
                elif file.endswith(".txt"):
                    txt_files.append(file)
                    logger.info(f"    -> 识别为TXT转录文件")
            else:
                logger.warning(f"  ✗ 未匹配到文件: {file}")
        
        logger.info(f"  结果统计:")
        logger.info(f"    - 元数据文件: {len(metadata_files)} 个")
        logger.info(f"    - JSON转录文件: {len(json_files)} 个")
        logger.info(f"    - TXT转录文件: {len(txt_files)} 个")

if __name__ == "__main__":
    test_cache_patterns()
