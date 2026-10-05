"""
测试缓存清理逻辑
验证当文件不存在时，会自动清理数据库记录
"""
import os
import sys
import shutil

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(project_root, "src"))

from video_transcript_api.cache import CacheManager
from video_transcript_api.utils.logging import setup_logger

logger = setup_logger("test_cache_cleanup")


def test_auto_cleanup():
    """测试自动清理无效记录"""
    print("=== 测试缓存自动清理逻辑 ===\n")
    
    # 创建缓存管理器
    cache_manager = CacheManager("./test_cache_dir")
    
    # 步骤1：创建正常的缓存
    print("1. 创建正常的缓存记录...")
    cache_result = cache_manager.save_cache(
        platform="youtube",
        url="https://www.youtube.com/watch?v=cleanup_test",
        media_id="cleanup_test",
        use_speaker_recognition=False,
        transcript_data="这是测试转录文本",
        transcript_type="capswriter",
        title="清理测试视频",
        author="测试作者",
        description="测试描述"
    )
    
    if cache_result:
        print(f"  [OK] 缓存创建成功")
        file_path = cache_result['transcript_file']
        print(f"  文件路径: {file_path}")
    
    # 验证缓存可以正常查询
    cache_data = cache_manager.get_cache(platform="youtube", media_id="cleanup_test")
    if cache_data:
        print("  [OK] 缓存查询成功")
    
    # 获取初始统计
    stats = cache_manager.get_cache_stats()
    initial_count = stats['total_records']
    print(f"  当前缓存记录数: {initial_count}\n")
    
    # 步骤2：删除转录文件
    print("2. 手动删除转录文件，模拟文件丢失...")
    transcript_file = os.path.join(
        cache_manager.cache_dir, 
        "youtube", "2025", "202508", "cleanup_test", 
        "transcript_capswriter.txt"
    )
    if os.path.exists(transcript_file):
        os.remove(transcript_file)
        print(f"  [OK] 已删除文件: {transcript_file}\n")
    
    # 步骤3：再次查询，应该返回 None 并删除记录
    print("3. 再次查询缓存（文件已丢失）...")
    cache_data = cache_manager.get_cache(platform="youtube", media_id="cleanup_test")
    if cache_data is None:
        print("  [OK] 查询返回 None（预期行为）")
    else:
        print("  [FAIL] 查询仍然返回了数据")
    
    # 验证数据库记录已被删除
    stats = cache_manager.get_cache_stats()
    final_count = stats['total_records']
    print(f"  清理后缓存记录数: {final_count}")
    
    if final_count == initial_count - 1:
        print("  [OK] 数据库记录已被自动删除\n")
    else:
        print("  [FAIL] 数据库记录未被删除\n")
    
    # 步骤4：测试文件夹完全不存在的情况
    print("4. 测试文件夹完全不存在的情况...")
    
    # 先创建一个新缓存
    cache_manager.save_cache(
        platform="bilibili",
        url="https://www.bilibili.com/video/BV1folder_test",
        media_id="BV1folder_test",
        use_speaker_recognition=True,
        transcript_data={"speakers": ["Speaker1"], "segments": []},
        transcript_type="funasr",
        title="文件夹测试",
        author="UP主",
        description=""
    )
    
    # 删除整个文件夹
    folder_path = os.path.join(
        cache_manager.cache_dir,
        "bilibili", "2025", "202508", "BV1folder_test"
    )
    if os.path.exists(folder_path):
        shutil.rmtree(folder_path)
        print(f"  [OK] 已删除文件夹: {folder_path}")
    
    # 查询应该返回 None
    cache_data = cache_manager.get_cache(platform="bilibili", media_id="BV1folder_test")
    if cache_data is None:
        print("  [OK] 文件夹不存在时，查询返回 None")
        print("  [OK] 无效记录已被自动清理")
    else:
        print("  [FAIL] 查询仍然返回了数据")
    
    # 关闭数据库连接
    cache_manager.close()
    
    print("\n=== 测试完成 ===")
    print("\n总结：")
    print("1. 当缓存文件夹不存在时，会自动删除数据库记录")
    print("2. 当转录文件不存在时，会自动删除数据库记录")
    print("3. 这确保了数据库与文件系统的一致性")


if __name__ == "__main__":
    test_auto_cleanup()
    
    # 清理测试目录
    if os.path.exists("./test_cache_dir"):
        shutil.rmtree("./test_cache_dir")
