#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import sys
import argparse

# 添加src目录到Python路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

def main():
    """主程序入口函数"""
    parser = argparse.ArgumentParser(description="视频转录API服务")

    # 添加命令行参数
    parser.add_argument("--start", action="store_true", help="启动API服务")
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate configuration without starting services or creating runtime resources",
    )
    parser.add_argument("--config", help="Configuration file used by --check-config")

    # 解析命令行参数
    args = parser.parse_args()

    if args.check_config:
        from video_transcript_api.api.context import load_and_validate_config

        load_and_validate_config(args.config)
        print("Configuration OK")
    elif args.start:
        # 启动API服务
        if args.config:
            os.environ["VTAPI_CONFIG"] = args.config
        from video_transcript_api.api.server import start_server

        start_server()
    else:
        # 显示帮助信息
        parser.print_help()

if __name__ == "__main__":
    # 确保工作目录是项目根目录
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    main() 
