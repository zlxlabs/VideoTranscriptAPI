"""处理器模块"""

from .plain_text_processor import PlainTextProcessor
from .speaker_aware_processor import SpeakerAwareProcessor
from .summary_processor import SummaryProcessor

__all__ = [
    "PlainTextProcessor",
    "SpeakerAwareProcessor",
    "SummaryProcessor",
]
