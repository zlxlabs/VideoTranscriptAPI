"""
测试 LLM 缓存逻辑
验证当缓存中有 LLM 结果时，不会重新请求 LLM
"""
import os
import sys
import time

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(project_root, "src"))

from video_transcript_api.cache import CacheManager
from video_transcript_api.utils.logging import setup_logger

logger = setup_logger("test_llm_cache")


def test_llm_cache_logic():
    """测试 LLM 缓存逻辑"""
    print("=== 测试 LLM 缓存逻辑 ===\n")
    
    # 创建缓存管理器
    cache_manager = CacheManager("./test_cache_dir")
    
    # 模拟场景1：首次请求，没有任何缓存
    print("1. 模拟首次请求（无缓存）...")
    
    # 保存转录结果
    cache_result = cache_manager.save_cache(
        platform="bilibili",
        url="https://www.bilibili.com/video/BV1test123",
        media_id="BV1test123",
        use_speaker_recognition=True,
        transcript_data={
            "speakers": ["Speaker1", "Speaker2"],
            "segments": [
                {"speaker": "Speaker1", "text": "这是测试对话内容", "start": 0, "end": 2}
            ]
        },
        transcript_type="funasr",
        title="测试视频 - LLM缓存",
        author="测试UP主",
        description="测试描述"
    )
    
    # 查询缓存（此时没有 LLM 结果）
    cache_data = cache_manager.get_cache(platform="bilibili", media_id="BV1test123")
    print(f"  - 转录缓存: {'存在' if cache_data else '不存在'}")
    print(f"  - LLM校对结果: {'存在' if cache_data and 'llm_calibrated' in cache_data else '不存在'}")
    print(f"  - LLM总结结果: {'存在' if cache_data and 'llm_summary' in cache_data else '不存在'}")
    print("  -> 预期行为：应该将任务加入 LLM 队列\n")
    
    # 模拟场景2：LLM 处理完成后，保存结果
    print("2. 模拟 LLM 处理完成...")
    time.sleep(0.5)  # 模拟处理时间
    
    # 保存 LLM 结果
    cache_manager.save_llm_result(
        platform="bilibili",
        media_id="BV1test123",
        use_speaker_recognition=True,
        llm_type="calibrated",
        content="这是经过 LLM 校对后的测试对话内容。"
    )
    
    cache_manager.save_llm_result(
        platform="bilibili",
        media_id="BV1test123",
        use_speaker_recognition=True,
        llm_type="summary",
        content="- 这是一个测试视频\n- 包含测试对话内容"
    )
    
    print("  - LLM 结果已保存\n")
    
    # 模拟场景3：再次请求同一个视频
    print("3. 模拟再次请求同一视频...")
    
    # 查询缓存（此时应该有完整的 LLM 结果）
    cache_data = cache_manager.get_cache(platform="bilibili", media_id="BV1test123")
    print(f"  - 转录缓存: {'存在' if cache_data else '不存在'}")
    print(f"  - LLM校对结果: {'存在' if cache_data and 'llm_calibrated' in cache_data else '不存在'}")
    print(f"  - LLM总结结果: {'存在' if cache_data and 'llm_summary' in cache_data else '不存在'}")
    
    if cache_data and 'llm_calibrated' in cache_data and 'llm_summary' in cache_data:
        print("  -> 预期行为：直接使用缓存的 LLM 结果，不应该重新请求 LLM")
        print(f"  - 校对文本预览: {cache_data['llm_calibrated'][:30]}...")
        print(f"  - 总结文本预览: {cache_data['llm_summary'][:30]}...")
    else:
        print("  -> [错误] 缓存中没有找到 LLM 结果")
    
    # 模拟场景4：不同的 use_speaker_recognition 参数
    print("\n4. 测试不同的 use_speaker_recognition 参数...")
    
    # 查询不需要说话人识别的缓存（应该能使用带说话人识别的缓存）
    cache_data = cache_manager.get_cache(
        platform="bilibili", 
        media_id="BV1test123",
        use_speaker_recognition=False
    )
    
    if cache_data:
        print(f"  - 查询 use_speaker_recognition=False 时，找到了缓存")
        print(f"  - 缓存的 use_speaker_recognition: {cache_data.get('use_speaker_recognition')}")
        print(f"  - LLM结果: {'存在' if 'llm_calibrated' in cache_data else '不存在'}")
        print("  -> 正确：可以使用带说话人识别的缓存（信息更丰富）")
    else:
        print("  - [错误] 没有找到缓存")
    
    # 清理测试数据
    cache_manager.close()
    
    print("\n=== 测试完成 ===")
    print("\n建议：")
    print("1. 当缓存中有完整的 LLM 结果时，应直接使用，避免重复请求")
    print("2. 这样可以大幅提升响应速度，减少 LLM API 调用成本")
    print("3. 缓存保留时间默认为 6 个月，可在 config.json 中调整")


if __name__ == "__main__":
    test_llm_cache_logic()
    
    # 清理测试目录
    import shutil
    if os.path.exists("./test_cache_dir"):
        shutil.rmtree("./test_cache_dir")
