import markdown
import pymdownx.emoji
from .logger import setup_logger

logger = setup_logger("markdown_renderer")

def render_markdown_to_html(markdown_text: str) -> str:
    """
    将Markdown文本渲染为HTML
    支持表格、代码高亮、emoji等
    
    Args:
        markdown_text: Markdown文本
        
    Returns:
        str: 渲染后的HTML
    """
    try:
        if not markdown_text:
            return ""
            
        md = markdown.Markdown(extensions=[
            'tables',           # 表格支持
            'codehilite',       # 代码高亮
            'toc',              # 目录
            'fenced_code',      # 围栏代码块
            'pymdownx.emoji',   # Emoji支持
            'pymdownx.superfences',  # 增强代码块
            'pymdownx.betterem',     # 更好的强调
            'pymdownx.highlight',    # 语法高亮
        ], extension_configs={
            'pymdownx.emoji': {
                'emoji_index': pymdownx.emoji.gemoji,
                'emoji_generator': pymdownx.emoji.to_svg,
            },
            'codehilite': {
                'css_class': 'highlight',
                'use_pygments': False,  # 使用JavaScript高亮，避免服务器依赖
            }
        })
        
        html = md.convert(markdown_text)
        return html
        
    except Exception as e:
        logger.error(f"Markdown渲染失败: {e}")
        # 出错时返回原始文本，用<pre>标签包装
        return f"<pre>{markdown_text}</pre>"

def get_base_url() -> str:
    """获取外部访问基础URL"""
    from utils import load_config
    
    config = load_config()
    base_url = config.get("web", {}).get("base_url", "http://localhost:8000")
    
    return base_url.rstrip('/')