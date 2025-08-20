import sys
import os

# 添加项目根目录到路径
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, project_root)

from src.video_transcript_api.utils.dialog_renderer import DialogRenderer, render_transcript_content

def test_dialog_detection():
    """测试对话检测功能"""
    renderer = DialogRenderer()
    
    # 测试多人对话文本
    dialog_text = """知白：嗯，欢迎来到知行小酒馆，这是一档有知有行出品的播客节目。
少楠：承认自己是弱者，不是自我否定，反而能让人活得更有生命力。
知白：这是中年危机的一种褒义的说法。"""
    
    assert renderer.detect_dialog_mode(dialog_text) == True
    
    # 测试普通文本
    normal_text = """这是一段普通的文本内容，没有说话人标识。
它包含多个段落，但不是对话格式。
这种文本应该被识别为普通模式。"""
    
    assert renderer.detect_dialog_mode(normal_text) == False

def test_dialog_parsing():
    """测试对话解析功能"""
    renderer = DialogRenderer()
    
    dialog_text = """知白：欢迎来到小酒馆。
少楠：很高兴来到这里。
知白：今天我们聊什么？"""
    
    dialogs = renderer.parse_dialog_content(dialog_text)
    
    assert len(dialogs) == 3
    assert dialogs[0]['speaker'] == '知白'
    assert dialogs[0]['content'] == '欢迎来到小酒馆。'
    assert dialogs[1]['speaker'] == '少楠'
    assert dialogs[1]['content'] == '很高兴来到这里。'

def test_html_rendering():
    """测试HTML渲染功能"""
    # 测试对话渲染
    dialog_text = """知白：欢迎大家。
少楠：谢谢邀请。"""
    
    html = render_transcript_content(dialog_text)
    
    assert 'dialog-container' in html
    assert 'speaker-tag' in html
    assert '知白' in html
    assert '少楠' in html
    
    # 测试普通文本渲染
    normal_text = """这是普通文本。
包含多个段落。"""
    
    html = render_transcript_content(normal_text)
    
    assert '<p>' in html
    assert 'dialog-container' not in html

def test_speaker_colors():
    """测试说话人颜色分配"""
    renderer = DialogRenderer()
    
    speakers = ['知白', '少楠', '主持人']
    
    color1 = renderer.get_speaker_color('知白', speakers)
    color2 = renderer.get_speaker_color('少楠', speakers)
    
    assert color1 != color2  # 不同说话人应该有不同颜色
    assert color1.startswith('#')  # 应该是十六进制颜色

def test_smart_paragraph_split():
    """测试智能分段功能"""
    renderer = DialogRenderer()
    
    # 测试1: 短文本不分段（小于100字符）
    short_text = "这是一段短文本。"
    result = renderer.smart_paragraph_split(short_text)
    assert result == short_text
    
    # 测试2: 足够长的文本才会分段（至少100字符）
    long_text = "这是第一句话。" * 15  # 创建足够长的文本
    assert len(long_text) >= 100  # 确保超过100字符阈值
    result = renderer.smart_paragraph_split(long_text)
    # 长文本应该触发智能分段逻辑
    assert isinstance(result, str)
    
    # 测试3: 中英文标点符号混合（足够长）
    mixed_text = "Hello world! 这是中文内容。How are you? 你好吗？I am fine. 我很好。Thank you! 谢谢你！" * 3
    assert len(mixed_text) >= 100  # 确保足够长
    result = renderer.smart_paragraph_split(mixed_text)
    assert isinstance(result, str)
    
    # 测试4: 逗号强制断句（超长句子）
    comma_text = "这是一个非常长的句子，" * 10 + "包含很多逗号分隔的内容。"
    assert len(comma_text) >= 100  # 确保足够长
    result = renderer.smart_paragraph_split(comma_text)
    assert isinstance(result, str)  # 基本验证返回字符串

def test_dialog_rendering_styles():
    """测试对话渲染的样式和视觉效果"""
    renderer = DialogRenderer()
    
    dialog_text = """知白：欢迎来到知行小酒馆，这是一档有知有行出品的播客节目。我们关注投资，更关注怎样更好地生活。我是知白。

少楠：承认自己是弱者，不是自我否定，反而能让人活得更有生命力。因为你不需要背着全能的包袱，可以更坦然地面对自己能做什么、不能做什么。这种坦然会让你更加专注于自己真正能做的事情，从而获得更好的结果。

知白：这是中年危机的一种褒义的说法。当我们开始承认自己的局限性时，反而能够找到真正适合自己的道路。"""
    
    html = renderer.render_dialog_html(dialog_text)
    
    # 检查基本结构
    assert 'dialog-container' in html
    assert 'dialog-item' in html
    assert 'speaker-tag' in html
    assert 'dialog-content' in html
    
    # 检查说话人标签
    assert '知白' in html
    assert '少楠' in html
    
    # 检查颜色样式
    assert 'background-color:' in html
    assert '#' in html  # 十六进制颜色
    
    # 检查内容分段效果
    assert '<p>' in html or '<br>' in html  # 应该有段落或换行
    
    print("对话渲染HTML结构测试通过")

def test_speaker_tag_centering():
    """测试说话人标签的居中对齐效果"""
    renderer = DialogRenderer()
    
    # 使用多人对话测试，确保被识别为对话格式
    dialog_text = """知白：欢迎大家。
少楠：谢谢邀请。"""
    
    html = renderer.render_dialog_html(dialog_text)
    
    # 说话人标签应该包含在speaker-tag类中
    assert 'speaker-tag' in html
    assert '知白' in html
    assert '少楠' in html
    
    # 检查HTML结构的完整性
    assert html.count('<div class="dialog-item">') == 2  # 两个说话人
    assert html.count('<div class="speaker-tag"') == 2
    assert html.count('<div class="dialog-content">') == 2
    
    print("说话人标签结构测试通过")

def test_long_text_paragraph_handling():
    """测试长文本的智能分段处理"""
    renderer = DialogRenderer()
    
    # 创建多人对话，确保被识别为对话格式
    long_content = "这是一个很长很长的内容，" * 20 + "应该会被智能分段处理。这里有更多的句子。还有更多的内容要处理。"
    dialog_text = f"""知白：{long_content}
少楠：这是另一个说话人的回应。"""
    
    html = renderer.render_dialog_html(dialog_text)
    
    # 检查是否应用了智能分段
    assert 'dialog-container' in html
    
    # 长文本应该包含段落分隔
    dialog_content_sections = html.split('<div class="dialog-content">')
    if len(dialog_content_sections) > 1:
        # 获取第一个对话内容区域
        first_content = dialog_content_sections[1].split('</div>')[0]
        
        # 检查是否有段落标签或换行
        has_paragraphs = '<p>' in first_content
        has_breaks = '<br>' in first_content
        
        assert has_paragraphs or has_breaks, "长文本应该包含段落分隔或换行"
    
    print("长文本分段处理测试通过")

if __name__ == "__main__":
    print("开始测试对话渲染器...")
    
    try:
        test_dialog_detection()
        print("√ 对话检测测试通过")
        
        test_dialog_parsing() 
        print("√ 对话解析测试通过")
        
        test_html_rendering()
        print("√ HTML渲染测试通过")
        
        test_speaker_colors()
        print("√ 说话人颜色测试通过")
        
        # 新增的测试
        test_smart_paragraph_split()
        print("√ 智能分段功能测试通过")
        
        test_dialog_rendering_styles()
        print("√ 对话渲染样式测试通过")
        
        test_speaker_tag_centering()
        print("√ 说话人标签结构测试通过")
        
        test_long_text_paragraph_handling()
        print("√ 长文本分段处理测试通过")
        
        print("\n所有测试通过！")
        
    except Exception as e:
        print(f"测试失败: {e}")
        import traceback
        traceback.print_exc()
    
    # 演示实际输出
    print("\n=== 演示输出 ===")
    
    sample_dialog = """知白：欢迎来到知行小酒馆，这是一档有知有行出品的播客节目，我们关注投资，更关注怎样更好地生活。我是知白。

少楠：承认自己是弱者，不是自我否定，反而能让人活得更有生命力。因为你不需要背着全能的包袱，可以更坦然地面对自己能做什么、不能做什么。这种坦然会让你更加专注于自己真正能做的事情，从而获得更好的结果。

知白：这是中年危机的一种褒义的说法。当我们开始承认自己的局限性时，反而能够找到真正适合自己的道路。"""
    
    # 测试智能分段功能
    print("\n1. 智能分段功能演示:")
    renderer = DialogRenderer()
    long_text = "这是第一句话。这是第二句话，包含很多内容。这是第三句话。这是第四句话。这是第五句话。这是第六句话。这是第七句话。这是第八句话。"
    segmented = renderer.smart_paragraph_split(long_text)
    print("原文:", long_text[:50] + "...")
    print("分段后:", segmented.replace('\n\n', ' [段落分隔] '))
    
    # 测试对话渲染
    print("\n2. 对话渲染演示:")
    html_output = render_transcript_content(sample_dialog)
    print("HTML结构包含:")
    if 'dialog-container' in html_output:
        print("- dialog-container (对话容器)")
    if 'speaker-tag' in html_output:
        print("- speaker-tag (说话人标签)")
    if 'dialog-content' in html_output:
        print("- dialog-content (对话内容)")
    if 'background-color:' in html_output:
        print("- 颜色样式 (说话人区分)")
    
    print(f"生成HTML长度: {len(html_output)} 字符")
    
    # 测试普通文本
    print("\n3. 普通文本渲染:")
    normal_text = """这是一段普通的转录文本，没有明确的说话人标识。

它可能来自单人演讲或者没有启用说话人识别功能的转录。

这种文本将使用普通段落样式进行渲染。"""
    
    normal_html = render_transcript_content(normal_text)
    print("普通文本HTML特征:")
    if '<p>' in normal_html:
        print("- 包含段落标签")
    if 'dialog-container' not in normal_html:
        print("- 不包含对话容器（正确）")
    
    print(f"生成HTML长度: {len(normal_html)} 字符")