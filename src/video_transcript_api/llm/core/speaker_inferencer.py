"""说话人推断器

核心策略：按说话人采样（而非全局前 N 字符截断），确保晚出场的说话人也能拿到
足够的发言样本用于 LLM 推断，并附带其首次出场前的上下文（他人称呼线索）。

推断结果携带 confidence，低于阈值的映射不采用真实姓名，而是降级为
"说话人N" 占位符，避免把低置信度的猜测当作确定结论展示给用户。
"""

import hashlib
import json
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

    @staticmethod
    def resolve_dialog_speaker(dialog: Dict) -> Optional[object]:
        """解析单条 dialog 的说话人标签，遍历 speaker/spk/speaker_id 别名链，
        is not None 判定（保留数字 0 这种合法但 falsy 的说话人 ID，不会被
        误判成"无标签"）。未命中任何别名字段时返回 None。

        供本类 input_fingerprint/extract_speaker_labels 与
        SpeakerAwareProcessor._coerce_dialogs 共用同一份别名解析逻辑（本地
        codex review 第 7 轮 H7），避免三处各自手写同一段 if/for 链、未来
        改动其中一处忘了同步另外两处。
        """
        for field in ("speaker", "spk", "speaker_id"):
            if field in dialog and dialog[field] is not None:
                return dialog[field]
        return None

    @staticmethod
    def resolve_dialog_text(dialog: Dict) -> Optional[object]:
        """解析单条 dialog 的文本内容，遍历 text/content/transcript 别名链。
        未命中任何别名字段时返回 None。与 resolve_dialog_speaker 同一用途
        说明（本地 codex review 第 7 轮 H7）。
        """
        text = dialog.get("text")
        if text is None:
            text = dialog.get("content")
        if text is None:
            text = dialog.get("transcript")
        return text

    @staticmethod
    def extract_speaker_labels(dialogs: List[Dict]) -> List[str]:
        """从原始（未经 SpeakerAwareProcessor._coerce_dialogs 规范化的）
        对话列表提取稳定的说话人标签集合，按首次出现顺序去重。

        跳过空文本 dialog（本地 codex review 第 7 轮 H7）：判定口径
        `resolve_dialog_text(...) in (None, "")` 与 _coerce_dialogs（写侧/
        落盘路径，SpeakerAwareProcessor.process() 用 _coerce_dialogs 之后
        的结果推导 speakers 列表）完全一致——否则同一份 dialogs 在读侧
        （本方法，供 transcription.py 的分层缓存预检计算 input_fingerprint
        用）与写侧算出的说话人集合会不一致：某个说话人只在空文本 dialog
        里出现时，读侧此前会把它算进去、写侧因为空文本先被过滤掉不会，
        两侧的 input_fingerprint 永久不同——同一份输入的预检天然判定为
        "从未出现过"，本该命中缓存的请求每次都重新触发一轮说话人推断
        LLM 调用，并用这轮结果覆写已经落盘的产物。

        也是写侧（SpeakerAwareProcessor.process()）在拿到 _coerce_dialogs
        结果后推导 speakers 列表的同一个实现：coerce 后的 dialog 只剩
        "speaker"/"text" 两个规范字段且 text 已保证非空，本方法在这份
        输入上的行为与直接从 coerced 列表取 "speaker" 字段完全等价，
        统一成单一实现供两侧调用，不再各自维护一份可能悄悄漂移的逻辑。

        Returns:
            list[str]: 按首次出现顺序去重的说话人标签（已 str() 转换）。
        """
        labels = []
        for item in dialogs:
            if not isinstance(item, dict):
                continue
            text = SpeakerInferencer.resolve_dialog_text(item)
            if text in (None, ""):
                continue
            speaker = SpeakerInferencer.resolve_dialog_speaker(item)
            if speaker is not None:
                labels.append(str(speaker))
        return list(dict.fromkeys(labels))

    @staticmethod
    def input_fingerprint(speakers: List[str], dialogs: List[Dict]) -> str:
        """Hash the diarization inputs that make a mapping reusable."""
        normalized_dialogs = []
        for value in dialogs:
            if not isinstance(value, dict):
                continue
            speaker = SpeakerInferencer.resolve_dialog_speaker(value)
            text = SpeakerInferencer.resolve_dialog_text(value)
            if text in (None, ""):
                continue
            normalized_dialogs.append({
                "speaker": str(speaker) if speaker is not None else "unknown",
                "text": str(text),
                "start_time": value.get("start_time", value.get("start")),
                "end_time": value.get("end_time", value.get("end")),
            })
        canonical = {
            "speakers": [str(value) for value in speakers],
            "dialogs": normalized_dialogs,
        }
        encoded = json.dumps(
            canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

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
        allow_llm: bool = True,
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
                "meta": {label: {"name", "confidence", "applied", "sampled"}},  # 每个 label 的推断细节
                "low_confidence": [label, ...],       # 被降级/未采样的 label 列表
                "source": "llm" | "cache_hit" | "identity_fallback",  # 本轮映射的真实来源
            }
            meta 中 "sampled" 标记该 label 是否被实际采样并送入 LLM 判断过：
            False 表示预算裁剪或无有效发言，从未获得任何推断依据，"name"
            即原始标签本身；True 表示送入过 LLM，"applied"/"confidence"
            才反映真实的置信度门控结果。

            "source"（本地 codex review 第 6 轮 G4）：调用方（llm_ops.
            _refresh_speaker_names_in_existing_structured_artifact，"补层
            刷新"）需要区分本轮映射是不是一次真实、可信的更新，才能决定是否
            用它覆盖既有展示产物里已经有的好名字：
            - "llm"：本轮真实调用 LLM 并成功产出了新映射（已经落盘到
              speaker_mapping.json，见下方 save_speaker_mapping 调用）。只有
              这个来源才应当触发下游的展示刷新。
            - "cache_hit"：命中此前已持久化的映射（本身也是历史上某次
              "llm"来源的产物，见 CacheManager.get_speaker_mapping 的读侧
              校验——非 "llm" 来源的缓存文件已被当缓存未命中处理），本轮
              没有发起任何新的 LLM 调用。既有展示产物理论上早已与它一致，
              不需要因为一次缓存命中就重新刷新一遍。
            - "identity_fallback"：没有任何真实推断依据——说话人列表为空、
              allow_llm=False、没有有效发言样本可采样、或 LLM 调用本身抛出
              异常（网络抖动/限流/超时等瞬时故障）。这几种情况原始产出都
              退化为"标签本身"，绝不能被下游拿来覆盖既有的好名字——瞬时
              故障不该把已经展示的真名替换成「说话人1」占位符。
        """
        if not speakers:
            logger.warning("Speaker list is empty, skipping inference")
            return self._identity_fallback(speakers)

        input_fingerprint = self.input_fingerprint(speakers, dialogs)

        # 缓存命中校验：缓存 mapping 必须覆盖当前 speakers 集合才能复用，
        # 否则说明本次转录出现了缓存里没有的新说话人，必须重新推断。
        if self.cache_manager and platform and media_id:
            cached = self.cache_manager.get_speaker_mapping(
                platform,
                media_id,
                input_fingerprint=input_fingerprint,
                speakers=speakers,
            )
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
                    # 缓存里的 meta 可能来自"从未采样、直接 identity fallback"
                    # 的说话人（sampled=False），必须原样保留这个标记一起重放，
                    # 否则这些说话人会被当成"送入过 LLM 但 confidence 缺失"，
                    # 走错路径降级成「说话人N」。缺失该字段的旧条目默认按
                    # True（已采样）处理，保持原有的阈值重判行为不变。
                    sampled = {
                        speaker: cached_meta[speaker].get("sampled", True)
                        for speaker in speakers
                    }
                    cache_hit_result = self._apply_confidence_gate(
                        speakers=speakers,
                        raw_mapping=raw_mapping,
                        confidence_by_speaker=confidence_by_speaker,
                        sampled=sampled,
                    )
                    # 命中缓存、未发起新的 LLM 调用——不是本轮真实推断，见
                    # infer() 顶部 docstring "source" 一节。
                    cache_hit_result["source"] = "cache_hit"
                    return cache_hit_result
                logger.info(
                    f"Cached speaker_mapping does not cover current speakers "
                    f"({platform}/{media_id}), ignoring stale cache and re-inferring"
                )

        if not allow_llm:
            logger.info("Speaker name inference disabled; using generic labels")
            return self._identity_fallback(speakers)

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

            # confidence 只对实际送入 LLM 的说话人（sample_groups 的 key，
            # 与 prompt 里的 original_speakers 一致）求解——被预算/无有效
            # 发言排除的说话人压根没被问过，对它们调用 _resolve_confidence
            # 只会产生误导性的"缺失 confidence"告警。它们的最终归宿由下面
            # 的 sampled 标记决定，不经过置信度门槛判断。
            confidence_by_speaker = self._resolve_confidence(
                raw_confidence, list(sample_groups.keys())
            )
            sampled = {speaker: speaker in sample_groups for speaker in speakers}

            inference_result = self._apply_confidence_gate(
                speakers=speakers,
                raw_mapping=raw_mapping,
                confidence_by_speaker=confidence_by_speaker,
                sampled=sampled,
            )
            # 本轮真实调用了 LLM 并成功产出新映射——见 infer() 顶部 docstring
            # "source" 一节，唯一允许下游"补层刷新"触碰既有展示产物的来源。
            inference_result["source"] = "llm"

            # 缓存结果（新格式：mapping + meta + low_confidence）
            if self.cache_manager and platform and media_id:
                self.cache_manager.save_speaker_mapping(
                    platform,
                    media_id,
                    inference_result,
                    input_fingerprint=input_fingerprint,
                    speakers=speakers,
                    source="llm",
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
        sampled: Optional[Dict[str, bool]] = None,
    ) -> Dict:
        """按置信度阈值决定是否采用推断姓名，低于阈值降级为「说话人N」

        Args:
            sampled: 每个 speaker 是否被实际采样并送入 LLM 判断过。为 False
                （预算裁剪或压根没有有效发言）的 speaker 从未获得任何推断
                依据，必须强制走 identity fallback（保留原始标签），不能
                和"送入 LLM 但 confidence 缺失/无法解析"混用同一条降级为
                「说话人N」占位符的路径——前者是压根没问过，后者是 LLM
                给出了不确定的判断，两者对用户的含义完全不同。
                缺省（None）视为全员已采样，兼容不需要区分的旧调用方。

        Returns:
            {"mapping": {...}, "meta": {...}, "low_confidence": [...]}
        """
        mapping: Dict[str, str] = {}
        meta: Dict[str, Dict] = {}
        low_confidence: List[str] = []

        for speaker in speakers:
            was_sampled = True if sampled is None else sampled.get(speaker, True)

            if not was_sampled:
                mapping[speaker] = speaker
                low_confidence.append(speaker)
                meta[speaker] = {
                    "name": speaker,
                    "confidence": self._UNRESOLVABLE_CONFIDENCE_DEFAULT,
                    "applied": False,
                    "sampled": False,
                }
                logger.info(
                    f"Speaker '{speaker}' was never sampled/sent to the LLM "
                    "(excluded by budget or no valid dialog), keeping original label"
                )
                continue

            inferred_name = raw_mapping.get(speaker, speaker)
            # 正常路径下 confidence_by_speaker 一定覆盖每个 speaker（由
            # _resolve_confidence(raw_confidence, ...) 或缓存重放构造），
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
                "sampled": True,
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
        """无法推断时的兜底：所有说话人使用原始标签，标记为未采信

        meta 里附带 "sampled": False，与 _apply_confidence_gate 的同名字段
        保持形状一致——虽然这两条路径的结果目前都不会被写入缓存（调用方
        没有传 platform/media_id 走到这里，或者是异常兜底），但形状统一
        能避免未来读取 meta 的代码要额外处理"某些路径没有这个 key"。

        统一在这里标记 "source": "identity_fallback"（本地 codex review
        第 6 轮 G4），而不是让 infer() 的每个调用点各自补一遍：这个方法是
        speakers 为空、allow_llm=False、无有效采样样本、LLM 调用异常这
        四条路径唯一共用的出口，集中标记既不遗漏也不会几处代码各写一遍、
        将来漂移。调用方（llm_ops._refresh_speaker_names_in_existing_
        structured_artifact）据此拒绝用这类没有真实推断依据的结果覆盖既有
        展示产物——尤其是"LLM 调用异常"这条路径，瞬时故障不该把已经展示的
        真名替换成占位符。
        """
        return {
            "mapping": {speaker: speaker for speaker in speakers},
            "meta": {
                speaker: {
                    "name": speaker,
                    "confidence": 0.0,
                    "applied": False,
                    "sampled": False,
                }
                for speaker in speakers
            },
            "low_confidence": list(speakers),
            "source": "identity_fallback",
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
