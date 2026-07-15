#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
测试通用URL下载器基本功能
"""

import os
import socket
import sys
import json
import requests
from unittest.mock import patch

# 添加项目根目录到Python路径

from video_transcript_api.downloaders import create_downloader

# 除带识别扩展名的 mp3/mp4 等 URL（GenericDownloader._is_media_url 靠路径扩展名
# 直接判定，不发请求）外，"https://www.example.com/" 没有扩展名，会触发一次真实
# HEAD 探测（GenericDownloader._safe_request -> 钉定 IP 的 HTTPAdapter.send）。
# 这里 mock 掉这一次网络 I/O，避免测试依赖外部网络可用性；做法沿用
# tests/unit/test_generic_downloader_retry.py 中记录的既有约定：
# 1) 伪造 socket.getaddrinfo 返回一个公网 IP，绕过 SSRF 校验阶段的真实 DNS 解析；
# 2) 在最终发请求的适配器层（requests.adapters.HTTPAdapter.send）返回一个
#    Content-Type 为 text/html 的假响应，还原"非媒体文件"的预期路径。
GETADDRINFO_PATH = "video_transcript_api.utils.url_validator.socket.getaddrinfo"
BASE_SEND_PATH = "requests.adapters.HTTPAdapter.send"


def _public_addrinfo(*args, **kwargs):
    """伪造 socket.getaddrinfo，返回一个可通过 SSRF 校验的公网 IPv4 地址"""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


def _fake_head_response():
    """伪造 HEAD 响应：非重定向 + Content-Type 为网页而非媒体文件。

    用真正的 requests.Response（而非裸 class）构造，让 Session.send() 后续
    访问的 .history/.raw/.content 等属性行为正确（is_redirect 是 Response
    的真实 property，靠 status_code + Location 头判定，不需要手动伪造）；
    _content 显式置空字节串，跳过 Response.content 对 .raw 的真实读取。
    """
    resp = requests.Response()
    resp.status_code = 200
    resp.headers = requests.structures.CaseInsensitiveDict(
        {"Content-Type": "text/html; charset=utf-8"}
    )
    resp._content = b""
    return resp


def _run_with_mocked_network():
    """mock 掉网络 I/O 后跑一遍用例，返回是否全部通过（bool）。

    供 test_generic_downloader()（assert 结果供 pytest 判定）和 __main__
    脚本入口（需要一个 bool 来算 sys.exit 退出码）共用，避免两处重复
    with patch(...) 语境。
    """
    with patch(GETADDRINFO_PATH, side_effect=_public_addrinfo), patch(
        BASE_SEND_PATH, return_value=_fake_head_response()
    ):
        return _run_generic_downloader_cases()


def test_generic_downloader():
    """测试通用URL下载器"""
    assert _run_with_mocked_network()


def _run_generic_downloader_cases():
    """实际执行下载器分发与信息获取断言（拆出来是为了让上面的 with 语境包住整个流程）"""

    print("测试通用URL下载器功能\n")

    # 测试URL列表
    test_cases = [
        {
            "url": "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3",
            "expected_downloader": "GenericDownloader",
            "should_succeed": True
        },
        {
            "url": "https://example.com/audio.mp3",
            "expected_downloader": "GenericDownloader", 
            "should_succeed": True
        },
        {
            "url": "https://example.com/video.mp4",
            "expected_downloader": "GenericDownloader",
            "should_succeed": True
        },
        {
            "url": "https://www.example.com/",
            "expected_downloader": "GenericDownloader",
            "should_succeed": False  # 不是媒体文件
        },
        {
            "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "expected_downloader": "YoutubeDownloader",
            "should_succeed": True
        },
    ]
    
    passed = 0
    failed = 0
    
    for i, test_case in enumerate(test_cases, 1):
        url = test_case["url"]
        expected_downloader = test_case["expected_downloader"]
        should_succeed = test_case["should_succeed"]
        
        print(f"测试 {i}: {url}")
        print("-" * 60)
        
        try:
            # 创建下载器
            downloader = create_downloader(url)
            actual_downloader = downloader.__class__.__name__
            
            # 检查下载器类型
            if actual_downloader != expected_downloader:
                print(f"[失败] 期望下载器: {expected_downloader}, 实际: {actual_downloader}")
                failed += 1
                continue
            
            print(f"[通过] 正确使用下载器: {actual_downloader}")
            
            # 如果是通用下载器，测试获取视频信息
            if actual_downloader == "GenericDownloader":
                try:
                    video_info = downloader.get_video_info(url)
                    
                    # 验证返回的信息
                    assert video_info.get("is_generic") == True
                    assert video_info.get("video_title") == ""
                    assert video_info.get("platform") == "generic"
                    
                    if should_succeed:
                        print(f"[通过] 成功获取视频信息")
                        print(f"  - 文件名: {video_info.get('filename')}")
                        print(f"  - 平台: {video_info.get('platform')}")
                        print(f"  - is_generic: {video_info.get('is_generic')}")
                        passed += 1
                    else:
                        print(f"[失败] 预期应该失败但成功了")
                        failed += 1
                        
                except Exception as e:
                    if not should_succeed:
                        print(f"[通过] 预期失败: {str(e)}")
                        passed += 1
                    else:
                        print(f"[失败] 获取视频信息失败: {str(e)}")
                        failed += 1
            else:
                passed += 1
                
        except Exception as e:
            print(f"[失败] 异常: {str(e)}")
            failed += 1
        
        print()
    
    # 总结
    print("=" * 60)
    print(f"测试完成: {passed} 通过, {failed} 失败")
    
    return failed == 0


if __name__ == "__main__":
    success = _run_with_mocked_network()
    sys.exit(0 if success else 1)