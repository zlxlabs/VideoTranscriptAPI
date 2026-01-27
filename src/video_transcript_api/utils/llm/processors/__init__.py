"""处理器模块"""

from .plain_text_processor import PlainTextProcessor
from .speaker_aware_processor import SpeakerAwareProcessor

__all__ = [
    "PlainTextProcessor",
    "SpeakerAwareProcessor",
]
