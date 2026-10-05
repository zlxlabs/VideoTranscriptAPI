"""
测试 LLM 失败时附加格式化原始转录文本的完整流程
"""
import sys
import os
import tempfile

# 添加项目根目录到 sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '../..'))
sys.path.insert(0, project_root)

from src.video_transcript_api.llm import PlainTextProcessor


def _format_text(text: str) -> str:
    """调用 PlainTextProcessor 的格式化逻辑（无需完整初始化）"""
    processor = PlainTextProcessor.__new__(PlainTextProcessor)
    return processor._format_plain_text(text)


def test_failure_with_formatting():
    """测试失败处理逻辑（包含格式化）"""

    long_transcript = """光无如实拒绝，虽不认职。今天我们尝试一种全新的讲述方式。最后呢附赠一个人民币未来三到五年的走势路径的地图。大家呢可以用来参考今天以 ai 工作流式的讲述方式来给人民币的公允价值，以及中共对于人民币定价的扭曲程度和扭曲强度做一个剖析式的穿透，来看看这玩意儿到底值多少钱，以及这种持续的价格扭曲能持续多久。这个问题今天呢我们采取一种新的精算方式，人机合作。因为今天的讲述难度跟信息密度实在是有点高，所以我完全不知道从哪里入手开始讲。不过呢后来我转念一想，这可能正好是一期能够跟 ai 应用深度结合的一期。本期节目我会给出大家所有的数据集合工作流以及所有的提示词工程。大家可以用自己的 ai 做一个复现。今天我们要用到的数学模型有两个，一个是比尔汇率模型和干预冲击模型。思路呢是这样的，我们可以用比尔模型呢来做回测日元、英镑、欧元这些自由交易的货币实际兑美元的一个价格变动。"""

    error_message = "【LLM call failed】400 Client Error: Bad Request for url: http://example.com"

    result_dict = {
        '校对文本': error_message,
        '内容总结': error_message
    }

    if result_dict.get('校对文本', '').startswith('【LLM call failed】'):
        print("Detected calibration failure, appending formatted transcript")
        formatted_transcript = _format_text(long_transcript)
        result_dict['校对文本'] = (
            f"{result_dict['校对文本']}\n\n"
            f"{'='*60}\n"
            f"以下是原始转录文本：\n"
            f"{'='*60}\n\n"
            f"{formatted_transcript}"
        )

    if result_dict.get('内容总结', '').startswith('【LLM call failed】'):
        print("Detected summary failure, appending formatted transcript")
        formatted_transcript = _format_text(long_transcript)
        result_dict['内容总结'] = (
            f"{result_dict['内容总结']}\n\n"
            f"{'='*60}\n"
            f"以下是原始转录文本：\n"
            f"{'='*60}\n\n"
            f"{formatted_transcript}"
        )

    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='_calibrated.txt', delete=False) as f:
        f.write(result_dict['校对文本'])
        calibrated_file = f.name

    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='_summary.txt', delete=False) as f:
        f.write(result_dict['内容总结'])
        summary_file = f.name

    print("\n" + "="*80)
    print("Test Results:")
    print("="*80)
    print(f"Calibrated file saved to: {calibrated_file}")
    print(f"Summary file saved to: {summary_file}")
    print()

    print("="*80)
    print("Calibrated Text Content (preview):")
    print("="*80)
    print(result_dict['校对文本'][:500])
    print("...")
    print()

    assert long_transcript[:50] in result_dict['校对文本']
    assert "以下是原始转录文本：" in result_dict['校对文本']
    assert "\n" in result_dict['校对文本']

    print("="*80)
    print("All assertions passed!")
    print("="*80)
    print()
    print("You can review the generated files to see the formatting:")
    print(f"  - {calibrated_file}")
    print(f"  - {summary_file}")
    print()
    print("Files will be kept for manual inspection.")


if __name__ == "__main__":
    test_failure_with_formatting()
