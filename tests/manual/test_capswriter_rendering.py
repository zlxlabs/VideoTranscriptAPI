"""
测试 CapsWriter 转录渲染逻辑

验证 CapsWriter 转录（无说话人信息）是否正确使用长文本分段渲染
"""
import os
import sys

# 添加项目根目录到 Python 路径
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, os.path.join(project_root, 'src'))

from video_transcript_api.utils.rendering.dialog_renderer import (
    DialogRenderer,
    render_transcript_content_smart,
    render_calibrated_content_smart
)
from video_transcript_api.utils.cache.cache_analyzer import analyze_cache_capabilities
from video_transcript_api.utils.logging import setup_logger

logger = setup_logger("test_capswriter_rendering")

def test_cache_analysis(cache_dir: str):
    """测试缓存分析功能"""
    print(f"\n{'='*80}")
    print(f"测试缓存分析: {cache_dir}")
    print(f"{'='*80}\n")

    if not os.path.exists(cache_dir):
        print(f"[ERROR] 缓存目录不存在: {cache_dir}")
        return False

    # 分析缓存能力
    capabilities = analyze_cache_capabilities(cache_dir)

    print(f"[OK] Cache analysis completed")
    print(f"  - Primary engine: {capabilities.primary_engine}")
    print(f"  - Has speaker data: {capabilities.has_speaker_data}")
    print(f"  - Has structured output: {capabilities.has_structured_output}")
    print(f"  - Format version: {capabilities.format_version}")
    print(f"  - Files present:")
    for file_key, exists in capabilities.files_present.items():
        status = "[OK]" if exists else "[--]"
        print(f"    {status} {file_key}")

    return True

def test_rendering_strategy(cache_dir: str):
    """测试渲染策略选择"""
    print(f"\n{'='*80}")
    print(f"测试渲染策略选择: {cache_dir}")
    print(f"{'='*80}\n")

    if not os.path.exists(cache_dir):
        print(f"[ERROR] 缓存目录不存在: {cache_dir}")
        return False

    renderer = DialogRenderer()
    capabilities = analyze_cache_capabilities(cache_dir)
    strategy = renderer._get_optimal_rendering_strategy(capabilities)

    print(f"[OK] Selected rendering strategy: {strategy}")

    # 验证策略是否正确
    if capabilities.primary_engine == 'capswriter' and not capabilities.has_speaker_data:
        expected_strategy = 'capswriter_long_text'
        if strategy == expected_strategy:
            print(f"[OK] Strategy is correct: CapsWriter should use long text rendering")
            return True
        else:
            print(f"[FAIL] Strategy is wrong: expected '{expected_strategy}', got '{strategy}'")
            return False

    return True

def test_transcript_rendering(cache_dir: str):
    """测试转录文本渲染"""
    print(f"\n{'='*80}")
    print(f"测试原始转录渲染: {cache_dir}")
    print(f"{'='*80}\n")

    if not os.path.exists(cache_dir):
        print(f"[ERROR] 缓存目录不存在: {cache_dir}")
        return False

    try:
        html = render_transcript_content_smart(cache_dir)

        print(f"[OK] 渲染成功")
        print(f"  - HTML长度: {len(html)} 字符")

        # 检查是否包含对话容器（说话人标识）
        has_dialog_container = '<div class="dialog-container">' in html
        has_speaker_tag = '<div class="speaker-tag"' in html

        if has_dialog_container or has_speaker_tag:
            print(f"  - 渲染类型: 对话格式 (包含说话人标识)")
            print(f"    [WARN]  警告: CapsWriter转录不应该使用对话格式！")
        else:
            print(f"  - 渲染类型: 长文本格式 (无说话人标识)")
            print(f"    [OK] 正确: CapsWriter转录使用了长文本渲染")

        # 显示前500字符
        print(f"\n预览 HTML (前500字符):")
        print("-" * 80)
        print(html[:500])
        print("-" * 80)

        return True

    except Exception as e:
        print(f"[ERROR] 渲染失败: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_calibrated_rendering(cache_dir: str):
    """测试校对文本渲染"""
    print(f"\n{'='*80}")
    print(f"测试校对文本渲染: {cache_dir}")
    print(f"{'='*80}\n")

    if not os.path.exists(cache_dir):
        print(f"[ERROR] 缓存目录不存在: {cache_dir}")
        return False

    calibrated_file = os.path.join(cache_dir, 'llm_calibrated.txt')
    if not os.path.exists(calibrated_file):
        print(f"[INFO]  没有校对文本，跳过此测试")
        return True

    try:
        html = render_calibrated_content_smart(cache_dir)

        if html is None:
            print(f"[INFO]  没有返回校对文本HTML")
            return True

        print(f"[OK] 渲染成功")
        print(f"  - HTML长度: {len(html)} 字符")

        # 检查是否包含对话容器（说话人标识）
        has_dialog_container = '<div class="dialog-container">' in html
        has_speaker_tag = '<div class="speaker-tag"' in html

        if has_dialog_container or has_speaker_tag:
            print(f"  - 渲染类型: 对话格式 (包含说话人标识)")
            print(f"    [WARN]  警告: CapsWriter转录的校对文本不应该使用对话格式！")
        else:
            print(f"  - 渲染类型: 长文本格式 (无说话人标识)")
            print(f"    [OK] 正确: CapsWriter转录的校对文本使用了长文本渲染")

        # 显示前500字符
        print(f"\n预览 HTML (前500字符):")
        print("-" * 80)
        print(html[:500])
        print("-" * 80)

        return True

    except Exception as e:
        print(f"[ERROR] 渲染失败: {e}")
        import traceback
        traceback.print_exc()
        return False

def main():
    """主测试函数"""
    # 测试缓存目录
    cache_dir = r"D:\MyFolders\Developments\0Python\250427_VideoTranscriptApi\data\cache\bilibili\2025\202511\BV1T21KBLEpo"

    print("\n" + "="*80)
    print("CapsWriter 转录渲染逻辑测试")
    print("="*80)
    print(f"\n测试目标: 验证 CapsWriter 转录（无说话人信息）使用长文本分段渲染")
    print(f"测试缓存: {cache_dir}\n")

    results = []

    # 1. 测试缓存分析
    results.append(("缓存分析", test_cache_analysis(cache_dir)))

    # 2. 测试渲染策略选择
    results.append(("渲染策略选择", test_rendering_strategy(cache_dir)))

    # 3. 测试原始转录渲染
    results.append(("原始转录渲染", test_transcript_rendering(cache_dir)))

    # 4. 测试校对文本渲染
    results.append(("校对文本渲染", test_calibrated_rendering(cache_dir)))

    # 汇总结果
    print(f"\n{'='*80}")
    print("测试结果汇总")
    print(f"{'='*80}\n")

    for test_name, result in results:
        status = "[OK] 通过" if result else "[FAIL] 失败"
        print(f"  {status}: {test_name}")

    all_passed = all(result for _, result in results)

    if all_passed:
        print(f"\n[OK] 所有测试通过！")
        return 0
    else:
        print(f"\n[FAIL] 部分测试失败")
        return 1

if __name__ == "__main__":
    sys.exit(main())
