"""说话人推断器

核心策略：按说话人采样（而非全局前 N 字符截断），确保晚出场的说话人也能拿到
足够的发言样本用于 LLM 推断，并附带其首次出场前的上下文（他人称呼线索）。

推断结果携带 confidence，低于阈值的映射不采用真实姓名，而是降级为
"说话人N" 占位符，避免把低置信度的猜测当作确定结论展示给用户。
"""

import re
from typing import Dict, List, Optional, Tuple

from ...utils.logging import setup_logger
from .llm_client import LLMClient
from .cache_manager import CacheManager
from .key_info_extractor import KeyInfo
from ..prompts import (
    SPEAKER_INFERENCE_SYSTEM_PROMPT,
    build_speaker_inference_user_prompt,
)
from ..prompts.schemas.speaker_mapping import SPEAKER_MAPPING_SCHEMA

logger = setup_logger(__name__)


class SpeakerInferencer:
    """说话人推断器"""

    # 单条采样文本的截断上限（字符数）。未纳入外部配置——过细粒度，固定即可。
    _MAX_CHARS_PER_SAMPLE = 120

    def __init__(
        self,
        llm_client: LLMClient,
        cache_manager: Optional[CacheManager] = None,
        model: str = "claude-3-5-sonnet",
        reasoning_effort: Optional[str] = None,
        samples_per_speaker: int = 3,
        max_chars_per_speaker: int = 400,
        context_dialogs: int = 2,
        confidence_threshold: float = 0.6,
        max_total_sample_chars: int = 8000,
    ):
        """初始化说话人推断器

        Args:
            llm_client: LLM 客户端
            cache_manager: 缓存管理器（可选）
            model: 使用的模型
            reasoning_effort: reasoning effort 参数
            samples_per_speaker: 每个说话人采样的发言条数上限（默认 3）
            max_chars_per_speaker: 每个说话人采样文本的总字符上限（默认 400）
            context_dialogs: 说话人首次出场前，采集他人发言作为上下文的条数（默认 2）
            confidence_threshold: 置信度阈值，低于此值不采用推断姓名（默认 0.6）
            max_total_sample_chars: 所有说话人采样文本合计的全局字符上限（默认 8000）。
                防止 diarization 切分错误产生大量虚假说话人标签时，
                单人 max_chars_per_speaker 上限仍因人数膨胀导致 prompt 总量失控
        """
        self.llm_client = llm_client
        self.cache_manager = cache_manager
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.samples_per_speaker = samples_per_speaker
        self.max_chars_per_speaker = max_chars_per_speaker
        self.context_dialogs = context_dialogs
        self.confidence_threshold = confidence_threshold
        self.max_total_sample_chars = max_total_sample_chars

    def infer(
        self,
        speakers: List[str],
        dialogs: List[Dict[str, str]],
        title: str,
        author: str = "",
        description: str = "",
        key_info: Optional[KeyInfo] = None,
        platform: str = "",
        media_id: str = "",
    ) -> Dict:
        """推断说话人真实姓名

        Args:
            speakers: 说话人 ID 列表（如 ["Speaker1", "Speaker2"]）
            dialogs: 对话列表（每项包含 speaker, text, start_time）
            title: 视频标题
            author: 作者/频道
            description: 视频描述
            key_info: 关键信息（可选，用于辅助推断）
            platform: 平台标识（用于缓存）
            media_id: 媒体 ID（用于缓存）

        Returns:
            推断结果字典：
            {
                "mapping": {label: 展示名},          # 实际应用的映射，低置信度已降级为"说话人N"
                "meta": {label: {"name", "confidence", "applied"}},  # 每个 label 的推断细节
                "low_confidence": [label, ...],       # 被降级的 label 列表
            }
        """
        if not speakers:
            logger.warning("Speaker list is empty, skipping inference")
            return {"mapping": {}, "meta": {}, "low_confidence": []}

        # 缓存命中校验：缓存 mapping 必须覆盖当前 speakers 集合才能复用，
        # 否则说明本次转录出现了缓存里没有的新说话人，必须重新推断。
        if self.cache_manager and platform and media_id:
            cached = self.cache_manager.get_speaker_mapping(platform, media_id)
            if cached:
                normalized_cache = self._normalize_cached_result(cached)
                if normalized_cache and set(normalized_cache["mapping"].keys()) >= set(speakers):
                    logger.info(f"Retrieved speaker_mapping from cache: {platform}/{media_id}")
                    # ci-gate review：缓存里的 mapping 是写入时那次调用的
                    # confidence_threshold 门控结果；用户之后调高/调低阈值
                    # 配置不该被已缓存内容悄悄绕过。meta 里存了每个 speaker
                    # 的原始 name/confidence，用当前（可能已变化的）阈值重跑
                    # 一遍既有的 _apply_confidence_gate，不发起任何新的 LLM
                    # 调用，纯本地重新判定。
                    cached_meta = normalized_cache["meta"]
                    raw_mapping = {
                        speaker: cached_meta[speaker]["name"] for speaker in speakers
                    }
                    confidence_by_speaker = {
                        speaker: cached_meta[speaker]["confidence"] for speaker in speakers
                    }
                    return self._apply_confidence_gate(
                        speakers=speakers,
                        raw_mapping=raw_mapping,
                        confidence_by_speaker=confidence_by_speaker,
                    )
                logger.info(
                    f"Cached speaker_mapping does not cover current speakers "
                    f"({platform}/{media_id}), ignoring stale cache and re-inferring"
                )

        # 按说话人采样：每人取全时间轴上的前 K 条发言 + 首次出场上下文
        sample_groups = self._extract_sample_dialogs(dialogs, speakers)

        if not sample_groups:
            logger.warning("No valid dialog samples, cannot infer speakers")
            return self._identity_fallback(speakers)

        logger.info(f"Inferring speakers using LLM: {speakers}")

        context_snippets = self._format_sample_dialogs(sample_groups)

        user_prompt = build_speaker_inference_user_prompt(
            context_snippets=context_snippets,
            original_speakers=speakers,
            video_title=title,
            author=author,
            description=description,
        )

        try:
            result = self.llm_client.call(
                model=self.model,
                system_prompt=SPEAKER_INFERENCE_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                response_schema=SPEAKER_MAPPING_SCHEMA,
                reasoning_effort=self.reasoning_effort,
                task_type="speaker_inference",
            )

            raw_mapping = dict(result.structured_output.get("speaker_mapping") or {})
            raw_confidence = result.structured_output.get("confidence")

            # 确保所有 speaker 都有推断名称：缺失则回退为原始标签
            for speaker in speakers:
                raw_mapping.setdefault(speaker, speaker)

            confidence_by_speaker = self._resolve_confidence(raw_confidence, speakers)

            inference_result = self._apply_confidence_gate(
                speakers=speakers,
                raw_mapping=raw_mapping,
                confidence_by_speaker=confidence_by_speaker,
            )

            # 缓存结果（新格式：mapping + meta + low_confidence）
            if self.cache_manager and platform and media_id:
                self.cache_manager.save_speaker_mapping(
                    platform, media_id, inference_result
                )
                logger.info(f"Speaker_mapping cached: {platform}/{media_id}")

            logger.info(f"Speaker inference completed: {inference_result['mapping']}")
            logger.debug(f"[SPEAKER_INFERENCE] Result for {title}: {inference_result['meta']}")

            return inference_result

        except Exception as e:
            logger.error(f"Speaker inference failed: {e}")
            return self._identity_fallback(speakers)

    # ------------------------------------------------------------------
    # 采样（按说话人）
    # ------------------------------------------------------------------

    def _extract_sample_dialogs(
        self, dialogs: List[Dict[str, str]], speakers: List[str]
    ) -> Dict[str, Dict]:
        """按说话人提取对话样本（而非全局前 N 字符截断）

        对每个说话人：
        - 采集其在全时间轴上前 samples_per_speaker 条发言（每条截断到
          _MAX_CHARS_PER_SAMPLE 字符，该说话人总采样不超过 max_chars_per_speaker）
        - 采集其首次发言前的 context_dialogs 条「其他人」的发言作为上下文
          （谁称呼/提及了这个人，是最强的身份信号）

        最后按 max_total_sample_chars 对所有说话人的采样总量施加全局上限
        （见 _apply_global_sample_budget），防止说话人数量异常多时 prompt 失控。

        Args:
            dialogs: 完整对话列表（按时间顺序）
            speakers: 需要采样的说话人列表

        Returns:
            {speaker: {
                "first_seen_index": int,
                "first_seen_time": Optional[str],   # HH:MM:SS，缺失则为 None
                "own_samples": [str, ...],
                "context_before": [(speaker, text), ...],  # 按时间顺序
            }}
            只包含至少有一条有效发言样本的说话人。
        """
        speaker_set = set(speakers)
        first_seen_index: Dict[str, int] = {}
        first_seen_time: Dict[str, Optional[str]] = {}
        context_before: Dict[str, List[Tuple[str, str]]] = {}
        own_samples: Dict[str, List[str]] = {speaker: [] for speaker in speakers}
        own_chars: Dict[str, int] = {speaker: 0 for speaker in speakers}

        for idx, dialog in enumerate(dialogs):
            speaker = dialog.get("speaker", "")
            text = (dialog.get("text") or "").strip()
            if not text or speaker not in speaker_set:
                continue

            if speaker not in first_seen_index:
                first_seen_index[speaker] = idx
                first_seen_time[speaker] = self._format_timestamp(dialog.get("start_time"))
                context_before[speaker] = self._collect_context_before(dialogs, idx, speaker)

            self._try_add_sample(speaker, text, own_samples, own_chars)

        sample_groups = {}
        for speaker in speakers:
            if not own_samples[speaker]:
                continue
            sample_groups[speaker] = {
                "first_seen_index": first_seen_index.get(speaker, len(dialogs)),
                "first_seen_time": first_seen_time.get(speaker),
                "own_samples": own_samples[speaker],
                "context_before": context_before.get(speaker, []),
            }

        return self._apply_global_sample_budget(sample_groups)

    def _apply_global_sample_budget(self, sample_groups: Dict[str, Dict]) -> Dict[str, Dict]:
        """对采样总字符数施加全局上限，防止说话人数量异常多时 prompt 失控

        背景：diarization 切分错误可能产生几十个虚假说话人标签，若每人都
        累加到 max_chars_per_speaker 上限，总量会随说话人数线性膨胀，导致
        prompt 达到数十万字符（上下文超限或 token 成本失控）。

        裁剪策略保持简单：按「说话人首次出现时间」顺序遍历（与
        _format_sample_dialogs 的排序一致），逐个说话人累加其采样字符数；
        一旦加入某说话人会超出全局预算，该说话人及之后（更晚出场）的说话人
        全部停止采样——不做部分截断，也不做跨说话人的动态再分配。

        取舍：这意味着预算耗尽时最晚出场的说话人最先被裁掉，是刻意接受的
        简单降级。被裁掉的说话人不在返回结果中，调用方（_extract_sample_dialogs
        的既有兜底路径）会将其视为"无采样样本"，从 prompt 中剔除该说话人，
        交由 LLM 依赖其余上下文推断或保留原始标签——复用 P1b 已有逻辑，
        不新造一套裁剪后的展示方式。

        Args:
            sample_groups: 裁剪前的按说话人采样结果（键为 speaker，值同
                _extract_sample_dialogs 返回结构）

        Returns:
            裁剪后的采样结果，总字符数（own_samples + context_before，两者
            都会被 _format_sample_dialogs 写入最终 prompt）不超过
            max_total_sample_chars
        """
        ordered = sorted(
            sample_groups.items(), key=lambda kv: kv[1].get("first_seen_index", 0)
        )

        kept: Dict[str, Dict] = {}
        total_chars = 0
        for speaker, info in ordered:
            # own_samples 是该说话人自己的发言样本；context_before 是首次
            # 出场前的其他说话人发言（用于辅助 LLM 推断称呼）——两者都会被
            # _format_sample_dialogs 完整写入 prompt，预算必须覆盖两者之和，
            # 否则说话人数量多、context_dialogs 配置较大时，即使 own_samples
            # 总量未超限，实际 prompt 仍可能远超 max_total_sample_chars。
            own_chars = sum(len(text) for text in info.get("own_samples") or [])
            context_chars = sum(
                len(text) for _, text in info.get("context_before") or []
            )
            speaker_chars = own_chars + context_chars
            if total_chars + speaker_chars > self.max_total_sample_chars:
                logger.warning(
                    f"Global sample char budget ({self.max_total_sample_chars}) exhausted "
                    f"at speaker '{speaker}'; dropping it and all later-appearing speakers "
                    f"from sampling ({len(ordered) - len(kept)} speaker(s) affected)"
                )
                break
            kept[speaker] = info
            total_chars += speaker_chars

        return kept

    def _collect_context_before(
        self, dialogs: List[Dict[str, str]], first_index: int, speaker: str
    ) -> List[Tuple[str, str]]:
        """采集说话人首次出场前，最近的 context_dialogs 条「其他人」发言

        倒序向前查找，找到后恢复为时间顺序，便于阅读。
        """
        if self.context_dialogs <= 0:
            return []

        collected: List[Tuple[str, str]] = []
        back = first_index - 1
        while back >= 0 and len(collected) < self.context_dialogs:
            prev = dialogs[back]
            prev_speaker = prev.get("speaker", "")
            prev_text = (prev.get("text") or "").strip()
            if prev_text and prev_speaker != speaker:
                collected.append((prev_speaker, self._truncate(prev_text)))
            back -= 1

        collected.reverse()
        return collected

    def _try_add_sample(
        self,
        speaker: str,
        text: str,
        own_samples: Dict[str, List[str]],
        own_chars: Dict[str, int],
    ) -> None:
        """尝试为说话人追加一条采样文本，受条数与总字符双重上限约束"""
        if len(own_samples[speaker]) >= self.samples_per_speaker:
            return

        remaining = self.max_chars_per_speaker - own_chars[speaker]
        if remaining <= 0:
            return

        truncated = self._truncate(text)
        if len(truncated) > remaining:
            truncated = truncated[:remaining]
        if not truncated:
            return

        own_samples[speaker].append(truncated)
        own_chars[speaker] += len(truncated)

    def _truncate(self, text: str, limit: Optional[int] = None) -> str:
        """截断单条采样文本到指定上限（默认 _MAX_CHARS_PER_SAMPLE）"""
        limit = limit if limit is not None else self._MAX_CHARS_PER_SAMPLE
        if len(text) <= limit:
            return text
        return text[:limit]

    @staticmethod
    def _format_timestamp(value) -> Optional[str]:
        """将 start_time（秒级浮点/整数，或已格式化字符串）转为 HH:MM:SS

        无法解析或缺失时返回 None（prompt 渲染时省略该时间戳）。
        """
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            if re.match(r"^\d{1,2}:\d{2}:\d{2}$", stripped):
                return stripped
            try:
                value = float(stripped)
            except ValueError:
                return None
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            return None
        if seconds < 0:
            return None
        total = int(seconds)
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    def _format_sample_dialogs(self, sample_groups: Dict[str, Dict]) -> str:
        """将按说话人采样结果格式化为 prompt 文本

        按「说话人首次出现时间」排序分组，每组标注首次出现时间戳（如有）、
        出场前上下文（他人称呼线索）与本人发言样本。

        Args:
            sample_groups: _extract_sample_dialogs 的输出

        Returns:
            格式化的对话文本
        """
        ordered = sorted(
            sample_groups.items(),
            key=lambda kv: kv[1].get("first_seen_index", 0),
        )

        parts = []
        for speaker, info in ordered:
            first_seen_time = info.get("first_seen_time")
            header = f"[{speaker}]"
            if first_seen_time:
                header += f"（首次出现于 {first_seen_time}）"
            parts.append(header)

            context = info.get("context_before") or []
            if context:
                parts.append("  上下文（出场前他人发言，可能包含称呼线索）：")
                for ctx_speaker, ctx_text in context:
                    parts.append(f"    [{ctx_speaker}]: {ctx_text}")

            own_samples = info.get("own_samples") or []
            if own_samples:
                parts.append("  本人发言样本：")
                for text in own_samples:
                    parts.append(f"    - {text}")

            parts.append("")

        return "\n".join(parts).rstrip()

    # ------------------------------------------------------------------
    # confidence 解析与降级
    # ------------------------------------------------------------------

    def _resolve_confidence(self, confidence_raw, labels: List[str]) -> Dict[str, float]:
        """解析 LLM 返回的 confidence 字段

        兼容两种格式：
        - per-speaker dict（schema 定义的标准格式）：{label: number}
        - 整体标量（部分模型可能不严格遵循 schema）：number，应用到所有 speaker

        解析失败（缺失/类型错误/超出 [0,1] 范围）按 1.0 处理并记录 warning。
        """
        if isinstance(confidence_raw, dict):
            return {
                label: self._coerce_confidence_value(confidence_raw.get(label), label)
                for label in labels
            }

        if isinstance(confidence_raw, (int, float)) and not isinstance(confidence_raw, bool):
            value = self._coerce_confidence_value(confidence_raw, "<all speakers>")
            return {label: value for label in labels}

        if confidence_raw is not None:
            logger.warning(
                f"Unrecognized confidence format ({type(confidence_raw).__name__}), "
                f"defaulting all speakers to confidence=1.0"
            )
        else:
            logger.warning(
                "Missing confidence field in LLM response, defaulting all speakers to confidence=1.0"
            )
        return {label: 1.0 for label in labels}

    @staticmethod
    def _coerce_confidence_value(value, label: str) -> float:
        """将单个 confidence 值规范化到 [0,1]，解析失败按 1.0 处理并记录 warning"""
        if value is None:
            logger.warning(f"Missing confidence for speaker '{label}', defaulting to 1.0")
            return 1.0
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            logger.warning(
                f"Invalid confidence value for speaker '{label}': {value!r}, defaulting to 1.0"
            )
            return 1.0
        if parsed < 0.0 or parsed > 1.0:
            clamped = max(0.0, min(1.0, parsed))
            logger.warning(
                f"Confidence out of range for speaker '{label}': {parsed}, clamping to {clamped}"
            )
            return clamped
        return parsed

    def _apply_confidence_gate(
        self,
        speakers: List[str],
        raw_mapping: Dict[str, str],
        confidence_by_speaker: Dict[str, float],
    ) -> Dict:
        """按置信度阈值决定是否采用推断姓名，低于阈值降级为「说话人N」

        Returns:
            {"mapping": {...}, "meta": {...}, "low_confidence": [...]}
        """
        mapping: Dict[str, str] = {}
        meta: Dict[str, Dict] = {}
        low_confidence: List[str] = []

        for speaker in speakers:
            inferred_name = raw_mapping.get(speaker, speaker)
            confidence = confidence_by_speaker.get(speaker, 1.0)
            applied = confidence >= self.confidence_threshold

            if applied:
                mapping[speaker] = inferred_name
            else:
                fallback_label = self._build_fallback_label(speaker, speakers)
                mapping[speaker] = fallback_label
                low_confidence.append(speaker)
                logger.warning(
                    f"Speaker '{speaker}' inference confidence too low "
                    f"({confidence:.2f} < {self.confidence_threshold}), "
                    f"downgrading inferred name '{inferred_name}' -> '{fallback_label}'"
                )

            meta[speaker] = {
                "name": inferred_name,
                "confidence": confidence,
                "applied": applied,
            }

        return {"mapping": mapping, "meta": meta, "low_confidence": low_confidence}

    @staticmethod
    def _build_fallback_label(speaker: str, speakers: List[str]) -> str:
        """构造降级占位符「说话人N」

        N 优先取原始标签中的数字序号（如 Speaker3 -> 3）；
        标签中没有数字时，按其在 speakers 列表中的出现顺序编号（1 基）。
        """
        match = re.search(r"(\d+)", speaker)
        if match:
            n = match.group(1)
        else:
            n = str(speakers.index(speaker) + 1)
        return f"说话人{n}"

    def _identity_fallback(self, speakers: List[str]) -> Dict:
        """无法推断时的兜底：所有说话人使用原始标签，标记为未采信"""
        return {
            "mapping": {speaker: speaker for speaker in speakers},
            "meta": {
                speaker: {"name": speaker, "confidence": 0.0, "applied": False}
                for speaker in speakers
            },
            "low_confidence": list(speakers),
        }

    # ------------------------------------------------------------------
    # 缓存兼容
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_cached_result(cached: Dict) -> Optional[Dict]:
        """只认新格式缓存（含 meta），旧格式一律视为不可复用

        新格式：{"mapping": {...}, "meta": {...}, "low_confidence": [...]}

        旧格式（纯 {label: name} 字典，本 PR 引入 confidence 门槛之前写入）
        不包含任何置信度信号——旧的推断逻辑压根没有 confidence 概念。曾经
        的做法是把这类缓存整体视为 confidence=1.0 直接采信，但这只是编造
        出的信心，会让"低置信度不再强行套用真名"这条新保证对所有已缓存
        视频形同虚设（ci-gate review 指出）。这里改为直接判定旧格式不可
        复用，触发调用方重新推断——重新推断一次的 token 成本是可接受的
        一次性代价，换来的是新写入的缓存都带有真实 confidence 数据，后续
        缓存命中判定才有意义。

        Returns:
            规范化后的推断结果字典；旧格式或无法识别的结构均返回 None
        """
        if not isinstance(cached, dict):
            return None

        if isinstance(cached.get("mapping"), dict) and isinstance(cached.get("meta"), dict):
            return {
                "mapping": dict(cached.get("mapping", {})),
                "meta": dict(cached.get("meta", {})),
                "low_confidence": list(cached.get("low_confidence", [])),
            }

        return None
