"""说话人推断器

核心策略：按说话人采样（而非全局前 N 字符截断），确保晚出场的说话人也能拿到
足够的发言样本用于 LLM 推断，并附带其首次出场前的上下文（他人称呼线索）。

推断结果携带 confidence，低于阈值的映射不采用真实姓名，而是降级为
"说话人N" 占位符，避免把低置信度的猜测当作确定结论展示给用户。
"""

import hashlib
import json
import math
import re
from typing import Dict, List, Optional, Tuple

from ...utils.logging import setup_logger
from .llm_client import LLMClient
from .cache_manager import CacheManager
from .key_info_extractor import KeyInfo
from .speaker_self_intro_verifier import apply_self_intro_review
from .speaker_duo_inference import apply_duo_metadata_roles
from .speaker_confidence_level import assign_confidence_levels
from ..prompts import (
    SPEAKER_INFERENCE_SYSTEM_PROMPT,
    build_speaker_inference_user_prompt,
    build_speaker_quote_sections,
)
from ..prompts.speaker_duo_roles import SPEAKER_DUO_ROLES_INSTRUCTION
from ..prompts.schemas.speaker_mapping import (
    SPEAKER_EVIDENCE_TYPES,
    SPEAKER_MAPPING_SCHEMA,
)

logger = setup_logger(__name__)

# 证据裁决规则版本，纳入 input_fingerprint；升级后旧缓存失效重算。
# 唯一定义处。v2-v5 见历史；v6 = 本人样本自我介绍改由独立 LLM 复核；v7 = 两人访谈开放「元数据+角色」推测命名通路。
SPEAKER_EVIDENCE_RULES_VERSION = 7


def build_speaker_risk_flags(
    inference_result: Dict, base_dialogs: List[Dict], confidence_threshold: float = 0.6
) -> List[str]:
    """Build deterministic speaker risk flags from gate metadata and raw dialogs.

    ``base_dialogs`` is intentionally the pre-merge timeline so a dropped cluster's
    share reflects the evidence the gate actually saw, not a merged display count.

    schema v1 meta（无 candidates）与 v2 meta（含 candidates）按形状分叉判定
    "dropped cluster"：v1 沿用旧 confidence 门控语义（confidence < threshold 且
    未采用）；v2 的 confidence 只是 LLM 自报诊断、不再参与采用判定，被采用的
    与否由证据核对决定，因此 v2 判据是「已采样但未采用」本身——自报 confidence
    高低不改变「主要说话人被降级」这一风险事实。
    """
    meta = inference_result.get("meta") if isinstance(inference_result, dict) else {}
    if not isinstance(meta, dict):
        return []

    speaker_counts: Dict[str, int] = {}
    total_dialogs = 0
    for dialog in base_dialogs or []:
        if not isinstance(dialog, dict):
            continue
        speaker = SpeakerInferencer.resolve_dialog_speaker(dialog)
        if speaker is None:
            continue
        label = str(speaker)
        speaker_counts[label] = speaker_counts.get(label, 0) + 1
        total_dialogs += 1

    flags: List[str] = []

    def _mapping_was_adopted(label: str, details: Dict) -> bool:
        if "applied" in details:
            return bool(details.get("applied"))
        sampled = bool(details.get("sampled", True))
        confidence = details.get("confidence")
        confidence_is_valid = (
            isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
            and math.isfinite(float(confidence))
        )
        return sampled and confidence_is_valid and float(confidence) >= confidence_threshold

    def _is_v2_evidence_meta(details: Dict) -> bool:
        """schema v2 meta 条目带 candidates（写侧唯一来源 SpeakerInferencer
        的证据裁决路径总会写入，空列表也写）；v1 产物从不带。"""
        return "candidates" in details

    def _entry_confidence_valid(details: Dict) -> bool:
        confidence = details.get("confidence")
        return (
            isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
            and math.isfinite(float(confidence))
        )

    if total_dialogs:
        dropped_cluster = any(
            bool(details.get("sampled", True))
            and not _mapping_was_adopted(str(label), details)
            and (
                # v2：已采样但证据核对未通过/无佐证 → 未采用即风险，不再看
                # 自报 confidence。
                _is_v2_evidence_meta(details)
                or (
                    # v1 语义保持不变：confidence 有效且低于阈值。
                    _entry_confidence_valid(details)
                    and float(details["confidence"]) < confidence_threshold
                )
            )
            and speaker_counts.get(str(label), 0) / total_dialogs >= 0.05
            for label, details in meta.items()
            if isinstance(details, dict)
        )
        if dropped_cluster:
            flags.append("low_confidence_cluster_dropped")

    applied_confidences = [
        float(details["confidence"])
        for label, details in meta.items()
        if isinstance(details, dict)
        and _mapping_was_adopted(str(label), details)
        and isinstance(details.get("confidence"), (int, float))
        and not isinstance(details.get("confidence"), bool)
        and math.isfinite(float(details["confidence"]))
    ]
    if applied_confidences and sum(applied_confidences) / len(applied_confidences) < 0.7:
        flags.append("low_average_mapping_confidence")
    return flags


class SpeakerInferencer:
    """说话人推断器"""

    # 单条采样文本的截断上限（字符数）。未纳入外部配置——过细粒度，固定即可。
    _MAX_CHARS_PER_SAMPLE = 120

    # 发言量 top2 说话人的采样配额下限：普通说话人配置是基准，top2 的条数与
    # 字符数分别取 max(基准, 此下限)。任一基准为 0 时全员停用采样并走 identity fallback。
    _TOP_SPEAKER_QUOTA_SLOTS = 2
    _TOP_SPEAKER_SAMPLE_COUNT = 8
    _TOP_SPEAKER_MAX_CHARS = 1200

    # ------------------------------------------------------------------
    # 第三人提及旁证常量（gate 修复轮第 5 轮①）
    # ------------------------------------------------------------------

    # 呼叫-回应发现窗口：他人呼叫段 P 之后最多看 3 段。窗口内首个发言段的
    # 说话人即「应答者」，应答者必须恰好是被采样说话人本人（收紧判据，见
    # _collect_call_response_evidence docstring）。coerce 后的对话无空文本段，
    # 首个发言段即 P 的紧邻下一段；窗口常数对含空文本段的原始输入保留「3 段内」语义。
    _CALL_RESPONSE_WINDOW = 3

    # 单条旁证短句截断上限（字符）。旁证只承担「别人怎么称呼他」的指向
    # 作用，截到短句足够。
    _MAX_CHARS_PER_CORROBORATION_ENTRY = 80

    # 每说话人至多收录的旁证条数（卡面锁定：每说话人至多 2 条短句）。
    _MAX_CORROBORATION_PER_SPEAKER = 2

    # 旁证区全局条数上限。diarization 污染产生大量说话人时，旁证若无全局
    # 上限会以 2×N 膨胀；24 条 ×（≤80 字符 + 渲染标注）≈ 2500 字符，与全局
    # 采样预算（默认 8000）同量级的可预期开销。按呼叫发生时间先到先得，
    # 超限后停止收集（不裁已收录的）。
    _MAX_CORROBORATION_ENTRIES_TOTAL = 24

    # 疑似称呼 token 判据（启发式白名单，宽松过滤：多收无妨，核对侧
    # fail-closed）。不按元数据人名索引（codex 收紧要求：「Adrian Punk」与
    # 「胖哥」无字面对应）——旁证发现机制就是呼叫-回应模式本身，token
    # 判据只负责排除「纯闲聊段落」：中文常见称呼后缀（X哥/X姐/X总/老师…）、
    # 平台 @提及、昵称后缀（酱/桑/君/大大/神）。
    _MENTION_TOKEN_PATTERN = re.compile(
        r"[\u4e00-\u9fa5A-Za-z0-9_·]{1,12}(?:哥|姐|总|叔|姑|姨|爷|奶|酱|桑|君|大大|神)"
        r"|(?:老师|老板|师傅|先生|女士|同学|兄弟|老铁|大夫|教授|博士|医生|经理)"
        r"|@[\w\u4e00-\u9fa5-]{1,20}"
    )

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
    def input_fingerprint(
        speakers: List[str],
        dialogs: List[Dict],
        *,
        title: str = "",
        author: str = "",
        description: str = "",
        rules_version: int = SPEAKER_EVIDENCE_RULES_VERSION,
    ) -> str:
        """Hash the diarization inputs that make a mapping reusable.

        card-vta-261015-speaker-evidence：除说话人集合与对话内容外，还纳入
        title/author/description 与裁决规则版本。三个消费点必须同源传参：
        - SpeakerInferencer.infer() 内部（写侧/缓存查询）
        - transcription.py 分层缓存预检（读侧）
        - llm_ops.py 补层刷新/身份恢复（读侧，从 existing_snapshot 取元数据）
        任何一侧漏改都会造成读写指纹永久不一致（历史坑，见类 docstring）。
        keyword 参数带默认值（旧调用仍能工作）；rules_version 默认取当前 SPEAKER_EVIDENCE_RULES_VERSION。
        """
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
            "title": str(title or ""),
            "author": str(author or ""),
            "description": str(description or ""),
            "rules_version": int(rules_version),
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
            samples_per_speaker: 普通说话人的采样条数基准（默认 3）；top2 至少 8，0 关闭全部采样
            max_chars_per_speaker: 普通说话人的采样字符基准（默认 400）；top2 至少 1200，0 关闭全部采样
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
              "llm"来源的产物，见 CacheManager.get_speaker_mapping 的读侧校验），
              本轮没有发起任何新的 LLM 调用，也不因此重新刷新既有展示产物。
            - "identity_fallback"：没有任何真实推断依据——说话人列表为空、
              allow_llm=False、没有有效发言样本可采样、或 LLM 调用本身抛出
              异常（网络抖动/限流/超时等瞬时故障）。这几种情况原始产出都
              退化为"标签本身"，绝不能被下游拿来覆盖既有的好名字——瞬时
              故障不该把已经展示的真名替换成「说话人1」占位符。
        """
        if not speakers:
            logger.warning("Speaker list is empty, skipping inference")
            return self._identity_fallback(speakers)

        if self.samples_per_speaker == 0 or self.max_chars_per_speaker == 0:
            logger.info("Speaker sampling disabled by zero quota; using identity labels")
            return self._identity_fallback(speakers)

        input_fingerprint = self.input_fingerprint(
            speakers, dialogs, title=title, author=author, description=description
        )

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
                    if self._is_evidence_result(normalized_cache):
                        # schema v2（证据裁决）产物：mapping/applied 是写入时
                        # 引用核对的结果，而核对需要本轮 prompt 输入文本，
                        # 缓存里只有 quote 没有全文，无法离线重核——直接采用
                        # 缓存结论。裁决规则变更时指纹里的 rules_version 会
                        # 变化，旧产物自然 miss 走重算，不会套旧规则结论。
                        cache_hit_result = {
                            "mapping": dict(normalized_cache["mapping"]),
                            "meta": dict(normalized_cache["meta"]),
                            "low_confidence": list(normalized_cache["low_confidence"]),
                        }
                    else:
                        # v1（confidence 门控）产物：meta 里存了每个 speaker
                        # 的原始 name/confidence，用当前（可能已变化的）阈值重跑
                        # 一遍既有的 _apply_confidence_gate，不发起任何新的 LLM
                        # 调用，纯本地重新判定（旧降级语义，见锁定决策 4）。
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

        corroboration = self._collect_call_response_evidence(dialogs, sample_groups)
        corroboration_snippets = self._format_corroboration_section(corroboration)

        # ci-gate review（第5轮）：这里必须用 sample_groups 的 key（预算裁剪后
        # 保留的说话人），而不是原始完整的 speakers 列表。_apply_global_sample_budget
        # 已经把采样片段区控制在预算内，但标签列表若仍塞入全部 speakers，光是
        # ', '.join() 在虚假说话人成百上千时就能贡献上万字符，预算形同虚设；被裁掉
        # 的说话人在采样区没有任何文本，让 LLM 猜真名没有意义。sample_groups 已按
        # 「首次出场时间」顺序写入，直接取其 key 顺序即可。
        # 注意：_apply_confidence_gate 仍遍历完整 speakers 列表组装最终结果——
        # 被预算裁掉的说话人依旧会出现在 mapping/meta/low_confidence 里。
        user_prompt = build_speaker_inference_user_prompt(
            context_snippets=context_snippets,
            original_speakers=list(sample_groups.keys()),
            video_title=title,
            author=author,
            description=description,
            platform=platform,
            corroboration_snippets=corroboration_snippets,
        )
        # Metadata sections share the prompt builder; dialog sources below split
        # the same sampled text by context_before versus own_samples.
        quote_sections = build_speaker_quote_sections(
            video_title=title,
            author=author,
            description=description,
            context_snippets=context_snippets,
        )
        # Keep raw evidence sources separate; prompt rendering remains unchanged.
        dialog_sections_by_speaker = {
            speaker: {
                "context_before": "\n".join(text for _, text in info["context_before"]),
                "own_samples": "\n".join(info["own_samples"]),
            }
            for speaker, info in sample_groups.items()
        }

        try:
            result = self.llm_client.call(
                model=self.model,
                # roles 与 candidates 同属一次调用：角色说明拼在系统提示词尾部。
                system_prompt=SPEAKER_INFERENCE_SYSTEM_PROMPT + SPEAKER_DUO_ROLES_INSTRUCTION,
                user_prompt=user_prompt,
                response_schema=SPEAKER_MAPPING_SCHEMA,
                reasoning_effort=self.reasoning_effort,
                task_type="speaker_inference",
            )

            raw_mapping = dict(result.structured_output.get("speaker_mapping") or {})
            raw_confidence = result.structured_output.get("confidence")
            raw_candidates = result.structured_output.get("candidates")

            # 确保所有 speaker 都有推断名称：缺失则回退为原始标签
            for speaker in speakers:
                raw_mapping.setdefault(speaker, speaker)

            # confidence 只对实际送入 LLM 的说话人（sample_groups 的 key，
            # 与 prompt 里的 original_speakers 一致）求解——被预算/无有效
            # 发言排除的说话人压根没被问过，对它们调用 _resolve_confidence
            # 只会产生误导性的"缺失 confidence"告警。它们的最终归宿由下面
            # 的 sampled 标记决定，不经过置信度门槛判断。schema v2 起
            # confidence 仅作诊断值记录进 meta，不参与采用判定。
            confidence_by_speaker = self._resolve_confidence(
                raw_confidence, list(sample_groups.keys())
            )
            sampled = {speaker: speaker in sample_groups for speaker in speakers}

            inference_result = self._apply_evidence_gate(
                speakers=speakers,
                raw_mapping=raw_mapping,
                confidence_by_speaker=confidence_by_speaker,
                candidates_by_speaker=(
                    raw_candidates if isinstance(raw_candidates, dict) else {}
                ),
                sampled=sampled,
                quote_sections=quote_sections,
                dialog_sections_by_speaker=dialog_sections_by_speaker,
                corroboration_by_speaker=corroboration,
            )
            # 两人访谈「元数据+角色」推测通路（evidence_tier=duo_metadata_role）：结构前提全由
            # 程序核对；之后按证据强弱补 confidence_level/guess_name/basis（卡面接口契约）。
            inference_result = assign_confidence_levels(
                apply_duo_metadata_roles(inference_result, dialogs, title, speakers, result.structured_output.get("roles"))
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
        - 默认从全时间轴按头/中/尾分层采集 samples_per_speaker 条发言（每条
          截断到单条上限与人均预算中的较小值，该说话人总采样不超过
          max_chars_per_speaker）；当有效发言不足 K 条时按原时间顺序退化，
          保持原有单条截断行为
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
        candidate_dialogs: Dict[str, List[str]] = {speaker: [] for speaker in speakers}

        for idx, dialog in enumerate(dialogs):
            speaker = dialog.get("speaker", "")
            text = (dialog.get("text") or "").strip()
            if not text or speaker not in speaker_set:
                continue

            if speaker not in first_seen_index:
                first_seen_index[speaker] = idx
                first_seen_time[speaker] = self._format_timestamp(dialog.get("start_time"))
                context_before[speaker] = self._collect_context_before(dialogs, idx, speaker)

            candidate_dialogs[speaker].append(text)

        # candidate_dialogs 按 speakers 的首次出现顺序初始化，因此稳定排序会
        # 在发言条数并列时保留既有顺序。只排序一次，供下面每位说话人复用。
        top_speakers = self._top_sample_speakers(candidate_dialogs)

        for speaker in speakers:
            candidates = candidate_dialogs[speaker]
            # top2 各自采用配置基准与加额下限的较大值，普通说话人沿用配置。
            if speaker in top_speakers:
                quota_count = max(self.samples_per_speaker, self._TOP_SPEAKER_SAMPLE_COUNT)
                quota_chars = max(self.max_chars_per_speaker, self._TOP_SPEAKER_MAX_CHARS)
            else:
                quota_count = self.samples_per_speaker
                quota_chars = self.max_chars_per_speaker
            if len(candidates) <= quota_count:
                selected = candidates
                per_sample_limit = None
            else:
                # Spread the default K=3 samples over the full timeline so a
                # polluted tail remains visible to the inference prompt.  For
                # other K values, evenly spaced positions retain the same rule.
                last_index = len(candidates) - 1
                positions = [
                    round(index * last_index / (quota_count - 1))
                    for index in range(quota_count)
                ] if quota_count > 1 else [0]
                selected = [candidates[position] for position in positions]
                # Reserve an equal share of the per-speaker budget for each
                # stratified sample so a long head/middle sample cannot crowd
                # out the tail.  K=0 keeps the existing empty-selection path.
                per_sample_limit = (
                    min(
                        self._MAX_CHARS_PER_SAMPLE,
                        quota_chars // quota_count,
                    )
                    if quota_count > 0
                    else None
                )
            for text in selected:
                if per_sample_limit is not None:
                    text = self._truncate(text, per_sample_limit)
                self._try_add_sample(
                    speaker,
                    text,
                    own_samples,
                    own_chars,
                    sample_count_limit=quota_count,
                    total_chars_limit=quota_chars,
                )

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

    def _top_sample_speakers(self, candidate_dialogs: Dict[str, List[str]]) -> set:
        """按发言条数选出采样加额的说话人集合（发言量 top2）。

        排序键：发言条数降序，并列按首次出现顺序（candidate_dialogs 的键
        按 speakers 列表顺序初始化，dict 插入序即首次出现序，sorted 稳定
        排序保持之）——不用 speakers.index(s) 当 key，那是每个元素 O(n)
        查找，说话人上千时整个排序退化成 O(n²)（
        test_extract_sample_dialogs_stays_fast_with_many_speakers 锁住）。
        发言条数取 candidate_dialogs（含后续会被分层截断的全部有效发言），
        与采样上限无关——加额针对的是「话说得多的人」。
        """
        ranked = sorted(
            candidate_dialogs.keys(),
            key=lambda s: -len(candidate_dialogs.get(s) or []),
        )
        return set(ranked[: self._TOP_SPEAKER_QUOTA_SLOTS])

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

    def _collect_call_response_evidence(
        self, dialogs: List[Dict], sample_groups: Dict[str, Dict]
    ) -> Dict[str, List[Dict]]:
        """收集「他人呼叫、紧邻本人应答」的第三人提及旁证。

        生产案例：主持人 Speaker1 从不自称、称呼（「胖哥」）全部出现在他人
        段落且本人采样覆盖不到——只有旁证能让这类称呼进入模型输入。

        对每个被采样的说话人 S（sample_groups 的 key；被全局预算裁掉的
        说话人不进 prompt，也不收集旁证），扫描全时间轴：

        - 段 P 是他人段落且文本含疑似称呼 token（_MENTION_TOKEN_PATTERN，
          启发式白名单；不按元数据人名索引——发现机制就是呼叫-回应模式
          本身）
        - P 之后 _CALL_RESPONSE_WINDOW 段窗口内，首个发言段的说话人恰为
          S（「呼叫后紧邻本人应答」）。刻意收紧为「首个应答者必须是被
          采样人」，而非「窗口内任意位置出现 S 发言」：宽松版会让同一条
          呼叫句同时归属窗口内多个说话人（如呼叫 A 后 B 插话），旁证
          条目可归属多人时「呼叫-回应核对」的程序证明力塌陷，卡面锁定
          测试「呼叫后非本人应答不成立」也无法程序化。
        - 每说话人至多 _MAX_CORROBORATION_PER_SPEAKER 条，全局至多
          _MAX_CORROBORATION_ENTRIES_TOTAL 条（按呼叫发生时间先到先得），
          单条截断到 _MAX_CHARS_PER_CORROBORATION_ENTRY 字符。

        Returns:
            {speaker: [{"speaker": 他人标签, "text": 定稿短句,
                        "index": 段号, "time": HH:MM:SS 或 None}]}
            只含有旁证条目的说话人。
        """
        speakers = list(sample_groups.keys())
        if not speakers or not dialogs:
            return {}

        speaker_set = set(speakers)
        entries: Dict[str, List[Dict]] = {speaker: [] for speaker in speakers}
        total = 0
        for idx, dialog in enumerate(dialogs):
            if total >= self._MAX_CORROBORATION_ENTRIES_TOTAL:
                break
            call_speaker = dialog.get("speaker", "")
            call_text = (dialog.get("text") or "").strip()
            if not call_text or not call_speaker:
                continue
            # 窗口内首个发言段即应答者（空文本段跳过：coerce 后不存在，
            # 容忍直接喂原始形态输入的调用方）。
            responder = None
            window_end = min(idx + 1 + self._CALL_RESPONSE_WINDOW, len(dialogs))
            for j in range(idx + 1, window_end):
                if (dialogs[j].get("text") or "").strip():
                    responder = dialogs[j].get("speaker", "")
                    break
            # 应答者不是被采样说话人（含呼叫者自答）：这条呼叫不构成任何
            # 被采样说话人的旁证——「呼叫后非本人应答不成立」。
            if not responder or responder not in speaker_set or responder == call_speaker:
                continue
            if not self._MENTION_TOKEN_PATTERN.search(call_text):
                continue
            if len(entries[responder]) >= self._MAX_CORROBORATION_PER_SPEAKER:
                continue
            entries[responder].append(
                {
                    "speaker": str(call_speaker),
                    "text": self._truncate(
                        call_text, self._MAX_CHARS_PER_CORROBORATION_ENTRY
                    ),
                    "index": idx,
                    "time": self._format_timestamp(dialog.get("start_time")),
                }
            )
            total += 1

        return {speaker: items for speaker, items in entries.items() if items}

    def _format_corroboration_section(self, corroboration: Dict[str, List[Dict]]) -> str:
        """渲染第三人提及旁证区（独立区，不混入任何说话人的本人分组）。

        每条标注实际发言者、段号与时间，并标注程序已核实的呼叫对象；
        说话人按其首条旁证的段号排序。无任何旁证时返回空串（整区不出现，
        prompt 与无旁证的旧输出逐字一致）。
        """
        if not corroboration:
            return ""
        ordered = sorted(
            corroboration.items(),
            key=lambda kv: min(entry["index"] for entry in kv[1]),
        )
        lines = [
            "**第三人提及旁证**（程序已核实：以下每条他人发言的紧邻回应者就是所标注的呼叫对象；"
            "这些句子只能作为其呼叫对象的 dialog_address 证据引用来源，程序会记录来源说话人与段号）："
        ]
        for speaker, speaker_entries in ordered:
            for entry in speaker_entries:
                time_tag = f"，{entry['time']}" if entry.get("time") else ""
                lines.append(
                    f"- 段{entry['index']}{time_tag} [{entry['speaker']}]: {entry['text']}"
                    f" → 呼叫对象：{speaker}"
                )
        return "\n".join(lines)

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
        sample_count_limit: Optional[int] = None,
        total_chars_limit: Optional[int] = None,
    ) -> None:
        """尝试为说话人追加一条采样文本，受条数与总字符双重上限约束

        上限缺省时用构造参数（samples_per_speaker/max_chars_per_speaker）；
        发言量 top2 的说话人由调用方传入加额后的上限（8 条/1200 字符）。
        """
        count_limit = (
            sample_count_limit if sample_count_limit is not None else self.samples_per_speaker
        )
        chars_limit = (
            total_chars_limit if total_chars_limit is not None else self.max_chars_per_speaker
        )
        if len(own_samples[speaker]) >= count_limit:
            return

        remaining = chars_limit - own_chars[speaker]
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
    # review（第5轮）指出，不确定的时候不要强行套用推断结果，缺失/无法解析
    # 恰恰是最不确定的情况，若默认按满分采信等于反向违背这个功能的初衷。
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

    # ------------------------------------------------------------------
    # 证据裁决（schema v2，card-vta-261015-speaker-evidence）
    # ------------------------------------------------------------------

    @staticmethod
    def _is_evidence_result(result: Dict) -> bool:
        """判定推断结果是否为 schema v2（证据裁决）产物。

        判据：meta 任一条目含 "candidates" 键。v2 写侧唯一来源
        （_apply_evidence_gate）对每个已采样条目都会写入 candidates
        （无候选时是空列表）；v1 产物（_apply_confidence_gate 及更早）
        从不写这个键。缓存重放据此分叉：v2 直接采用，v1 走旧 confidence
        门控重判。
        """
        meta = result.get("meta")
        if not isinstance(meta, dict):
            return False
        return any(
            isinstance(entry, dict) and "candidates" in entry
            for entry in meta.values()
        )

    @staticmethod
    def _verify_candidates(
        candidates,
        quote_sections: Optional[Dict[str, str]] = None,
        corroboration_entries: Optional[List[Dict]] = None,
        *,
        dialog_sources: Dict[str, str],
    ) -> Tuple[List[Dict], List[str], List[Dict]]:
        """核对引用与名字绑定；本人样本 dialog_address 放入 pending 待复核。"""
        verified: List[Dict] = []
        rejected: List[str] = []
        pending: List[Dict] = []
        sections = quote_sections or {}
        context_before = dialog_sources.get("context_before", "")
        own_samples = dialog_sources.get("own_samples", "")
        if not isinstance(candidates, list):
            return verified, rejected, pending
        for cand in candidates:
            if not isinstance(cand, dict):
                rejected.append("invalid_candidate")
                continue
            name = cand.get("name")
            evidence_type = cand.get("evidence_type")
            quote = cand.get("evidence_quote")
            if not isinstance(name, str) or not name.strip():
                rejected.append("invalid_candidate")
                continue
            if evidence_type not in SPEAKER_EVIDENCE_TYPES:
                rejected.append("unknown_evidence_type")
                continue
            if not isinstance(quote, str) or not quote:
                rejected.append("quote_missing")
                continue
            stripped = quote.strip()
            if len(stripped) < 2 or not any(ch.isalnum() for ch in stripped):
                rejected.append("quote_missing")
                continue
            corroboration_entry = next(
                (
                    entry
                    for entry in (corroboration_entries or [])
                    if evidence_type == "dialog_address"
                    and isinstance(entry, dict)
                    and quote in (entry.get("text") or "")
                ),
                None,
            )
            corroboration_meta: Optional[Dict] = None
            if evidence_type == "dialog_address":
                section_hit = quote in context_before or quote in own_samples
            else:
                section_hit = quote in sections.get(evidence_type, "")
            if not section_hit:
                if evidence_type != "dialog_address" or corroboration_entry is None:
                    rejected.append("quote_missing")
                    continue
                corroboration_meta = {
                    "source_speaker": str(corroboration_entry.get("speaker", "")),
                    "segment_index": corroboration_entry.get("index"),
                    "quote_time": corroboration_entry.get("time"),
                }
            bound_name = name.strip()
            if bound_name.casefold() not in quote.casefold():
                rejected.append("name_not_in_quote")
                continue
            if (
                evidence_type == "dialog_address"
                and quote in own_samples
                and quote not in context_before
                and corroboration_entry is None
            ):
                pending.append(
                    {
                        "name": bound_name,
                        "evidence_type": evidence_type,
                        "evidence_quote": quote,
                        "source": cand,
                    }
                )
                continue
            verified_candidate = {
                "name": bound_name,
                "evidence_type": evidence_type,
                "evidence_quote": quote,
            }
            if corroboration_meta is not None:
                verified_candidate["corroboration"] = corroboration_meta
            verified.append(verified_candidate)
        return verified, rejected, pending

    @staticmethod
    def _decide_display_name(
        verified: List[Dict],
        speaker: str,
        speakers: List[str],
    ) -> Tuple[Optional[str], Optional[str]]:
        """按确定性规则从核对通过的候选中裁决显示名。

        规则（卡面锁定决策 3 + gate 修复轮第 3 轮语义显式化）：
        - dialog_address + title_credit/author_host 双证据 → 带标注复合
          形式，三要素缺一不可：原始说话人编号（说话人N）、「推测」字样、
          两条来源名（对话称呼 + 标题/作者名），形如
          ``说话人2｜标题关联推测:魂蓝｜对话称:兰姐``。标题名与对话称呼
          是否同一人是模型推测，程序只能核实引用存在与分区归属，因此
          复合名必须就地自证推测属性——禁止输出裸的标题名/作者名。
        - 仅 dialog_address → 直接采用对话称呼原文（可证实事实，不标注）
        - 仅 title_credit/author_host → 拒绝（无对话佐证不得采用元数据名）
        - 仅 description_roster → 拒绝（名单≠发言人，永不独立生效）
        - 无核对通过候选 → 拒绝

        mapping 值是单一事实源：网页渲染、导出文本、LLM 后续输入全部
        自然继承该字符串，渲染层不得二次加工。
        """
        dialog = next(
            (c for c in verified if c["evidence_type"] == "dialog_address"), None
        )
        credit = next(
            (
                c
                for c in verified
                if c["evidence_type"] in ("title_credit", "author_host")
            ),
            None,
        )
        if dialog and credit:
            fallback_label = SpeakerInferencer._build_fallback_label(speaker, speakers)
            marker = (
                "标题关联推测"
                if credit["evidence_type"] == "title_credit"
                else "作者关联推测"
            )
            return (
                f"{fallback_label}｜{marker}:{credit['name']}｜对话称:{dialog['name']}",
                None,
            )
        if dialog:
            return dialog["name"], None
        if credit:
            return None, "title_credit_without_dialog_support"
        if any(c["evidence_type"] == "description_roster" for c in verified):
            return None, "description_roster_not_standalone"
        return None, "no_verified_evidence"

    def _apply_evidence_gate(
        self,
        speakers: List[str],
        raw_mapping: Dict[str, str],
        confidence_by_speaker: Dict[str, float],
        candidates_by_speaker: Dict,
        sampled: Optional[Dict[str, bool]] = None,
        quote_sections: Optional[Dict[str, str]] = None,
        *,
        dialog_sections_by_speaker: Dict[str, Dict[str, str]],
        corroboration_by_speaker: Optional[Dict[str, List[Dict]]] = None,
    ) -> Dict:
        """按核对通过的证据裁决显示名；confidence 只记录到 meta。

        本人样本 dialog_address 经 apply_self_intro_review 一次打包复核。
        """
        mapping: Dict[str, str] = {}
        meta: Dict[str, Dict] = {}
        low_confidence: List[str] = []
        prepared: List[Tuple] = []

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
                    "candidates": [],
                    "verified_candidates": [],
                    "rejection_reason": "not_sampled",
                }
                logger.info(
                    f"Speaker '{speaker}' was never sampled/sent to the LLM "
                    "(excluded by budget or no valid dialog), keeping original label"
                )
                continue

            confidence = confidence_by_speaker.get(
                speaker, self._UNRESOLVABLE_CONFIDENCE_DEFAULT
            )
            candidates = candidates_by_speaker.get(speaker)
            if not isinstance(candidates, list):
                candidates = []
            dialog_sources = dialog_sections_by_speaker.get(speaker, {})
            verified, rejected, pending = self._verify_candidates(
                candidates,
                quote_sections,
                corroboration_entries=(corroboration_by_speaker or {}).get(speaker),
                dialog_sources=dialog_sources,
            )
            prepared.append(
                (speaker, confidence, candidates, verified, rejected, pending)
            )

        apply_self_intro_review(
            self.llm_client, self.model, self.reasoning_effort, prepared
        )

        for speaker, confidence, candidates, verified, rejected, _pending in prepared:
            display_name, reason = self._decide_display_name(verified, speaker, speakers)
            inferred_name = raw_mapping.get(speaker, speaker)

            if display_name is None:
                if not verified and rejected and all(
                    r in ("quote_missing", "name_not_in_quote", "name_not_self_intro")
                    for r in rejected
                ):
                    reason = next(
                        r
                        for r in ("quote_missing", "name_not_in_quote", "name_not_self_intro")
                        if r in rejected
                    )
                fallback_label = self._build_fallback_label(speaker, speakers)
                mapping[speaker] = fallback_label
                low_confidence.append(speaker)
                meta[speaker] = {
                    "name": inferred_name,
                    "confidence": confidence,
                    "applied": False,
                    "sampled": True,
                    "candidates": candidates,
                    "verified_candidates": verified,
                    "rejection_reason": reason,
                }
                logger.warning(
                    f"Speaker '{speaker}' evidence gate rejected (reason={reason}), "
                    f"downgrading inferred name '{inferred_name}' -> '{fallback_label}'"
                )
            else:
                mapping[speaker] = display_name
                meta[speaker] = {
                    "name": inferred_name,
                    "confidence": confidence,
                    "applied": True,
                    "sampled": True,
                    "candidates": candidates,
                    "verified_candidates": verified,
                }

        return {"mapping": mapping, "meta": meta, "low_confidence": low_confidence}

    @staticmethod
    def _build_fallback_label(speaker: str, speakers: List[str]) -> str:
        """构造降级占位符「说话人N」/「Speaker N」

        N 优先取原始标签中的数字序号（如 Speaker3 -> 3）；
        标签中没有数字时，按其在 speakers 列表中的出现顺序编号（1 基）。
        输出语言跟随全局 ``language``：zh-CN 为 ``说话人N``，en 为 ``Speaker N``。
        """
        match = re.search(r"(\d+)", speaker)
        if match:
            n = match.group(1)
        else:
            n = str(speakers.index(speaker) + 1)
        from ...i18n import get_language

        if get_language() == "en":
            return f"Speaker {n}"
        return f"说话人{n}"

    def _identity_fallback(self, speakers: List[str]) -> Dict:
        """无法推断时的兜底：所有说话人使用原始标签，标记为未采信。

        meta 带 sampled=False；source 固定 identity_fallback，调用方不得拿它覆盖
        既有好名字。档位字段由 assign_confidence_levels 补齐（无证据，全落 none）。
        """
        result = {
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
        return assign_confidence_levels(result)

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
