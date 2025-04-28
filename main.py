#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import sys
import argparse
from api import start_server

def main():
    """主程序入口函数"""
    parser = argparse.ArgumentParser(description="视频转录API服务")
    
    # 添加命令行参数
    parser.add_argument("--start", action="store_true", help="启动API服务")
    
    # 解析命令行参数
    args = parser.parse_args()
    
    if args.start:
        # 启动API服务
        start_server()
    else:
        # 显示帮助信息
        parser.print_help()

if __name__ == "__main__":
    # 确保工作目录是项目根目录
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    main() 