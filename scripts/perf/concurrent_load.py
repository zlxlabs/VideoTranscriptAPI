#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Manual concurrent load test. Run: python scripts/perf/concurrent_load.py
"""
并发测试脚本
用于验证视频转录API的并发处理能力
"""

import asyncio
import aiohttp
import time
import json
from datetime import datetime

# 配置
API_BASE_URL = "http://localhost:8000"
AUTH_TOKEN = "x"  # 请替换为实际的token

# 测试视频URL列表
TEST_URLS = [
    "https://www.douyin.com/video/7508714157055806783",  # 抖音视频1
    "https://b23.tv/5SpLz72",                            # B站视频1
    # 可以添加更多测试URL
]

async def submit_task(session, url, task_name):
    """提交转录任务"""
    headers = {
        "Authorization": f"Bearer {AUTH_TOKEN}",
        "Content-Type": "application/json"
    }
    
    data = {"url": url}
    
    try:
        start_time = time.time()
        async with session.post(f"{API_BASE_URL}/api/transcribe", 
                               headers=headers, 
                               json=data) as response:
            result = await response.json()
            submit_time = time.time() - start_time
            
            if response.status == 202:
                task_id = result.get("data", {}).get("task_id")
                print(f"✅ {task_name} 提交成功: {task_id} (耗时: {submit_time:.2f}s)")
                return task_id
            else:
                print(f"❌ {task_name} 提交失败: {result}")
                return None
                
    except Exception as e:
        print(f"❌ {task_name} 提交异常: {str(e)}")
        return None

async def check_task_status(session, task_id, task_name):
    """检查任务状态"""
    headers = {
        "Authorization": f"Bearer {AUTH_TOKEN}",
    }
    
    try:
        async with session.get(f"{API_BASE_URL}/api/task/{task_id}", 
                              headers=headers) as response:
            result = await response.json()
            status = result.get("data", {}).get("status", "unknown")
            message = result.get("message", "")
            
            return status, message
            
    except Exception as e:
        print(f"❌ {task_name} 状态检查异常: {str(e)}")
        return "error", str(e)

async def monitor_task(session, task_id, task_name):
    """监控任务进度"""
    print(f"🔍 开始监控 {task_name} (ID: {task_id})")
    
    start_time = time.time()
    last_status = None
    
    while True:
        status, message = await check_task_status(session, task_id, task_name)
        
        if status != last_status:
            elapsed = time.time() - start_time
            print(f"📊 {task_name}: {status} - {message} (已用时: {elapsed:.1f}s)")
            last_status = status
        
        if status in ["success", "failed", "error"]:
            total_time = time.time() - start_time
            if status == "success":
                print(f"✅ {task_name} 完成! 总耗时: {total_time:.1f}s")
            else:
                print(f"❌ {task_name} 失败! 总耗时: {total_time:.1f}s")
            break
            
        await asyncio.sleep(2)  # 每2秒检查一次

async def test_concurrent_processing():
    """测试并发处理"""
    print("🚀 开始并发测试")
    print(f"📅 测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"🎯 测试URL数量: {len(TEST_URLS)}")
    print("-" * 60)
    
    async with aiohttp.ClientSession() as session:
        # 1. 并发提交所有任务
        print("📤 并发提交任务...")
        submit_start = time.time()
        
        submit_tasks = []
        for i, url in enumerate(TEST_URLS):
            task_name = f"Task-{i+1}"
            submit_tasks.append(submit_task(session, url, task_name))
        
        # 等待所有任务提交完成
        task_ids = await asyncio.gather(*submit_tasks)
        submit_total_time = time.time() - submit_start
        
        print(f"📤 所有任务提交完成，总耗时: {submit_total_time:.2f}s")
        print("-" * 60)
        
        # 2. 并发监控所有任务
        print("🔍 开始并发监控...")
        monitor_tasks = []
        
        for i, task_id in enumerate(task_ids):
            if task_id:
                task_name = f"Task-{i+1}"
                monitor_tasks.append(monitor_task(session, task_id, task_name))
        
        # 等待所有任务完成
        if monitor_tasks:
            await asyncio.gather(*monitor_tasks)
        
        print("-" * 60)
        print("🎉 并发测试完成!")

async def test_sequential_vs_concurrent():
    """对比串行和并发的性能差异"""
    print("⚡ 性能对比测试")
    print("=" * 60)
    
    # 注意：这里只是模拟提交时间的对比
    # 实际的处理时间取决于视频长度和服务器性能
    
    async with aiohttp.ClientSession() as session:
        # 串行提交测试
        print("🐌 串行提交测试...")
        serial_start = time.time()
        
        for i, url in enumerate(TEST_URLS[:2]):  # 只测试前2个
            task_name = f"Serial-{i+1}"
            await submit_task(session, url, task_name)
            
        serial_time = time.time() - serial_start
        print(f"🐌 串行提交总耗时: {serial_time:.2f}s")
        
        await asyncio.sleep(2)  # 等待一下
        
        # 并发提交测试
        print("\n🚀 并发提交测试...")
        concurrent_start = time.time()
        
        concurrent_tasks = []
        for i, url in enumerate(TEST_URLS[:2]):  # 只测试前2个
            task_name = f"Concurrent-{i+1}"
            concurrent_tasks.append(submit_task(session, url, task_name))
            
        await asyncio.gather(*concurrent_tasks)
        concurrent_time = time.time() - concurrent_start
        
        print(f"🚀 并发提交总耗时: {concurrent_time:.2f}s")
        
        # 性能提升计算
        if concurrent_time > 0:
            speedup = serial_time / concurrent_time
            print(f"⚡ 性能提升: {speedup:.2f}x")

def main():
    """主函数"""
    print("🎬 视频转录API并发测试工具")
    print("=" * 60)
    
    # 检查配置
    if AUTH_TOKEN == "your-auth-token-here":
        print("⚠️  请先在脚本中配置正确的AUTH_TOKEN")
        return
    
    try:
        # 运行并发测试
        asyncio.run(test_concurrent_processing())
        
    except KeyboardInterrupt:
        print("\n⏹️  测试被用户中断")
    except Exception as e:
        print(f"❌ 测试异常: {str(e)}")

if __name__ == "__main__":
    main() 