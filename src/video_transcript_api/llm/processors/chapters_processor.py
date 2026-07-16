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
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union

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


# ============================================================
# 结果类型
# ============================================================

@dataclass(frozen=True)
class Chapter:
    """单个章节。

    start_seg/end_seg 是 segments 列表里的下标（闭区间）；start_time/end_time
    是反查对应 segment 的时间戳后转换出的秒数，上游缺失时间信息时可能为 None。
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

    fingerprint 是全部 segment text 按序拼接后的 sha1，用于上层判断"原文是否变化"
    （例如决定是否可以复用缓存），segments 为 None/空时该值为 None。
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

    上游两种时间格式并存：数值型秒数，或 "HH:MM:SS"/"MM:SS" 字符串。
    保持私有 —— 这是本处理器内部的一次性兼容 shim，将来与协调器合并时
    会被替换为项目共享的时间转换工具，不对外暴露。

    Args:
        value: 原始时间值，None/数值/字符串三种之一

    Returns:
        秒数（float），无法解析或输入为 None 时返回 None
    """
    if value is None:
        return None
    if isinstance(value, bool):
        # bool 是 int 的子类，显式排除，避免 True/False 被当成 1/0 秒
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if not math.isfinite(seconds):
            logger.warning(f"[CHAPTERS] Non-finite time value, treating as None: {value!r}")
            return None
        return seconds
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            # float() happily parses "inf"/"nan" without raising -- the isfinite
            # check below is what actually rejects those, not this try/except.
            parts = [float(p) for p in raw.split(":")]
        except ValueError:
            logger.warning(f"[CHAPTERS] Unparseable time value, treating as None: {value!r}")
            return None
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + part
        if not math.isfinite(seconds):
            logger.warning(f"[CHAPTERS] Non-finite time value, treating as None: {value!r}")
            return None
        return seconds
    logger.warning(f"[CHAPTERS] Unexpected time value type {type(value)!r}, treating as None: {value!r}")
    return None


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


def _build_segment_lines(segments: List[Dict[str, Any]]) -> str:
    """把 segments 压缩为带编号的正文，供拼进 user prompt。

    每行格式：`[i] mm:ss (speaker:)? text`
    （时间缺失则省略时间部分，没有 speaker 字段则省略说话人前缀）。
    """
    lines = []
    for i, seg in enumerate(segments):
        text = (seg.get("text") or "").strip()
        timestamp = _format_timestamp(_to_seconds(seg.get("start_time")))
        speaker = seg.get("speaker")

        prefix = f"[{i}]"
        if timestamp:
            prefix += f" {timestamp}"
        if speaker:
            prefix += f" {speaker}:"

        lines.append(f"{prefix} {text}".strip())
    return "\n".join(lines)


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

    Args:
        raw_chapters: response.structured_output.get("chapters") 的原始返回
        segment_count: 输入 segments 的总条数（N），用于范围校验

    Returns:
        (normalized, error):
        - 成功：normalized 是去重 + 钳制后的 [{"title","gist","start_seg"}, ...]
          列表（按 start_seg 升序），error 为 None
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

        # 门控 1：没有可用的分段时间轴，无法生成章节（正常路径，非失败）
        if not segments:
            logger.info("[CHAPTERS] No segments provided, skipping (no timeline)")
            return ChaptersResult(
                chapters=[], status=ChaptersStatus.SKIPPED_NO_TIMELINE,
                error=None, fingerprint=None, segment_count=0,
            )

        segment_count = len(segments)
        full_text = "".join((seg.get("text") or "") for seg in segments)
        fingerprint = hashlib.sha1(full_text.encode("utf-8")).hexdigest()

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

        # 门控 4：原文过长，直接判失败而不是把超大输入硬塞给模型
        if len(full_text) > self.config.max_chapters_input_chars:
            error = (
                f"Input too long for chapters: {len(full_text)} chars > "
                f"max_chapters_input_chars={self.config.max_chapters_input_chars}"
            )
            logger.error(f"[CHAPTERS] {error}")
            return ChaptersResult(
                chapters=[], status=ChaptersStatus.FAILED,
                error=error, fingerprint=fingerprint, segment_count=segment_count,
            )

        logger.info(
            f"[CHAPTERS] Generating chapters for {segment_count} segments "
            f"(text length: {len(full_text)})"
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

        # 步骤 2：压缩输入 + 调用 LLM（内含语义校验失败时的单次重试）
        segment_lines = _build_segment_lines(segments)
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

        # 步骤 5：合并相邻同名章节 + 密度 warning（基于合并后的最终章节计算）
        merged = _merge_adjacent_same_title(normalized)
        _warn_if_density_out_of_range(merged)

        final_chapters = [
            Chapter(
                index=i,
                title=chapter["title"],
                gist=chapter["gist"],
                start_seg=chapter["start_seg"],
                end_seg=chapter["end_seg"],
                start_time=chapter["start_time"],
                end_time=chapter["end_time"],
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
