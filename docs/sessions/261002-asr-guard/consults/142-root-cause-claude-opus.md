核对完了：消费链和全仓的长度上限扫描都已做完，下面是报告。

先说限制：我这次只能读文件、做 grep，范围限定在 `/tmp/consult-repo`，不能执行命令。所以第二节的复现命令（`uv run …`）、归档分支 `d8c22e67` 以及主干 commit 号我都**没能实际跑或核对**。下文标「独立核实」的结论都来自读源码；标「对方提供」的是我没复核的。

---

## 1. 前提核实

| # | 结论 | 依据 |
|---|---|---|
| 1 复现 1 段 800 字 | 读代码后认为可信，但没实际运行 | 独立核实（静态）：`capswriter_client.py:285-286` 原样返回，`:470-473` 只打 info 级统计日志 |
| 2 病根 A | **成立** | 独立核实：`capswriter_client.py:285` `if len(parts) <= 1: return [segment]`，这条分支不打日志 |
| 3 病根 B | **成立** | 独立核实：`capswriter_client.py:179` `primary_punct = "。！？!?"`，不含 ASCII `.` |
| 4 执法点表 | 列出的 3 处属实，但**表不完整**（见第 4 节）。规则 5 的有效上限确实是 `2*hard_max`（`paragraphize.py:244`） | 独立核实 |
| 4 默认值不一致 | 属实，但**不是同一类问题**。漂移不止这一处，整个 `LLMConfig` 都有：`enable_threshold` 5000 vs 20000，`max_chunk_length` 1500 vs 3000，`preferred` 800 vs 2000，`min_chunk_length` 300 vs 800，`calibration_concurrent_limit` 3 vs 10（`config.py:63-80` 对 `config.example.jsonc:279-304`）。这是「dataclass 默认值与示例配置漂移」，只在配置文件删掉某个键时才生效，跟「上限可以被放弃」无关 | 独立核实 |
| **5 两层叠加，最终进 LLM prompt** | **需要修正** | 独立核实，见下 |

**第 5 条的修正**：`paragraphize` 跑在 LLM 校准**之后**（`speaker_aware_processor.py:277-280`），输入是校准后的 dialogs，不是 capswriter 段的原始输出。它的输出去了矛盾扫描、章节生成和渲染（`:287` 之后；`config.example.jsonc:329-330`），**并不进入校准 prompt**。

capswriter 段真正到达 LLM 的入口是 **`DialogSegmenter`** 分出的校准 chunk：plain 模式 `max_chunk_length=4000`（`speaker_aware_processor.py:247-252`，`config.py:120`）。所以「600 和 300 连续两道都没兜住」这个说法的链条不对。实际链条是：300（可放弃）→ 4000（**也可放弃**，见第 4 节）→ 校准 LLM → 600 / 1200（按设计就不改文本）→ 下游 LLM。

---

## 2. 裁定

### Q1：硬契约还是软目标？→ `max_len=300` 是**质量启发式**（独立核实）

沿消费链逐层看：
- sidecar 段 → plain `base_dialogs`，一条一条规范化、不合并（`:502-509`）→ `DialogSegmenter` 打包成 ≤4000 的 chunk → 按 id 锚点逐条校准。
- 一条 2999 字的段放进 4000 的 chunk 完全没问题，**下游没有任何地方需要 ≤300**。

300 只影响三件事，都是质量问题：
- 章节锚点的粒度
- 段内插值出来的时间精度
- 校准时单条输出的长度

即使超长段把校准撑爆（截断或超时），结果也只是 chunk 被 fallback 回原文，并计入 `fallback_count`，状态变成 partial（`:786-833`、`:985-1027`）。这是**可观测的质量降级**，不会丢数据，也不会崩溃。

本仓里**唯一接近硬需求的是校准 chunk 上限**（单次 LLM 调用的输入和输出体积）。它的执法点是 `DialogSegmenter`，不是 capswriter。

「注释写硬上限、实现却可放弃」该按**文档 / 命名缺陷**处理，不算正确性缺陷：
- `paragraphize.py:7-11` 的模块文档本来就写着「长度只是预算不是闸刀」。
- 配置注释 `config.example.jsonc:337` 里那句「超出放宽到逗号级断点」也描述了这种放宽。
- 真正误导人的只有「硬上限」这三个字。

### Q2：方案 1 还是方案 2？→ **都不选原样。选「方案 1 加一个点」**（独立核实）

方案 2 有两个前提是错的：

1. **「`paragraphize` 不再承担切段职责」**：它本来就不切段。「只选边界、不改文本」是它的核心契约（`paragraphize.py:3-5`）。规则 6 是这个契约的直接推论，不是漏洞。真把它改硬，就得在成员内部切文本，等于破坏契约，还会和 `original_text` / duration 的拼接逻辑冲突。
2. **「唯一执法点」**：段构造（capswriter）和 LLM 分块（`DialogSegmenter`）是两个不同的生产者，上限也不同（300 是质量，4000 是调用体积）。has_speaker 路径的 FunASR 段根本不经过 capswriter（见第 4 节）。把它们合成一个执法点在结构上做不到。

所以正确的边界是：**在每个需要上限的生产者那里，各自做到不可放弃**。这样的点只有两个，第 3 节详述。新增的结构化校验 / 降级框架不要做。

### Q3：「静默放弃」的正确形态 → **分层**（独立核实 + 判断）

判据：越过上限的后果是什么？
- **只是质量下降**（capswriter 的 300、`DialogSegmenter` 的 4000）：在最近的空白或任意字符处**硬切，并打 warning**。硬切不丢字（拼接后仍等于原文），时间沿用现有的插值。用 fail fast 不对：会把一次质量问题升级成整个任务失败，这才是越过 personal 档的红线（崩溃）。
- **会丢数据或产生静默错误**（比如「拼接 != 原文」、时间倒挂）：**fail fast**。capswriter 已经有这样的守卫：`:604-608` 对空段 raise，`_validate_capswriter_contract` 负责契约校验。
- **按设计就不执法**（paragraphize 的 600 / 1200）：保持现状和 warning，把文案改成「预算 / 软上限」。
- 「计数告警」不用新建机制：校准层已经有 `fallback_count` 和 `calibration_status`，硬切那里打 warning 就够了。

### Q4：病根 B 要不要同一批修？→ **同意不修**，但理由要补一条（独立核实）

整个仓库对「句末」有 **4 套互不一致的定义**：

| 位置 | 句末字符集 |
|---|---|
| `capswriter_client.py:179` | `。！？!?` |
| `paragraphize.py:21` | `。！？….!?` |
| `dialog_segmenter.py:297` | 只有 `。！？`，**连 `!?` 都没有** |
| `text_segmenter.py:81` | `。！？!?\.…，,；;：:\n` |

单独给 capswriter 加 `.`，会遇到小数、缩写、URL 被误切的问题；而且 `DialogSegmenter` 仍然不认英文句点，等于没修。这件事应该单独立一个 issue：「统一句末定义」，需要先明确产品期望。本批次让硬切兜底来覆盖英文场景就够了。

---

## 3. 落点（最小）

**① `capswriter_client._split_long_segment`**（#142 本体）
- 逗号切完后，对仍然超过 `max_len` 的片段，优先在空白处切，找不到就按 `max_len` 硬切。
- 时间继续走现有的 `interpolate_segment_times`。
- 硬切时打 warning，写明段长和切出的片数。
- 可观察判据：
  - 输入 `'词'*800`、`max_len=300` → 每段 ≤300，`''.join == body`，`start ≤ end`。
  - 现有 `tests/unit/test_cache_timeline_wiring.py:350-482` 里的非有限时间和倒挂用例全部仍然通过。

**② `DialogSegmenter._split_long_dialog`**（`dialog_segmenter.py:110-147`）
- 我认为**这个点比 ① 更重要**。
- 句子切完后，对仍然超过 `max_chunk_length` 的片段做同样的空白优先 / 硬切兜底，并打 warning。
- 判据：任何输入下每个 chunk 都 ≤ `max_chunk_length`，英文无句号的长文本同样满足。
- 注意 `:132,141` 的 `.strip()` 会丢掉片段之间的空白，是否顺手修请自己定。补测试时应断言拼接后等于原文。

**③ 文案**（不改代码行为）
- `config.example.jsonc:337` 和 `config.py:123` 的「硬上限」改成「软上限：成员内不切，单成员超限会自成一段，组长 ≤2×」。

**明确不做：**
- 不改 `paragraphize` 的规则 5 和规则 6。
- 不新建统一校验 / 降级框架。
- 不在本批对齐 `LLMConfig` 默认值（另开 issue，它是整个 dataclass 层面的漂移）。
- 不修病根 B。
- 不动 `TextSegmenter`（它已经是硬切）。

部署方面：①② 只影响新任务的分段和分块，对已经落盘的产物没有影响。在 n305 上要不要更新，由你自己判断。

---

## 4. 风险与盲点

1. **你漏掉了最可能在真实场景触发的执法点**（独立核实）：
   - has_speaker 路径的 `_normalize_and_merge_dialogs`（`speaker_aware_processor.py:522-523`）会**无上限地**把连续同一说话人的 dialog 拼成一条。
   - 单人播客或独白就会合并成一条很长的 dialog。
   - 然后它进 `_split_long_dialog`，而那里只认 `。！？`。所以**英文 FunASR 独白会被整条塞进一个 chunk 交给 LLM**，`max_chunk_length=3000` 完全失效。
   - 这比 capswriter 遇到无标点输入常见得多。落点 ② 正好覆盖它。合并步骤本身要不要加上限，可以留到以后再说。
2. **方案 1 只修 ① 的话，校准侧的放弃路径仍然留着**。#142 修完以后，「上限可被放弃」这一类问题在主干里并没有消失。
3. **`TextSegmenter._segment_by_sentences`（`text_segmenter.py:81-89`）是另一类问题**（独立核实）：它把所有逗号、冒号、换行都替换成「。」，还会 strip。这属于**改写正文**，不是长度问题。它只在 `structured_calibration_for_plain=false` 的旧路径上生效。建议单独记一笔，不要混进本批。
4. **全仓扫描里其余的上限都正常**，无需处理：
   - `max_chapters_input_chars`（超限时 fail fast，`config.py:108-110`）
   - 说话人采样的 `max_chars_per_speaker` / `max_total_sample_chars`（截断的是采样，不是正文）
   - `_truncate_description` 和各处 `[:500]`（只作用于描述和日志）
   - 下载体积上限（`base.py:239`，超限时中止）

   这些上限要么是 fail fast，要么截断的只是非正文的辅助内容。
5. **未知项**：
   - 归档分支的内容和主干 commit 号都没核对。
   - 「硬切会不会让校准 LLM 在切口处补字或改字」没有实证。id 锚点逐条校准理论上应该能挡住，但需要拿一个真实样本验证。