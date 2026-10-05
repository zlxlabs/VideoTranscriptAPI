"""
测试新的缓存管理系统
"""
import os
import sys
import json

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(project_root, "src"))

from video_transcript_api.cache import CacheManager
from video_transcript_api.utils.logging import setup_logger

logger = setup_logger("test_cache")


def test_cache_manager():
    """测试缓存管理器的基本功能"""
    print("=== 测试缓存管理器 ===\n")
    
    # 创建缓存管理器
    cache_manager = CacheManager("./test_cache_dir")
    
    # 测试1：保存 CapsWriter 转录结果
    print("1. 测试保存 CapsWriter 转录结果...")
    cache_result = cache_manager.save_cache(
        platform="youtube",
        url="https://www.youtube.com/watch?v=test123",
        media_id="test123",
        use_speaker_recognition=False,
        transcript_data="这是一个测试转录文本。测试内容包含多个句子。",
        transcript_type="capswriter",
        title="测试视频",
        author="测试作者",
        description="这是一个测试视频的描述"
    )
    
    if cache_result:
        print(f"[[OK]] CapsWriter 缓存保存成功: {cache_result}")
    else:
        print("[[FAIL]] CapsWriter 缓存保存失败")
    
    # 测试2：保存 FunASR 转录结果
    print("\n2. 测试保存 FunASR 转录结果...")
    funasr_data = {
        "speakers": ["Speaker1", "Speaker2"],
        "segments": [
            {"speaker": "Speaker1", "text": "你好，这是说话人1", "start": 0, "end": 2},
            {"speaker": "Speaker2", "text": "你好，这是说话人2", "start": 2, "end": 4}
        ]
    }
    
    cache_result = cache_manager.save_cache(
        platform="bilibili",
        url="https://www.bilibili.com/video/BV1test",
        media_id="BV1test",
        use_speaker_recognition=True,
        transcript_data=funasr_data,
        transcript_type="funasr",
        title="B站测试视频",
        author="B站UP主",
        description="这是B站测试视频"
    )
    
    if cache_result:
        print(f"[OK] FunASR 缓存保存成功: {cache_result}")
    else:
        print("[FAIL] FunASR 缓存保存失败")
    
    # 测试3：查询缓存（通过 platform + media_id）
    print("\n3. 测试查询缓存（通过 platform + media_id）...")
    
    # 查询不需要说话人识别的缓存
    cache_data = cache_manager.get_cache(platform="youtube", media_id="test123", use_speaker_recognition=False)
    if cache_data:
        print(f"[OK] 查询成功（不需要说话人识别）:")
        print(f"  - 标题: {cache_data.get('title')}")
        print(f"  - 作者: {cache_data.get('author')}")
        print(f"  - 转录类型: {cache_data.get('transcript_type')}")
        print(f"  - 转录内容预览: {cache_data.get('transcript_data')[:50]}...")
    else:
        print("[FAIL] 查询失败（不需要说话人识别）")
    
    # 查询需要说话人识别的缓存
    cache_data = cache_manager.get_cache(platform="bilibili", media_id="BV1test", use_speaker_recognition=True)
    if cache_data:
        print(f"\n[OK] 查询成功（需要说话人识别）:")
        print(f"  - 标题: {cache_data.get('title')}")
        print(f"  - 作者: {cache_data.get('author')}")
        print(f"  - 转录类型: {cache_data.get('transcript_type')}")
        print(f"  - 说话人数量: {len(cache_data.get('transcript_data', {}).get('speakers', []))}")
    else:
        print("\n[FAIL] 查询失败（需要说话人识别）")
    
    # 测试4：查询缓存（通过 URL）
    print("\n4. 测试查询缓存（通过 URL）...")
    cache_data = cache_manager.get_cache(url="https://www.youtube.com/watch?v=test123")
    if cache_data:
        print(f"[OK] 通过 URL 查询成功: {cache_data.get('title')}")
    else:
        print("[FAIL] 通过 URL 查询失败")
    
    # 测试5：保存 LLM 结果
    print("\n5. 测试保存 LLM 结果...")
    
    # 保存校对文本
    success = cache_manager.save_llm_result(
        platform="youtube",
        media_id="test123",
        use_speaker_recognition=False,
        llm_type="calibrated",
        content="这是经过 LLM 校对后的文本。\n\n测试内容已经被整理和校对。"
    )
    print(f"  - 保存校对文本: {'[OK] 成功' if success else '[FAIL] 失败'}")
    
    # 保存总结文本
    success = cache_manager.save_llm_result(
        platform="youtube",
        media_id="test123",
        use_speaker_recognition=False,
        llm_type="summary",
        content="- 这是一个测试视频\n- 包含测试内容\n- 用于验证缓存系统"
    )
    print(f"  - 保存总结文本: {'[OK] 成功' if success else '[FAIL] 失败'}")
    
    # 测试6：再次查询，验证 LLM 结果
    print("\n6. 测试查询包含 LLM 结果的缓存...")
    cache_data = cache_manager.get_cache(platform="youtube", media_id="test123")
    if cache_data:
        print("[OK] 查询成功:")
        print(f"  - 包含校对文本: {'是' if 'llm_calibrated' in cache_data else '否'}")
        print(f"  - 包含总结文本: {'是' if 'llm_summary' in cache_data else '否'}")
        if 'llm_calibrated' in cache_data:
            print(f"  - 校对文本预览: {cache_data['llm_calibrated'][:50]}...")
        if 'llm_summary' in cache_data:
            print(f"  - 总结文本预览: {cache_data['llm_summary'][:50]}...")
    else:
        print("[FAIL] 查询失败")
    
    # 测试7：获取缓存统计
    print("\n7. 测试缓存统计...")
    stats = cache_manager.get_cache_stats()
    print(f"[OK] 缓存统计:")
    print(f"  - 总记录数: {stats.get('total_records', 0)}")
    print(f"  - 平台分布: {stats.get('platform_stats', {})}")
    print(f"  - 说话人识别分布: {stats.get('speaker_recognition_stats', {})}")
    print(f"  - 缓存大小: {stats.get('cache_size_mb', 0)} MB")
    
    # 测试8：列出缓存
    print("\n8. 测试列出缓存...")
    cache_list = cache_manager.list_cache(limit=10)
    print(f"[OK] 找到 {len(cache_list)} 条缓存记录")
    for idx, cache in enumerate(cache_list[:3]):  # 只显示前3条
        print(f"  {idx+1}. {cache.get('platform')}/{cache.get('media_id')} - {cache.get('title')}")
    
    # 关闭数据库连接
    cache_manager.close()
    
    print("\n=== 测试完成 ===")


if __name__ == "__main__":
    test_cache_manager()
