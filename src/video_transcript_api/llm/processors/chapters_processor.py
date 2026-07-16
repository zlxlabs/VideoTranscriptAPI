"""章节梗概生成处理器

职责：把一份长逐字稿（按 segment 切分，带时间戳）切分成若干章节，每章给出
标题（title）、梗概（gist）和时间范围（start_time/end_time）。

设计要点（与 SummaryProcessor 的"诚实状态模型"一脉相承）：
- LLM 只被要求给出每章的起始编号（start_seg），不给时间、不给结束编号——
  时间戳交给它大概率会抄错或算错，起止编号则完全由本地代码反查/推导，
  保证与真实 segments 数据一致。
- LLM 调用必须走 json_object 模式（通过 force_json_mode 强制指定）：
  json_schema 严格模式失败即返回、没有重试，而本处理器依赖的"语义校验失败
  ->带着具体错误重试一次"这套语义完全建立在 json_object 模式的
  Self-Correction 能力之上（见 llm/llm.py `_call_with_json_object_mode`）。
- 语义校验（start_seg 合法性/去重/递增/首位钳制）与结构校验（章节数量、
  密度、同名合并）都在本地代码里做，不依赖 LLM 自我把关。
"""

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union

from ...transcriber.segments import parse_time_to_seconds
from ...utils.logging import setup_logger
from ...utils.llm_status import ChaptersStatus
from ..core.config import LLMConfig
from ..core.llm_client import LLMClient
from ..prompts import (
    CHAPTERS_SYSTEM_PROMPT,
    build_chapters_user_prompt,
)

logger = setup_logger(__name__)


# ============================================================
# JSON Schema（章节生成的结构化输出契约）
# ============================================================

CHAPTERS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "chapters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "章节标题，不超过 20 字，不带序号",
                    },
                    "gist": {
                        "type": "string",
                        "description": "章节梗概，2-3 句话",
                    },
                    "start_seg": {
                        "type": "integer",
                        "description": "该章节在输入编号正文中的起始编号",
                    },
                },
                "required": ["title", "gist", "start_seg"],
            },
        },
    },
    "required": ["chapters"],
}


# ============================================================
# 结构校验阈值
# ============================================================

_MIN_CHAPTER_COUNT = 2
_MAX_CHAPTER_COUNT = 100
_MIN_CHAPTER_SECONDS = 60          # 平均章节时长下限（秒），低于此值只 warning，不算失败
_MAX_CHAPTER_SECONDS = 40 * 60     # 平均章节时长上限（秒）

# prompt 里要求 title 不超过 20 字，但纯靠 LLM 自觉不可靠，本地不做硬校验
# （不值得为超长标题触发重试/判 FAILED），超过此长度直接截断 + 记 warning。
_TITLE_MAX_CHARS = 24
_TITLE_TRUNCATE_TO = 23


# ============================================================
# 结果类型
# ============================================================

@dataclass(frozen=True)
class Chapter:
    """单个章节。

    start_seg/end_seg 是 segments 列表里的下标（闭区间）；start_time/end_time
    是反查对应 segment 的时间戳后转换出的秒数，上游缺失时间信息时可能为 None。

    若推导出 end_time < start_time（segments 时间乱序/脏数据导致），end_time
    会被诚实降级为 None，绝不产出非法区间；不改变 start_seg/end_seg 的顺序
    语义——索引顺序仍是正文顺序，不做重排（见 `_sanitize_end_time`）。
    """

    index: int
    title: str
    gist: str
    start_seg: int
    end_seg: int
    start_time: Optional[float]
    end_time: Optional[float]


@dataclass(frozen=True)
class ChaptersResult:
    """章节生成结果："诚实状态模型"的载体，组织方式参考 SummaryResult。

    - segments 为 None/空，或非空但没有任何一条能解析出 start_time（完全没有可用
      时间轴）: chapters=[], status=SKIPPED_NO_TIMELINE
    - 原文过短: chapters=[], status=SKIPPED_SHORT
    - 原文过长/LLM 异常/语义或结构校验不通过: chapters=[], status=FAILED, error 非空
    - 成功: chapters=完整列表, status=GENERATED

    fingerprint 覆盖每条 segment 的 text + 规范化后的 start_time/end_time +
    speaker（而非只哈希文本）：把 segments 规范化为结构化条目列表后做规范化
    JSON 序列化（ensure_ascii + 固定分隔符 + sort_keys，逐字节确定性输出），
    再对 JSON 文本求 sha1——JSON 的字符串转义与"每条 segment 是数组里独立
    一个元素"的结构，从根上排除手工分隔符拼接可能出现的字段/条目边界碰撞
    （包括 speaker 里恰好含有分隔符字符本身的场景）。同一段文本若时间轴被
    修正（如时间戳纠错）或说话人被修正（如"说话人2"改成真实姓名）但文本
    本身未变，指纹也会随之变化——这样上层若把 fingerprint 接入缓存层，就
    不会复用一份挂着旧时间戳/旧说话人的章节结果。详见 `_compute_fingerprint`。
    segments 为 None/空时该值为 None。
    """

    chapters: List[Chapter]
    status: ChaptersStatus
    error: Optional[str]
    fingerprint: Optional[str]
    segment_count: int


# ============================================================
# 私有工具函数
# ============================================================

def _to_seconds(value: Union[float, int, str, None]) -> Optional[float]:
    """将 segment 的 start_time/end_time 归一化为秒数（float）。

    薄包装：全部解析与防御逻辑（数值/字符串两种输入、"HH:MM:SS"/"MM:SS" 时间
    格式、非有限值/负数/OverflowError/非法段数等）都委托给项目共享实现
    transcriber.segments.parse_time_to_seconds，不再自行重复实现。

    背景：本函数曾经手写一套几乎相同的解析逻辑（对时间字符串按 ":" split 后
    累加 seconds*60+part），但没有像共享实现那样校验 split 出的段数必须是
    2 或 3——导致 "00:00:00:41" 这类四段畸形时间被当成合法值解析出 41 秒，
    与共享实现"len(parts) not in (2, 3) 一律拒绝"的语义产生分歧。委托给
    共享实现从根上消除这种分歧，而不是在这里补一条针对性校验。

    这里只保留一层薄薄的日志包装：解析失败时补一条 warning，方便定位上游
    数据问题（共享实现本身不打日志，因为它同时服务于多个调用方，日志语义
    应由各自调用方决定）。

    Args:
        value: 原始时间值，None/数值/字符串三种之一

    Returns:
        秒数（float，>= 0），无法解析或输入为 None 时返回 None
    """
    seconds = parse_time_to_seconds(value)
    if seconds is None and value is not None:
        logger.warning(f"[CHAPTERS] Unparseable time value, treating as None: {value!r}")
    return seconds


def _compute_fingerprint(segments: List[Dict[str, Any]]) -> str:
    """计算 segments 的指纹（sha1 十六进制摘要），用于上层判断"原文是否变化"。

    指纹覆盖每条 segment 的 text + 规范化后的 start_time/end_time + speaker，
    而不是只哈希文本——否则上游对同一段文本重新做时间轴修正（segment 分组
    不变，仅 start_time/end_time 变化）或说话人纠正（如"说话人2"改成真实
    姓名，文本/时间不变）后指纹依然不变，未来若指纹被接入缓存层，会导致
    复用一份挂着旧时间戳/旧说话人的章节结果——旧说话人尤其隐蔽：speaker
    会拼进 LLM prompt（`[i] mm:ss speaker: text`，见 `_build_segment_lines`），
    说话人纠正后章节梗概里引用的人名可能整段是错的，却因为指纹没变而被
    缓存层静默判定为"原文未变"继续复用。

    实现：结构化条目列表 + 规范化 JSON 序列化，而不是手工分隔符拼接。
    早期实现用两级不可见字符分隔符（条目间 "\\x1f"、条目内字段间 "\\x1e"）
    手工拼接字符串，但字段内容本身未经任何转义——一旦 speaker 恰好含有这两
    个分隔符字符（哪怕是构造出来的边界情况），拼接结果可以与完全不同的
    segment 序列产生字节级碰撞（例如一条"speaker 里塞了假分隔符"的 segment
    可以伪造成两条独立 segment 拼接后的相同字节串），指纹从"内容摘要"退化
    成"可被构造碰撞的弱校验"。同理，固定占位符方案（缺字段用 "\\x00" 代替）
    本身也不安全：一旦真实数据里出现字面值恰好是占位符的 speaker（同样属于
    构造/损坏数据），会与"字段缺失"混同。

    改为对结构化条目列表做 `json.dumps(..., ensure_ascii=True,
    separators=(",", ":"), sort_keys=True)` 后再 sha1：
    - JSON 字符串会转义控制字符（含 "\\x1f"/"\\x1e"/"\\x00" 等），不存在
      "字段内容里塞入分隔符即可伪造边界"的问题；
    - 每条 segment 是数组里独立的一个 dict 元素，序列的分段方式由 JSON 语法
      本身保证，不依赖任何自定义分隔符；
    - "字段缺失" 用 dict 里显式**不放该 key** 表达（而不是某个固定占位符
      字符串）：speaker 完全不存在时不放 "sp" 键，与 "sp" 恰好是任意字符串
      （包括边界值如空串或 "\\x00"）在 JSON 层面就是两种不同的结构，不可能
      混同；start_time/end_time 解析失败或缺失时 "s"/"e" 的值是 JSON
      null，与合法秒数（哪怕是 0）用 JSON 数字表示，两者不可能重合。
    - `separators=(",", ":")` 去掉默认的空格，`sort_keys=True` 固定键序，
      保证同一逻辑输入不论 dict 构造顺序如何都序列化成同一段字节，指纹
      仍然稳定可复现。

    start_time/end_time 在写入前先用 `_to_seconds` 规范化（同一个时间不论用
    float 还是 "HH:MM:SS" 字符串表示，指纹片段都一致）。speaker 写入前强转
    str（上游 FunASR 原始 dict 的 speaker 可能是裸 int），避免类型不一致
    导致同一说话人因表示类型不同（int 2 与字符串 "2"）产生不同指纹。

    Args:
        segments: 原始 segment 列表（未经任何处理），每条形如
            {"text": str, "start_time": ..., "end_time": ..., "speaker": 可选, ...}

    Returns:
        str: sha1 十六进制摘要
    """
    entries: List[Dict[str, Any]] = []
    for seg in segments:
        entry: Dict[str, Any] = {
            "t": seg.get("text") or "",
            "s": _to_seconds(seg.get("start_time")),
            "e": _to_seconds(seg.get("end_time")),
        }
        speaker = seg.get("speaker")
        if speaker is not None:
            # 显式区分"无 speaker 键"（缺失）与"speaker 为任意字符串"（含
            # 空串、含分隔符字符等边界值）；强转 str 避免 int/str 表示同一
            # 说话人时产生不同指纹（同 `_build_segment_lines` 的处理）。
            entry["sp"] = str(speaker)
        entries.append(entry)

    fingerprint_source = json.dumps(
        entries, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha1(fingerprint_source.encode("utf-8")).hexdigest()


def _format_timestamp(seconds: Optional[float]) -> str:
    """把秒数格式化为 mm:ss（超过 1 小时则 h:mm:ss）；None 返回空字符串。

    对 inf/nan 也返回空字符串——防御性兜底：正常流程下非有限值理应已被
    _to_seconds 过滤掉，但 int(inf) 会抛 OverflowError、int(nan) 会抛
    ValueError，一旦有调用方绕过 _to_seconds 直接传入非有限值就会崩溃，
    这里独立做一次校验，不依赖上游已经过滤。
    """
    if seconds is None or not math.isfinite(seconds):
        return ""
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


# 匹配任意一段连续空白字符（含 "\n"/"\r"/"\t"/普通空格及其任意组合）。用于
# `_build_segment_lines` 把每条 segment 的 text 压平成单行——见该函数文档。
_WHITESPACE_RUN_RE = re.compile(r"\s+")


def _build_segment_lines(segments: List[Dict[str, Any]]) -> str:
    """把 segments 压缩为带编号的正文，供拼进 user prompt。

    每行格式：`[i] mm:ss (speaker:)? text`
    （时间缺失则省略时间部分，没有 speaker 字段则省略说话人前缀）。

    text 中的换行/回车会被压成单个空格（连续空白进一步折叠为一个）：这是
    "一个 segment 恰好一行"这个结构性不变式的安全边界——若不处理，text 里
    只要含有形如 "正常内容\\n[5] 00:00 fake" 的内容，拼进 prompt 后就会在
    编号列表里凭空多出一行、看起来与真实编号条目别无二致，LLM 可能把
    start_seg 错误地锚定到这条由 text 内容伪造出来的"行"上，而不是任何真实
    的 segment 边界。压平后，无论 text 里塞了多少个换行或形如 "[n] mm:ss"
    的字面内容，都只会作为该 segment 唯一一行里的普通文本片段出现，不可能
    被解读成独立的编号行。

    仅影响这里构建的 prompt 文本，不改变 segment 原始数据：
    - 指纹计算（`_compute_fingerprint`）直接使用原始 seg["text"]，不经过
      本函数——指纹要如实反映输入内容本身，不应因展示形态的处理而变化；
    - 门控 3（`min_chapters_threshold`）用未压平的 full_text 衡量"内容量是否
      值得分章"，这个语义与文本如何展示给 LLM 无关，同样不应受影响；
    - 门控 4（`max_chapters_input_chars`）测量的正是本函数的输出
      `segment_lines`，压平后的换行往往比原始换行更短（多个连续空白折叠成
      一个空格），门控测量"实际发送的 prompt 长度"这个语义与压平后的结果本
      就应该一致，不存在需要额外处理的分歧。
    """
    lines = []
    for i, seg in enumerate(segments):
        text = _WHITESPACE_RUN_RE.sub(" ", seg.get("text") or "").strip()
        timestamp = _format_timestamp(_to_seconds(seg.get("start_time")))
        speaker = seg.get("speaker")

        prefix = f"[{i}]"
        if timestamp:
            prefix += f" {timestamp}"
        if speaker is not None:
            # is not None 而非直接判真值：合法的说话人 ID 可能是数字 0
            # （FunASR 说话人分离结果里第一位说话人的标签），`if speaker:`
            # 会把它当假值误判为"无说话人"而漏掉前缀。强转 str：speaker 可能是裸 int，
            # f-string 插值本身不会因此报错，这里显式转换只为类型意图明确、
            # 与指纹计算侧（`_compute_fingerprint` 同样用 is not None）保持
            # 一致，不依赖插值的隐式转换。
            prefix += f" {str(speaker)}:"

        lines.append(f"{prefix} {text}".strip())
    return "\n".join(lines)


def _normalize_title_length(title: str, idx: int) -> str:
    """把已经 strip 过的 title 截断到 `_TITLE_MAX_CHARS` 字以内（软处理）。

    prompt 里声明 title 不超过 20 字，但完全依赖 LLM 自觉不可靠。超长标题
    本身不是"语义错误"，不值得为它触发重试或判 FAILED——直接在本地截断为
    前 `_TITLE_TRUNCATE_TO` 字 + "…"，只记一条 warning 日志，不影响结果状态。

    调用时机：必须在"相邻同名章节合并"（`_merge_adjacent_same_title`）及合并后
    的数量校验**之后**才截断，不能提前——否则两个前 23 字相同但完整内容不同
    的长标题会被截成同一字符串，被合并逻辑误判成"同名"而错误合并。

    Args:
        title: 已经 strip 过的标题原文
        idx: 该章节在最终章节列表中的下标，仅用于日志定位

    Returns:
        str: 长度 <= `_TITLE_MAX_CHARS` 的标题（未超长时原样返回）
    """
    if len(title) <= _TITLE_MAX_CHARS:
        return title
    truncated = title[:_TITLE_TRUNCATE_TO] + "…"
    logger.warning(
        f"[CHAPTERS] chapters[{idx}].title exceeds {_TITLE_MAX_CHARS} chars "
        f"({len(title)}), truncating: {title!r} -> {truncated!r}"
    )
    return truncated


def _validate_and_normalize_start_segs(
    raw_chapters: Any,
    segment_count: int,
) -> tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """校验并规范化 LLM 返回的 chapters 列表的 start_seg 序列。

    校验以下内容：
    1. 每项 start_seg 必须是 int（排除 bool）且落在 [0, segment_count) 范围内
    2. 按 start_seg 去重（保留首次出现），去重后的序列必须严格递增
    3. 去重后若首项 start_seg > 0，钳制为 0（覆盖开头；这是自动修正，不算校验失败）
    4. title、gist 必须是非空字符串（strip 后非空）——曾经缺失/非字符串/空白值
       会被 `str(x or "").strip()` 强转成空串照样成章，导致章节标题/梗概为空。
       与 start_seg 越界/重复同等对待：视为语义校验失败，触发重试。

    注意：title 长度规范化（超长截断）**不**在这里做。这里保留的是原始完整
    title（仅 strip），供调用方在"合并相邻同名章节"时用完整标题比较——如果在
    这一步就截断，两个前 23 字相同但完整内容不同的长标题会被截成同一个字符
    串，随后被 `_merge_adjacent_same_title` 误判成"同名"而错误合并。截断改
    为在合并（及合并后的数量校验）**之后**、构造最终 Chapter 列表前统一做
    （见 `_normalize_title_length` 的调用点）。

    Args:
        raw_chapters: response.structured_output.get("chapters") 的原始返回
        segment_count: 输入 segments 的总条数（N），用于范围校验

    Returns:
        (normalized, error):
        - 成功：normalized 是去重 + 钳制后的 [{"title","gist","start_seg"}, ...]
          列表（按 start_seg 升序），title 为未截断的完整原文，error 为 None
        - 失败：normalized 为 None，error 是具体原因描述（用于拼进重试 prompt
          或写入最终失败结果的 error 字段）
    """
    if not isinstance(raw_chapters, list) or not raw_chapters:
        return None, "chapters field is missing, not a list, or empty"

    items: List[Dict[str, Any]] = []
    for idx, item in enumerate(raw_chapters):
        if not isinstance(item, dict):
            return None, f"chapters[{idx}] is not an object"

        start_seg = item.get("start_seg")
        if not isinstance(start_seg, int) or isinstance(start_seg, bool):
            return None, f"chapters[{idx}].start_seg is not an int: {start_seg!r}"
        if not (0 <= start_seg < segment_count):
            return None, (
                f"chapters[{idx}].start_seg={start_seg} out of range [0, {segment_count})"
            )

        raw_title = item.get("title")
        if not isinstance(raw_title, str) or not raw_title.strip():
            return None, (
                f"chapters[{idx}].title is missing, not a string, or blank: {raw_title!r}"
            )

        raw_gist = item.get("gist")
        if not isinstance(raw_gist, str) or not raw_gist.strip():
            return None, (
                f"chapters[{idx}].gist is missing, not a string, or blank: {raw_gist!r}"
            )

        items.append({
            "title": raw_title.strip(),
            "gist": raw_gist.strip(),
            "start_seg": start_seg,
        })

    # 去重：按 start_seg 保留首次出现
    seen: set = set()
    deduped: List[Dict[str, Any]] = []
    for item in items:
        if item["start_seg"] in seen:
            continue
        seen.add(item["start_seg"])
        deduped.append(item)

    # 去重后必须严格递增
    for prev, curr in zip(deduped, deduped[1:]):
        if curr["start_seg"] <= prev["start_seg"]:
            seq = [d["start_seg"] for d in deduped]
            return None, f"start_seg sequence not strictly increasing after dedup: {seq}"

    # 首项若 > 0，钳制为 0（覆盖开头，不算失败）
    if deduped[0]["start_seg"] > 0:
        logger.info(
            f"[CHAPTERS] Clamping first chapter start_seg from "
            f"{deduped[0]['start_seg']} to 0"
        )
        deduped[0] = {**deduped[0], "start_seg": 0}

    return deduped, None


def _derive_end_segs(chapters: List[Dict[str, Any]], segment_count: int) -> None:
    """原地为每章填充 end_seg：下一章起点的前一个位置，末章到最后一条 segment。"""
    n = len(chapters)
    for i, chapter in enumerate(chapters):
        if i + 1 < n:
            chapter["end_seg"] = chapters[i + 1]["start_seg"] - 1
        else:
            chapter["end_seg"] = segment_count - 1


def _derive_times(chapters: List[Dict[str, Any]], segments: List[Dict[str, Any]]) -> None:
    """原地为每章填充 start_time/end_time：反查对应 segment 的时间戳并转秒（None 容忍）。"""
    for chapter in chapters:
        chapter["start_time"] = _to_seconds(segments[chapter["start_seg"]].get("start_time"))
        chapter["end_time"] = _to_seconds(segments[chapter["end_seg"]].get("end_time"))


def _sanitize_end_time(
    start_time: Optional[float], end_time: Optional[float], idx: int
) -> Optional[float]:
    """诚实降级：若 end_time < start_time，丢弃 end_time（置 None），不产出非法区间。

    segments 时间乱序或坏数据会让反查出的 start_time/end_time 组合倒挂——
    不论是单章内部（_derive_times 直接反查出的一对）还是相邻同名章节合并后
    （`_merge_adjacent_same_title` 拼接首章 start_time 与末章 end_time）产生的
    倒挂，都会在这里被拦下。不重排 segments、不改变 start_seg/end_seg，只是让
    这一条的"结束时间"退化为"未知"（与部分 segment 缺时间戳同一等级的容忍），
    状态仍然 GENERATED——这不是需要重试或判失败的语义错误，只是数据本身
    不足以支撑一个合法的时间区间。

    调用时机：必须在合并（`_merge_adjacent_same_title`）**之后**、最终构造
    `Chapter` **之前**做，这样才能同时覆盖两类倒挂来源。

    Args:
        start_time: 章节 start_time（可能为 None）
        end_time: 章节 end_time（可能为 None）
        idx: 该章节在最终章节列表中的下标，仅用于日志定位

    Returns:
        原始 end_time；若两者均非 None 且 end_time < start_time 则返回 None
    """
    if start_time is not None and end_time is not None and end_time < start_time:
        logger.warning(
            f"[CHAPTERS] chapters[{idx}] end_time ({end_time}) < start_time "
            f"({start_time}), discarding end_time (segments may be out of order)"
        )
        return None
    return end_time


def _merge_adjacent_same_title(chapters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """本地合并相邻且标题完全相同的章节（LLM 有时会把同一话题拆成两段返回同一个标题）。

    合并时：保留前一章的 start_seg/start_time，采用被合并章节的 end_seg/end_time，
    gist 用空格拼接两段，避免信息丢失。
    """
    if not chapters:
        return []

    merged = [dict(chapters[0])]
    for chapter in chapters[1:]:
        if chapter["title"] and chapter["title"] == merged[-1]["title"]:
            logger.info(
                f"[CHAPTERS] Merging adjacent chapter with duplicate title: "
                f"{chapter['title']!r}"
            )
            merged[-1]["end_seg"] = chapter["end_seg"]
            merged[-1]["end_time"] = chapter["end_time"]
            merged[-1]["gist"] = f"{merged[-1]['gist']} {chapter['gist']}".strip()
        else:
            merged.append(dict(chapter))
    return merged


def _warn_if_density_out_of_range(chapters: List[Dict[str, Any]]) -> None:
    """章节平均时长若明显偏短/偏长，记 warning 日志但不影响结果（结果仍然保留）。

    只用两端时间戳均可用的章节计算平均值；若全部章节都缺时间信息，跳过检查
    （没有数据就不该给出误导性的判断）。
    """
    durations = [
        chapter["end_time"] - chapter["start_time"]
        for chapter in chapters
        if chapter["start_time"] is not None and chapter["end_time"] is not None
    ]
    if not durations:
        return

    avg = sum(durations) / len(durations)
    if avg < _MIN_CHAPTER_SECONDS:
        logger.warning(
            f"[CHAPTERS] Average chapter duration is short: {avg:.1f}s "
            f"(< {_MIN_CHAPTER_SECONDS}s)"
        )
    elif avg > _MAX_CHAPTER_SECONDS:
        logger.warning(
            f"[CHAPTERS] Average chapter duration is long: {avg:.1f}s "
            f"(> {_MAX_CHAPTER_SECONDS}s)"
        )


# ============================================================
# 处理器
# ============================================================

class ChaptersProcessor:
    """章节梗概生成处理器

    职责：
    - 把长逐字稿的 segments 压缩为带编号的正文喂给 LLM
    - 只信任 LLM 给出的章节起始编号（start_seg），本地推导结束编号与时间范围
    - 对 LLM 输出做语义校验，违反时带着具体错误重试一次；仍不合法则判定失败
    - 结构校验（章节数量、密度、相邻同名合并）在本地完成，不依赖 LLM
    """

    def __init__(
        self,
        llm_client: LLMClient,
        config: LLMConfig,
    ):
        """初始化章节处理器

        Args:
            llm_client: LLM 客户端（含智能重试）
            config: LLM 配置对象
        """
        self.llm_client = llm_client
        self.config = config

        logger.info("ChaptersProcessor initialized")

    def process(
        self,
        segments: Optional[List[Dict[str, Any]]],
        title: str,
        author: str = "",
        description: str = "",
        selected_models: Optional[Dict[str, Any]] = None,
    ) -> ChaptersResult:
        """生成章节列表

        Args:
            segments: 带时间戳的分段列表，每条形如
                {"text": str, "start_time": float|"HH:MM:SS"|None,
                 "end_time": 同左, "speaker": 可选}
            title: 视频标题
            author: 作者/频道
            description: 视频描述
            selected_models: 选定的模型配置（可选，来自风险检测），支持
                "chapters_model"/"chapters_reasoning_effort" 覆盖 config 默认值

        Returns:
            ChaptersResult: 见类文档的状态说明

        Raises:
            不抛出异常，出错时返回 status=FAILED 的 ChaptersResult
        """
        logger.info(f"[CHAPTERS] process() called: title={title!r}")

        # 兜底防线：process() 是本处理器唯一的对外入口，契约是"永远返回诚实
        # 状态、不崩溃"——下面的门控/校验/推导逻辑已经覆盖了所有已知的脏数据
        # 场景，但这里仍然包一层 try/except 作为接线批完成前的最后一道防线，
        # 只捕获"未预见的" bug（比如上游又混入了一种没考虑到的脏数据形态）。
        # 已知分支（门控 1-4、语义/结构校验等）必须继续各自显式返回对应的
        # 诚实状态（SKIPPED_*/FAILED/GENERATED），不能靠这层兜底代劳——这层
        # 只是防线，不是正常控制流的一部分。
        try:
            return self._process_impl(segments, title, author, description, selected_models)
        except Exception as exc:  # noqa: BLE001 - 有意宽泛捕获，见上方注释
            logger.error(
                f"[CHAPTERS] Unexpected error in process(): {exc}",
                exc_info=True,
            )
            return ChaptersResult(
                chapters=[], status=ChaptersStatus.FAILED,
                error=f"unexpected error: {exc}", fingerprint=None, segment_count=0,
            )

    def _process_impl(
        self,
        segments: Optional[List[Dict[str, Any]]],
        title: str,
        author: str,
        description: str,
        selected_models: Optional[Dict[str, Any]],
    ) -> ChaptersResult:
        """process() 的实际实现：门控 -> LLM 调用 -> 校验 -> 构造结果。

        从 process() 拆出来，纯粹是为了让 process() 能用一层 try/except 包住
        整个主体（见 process() 文档的"兜底防线"说明），本身不是独立的公共
        入口，不直接被外部调用。
        """
        # 门控 1：没有可用的分段时间轴，无法生成章节（正常路径，非失败）
        if not segments:
            logger.info("[CHAPTERS] No segments provided, skipping (no timeline)")
            return ChaptersResult(
                chapters=[], status=ChaptersStatus.SKIPPED_NO_TIMELINE,
                error=None, fingerprint=None, segment_count=0,
            )

        # 入口过滤：segments 混入非 dict 条目时（如 JSON null 反序列化成
        # None，或上游脏数据混进裸字符串），下面所有逻辑都假设每条 segment
        # 是 dict、可以 .get(...)，非 dict 条目会直接抛 AttributeError，
        # 违反"处理器永远返回诚实状态、不崩溃"的契约。这里直接跳过非 dict
        # 条目，语义与 transcriber.segments.normalize_segments 对非 dict
        # 条目"直接跳过"保持一致，避免同一类脏数据在两处产生不同的容忍口径。
        original_count = len(segments)
        segments = [seg for seg in segments if isinstance(seg, dict)]
        filtered_count = original_count - len(segments)
        if filtered_count > 0:
            logger.warning(
                f"[CHAPTERS] Filtered {filtered_count} non-dict segment "
                f"entries out of {original_count} (upstream data quality issue)"
            )
        if not segments:
            logger.info(
                "[CHAPTERS] All segments were non-dict entries after "
                "filtering, skipping (no timeline)"
            )
            return ChaptersResult(
                chapters=[], status=ChaptersStatus.SKIPPED_NO_TIMELINE,
                error=None, fingerprint=None, segment_count=0,
            )

        segment_count = len(segments)
        full_text = "".join((seg.get("text") or "") for seg in segments)
        # 指纹覆盖每条 segment 的 text + 规范化后的 start_time/end_time（细节见
        # `_compute_fingerprint`），而不是只哈希文本拼接——full_text 本身继续
        # 只用于下面的长度门控，不受指纹算法影响。
        fingerprint = _compute_fingerprint(segments)

        # 门控 2：segments 非空，但没有任何一条能解析出 start_time —— 章节功能
        # 的核心价值就是时间范围，完全没有时间信息时生成出来的章节也没有意义。
        # 只要求"至少一条"，允许部分 segment 缺时间（下游 _derive_times 本就
        # 容忍单条 None）。必须在调用 LLM 之前判定，不能等生成完了再发现没用。
        has_any_start_time = any(
            _to_seconds(seg.get("start_time")) is not None for seg in segments
        )
        if not has_any_start_time:
            logger.info(
                "[CHAPTERS] All segments lack a parseable start_time, "
                "skipping (no usable timeline)"
            )
            return ChaptersResult(
                chapters=[], status=ChaptersStatus.SKIPPED_NO_TIMELINE,
                error=None, fingerprint=fingerprint, segment_count=segment_count,
            )

        # 门控 2.5：过滤后有效 segment 数少于 _MIN_CHAPTER_COUNT（2）个时，结构上
        # 不可能产出步骤 3 要求的至少 2 个章节——哪怕单个 segment 的文本长度已经
        # 超过 min_chapters_threshold（门控 3）也一样：单块时间轴没有任何内部
        # 边界可供切分，对导航毫无锚定价值，语义上等同于"没有可用的时间轴"。
        # 必须在调用 LLM 之前判定，否则这类输入注定在步骤 3 的章节数量下限校验
        # 处失败——FAILED 在未来分层补跑语义里是"可重试"状态，会让这种结构上
        # 永远不可能成功的请求被反复重试、反复烧钱。因此复用 SKIPPED_NO_TIMELINE
        # （正常跳过，不重试），而不是 FAILED。
        if segment_count < _MIN_CHAPTER_COUNT:
            error = (
                f"timeline 不足 {_MIN_CHAPTER_COUNT} 个分段，无法分章 "
                f"(only {segment_count} usable segment(s) after filtering)"
            )
            logger.info(f"[CHAPTERS] {error}, skipping (timeline insufficient for chapters)")
            return ChaptersResult(
                chapters=[], status=ChaptersStatus.SKIPPED_NO_TIMELINE,
                error=error, fingerprint=fingerprint, segment_count=segment_count,
            )

        # 门控 3：原文过短，未触发章节生成（正常路径，非失败）
        if len(full_text) < self.config.min_chapters_threshold:
            logger.info(
                f"[CHAPTERS] Text too short for chapters: "
                f"{len(full_text)} < {self.config.min_chapters_threshold}"
            )
            return ChaptersResult(
                chapters=[], status=ChaptersStatus.SKIPPED_SHORT,
                error=None, fingerprint=fingerprint, segment_count=segment_count,
            )

        # 压缩为带编号的正文——门控 4 必须用这份"实际将发送给 LLM 的文本"来衡量
        # 长度，而不是未编号的纯正文 full_text：大量短 segment 场景下，`[i]`
        # 编号、mm:ss 时间戳、speaker 前缀的开销会让真实 prompt 远超纯正文长度，
        # 仅测量 full_text 会漏判，实际发送的 prompt 仍可能远超模型上限。
        # 注：门控 3（min_chapters_threshold）语义是"内容量是否值得分章"，与发送
        # 开销无关，继续用 full_text 测量，不受这里影响。
        segment_lines = _build_segment_lines(segments)

        # 门控 4：原文过长，直接判失败而不是把超大输入硬塞给模型
        if len(segment_lines) > self.config.max_chapters_input_chars:
            error = (
                f"Input too long for chapters: {len(segment_lines)} chars > "
                f"max_chapters_input_chars={self.config.max_chapters_input_chars}"
            )
            logger.error(f"[CHAPTERS] {error}")
            return ChaptersResult(
                chapters=[], status=ChaptersStatus.FAILED,
                error=error, fingerprint=fingerprint, segment_count=segment_count,
            )

        logger.info(
            f"[CHAPTERS] Generating chapters for {segment_count} segments "
            f"(text length: {len(full_text)}, prompt text length: {len(segment_lines)})"
        )

        # 步骤 1：选择模型
        if selected_models:
            model = selected_models.get("chapters_model", self.config.chapters_model)
            reasoning_effort = selected_models.get(
                "chapters_reasoning_effort", self.config.chapters_reasoning_effort
            )
        else:
            model = self.config.chapters_model
            reasoning_effort = self.config.chapters_reasoning_effort

        # 运行时兜底：LLMConfig.from_dict() 会把未配置的 chapters_model 默认
        # 成 calibrate_model，但直接用 LLMConfig(...) 构造（跳过 from_dict）时
        # chapters_model 保持 None——不做兜底会把 None 传给 LLM 客户端。
        # 兜底链与 from_dict 的默认语义对齐，只是多绕一步 summary_model：
        # chapters_model -> summary_model -> calibrate_model。
        if not model:
            model = self.config.summary_model or self.config.calibrate_model
            logger.warning(
                f"[CHAPTERS] chapters_model not configured, falling back to {model!r}"
            )

        # 步骤 2：调用 LLM（内含语义校验失败时的单次重试）——segment_lines 已在
        # 门控 4 处构建完毕，此处直接复用，避免重复压缩同一份输入。
        normalized, raw_chapters, call_error = self._generate_with_retry(
            segment_lines=segment_lines,
            title=title,
            author=author,
            description=description,
            model=model,
            reasoning_effort=reasoning_effort,
            segment_count=segment_count,
        )

        if normalized is None:
            error = call_error or "chapters generation failed"
            if raw_chapters is not None:
                error = f"{error} | raw={raw_chapters!r}"
            logger.error(f"[CHAPTERS] {error}")
            return ChaptersResult(
                chapters=[], status=ChaptersStatus.FAILED,
                error=error, fingerprint=fingerprint, segment_count=segment_count,
            )

        # 步骤 3：结构校验 —— 章节数量
        if not (_MIN_CHAPTER_COUNT <= len(normalized) <= _MAX_CHAPTER_COUNT):
            error = (
                f"Chapter count out of bounds: {len(normalized)} "
                f"(expected [{_MIN_CHAPTER_COUNT}, {_MAX_CHAPTER_COUNT}])"
            )
            logger.error(f"[CHAPTERS] {error}")
            return ChaptersResult(
                chapters=[], status=ChaptersStatus.FAILED,
                error=error, fingerprint=fingerprint, segment_count=segment_count,
            )

        # 步骤 4：推导 end_seg 与 start_time/end_time
        _derive_end_segs(normalized, segment_count)
        _derive_times(normalized, segments)

        # 步骤 5：合并相邻同名章节
        merged = _merge_adjacent_same_title(normalized)

        # 步骤 6：合并后重新校验章节数下限——步骤 3 的校验发生在合并之前，
        # LLM 返回两个同名相邻章节时能通过"至少 2 章"的检查，合并后却可能
        # 只剩 1 章，突破了"至少 2 章"这个不变式。合并只会减少数量，不会
        # 增加，所以这里只需要再查下限，不用重查上限。
        if len(merged) < _MIN_CHAPTER_COUNT:
            error = (
                f"Chapter count dropped below minimum after merging adjacent "
                f"duplicate titles: {len(merged)} < {_MIN_CHAPTER_COUNT} "
                f"(合并后不足两章)"
            )
            logger.error(f"[CHAPTERS] {error}")
            return ChaptersResult(
                chapters=[], status=ChaptersStatus.FAILED,
                error=error, fingerprint=fingerprint, segment_count=segment_count,
            )

        _warn_if_density_out_of_range(merged)

        # 步骤 7：构造最终 Chapter 列表——title 长度规范化（软处理，超长截断）
        # 必须放在合并与合并后数量校验之后：截断是有损操作，若提前做，两个
        # 前 23 字相同但完整内容不同的长标题会被截成同一字符串，被步骤 5 的
        # 合并逻辑误判成"同名"而错误合并（见 `_normalize_title_length` 文档）。
        # 同时对 end_time 做倒挂防御（见 `_sanitize_end_time`）：segments 时间
        # 乱序/坏数据可能让某章 end_time < start_time（无论是单章内部反查
        # 出来的，还是合并相邻同名章节拼出来的），这里统一诚实降级为 None，
        # 不放行非法区间。
        final_chapters = [
            Chapter(
                index=i,
                title=_normalize_title_length(chapter["title"], i),
                gist=chapter["gist"],
                start_seg=chapter["start_seg"],
                end_seg=chapter["end_seg"],
                start_time=chapter["start_time"],
                end_time=_sanitize_end_time(chapter["start_time"], chapter["end_time"], i),
            )
            for i, chapter in enumerate(merged)
        ]

        logger.info(
            f"[CHAPTERS] Generated {len(final_chapters)} chapters "
            f"(from {len(normalized)} before merge)"
        )
        return ChaptersResult(
            chapters=final_chapters, status=ChaptersStatus.GENERATED,
            error=None, fingerprint=fingerprint, segment_count=segment_count,
        )

    def _generate_with_retry(
        self,
        *,
        segment_lines: str,
        title: str,
        author: str,
        description: str,
        model: str,
        reasoning_effort: Optional[str],
        segment_count: int,
    ) -> tuple[Optional[List[Dict[str, Any]]], Any, Optional[str]]:
        """调用 LLM 生成章节，若语义校验失败则携带具体错误重试一次。

        LLM 调用强制走 json_object 模式（force_json_mode="json_object"）：
        json_schema 严格模式失败即返回、没有重试，本方法依赖的"重试"语义完全
        建立在 json_object 模式的 Self-Correction 能力之上。

        Returns:
            (normalized_chapters, raw_chapters, error):
            - 成功：normalized_chapters 是校验通过的列表，error 为 None
            - 失败：normalized_chapters 为 None，raw_chapters 是最后一次的原始
              返回（用于写入结果 error 字段辅助排查），error 是失败原因
        """
        user_prompt = build_chapters_user_prompt(
            segment_lines=segment_lines,
            video_title=title,
            author=author,
            description=description,
        )

        raw_chapters: Any = None
        validation_error: Optional[str] = None
        max_attempts = 2  # 首次调用 + 最多 1 次语义重试

        for attempt in range(max_attempts):
            try:
                response = self.llm_client.call(
                    model=model,
                    system_prompt=CHAPTERS_SYSTEM_PROMPT,
                    user_prompt=user_prompt,
                    response_schema=CHAPTERS_SCHEMA,
                    reasoning_effort=reasoning_effort,
                    task_type="chapters",
                    force_json_mode="json_object",
                )
            except Exception as e:
                logger.error(
                    f"[CHAPTERS] LLM call failed (attempt {attempt + 1}/{max_attempts}): {e}",
                    exc_info=True,
                )
                return None, None, f"LLM call failed: {e}"

            raw_chapters = (response.structured_output or {}).get("chapters")
            normalized, validation_error = _validate_and_normalize_start_segs(
                raw_chapters, segment_count
            )

            if normalized is not None:
                if attempt > 0:
                    logger.info("[CHAPTERS] Semantic validation retry succeeded")
                return normalized, raw_chapters, None

            logger.warning(
                f"[CHAPTERS] Semantic validation failed (attempt {attempt + 1}/"
                f"{max_attempts}): {validation_error}"
            )
            if attempt + 1 < max_attempts:
                user_prompt = build_chapters_user_prompt(
                    segment_lines=segment_lines,
                    video_title=title,
                    author=author,
                    description=description,
                    retry_hint=validation_error,
                )

        return None, raw_chapters, f"Semantic validation failed after retry: {validation_error}"
