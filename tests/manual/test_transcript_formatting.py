"""
测试转录文本的分段格式化功能
"""
import sys
import os

# 添加项目根目录到 sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '../..'))
sys.path.insert(0, project_root)

from src.video_transcript_api.llm import PlainTextProcessor


def _format_text(text: str) -> str:
    """调用 PlainTextProcessor 的格式化逻辑（无需完整初始化）"""
    processor = PlainTextProcessor.__new__(PlainTextProcessor)
    return processor._format_plain_text(text)


def test_format_transcript_for_display():
    """测试格式化转录文本功能"""

    long_text = "光无如实拒绝，虽不认职。今天我们尝试一种全新的讲述方式。最后呢附赠一个人民币未来三到五年的走势路径的地图。大家呢可以用来参考今天以 ai 工作流式的讲述方式来给人民币的公允价值，以及中共对于人民币定价的扭曲程度和扭曲强度做一个剖析式的穿透，来看看这玩意儿到底值多少钱，以及这种持续的价格扭曲能持续多久。这个问题今天呢我们采取一种新的精算方式，人机合作。因为今天的讲述难度跟信息密度实在是有点高，所以我完全不知道从哪里入手开始讲。"

    formatted = _format_text(long_text)

    print("="*80)
    print("原始文本（单行）:")
    print("="*80)
    print(long_text)
    print()

    print("="*80)
    print("格式化后的文本（分段）:")
    print("="*80)
    print(formatted)
    print()

    assert "\n" in formatted, "格式化后的文本应包含换行"
    assert len(formatted) >= len(long_text), "格式化后文本长度不应短于原文"

    paragraphs = formatted.split("\n\n")
    print("="*80)
    print("统计信息:")
    print(f"- 原始文本长度: {len(long_text)} 字符")
    print(f"- 格式化后长度: {len(formatted)} 字符")
    print(f"- 段落数量: {len(paragraphs)}")
    print("="*80)

    print("\nTest passed! Formatting works correctly.")


def test_with_real_file():
    """使用实际的转录文件进行测试"""
    file_path = r"D:\MyFolders\Developments\0Python\250427_VideoTranscriptApi\data\cache\youtube\2025\202510\WBSl49dZMTw\transcript_capswriter.txt"

    if not os.path.exists(file_path):
        print(f"Warning: Test file not found: {file_path}")
        print("Skipping real file test.")
        return

    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    sample = content[:1000]

    print("\n" + "="*80)
    print("使用真实文件测试（前1000字符）:")
    print("="*80)

    formatted = _format_text(sample)

    print("\n格式化结果预览（前500字符）:")
    print("-"*80)
    print(formatted[:500])
    print("-"*80)

    paragraphs = formatted.split("\n\n")
    print(f"\n段落数量: {len(paragraphs)}")
    print("\nTest with real file passed!")


if __name__ == "__main__":
    test_format_transcript_for_display()
    test_with_real_file()
