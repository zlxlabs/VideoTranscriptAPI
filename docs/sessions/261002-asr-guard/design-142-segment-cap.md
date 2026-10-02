# DESIGN-note：段长上限的根治边界（#142 的根因收束）

配套 issue #142。顾问报告原文归档在**私有中心仓 agent-config** 的 `retro/consult/20261002-191833-claude-opus-09e2bb.md`——本仓是公开仓，`.gitignore:139-141` 明确「顾问咨询记录不随仓分发（含 payload/report 的本机绝对路径）」，所以本仓只留这份方案对齐单，不留 payload 与报告原文。

## 目标

把「段长上限」在本仓变成**在其余的层不可放弃**，消掉「声明了上限、切不开就放弃」这一类缺陷；同时把一处**声明与实现不一致**的文案对齐。

## 我给顾问的两条错前提（顾问纠正后已独立复核）

1. **错**：我原以为 `paragraphize` 的 `hard_max_chars=600` 是 capswriter 段之后的第一道防线，段长 300 逃逸后会被 600 兜住。**实际**：`speaker_aware_processor.py:247-285` 的顺序是 `DialogSegmenter.segment()` → `_calibrate_chunks()` → `_paragraphize_no_speaker_dialogs()`，即 `paragraphize` 跑在**校准之后**，其输出不进任何 prompt。所以段真正到 LLM 的入口是 `DialogSegmenter` 的 chunk（plain 路径 `max_chunk_length` 覆盖值），不是 600。
2. **错**：我把 `paragraphize` 规则 5/6 的放弃路径当成同类缺陷。**实际**：`paragraphize.py:1-11` 的模块文档明写「**只选边界、不改文本**」与「**长度只是预算不是闸刀**」，规则 6「单成员超限自成一段」是该契约的直接推论。误导人的只有配置注释里的「硬上限」三个字。

顾问另补一个我漏掉的执法点（已实测复现，见下）：`DialogSegmenter._split_long_dialog` 同样在切不开时放弃，且它的输入是**无上限合并**的同说话人 dialog（`speaker_aware_processor.py:522-523`），单人独白场景比 capswriter 更容易触发。

## 机理（两条并行的链，都已实测）

链 1（转录段）：`_split_text_by_punctuation` 的 `primary_punct = "。！？!?"` 不含 ASCII `.` → 英文整篇不分句；`_split_long_segment` 只按 `，,；;` 切，`len(parts) <= 1` 时 `return [segment]` 且**不打日志**。实测 `min_len=80, max_len=300` 下 `'词'*800` → **1 段 800 字**（函数自己打 `0/1 在目标范围内`）。

链 2（LLM 分块）：`_split_by_sentences` = `re.split(r'([。！？])', text)`（连 `!?` 都没有）→ 英文长段是一个「句」；`_split_long_dialog` 把超长「句」整条塞进一个子 dialog，无兜底、`.strip()` 还会丢片段间空白。实测 `max_chunk_length=1500`（dataclass 默认）下 2159 字无逗号英文独白 → **1 个 2159 字 chunk**；同长中文有句号的对照组 → 正常切成 ≤1400。

## 根治的边界：按生产者收束，不建统一框架

「上限」在本仓有两类消费者，语义不同，**结构上无法合成一个执法点**（顾问裁定，我采纳）：

| 生产者 | 上限 | 语义 | 判级 |
|---|---|---|---|
| `capswriter_client._create_segments_from_capswriter` | `max_len=300` | **质量启发式**（章节锚点粒度、段内插值时间精度、校准单条输出长度）。下游无任何地方需要 ≤300 | 硬切兜底 + warning；**不** fail fast |
| `DialogSegmenter` | `max_chunk_length`（plain 路径覆盖值 / dataclass 默认 1500） | **接近硬需求**（单次 LLM 调用的输入体积） | 硬切兜底 + warning |
| `paragraphize` | `hard_max_chars=600` | **按设计的预算**，模块文档已声明不改文本 | 不改行为，只改文案 |
| `TextSegmenter._append_fragment` | `max_segment_size` | 已是硬切（`take = min(len(fragment), available)`） | 不动 |

判级依据（personal 档红线：数据丢失 / 静默出错 / 崩溃）：越过这两个上限的后果是**质量下降**，不是丢数据也不是崩溃——校准层已有 `fallback_count` / `calibration_status` 把它变成可观测降级。因此用**硬切 + warning**，不用 fail fast（fail fast 会把一次质量问题升级成整任务失败，那才越过本档红线）。硬切不丢字（拼接后与原文一致，`.strip()` 造成的差异按「去空白后一致」判），时间沿用现有插值器。

## 方案要点与已否决方案

- **要点**：
  1. `_split_long_segment`：逗号切完后对仍超 `max_len` 的片段，**优先空白切、无空白按 `max_len` 硬切**，并打 warning（写明原段长与切出片数）。时间继续走现有插值。
  2. `_split_long_dialog`：同样形态（空白优先 / 硬切兜底 + warning），保证**任何输入下每个 chunk ≤ `max_chunk_length`**，英文无句号长文本同样满足。
  3. 文案对齐：`config.example.jsonc:337` 与 `llm/core/config.py:123` 的「硬上限」改成「软上限：成员内不切，单成员超限会自成一段，组长 ≤2×」，与 `paragraphize` 模块文档一致。
- **已否决**（接手人不得重新提起）：
  - 建「统一校验 / 降级框架」或「唯一执法点」：顾问指出两个前提不成立——`paragraphize` 的契约就是不改文本（改硬就破坏契约并与 `original_text`/duration 拼接冲突），而段构造与 LLM 分块是两个不同生产者、上限语义不同。**判据：越过上限的后果只是质量下降时，用「在该生产者内不可放弃」代替「新框架」**。
  - 新建「计数告警」机制：校准层已有 `fallback_count` / `calibration_status`，硬切处打 warning 足够。
  - 单独给 capswriter 加 ASCII `.` 到 `primary_punct`（病根 B 单点修）：小数/缩写/URL 会被误切，且 `DialogSegmenter` 仍不认英文句点，等于没修。**另开 issue「统一句末定义」**（本仓现有 4 套互不一致的定义，见下），需先定产品期望。
  - 顺手把 `LLMConfig` 的 dataclass 默认值与示例配置对齐：漂移遍及 `enable_threshold` / `max_chunk_length` / `preferred` / `min_chunk_length` / `calibration_concurrent_limit` 等，是 dataclass 层面的独立问题，**另开 issue**，不在本批。
  - 改 `TextSegmenter._segment_by_sentences`（它把逗号/冒号/换行替换成「。」并 strip，属**改写正文**而非长度问题，只在 `structured_calibration_for_plain=false` 的旧路径生效）：另开 issue。
  - 合并步骤（`_normalize_and_merge_dialogs`）的同说话人合并加长度上限：合并策略是产品决策（单人独白本来就该是一条），不在本批。

## 关键不变式

1. [实测] 转录段：任意输入下 `_create_segments_from_capswriter` 的每个段 `len(text) ≤ max_len`，且 `"".join(段文本) == 原文`，且 `start ≤ end`（不倒挂）。锁死它的测试：新增的硬切兜底用例（`max_len=300` + 无标点 800/2999 字输入）。
2. [实测] LLM 分块：任意输入下每个 chunk 文本长度 `≤ max_chunk_length`（`cap=1500` + 2159 字无逗号英文独白这条当前失败，修后必须绿），且片段拼接后与原文一致（允许 `.strip()` 造成的首尾空白差异，按去空白后一致判）。
3. [实测] 既有降级语义不变：NaN/Inf/None → `None`、`end < start` → `None`、重复时间戳不丢文本（`tests/unit/test_cache_timeline_wiring.py:350-482` 的 6 条 + `tests/unit/test_capswriter_contract.py` 的坐标/段尾/不丢文本三条原样绿）。
4. [实测] 句末定义在本仓有 4 套互不一致的实现，本批**明确不统一**，但卡面必须把这条写进代码注释指向 issue，避免后人误以为已统一：
   - `capswriter_client.py:179` `。！？!?`
   - `paragraphize.py:21` `。！？….!?`
   - `dialog_segmenter.py:297` `。！？`（无 `!?`）
   - `text_segmenter.py:81` `。！？!?\.…，,；;：:\n`

## 待验证前提

1. [推断] 硬切在切口处不破坏下游：`DialogSegmenter` 产出的 dialog 带 `id`，校准按 id 锚点逐条回填（`speaker_aware_processor.py` 的 `_calibrate_chunks`），理论上硬切产生的碎片不会让 LLM 补字/改字。**未实证**——需要拿一个真实样本跑一次校准路径，比对「校准后 id 集合 == 校准前 id 集合」。若实测发现 LLM 会跨碎片改字，改为「空白优先 + 仍超限时 warning 不硬切」并在 issue 里记降级理由。
2. [推断] 触发频率：中文语音几乎总有逗号，链 1 触发率低；链 2 在**英文或无标点 ASR + 单人独白**场景触发率不低，**未在生产真实转录上量过**。

## 验收路径

1. 入口：本地 `make test`（`pytest -q tests`，`tests/manual/` 默认不收）。
2. 步骤：
   - 链 1 复现脚本（当前输出 `1 800`，修后必须每段 ≤300 且正文完整）：
     ```sh
     PYTHONPATH=src uv run --frozen python -c "
     from video_transcript_api.transcriber.capswriter_client import _create_segments_from_capswriter as f
     b='词'*800
     segs=f(tokens=list(b),timestamps=[i*0.1 for i in range(len(b))],min_len=80,max_len=300)
     print(len(segs), max(len(s['text']) for s in segs), ''.join(s['text'] for s in segs)==b)"
     ```
   - 链 2 复现脚本（当前输出超上限 `True`，修后必须 `False`）：
     ```sh
     PYTHONPATH=src uv run --frozen python -c "
     from video_transcript_api.llm.core.config import LLMConfig
     from video_transcript_api.llm.segmenters.dialog_segmenter import DialogSegmenter
     cfg=LLMConfig(api_key='x',base_url='http://127.0.0.1:1',calibrate_model='m',summary_model='m')
     b=' '.join(['this is a spoken monologue sentence without any comma']*40)
     s=DialogSegmenter(cfg,preferred_chunk_length=800,max_chunk_length=1500)
     for c in s.segment([{'id':'1','speaker_id':'A','text':b,'start_time':0.0,'end_time':600.0,'duration':600.0}]):
         n=sum(len(d.get('text','')) for d in c); print(n, n<=1500)"
     ```
   - 两条链各做一次**反向红验**：把新增的硬切兜底去掉 → 对应用例必须转红（红的是断言失败，不是 ImportError/AttributeError）。
   - `make test` 退出 0。
3. 预期：两条链的复现脚本都从「超上限」变为「不超上限 + 正文完整 + 时间不倒挂」，既有降级用例全绿。
