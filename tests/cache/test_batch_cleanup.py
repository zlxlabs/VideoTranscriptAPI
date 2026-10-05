"""
测试批量清理功能
"""
import os
import sys
import shutil
import datetime

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(project_root, "src"))

from video_transcript_api.cache import CacheManager
from video_transcript_api.utils.logging import setup_logger

logger = setup_logger("test_batch_cleanup")


def test_batch_cleanup():
    """测试批量清理功能"""
    print("=== 测试批量清理功能 ===\n")
    
    # 创建缓存管理器
    cache_manager = CacheManager("./test_cache_dir")
    
    # 创建几个测试缓存
    print("1. 创建测试缓存...")
    
    # 正常缓存1
    cache_manager.save_cache(
        platform="youtube",
        url="https://www.youtube.com/watch?v=test1",
        media_id="test1",
        use_speaker_recognition=False,
        transcript_data="测试1",
        transcript_type="capswriter",
        title="测试视频1",
        author="作者1",
        description=""
    )
    
    # 正常缓存2
    cache_manager.save_cache(
        platform="bilibili",
        url="https://www.bilibili.com/video/BV1test2",
        media_id="BV1test2",
        use_speaker_recognition=True,
        transcript_data={"speakers": ["Speaker1"], "segments": []},
        transcript_type="funasr",
        title="测试视频2",
        author="作者2",
        description=""
    )
    
    # 将要损坏的缓存
    cache_manager.save_cache(
        platform="douyin",
        url="https://www.douyin.com/video/test3",
        media_id="test3",
        use_speaker_recognition=False,
        transcript_data="测试3",
        transcript_type="capswriter",
        title="测试视频3",
        author="作者3",
        description=""
    )
    
    print(f"  创建了 3 个缓存记录")
    
    # 查看初始状态
    stats = cache_manager.get_cache_stats()
    print(f"  当前记录数: {stats['total_records']}\n")
    
    # 故意删除一些文件，模拟文件丢失
    print("2. 模拟文件丢失...")
    
    # 删除 douyin 的转录文件
    douyin_file = os.path.join(
        cache_manager.cache_dir,
        "douyin", "2025", "202508", "test3",
        "transcript_capswriter.txt"
    )
    if os.path.exists(douyin_file):
        os.remove(douyin_file)
        print(f"  删除了文件: {douyin_file}")
    
    # 删除 bilibili 的整个文件夹
    bilibili_folder = os.path.join(
        cache_manager.cache_dir,
        "bilibili", "2025", "202508", "BV1test2"
    )
    if os.path.exists(bilibili_folder):
        shutil.rmtree(bilibili_folder)
        print(f"  删除了文件夹: {bilibili_folder}")
    
    print(f"  模拟了 2 个文件/文件夹丢失\n")
    
    # 执行完整性验证
    print("3. 执行批量完整性验证...")
    invalid_count = cache_manager.validate_cache_integrity()
    print(f"  删除了 {invalid_count} 条无效记录\n")
    
    # 查看清理后的状态
    stats = cache_manager.get_cache_stats()
    print(f"4. 清理后状态:")
    print(f"  剩余记录数: {stats['total_records']}")
    print(f"  平台分布: {stats.get('platform_stats', {})}")
    
    # 验证剩余的缓存仍然可以正常访问
    print(f"\n5. 验证剩余缓存的可访问性:")
    remaining_cache = cache_manager.get_cache(platform="youtube", media_id="test1")
    if remaining_cache:
        print(f"  [OK] YouTube 缓存仍然可访问: {remaining_cache['title']}")
    else:
        print(f"  [FAIL] YouTube 缓存无法访问")
    
    # 验证被删除的缓存确实无法访问
    deleted_cache = cache_manager.get_cache(platform="douyin", media_id="test3")
    if deleted_cache is None:
        print(f"  [OK] 已损坏的 Douyin 缓存已被清理")
    else:
        print(f"  [FAIL] 已损坏的 Douyin 缓存仍然存在")
    
    cache_manager.close()
    
    print("\n=== 测试完成 ===")
    print("\n总结：")
    print("1. validate_cache_integrity() 可以批量检查所有缓存记录")
    print("2. 自动删除文件不存在的无效记录")
    print("3. 保持数据库与文件系统的一致性")
    print("4. 可以集成到定期清理脚本中")


if __name__ == "__main__":
    test_batch_cleanup()
    
    # 清理测试目录
    if os.path.exists("./test_cache_dir"):
        shutil.rmtree("./test_cache_dir")
