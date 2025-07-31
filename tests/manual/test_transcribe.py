#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
音视频转录测试脚本
用于测试Client_Only转录功能
"""

import os
import sys
import argparse
import time
from pathlib import Path

# 添加项目根目录到系统路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from transcriber import Transcriber
from utils import setup_logger, load_config, wechat_notify

# 创建日志记录器
logger = setup_logger("test_transcribe")

def main():
    """主函数"""
    parser = argparse.ArgumentParser(description="音视频转录测试工具")
    parser.add_argument("file_path", help="音视频文件路径")
    parser.add_argument("--notify", action="store_true", help="是否发送企业微信通知")
    args = parser.parse_args()
    
    file_path = args.file_path
    if not os.path.exists(file_path):
        logger.error(f"文件不存在: {file_path}")
        return 1
    
    # 加载配置
    config = load_config()
    
    # 创建转录器
    try:
        logger.info(f"开始转录文件: {file_path}")
        
        if args.notify:
            wechat_notify(f"开始转录文件: {os.path.basename(file_path)}", config=config)
        
        start_time = time.time()
        transcriber = Transcriber(config)
        
        # 执行转录
        result = transcriber.transcribe(file_path)
        
        # 计算耗时
        elapsed_time = time.time() - start_time
        minutes, seconds = divmod(elapsed_time, 60)
        
        # 输出结果
        logger.info(f"转录完成，耗时: {int(minutes)}分{seconds:.2f}秒")
        
        # 显示文件路径
        if "merge_txt_path" in result and result["merge_txt_path"]:
            logger.info(f"合并文本文件: {result['merge_txt_path']}")
        
        # 显示文本预览
        if "transcript" in result and result["transcript"]:
            preview = result["transcript"]
            if len(preview) > 100:
                preview = preview[:100] + "..."
            logger.info(f"文本预览: {preview}")
        
        # 发送通知
        if args.notify:
            notification_message = (
                f"转录完成: {os.path.basename(file_path)}\n"
                f"耗时: {int(minutes)}分{seconds:.2f}秒\n"
            )
            
            if "transcript" in result and result["transcript"]:
                text_preview = result["transcript"][:50] + "..." if len(result["transcript"]) > 50 else result["transcript"]
                notification_message += f"预览: {text_preview}"
                
            wechat_notify(notification_message, config=config)
        
        return 0
        
    except Exception as e:
        logger.exception(f"转录失败: {str(e)}")
        
        if args.notify:
            wechat_notify(f"转录失败: {os.path.basename(file_path)}\n"
                          f"错误: {str(e)}", config=config)
        
        return 1

if __name__ == "__main__":
    # 确保工作目录是项目根目录
    os.chdir(os.path.join(os.path.dirname(__file__), '../..'))
    sys.exit(main()) 