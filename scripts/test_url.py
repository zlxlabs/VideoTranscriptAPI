#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import sys
import argparse
import json
from typing import List, Dict, Any

# 添加项目根目录到导入路径
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from utils import setup_logger
from downloaders import create_downloader
from transcriber import Transcriber

# 创建日志记录器
logger = setup_logger("test_url")

def test_single_url(url: str) -> Dict[str, Any]:
    """
    测试单个URL的转录效果
    
    参数:
        url: 视频URL
        
    返回:
        dict: 包含转录结果的字典
    """
    logger.info(f"开始测试URL: {url}")
    
    try:
        # 创建下载器
        downloader = create_downloader(url)
        if not downloader:
            error_msg = f"不支持的URL类型: {url}"
            logger.error(error_msg)
            return {"status": "failed", "message": error_msg}
        
        # 获取视频信息
        logger.info(f"获取视频信息: {url}")
        video_info = downloader.get_video_info(url)
        
        # 尝试获取字幕
        logger.info(f"尝试获取字幕: {url}")
        subtitle = downloader.get_subtitle(url)
        
        if subtitle:
            # 如果有字幕，直接使用
            logger.info(f"使用平台提供的字幕: {url}")
            
            # 生成输出文件名
            output_dir = "./output"
            os.makedirs(output_dir, exist_ok=True)
            subtitle_filename = f"{video_info.get('platform')}_{video_info.get('video_id')}.txt"
            subtitle_path = os.path.join(output_dir, subtitle_filename)
            
            # 保存字幕文件
            with open(subtitle_path, "w", encoding="utf-8") as f:
                f.write(subtitle)
            
            result = {
                "status": "success",
                "message": "使用平台字幕成功",
                "data": {
                    "video_title": video_info.get("video_title", ""),
                    "author": video_info.get("author", ""),
                    "transcript": subtitle,
                    "subtitle_path": subtitle_path
                }
            }
        else:
            # 没有字幕，需要下载音视频并转录
            logger.info(f"下载视频进行转录: {url}")
            
            # 下载视频
            download_url = video_info.get("download_url")
            filename = video_info.get("filename")
            
            if not download_url or not filename:
                error_msg = f"无法获取下载信息: {url}"
                logger.error(error_msg)
                return {"status": "failed", "message": error_msg}
            
            # 下载文件
            local_file = downloader.download_file(download_url, filename)
            if not local_file:
                error_msg = f"下载文件失败: {url}"
                logger.error(error_msg)
                return {"status": "failed", "message": error_msg}
            
            try:
                # 开始转录
                logger.info(f"开始转录音视频: {local_file}")
                
                # 生成转录文件名
                output_base = f"{video_info.get('platform')}_{video_info.get('video_id')}"
                
                # 创建转录器并转录
                transcriber = Transcriber()
                transcription_result = transcriber.transcribe(local_file, output_base)
                
                # 返回结果
                result = {
                    "status": "success",
                    "message": "转录成功",
                    "data": {
                        "video_title": video_info.get("video_title", ""),
                        "author": video_info.get("author", ""),
                        "transcript": transcription_result.get("transcript", ""),
                        "srt_path": transcription_result.get("srt_path", ""),
                        "lrc_path": transcription_result.get("lrc_path", ""),
                        "json_path": transcription_result.get("json_path", "")
                    }
                }
            finally:
                # 清理下载的文件
                logger.info(f"清理下载的文件: {local_file}")
                downloader.clean_up(local_file)
        
        return result
    except Exception as e:
        logger.exception(f"转录处理异常: {str(e)}")
        return {
            "status": "failed",
            "message": f"转录任务异常: {str(e)}",
            "error": str(e)
        }

def test_url_list(url_list_file: str, output_file: str = None) -> List[Dict[str, Any]]:
    """
    测试URL列表文件中的所有URL
    
    参数:
        url_list_file: URL列表文件路径，每行一个URL
        output_file: 输出结果的JSON文件路径，如果为None则不输出文件
        
    返回:
        list: 包含所有URL测试结果的列表
    """
    logger.info(f"开始测试URL列表: {url_list_file}")
    
    # 读取URL列表
    with open(url_list_file, "r", encoding="utf-8") as f:
        urls = [line.strip() for line in f if line.strip()]
    
    logger.info(f"共读取到 {len(urls)} 个URL")
    
    # 测试每个URL
    results = []
    for i, url in enumerate(urls, 1):
        logger.info(f"测试进度: {i}/{len(urls)}")
        result = test_single_url(url)
        result["url"] = url
        results.append(result)
    
    # 输出结果到文件
    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        logger.info(f"测试结果已保存至: {output_file}")
    
    return results

def test_audio_file(file_path: str) -> Dict[str, Any]:
    """
    测试单个音频文件的转录效果
    
    参数:
        file_path: 音频文件路径
        
    返回:
        dict: 包含转录结果的字典
    """
    logger.info(f"开始测试音频文件: {file_path}")
    
    try:
        # 检查文件是否存在
        if not os.path.exists(file_path):
            error_msg = f"文件不存在: {file_path}"
            logger.error(error_msg)
            return {"status": "failed", "message": error_msg}
        
        # 获取文件名（不包含扩展名）
        file_base = os.path.splitext(os.path.basename(file_path))[0]
        
        # 创建转录器并转录
        transcriber = Transcriber()
        transcription_result = transcriber.transcribe(file_path, file_base)
        
        # 返回结果
        result = {
            "status": "success",
            "message": "转录成功",
            "data": {
                "transcript": transcription_result.get("transcript", ""),
                "srt_path": transcription_result.get("srt_path", ""),
                "lrc_path": transcription_result.get("lrc_path", ""),
                "json_path": transcription_result.get("json_path", "")
            }
        }
        
        return result
    except Exception as e:
        logger.exception(f"转录处理异常: {str(e)}")
        return {
            "status": "failed",
            "message": f"转录任务异常: {str(e)}",
            "error": str(e)
        }

def main():
    """命令行入口函数"""
    parser = argparse.ArgumentParser(description="测试视频转录效果")
    
    # 创建子命令解析器
    subparsers = parser.add_subparsers(dest="command", help="子命令")
    
    # URL测试子命令
    url_parser = subparsers.add_parser("url", help="测试单个URL")
    url_parser.add_argument("url", help="要测试的视频URL")
    
    # URL列表测试子命令
    url_list_parser = subparsers.add_parser("url_list", help="测试URL列表")
    url_list_parser.add_argument("url_list_file", help="URL列表文件路径，每行一个URL")
    url_list_parser.add_argument("-o", "--output", help="输出结果的JSON文件路径")
    
    # 音频文件测试子命令
    audio_parser = subparsers.add_parser("audio", help="测试音频文件")
    audio_parser.add_argument("file_path", help="音频文件路径")
    
    # 解析命令行参数
    args = parser.parse_args()
    
    if args.command == "url":
        result = test_single_url(args.url)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "url_list":
        test_url_list(args.url_list_file, args.output)
    elif args.command == "audio":
        result = test_audio_file(args.file_path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        parser.print_help()

if __name__ == "__main__":
    main() 