#!/usr/bin/env python3
"""测试缓存文件读取修复"""

import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(project_root, "src"))

from video_transcript_api.utils.logging import setup_logger

logger = setup_logger("test_cache_fix")

def test_cache_file_detection():
    """测试缓存文件检测逻辑"""
    output_dir = "./output"
    platform = "bilibili"
    video_id = "BV1JXt3z9EUX"
    
    if os.path.exists(output_dir):
        json_files = []
        txt_files = []
        metadata_files = []
        
        for file in os.listdir(output_dir):
            # 检查两种命名模式
            if f"_{platform}_{video_id}" in file or f"{platform}_{video_id}" in file:
                logger.info(f"找到文件: {file}")
                # 排除元数据文件
                if file.endswith(".metadata.json"):
                    metadata_files.append(os.path.join(output_dir, file))
                    logger.info(f"  -> 识别为元数据文件")
                elif file.endswith(".json"):
                    json_files.append(os.path.join(output_dir, file))
                    logger.info(f"  -> 识别为JSON转录文件")
                elif file.endswith(".txt"):
                    txt_files.append(os.path.join(output_dir, file))
                    logger.info(f"  -> 识别为TXT转录文件")
        
        logger.info(f"\n统计结果:")
        logger.info(f"元数据文件: {len(metadata_files)} 个")
        for f in metadata_files:
            logger.info(f"  - {os.path.basename(f)}")
        
        logger.info(f"JSON转录文件: {len(json_files)} 个")
        for f in json_files:
            logger.info(f"  - {os.path.basename(f)}")
            
        logger.info(f"TXT转录文件: {len(txt_files)} 个")
        for f in txt_files:
            logger.info(f"  - {os.path.basename(f)}")
        
        # 选择转录文件
        existing_files = json_files + txt_files
        logger.info(f"\n可用的转录文件: {len(existing_files)} 个")
        
        if existing_files:
            latest_file = max(existing_files, key=os.path.getmtime)
            logger.info(f"选择最新的转录文件: {os.path.basename(latest_file)}")
        else:
            logger.warning("未找到任何转录文件")

if __name__ == "__main__":
    test_cache_file_detection()
