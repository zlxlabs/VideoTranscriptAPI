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

        # ci-gate review（第5轮）：这里必须用 sample_groups 的 key（预算裁剪后
        # 保留的说话人），而不是原始完整的 speakers 列表。_apply_global_sample_budget
        # 已经把采样片段区控制在预算内，但如果标签列表本身仍塞入全部 speakers，
        # 光是 ', '.join() 这一行在虚假说话人成百上千时也能贡献上万字符，
        # 预算形同虚设。被裁掉的说话人在采样区没有任何文本，让 LLM 对着一个
        # 完全没有上下文的陌生标签猜真名没有意义。sample_groups 在
        # _apply_global_sample_budget 内部已按「首次出场时间」顺序写入，
        # 直接取其 key 顺序即可，无需重新排序。
        #
        # 注意：_apply_confidence_gate 仍遍历完整 speakers 列表组装最终结果，
        # 这一处不受影响——被预算裁掉的说话人依旧会出现在 mapping/meta/
        # low_confidence 里，走「无采样样本」的既有兜底路径。
        user_prompt = build_speaker_inference_user_prompt(
            context_snippets=context_snippets,
            original_speakers=list(sample_groups.keys()),
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

        背景：diarization 切分错误可能产生几十甚至上千个虚假说话人标签，若
        每人都累加到 max_chars_per_speaker 上限，总量会随说话人数线性膨胀，
        导致 prompt 达到数十万字符（上下文超限或 token 成本失控）。

        ci-gate review 指出：早期实现只累加 own_samples + context_before 的
        裸文本长度作为"该说话人占用的预算"，但真正写入 prompt 的是
        _format_sample_dialogs 的渲染结果，它还会给每个说话人加上分组头、
        时间戳标注、小节标题、发言前缀、列表符号等结构性文本。当说话人数量
        异常多且每人文本很短时，这部分固定渲染开销本身就能让实际 prompt
        远超预算，而裸文本长度完全没算到这部分。

        修复：预算判断改用 _render_speaker_segment 渲染出的真实片段长度
        （与 _format_sample_dialogs 复用同一份渲染逻辑），而不是自行估算
        渲染格式的开销——避免"模板改了、预算估算跟着漂移"的脆弱性。
        _format_sample_dialogs 用 "\n\n" 拼接各说话人片段，因此除第一个
        保留的说话人外，每多保留一个都要把这个分隔符的长度也计入预算，
        这样累加出的 total_chars 精确等于最终拼接字符串（rstrip 之前）的
        长度，不是近似值。

        裁剪策略保持简单：按「说话人首次出现时间」顺序遍历（与
        _format_sample_dialogs 的排序一致），逐个说话人累加其渲染片段的
        真实字符数；一旦加入某说话人会超出全局预算，该说话人及之后（更晚
        出场）的说话人全部停止采样——不做部分截断，也不做跨说话人的动态
        再分配。

        取舍：这意味着预算耗尽时最晚出场的说话人最先被裁掉，是刻意接受的
        简单降级。被裁掉的说话人不在返回结果中，调用方（_extract_sample_dialogs
        的既有兜底路径）会将其视为"无采样样本"，从 prompt 中剔除该说话人，
        交由 LLM 依赖其余上下文推断或保留原始标签——复用 P1b 已有逻辑，
        不新造一套裁剪后的展示方式。

        复杂度：每个说话人的渲染片段只在本函数中计算一次（O(1) 增量，不
        依赖其他说话人的片段），整体是 O(n)（n 为说话人总数），不会因为
        说话人数量达到几百上千而出现 O(n²) 的重复渲染开销。

        Args:
            sample_groups: 裁剪前的按说话人采样结果（键为 speaker，值同
                _extract_sample_dialogs 返回结构）

        Returns:
            裁剪后的采样结果；对其调用 _format_sample_dialogs 得到的最终
            渲染字符串长度（rstrip 之前）不超过 max_total_sample_chars
        """
        ordered = sorted(
            sample_groups.items(), key=lambda kv: kv[1].get("first_seen_index", 0)
        )

        kept: Dict[str, Dict] = {}
        total_chars = 0
        for speaker, info in ordered:
            segment = self._render_speaker_segment(speaker, info)
            # _format_sample_dialogs 用 "\n\n" 连接各片段：第一个保留的
            # 说话人不需要前置分隔符，之后每个都要多算 2 个字符，才能让
            # total_chars 精确对齐最终拼接结果的长度。
            separator_chars = 2 if kept else 0
            speaker_chars = separator_chars + len(segment)
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

    def _render_speaker_segment(self, speaker: str, info: Dict) -> str:
        """渲染单个说话人在 prompt 中对应的文本片段

        从 _format_sample_dialogs 中拆出：该方法按说话人分组渲染、组间无
        交叉引用（不依赖其他说话人的渲染结果），因此可以把"单个说话人的
        渲染片段"作为独立单元，同时供 _format_sample_dialogs 拼接最终
        prompt、供 _apply_global_sample_budget 计算该说话人真实占用的预算
        ——两处用同一份格式化逻辑，保证"预算算的就是最终写入 prompt 的
        内容"，不会因为渲染模板改动而跟着漂移。

        Args:
            speaker: 说话人标签
            info: 该说话人的采样信息（_extract_sample_dialogs 返回结构中的
                单个 value：first_seen_time / context_before / own_samples）

        Returns:
            该说话人对应的渲染文本（多行，不含片段间分隔空行）
        """
        lines = []
        first_seen_time = info.get("first_seen_time")
        header = f"[{speaker}]"
        if first_seen_time:
            header += f"（首次出现于 {first_seen_time}）"
        lines.append(header)

        context = info.get("context_before") or []
        if context:
            lines.append("  上下文（出场前他人发言，可能包含称呼线索）：")
            for ctx_speaker, ctx_text in context:
                lines.append(f"    [{ctx_speaker}]: {ctx_text}")

        own_samples = info.get("own_samples") or []
        if own_samples:
            lines.append("  本人发言样本：")
            for text in own_samples:
                lines.append(f"    - {text}")

        return "\n".join(lines)

    def _format_sample_dialogs(self, sample_groups: Dict[str, Dict]) -> str:
        """将按说话人采样结果格式化为 prompt 文本

        按「说话人首次出现时间」排序分组，每组标注首次出现时间戳（如有）、
        出场前上下文（他人称呼线索）与本人发言样本。各说话人的渲染片段由
        _render_speaker_segment 生成（与 _apply_global_sample_budget 的预算
        计算复用同一份逻辑），此处只负责按顺序拼接。

        Args:
            sample_groups: _extract_sample_dialogs 的输出

        Returns:
            格式化的对话文本
        """
        ordered = sorted(
            sample_groups.items(),
            key=lambda kv: kv[1].get("first_seen_index", 0),
        )

        segments = [
            self._render_speaker_segment(speaker, info) for speaker, info in ordered
        ]
        return "\n\n".join(segments).rstrip()

    # ------------------------------------------------------------------
    # confidence 解析与降级
    # ------------------------------------------------------------------

    # 缺失/无法解析的 confidence 找不到任何依据判断"该采信"，因此按此值
    # （低于任何合理阈值）处理，而不是按 1.0（最高置信度）处理——ci-gate
    # review（第5轮）指出，这个功能存在的意义就是"不确定的时候不要强行
    # 套用推断结果"，缺失/无法解析恰恰是最不确定的情况，若默认按满分
    # 采信，等于反向违背了这个功能本身的初衷。
    _UNRESOLVABLE_CONFIDENCE_DEFAULT = 0.0

    def _resolve_confidence(self, confidence_raw, labels: List[str]) -> Dict[str, float]:
        """解析 LLM 返回的 confidence 字段

        兼容两种格式：
        - per-speaker dict（schema 定义的标准格式）：{label: number}
        - 整体标量（部分模型可能不严格遵循 schema）：number，应用到所有 speaker

        缺失/类型错误等「无法解析」的情况按 _UNRESOLVABLE_CONFIDENCE_DEFAULT
        （低置信度，会触发降级为占位符）处理并记录 warning。数值本身合法但
        超出 [0,1] 范围的，走 clamp（不属于「无法解析」，保留原有行为）。
        """
        if isinstance(confidence_raw, dict):
            return {
                label: self._coerce_confidence_value(confidence_raw.get(label), label)
                for label in labels
            }

        if isinstance(confidence_raw, (int, float)) and not isinstance(confidence_raw, bool):
            value = self._coerce_confidence_value(confidence_raw, "<all speakers>")
            return {label: value for label in labels}

        default = self._UNRESOLVABLE_CONFIDENCE_DEFAULT
        if confidence_raw is not None:
            logger.warning(
                f"Unrecognized confidence format ({type(confidence_raw).__name__}), "
                f"treating all speakers as low confidence ({default}) -- downgraded to placeholder"
            )
        else:
            logger.warning(
                "Missing confidence field in LLM response, treating all speakers as low "
                f"confidence ({default}) -- downgraded to placeholder"
            )
        return {label: default for label in labels}

    @classmethod
    def _coerce_confidence_value(cls, value, label: str) -> float:
        """将单个 confidence 值规范化到 [0,1]

        缺失或无法解析成浮点数时按 _UNRESOLVABLE_CONFIDENCE_DEFAULT（低置信度）
        处理并记录 warning，触发后续降级为占位符的保守路径。数值本身能解析出来
        但超出 [0,1] 范围的，clamp 到边界（不属于"无法解析"，保留原有行为）。
        """
        default = cls._UNRESOLVABLE_CONFIDENCE_DEFAULT
        if value is None:
            logger.warning(
                f"Missing confidence for speaker '{label}', treating as low confidence "
                f"({default}) -- downgraded to placeholder"
            )
            return default
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            logger.warning(
                f"Invalid confidence value for speaker '{label}': {value!r}, treating as low "
                f"confidence ({default}) -- downgraded to placeholder"
            )
            return default
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
            # 正常路径下 confidence_by_speaker 一定覆盖每个 speaker（由
            # _resolve_confidence(raw_confidence, speakers) 或缓存重放构造），
            # 这里的 .get 默认值只是防御性兜底；同样按低置信度处理，避免
            # 万一真的缺键也被误判为满分采信。
            confidence = confidence_by_speaker.get(
                speaker, self._UNRESOLVABLE_CONFIDENCE_DEFAULT
            )
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
