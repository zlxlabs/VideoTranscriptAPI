"""
测试缓存清理逻辑
验证当文件不存在时，会自动清理数据库记录
"""
from pathlib import Path
import shutil

import pytest

from src.video_transcript_api.cache.cache_manager import CacheManager


@pytest.fixture
def cache_manager(tmp_path):
    manager = CacheManager(cache_dir=str(tmp_path / "cache"))
    yield manager
    manager.close()


def test_auto_cleanup(cache_manager):
    """测试自动清理无效记录"""
    # 步骤1：创建正常的缓存
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
    
    assert cache_result is not None
    
    # 验证缓存可以正常查询
    cache_data = cache_manager.get_cache(platform="youtube", media_id="cleanup_test")
    assert cache_data is not None
    
    # 获取初始统计
    stats = cache_manager.get_cache_stats()
    initial_count = stats['total_records']
    
    # 步骤2：删除转录文件
    transcript_file = Path(cache_result["transcript_file"])
    transcript_file.unlink()
    
    # 步骤3：再次查询，应该返回 None 并删除记录
    cache_data = cache_manager.get_cache(platform="youtube", media_id="cleanup_test")
    assert cache_data is None
    
    # 验证数据库记录已被删除
    stats = cache_manager.get_cache_stats()
    final_count = stats['total_records']
    
    assert final_count == initial_count - 1
    
    # 步骤4：测试文件夹完全不存在的情况
    
    # 先创建一个新缓存
    second_cache = cache_manager.save_cache(
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
    
    # Delete the path returned by the actual cache producer.
    assert second_cache is not None
    shutil.rmtree(Path(second_cache["transcript_file"]).parent)
    
    # 查询应该返回 None
    cache_data = cache_manager.get_cache(platform="bilibili", media_id="BV1folder_test")
    assert cache_data is None
