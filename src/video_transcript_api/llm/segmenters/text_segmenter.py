"""无说话人文本分段器

基于现有 text_segmentation.py 的分段逻辑重构
"""

import re
from typing import List

from ...utils.logging import setup_logger
from ..core.config import LLMConfig

logger = setup_logger(__name__)


# 断点字符集合见 #146：不认 ASCII `.`。
# 理由：小数（3.14）、版本号（v1.2.3）、缩写（e.g.）、URL 在 `.` 处断开会撕裂正文；
# 且本仓另外三处（capswriter_client / dialog_segmenter / paragraphize）都不认 `.`。
# ASCII 句号是否要单独识别仍是待产品决策项，本模块只保证「不撕裂正文」。
_SENTENCE_BREAK_RE = re.compile(r"([。！？!?…，,；;：:\n])")


class TextSegmenter:
    """无说话人文本分段器"""

    def __init__(self, config: LLMConfig):
        """初始化文本分段器

        Args:
            config: LLM 配置

        Raises:
            ValueError: ``max_segment_size`` 不是「正的非布尔 int」。必须响亮失败而不是
                clamp——clamp 会把配置错误静默纠正成另一个宽度，正是「静默出错」那一档。

                拒绝面分两类，``condition`` 具名区分：

                - ``max_len_not_an_int``：不是 ``int``（``str`` / ``float`` / ``None`` /
                  任何其它类型），或是 ``bool``。``bool`` 是 ``int`` 子类，不显式排除的
                  话 ``True`` 会以 ``max_segment_size=1`` 的身份通过——``available = True - 0``
                  得 1，于是每段只切出 1 个字符：不报错、不告警，分段静默退化到极致。
                  ``float`` 同理穿过「只比大小」的守卫，随后死在更远处的
                  ``fragment[:3000.5]``（slice indices must be integers），栈更远更难定位。
                - ``max_len_not_positive``：是 ``int`` 但 ``<= 0``。
                  ``_append_fragment`` 的 ``while fragment:`` 在上限 <= 0 时
                  ``take = min(len(fragment), available)`` 恒为 0，循环变量不单调，
                  进程会挂死且 ``segments`` 无界增长。
        """
        max_segment_size = config.max_segment_size
        # 先判类型再比大小：非 int 会带着自己的比较语义（抛 TypeError、或静默通过）
        # 进入 ``_append_fragment``，两种都比在这里报错更贵。
        if isinstance(max_segment_size, bool) or not isinstance(max_segment_size, int):
            raise ValueError(
                "TEXT_SEGMENT_INVALID_MAX_SIZE condition=max_len_not_an_int "
                f"func=TextSegmenter.__init__ max_segment_size={max_segment_size!r} "
                f"type={type(max_segment_size).__name__}"
            )
        if max_segment_size <= 0:
            raise ValueError(
                "TEXT_SEGMENT_INVALID_MAX_SIZE condition=max_len_not_positive "
                f"func=TextSegmenter.__init__ max_segment_size={max_segment_size!r} "
                f"type={type(max_segment_size).__name__}"
            )

        self.config = config
        self.segment_size = config.segment_size
        # 注意：``segment_size`` 故意不做取值校验。它只参与
        # ``if len(current_segment) >= self.segment_size`` 的落盘判断，
        # 不参与任何循环的终止条件，因此 <= 0 只会让落盘更频繁，不会挂死。
        # 它不校验的前提是「类型同样由配置层校验卡兜住」，那张卡已排期但**尚未落地**
        # （#147 的 P2 部分，2026-10-03 锁定为延后）；在此之前 ``segment_size`` 写成非数字仍会在
        # 落盘判断处抛 TypeError，那属于配置层的责任边界，不是本模块承诺过的行为。
        self.max_segment_size = max_segment_size

    def segment(self, content: str) -> List[str]:
        """对纯文本内容进行分段

        Args:
            content: 文本内容

        Returns:
            分段后的文本列表
        """
        segments = []

        # 检测文本格式：判断是否为 CapsWriter 格式（短句换行，无标点符号）
        # 统计标点符号密度：每1000字符中的句号、问号、感叹号数量
        text_length = len(content)
        if text_length > 0:
            punctuation_count = (
                content.count('。') + content.count('！') + content.count('？') +
                content.count('!') + content.count('?')
            )
            punctuation_density = (punctuation_count / text_length) * 1000  # 每1000字符的标点数

            # 如果标点密度小于5（即每1000字符少于5个标点），认为是 CapsWriter 格式
            is_capswriter_format = punctuation_density < 5
        else:
            is_capswriter_format = False

        if is_capswriter_format:
            # 行式分支按行重建文本：行内 strip、行间丢掉换行。它**连分段器内部的守恒
            # 都不满足**（"".join(segments) != content，丢的是行与行之间的换行），
            # 更谈不上交付文本守恒。这里只如实标注，不改行为：CapsWriter 输出一行一句，
            # 行即语义边界，是否要逐字保留换行是排版决策，另行跟进。测试里两条分支的
            # 断言互不引用，别用句式分支的判据去套这条分支。
            logger.info("Detected CapsWriter format, segmenting by lines")
            lines = [line.strip() for line in content.split('\n') if line.strip()]

            if len(lines) <= 1:
                logger.info("CapsWriter text lacks valid newlines, falling back to sentence segmentation")
                segments = self._segment_by_sentences(content)
                logger.info(f"Text segmentation completed: {len(segments)} segments")
                return segments

            current_segment = ""
            for line in lines:
                current_segment = self._append_fragment(line, segments, current_segment)
        else:
            segments = self._segment_by_sentences(content)
            logger.info(f"Text segmentation completed: {len(segments)} segments")
            return segments

        if current_segment.strip():
            segments.append(current_segment.strip())

        logger.info(f"Text segmentation completed: {len(segments)} segments")
        return segments

    def _segment_by_sentences(self, content: str) -> List[str]:
        """按标点符号分段：只切不换，**逐字（含空白）守恒**。

        不变式（**只覆盖本函数的返回值**）：
        ``"".join(self._segment_by_sentences(content)) == content``，
        对任意输入成立，空白也算。每个输出段都是入参的一段连续切片，不 strip、不丢弃、不注入。

        边界：**这条不变式不覆盖最终交付文本的逐字守恒。** 消费端
        ``plain_text_processor`` 会用 ``"\n\n".join(calibrated_segments)`` 把各段
        无条件拼成段落，因此段与段之间一定出现 ``\n\n``——即便原文那里没有任何句读边界
        （纯长度硬切点也会变成用户可见的空行）。那属于既有的交付排版语义，
        由另一张单跟进，本模块既不改它、也不在这里承诺它。谁要把「交付文本逐字守恒」
        写成这个函数的验收条件，会得到一个假承诺。

        历史形态（均为静默出错，会原样进入交付文本）：

        - 无捕获组的 split 丢掉全部原标点 + 每片注入 ``。``，把 ``3.14`` 改成
          ``3。14``、``https://a.b/c`` 改成 ``https。//a。b/c``。
        - 对每片 ``.strip()`` 会吃掉标点后的空白，把 ``Hello! World?`` 粘成
          ``Hello!World?``——与上一条同类的正文改写，只是发生在空白上。

        段边界空白：本实现连段首尾的空白也原样保留，这样「不变式成立」不依赖
        「哪一刀恰好切在空白上」这种只有实现者知道的隐式条件。消费端随后用 ``\n\n``
        覆盖段边界（见上面的边界说明），所以这些空白在交付文本里不可见。
        行式分支（``segment()`` 的 CapsWriter 格式分支）不在本不变式范围内：
        它按行重建文本，见 ``segment()`` 里的说明。

        长度上限仍由 ``_append_fragment`` 逐字符拼装保证，不依赖句号。
        """
        segments = []
        # re.split 带一个捕获组时，切片形态是 [文本, 断点, 文本, 断点, ..., 文本]。
        parts = _SENTENCE_BREAK_RE.split(content)

        # 先把「文本 + 其后的断点」拼成整片：连续断点（空文本 + 非空断点）挂回上一片，
        # 否则句末的 ``！`` / ``？`` 会被当成孤立断点丢掉。整片不做 strip。
        fragments = []
        for index in range(0, len(parts), 2):
            sentence = parts[index]
            tail = parts[index + 1] if index + 1 < len(parts) else ""
            if sentence:
                fragments.append(sentence + tail)
            elif tail:
                if fragments:
                    fragments[-1] += tail
                else:
                    fragments.append(tail)

        current_segment = ""
        for fragment in fragments:
            current_segment = self._append_fragment(fragment, segments, current_segment)

        if current_segment:
            segments.append(current_segment)

        return segments

    def _append_fragment(self, fragment: str, segments: List[str], current_segment: str) -> str:
        """把 fragment 逐字符并入 current_segment，按上限切段并及时落盘。

        不变式（**只覆盖本函数产出的分段列表**）：``fragment`` 的每个字符恰好进入
        一个输出段或返回的余料，``"".join(segments) + current_segment`` 恒等于进入时的
        内容，空白也算。**任何 strip 都会破坏它**——被 strip 掉的空白若落在两段之间
        就是词粘连（``Hello! World?`` -> ``Hello!World?``），转录正文会静默变形。

        边界：与 ``_segment_by_sentences`` 一样，这条不变式止于分段列表。消费端
        ``plain_text_processor`` 用 ``"\n\n"`` 合并各段，所以最终交付文本不是
        本函数的逐字输出，段边界一律变成段落分隔。
        """
        if not fragment:
            return current_segment

        while fragment:
            available = self.max_segment_size - len(current_segment)
            if available <= 0:
                segments.append(current_segment)
                current_segment = ""
                available = self.max_segment_size

            take = min(len(fragment), available)
            current_segment += fragment[:take]
            fragment = fragment[take:]

            if len(current_segment) >= self.max_segment_size:
                segments.append(current_segment)
                current_segment = ""

        if len(current_segment) >= self.segment_size:
            segments.append(current_segment)
            current_segment = ""

        return current_segment
