# 咨询：段长上限在本仓是「软目标」而非不变式，#142 只是这个类问题的一处；请裁根治边界

仓库：`<repo 根>`（只读挂载；绝对路径按公开仓扫描要求在此脱敏）。风险等级 `risk-tier: personal`（CLAUDE.md 首行）。
主干：`2df7f50b`。本仓未接自动部署，生产在 n305 跑旧镜像，改动需不需要部署我这边自己判断。

我需要你**独立核实下面每条前提**（命令都可复现），不要因为我下了结论就顺着点头。然后回答最后一节的四个裁定点。

---

## 一、要解决的问题（一句话）

声明的「段长上限」在主干里没有任何一处是不可放弃的：4 个分段/分段化执法点各有自己的「切不开就放弃」分支，合起来的语义是**没有上限**。已实测复现：3000 字符的无标点输入产出 1 个 2999 字符的段，`max_len=300` 与 `hard_max_chars=600` 双双失效。issue #142 只记了其中最显眼的一处（`capswriter_client._split_long_segment`），我怀疑它不是根因。

## 二、已核实的事实（每条附复核命令）

### 1. 复现：段长上限被放弃

```sh
cd <repo 根>
PYTHONPATH=src uv run --frozen python -c "
from video_transcript_api.transcriber.capswriter_client import _create_segments_from_capswriter as f
b='词'*800
segs=f(tokens=list(b),timestamps=[i*0.1 for i in range(len(b))],min_len=80,max_len=300)
print(len(segs), max(len(s['text']) for s in segs), ''.join(s['text'] for s in segs)==b)"
```

实测输出 `1 800 True`：1 段、800 字、正文完整。函数自己的日志写着
`Segments 生成完成: 1 个片段, 0/1 在目标范围内`（`capswriter_client.py:471`）——即代码自己知道超范围，仍然输出。

### 2. 病根 A：切不开就原样返回，且**无日志**

`src/video_transcript_api/transcriber/capswriter_client.py:275-287`：

```python
def _split_long_segment(segment, max_len):
    """在次级标点处分割超长句子。"""
    secondary_punct = r"([，,；;])"
    parts = re.split(secondary_punct, text)
    if len(parts) <= 1:
        return [segment]          # 原样返回，不受 max_len 约束，且不打日志
```

### 3. 病根 B：分句不认英文句点，放大 A

`capswriter_client.py:174-179`：`primary_punct = "。！？!?"` —— **不含 ASCII `.`**。
所以英文转录整篇不分句（实测英文无逗号 2999 字 → 1 段 2999 字），唯一还能起作用的切分器就是病根 A 的逗号切分。

### 4. 同类扫描：4 个执法点，3 个可放弃

| 执法点 | 声明的上限 | 放弃路径 | 有无日志 | 复核命令 |
|---|---|---|---|---|
| `capswriter_client._split_long_segment` | `max_len=300`（生产调用点 `capswriter_client.py:601-602` 传 `min_len=80, max_len=300`） | `if len(parts) <= 1: return [segment]` | **无** | 见上 |
| `paragraphize.规则 6` | `hard_max_chars=600`，配置注释写「**硬上限**（字符）」 | 单成员超限 → `kept as its own paragraph` | warning | `sed -n '205,225p' src/video_transcript_api/transcriber/paragraphize.py` |
| `paragraphize.规则 5` | 同上 | 无任何授权点 → `force break at segment boundary`，**有效上限变成 `2 * hard_max_chars`** | warning | `sed -n '237,262p' src/video_transcript_api/transcriber/paragraphize.py` |
| `llm/segmenters/text_segmenter._append_fragment` | `max_segment_size`（配置 12000） | **无放弃路径**（`take = min(len(fragment), available)` 硬切） | — | `sed -n '96,120p' src/video_transcript_api/llm/segmenters/text_segmenter.py` |

配置侧声明（`config/config.example.jsonc`）另有 `max_segment_size: 12000`、`hard_max_chars: 600`、`max_chars_per_speaker: 400`；`llm/core/config.py:65` 的 `max_segment_size` 默认值是 3000，与配置示例的 12000 **不一致**（`config.py:260` 用 `segmentation_config.get("max_segment_size", 3000)`）。请你核实这个不一致，以及它是否属于同一类问题。

### 5. 两个层级叠加：#142 的产物直接喂给 规则 6

`paragraphize_segments` 的输入就是 `_create_segments_from_capswriter` 的输出（`llm/processors/speaker_aware_processor.py:1259-1264`）。所以一个 2999 字符的段会先在 A 处逃逸，再在 规则 6 处被「kept as its own paragraph」放行，配置声明的 600 与 300 连续两道都没兜住，最终原样进 LLM prompt。

## 三、我的两个候选根治方案（我不确定选哪个，请你裁）

**方案 1（窄）**：只在 `_split_long_segment` 加硬切兜底（次级标点切不开时按 `max_len` 硬切），并补锁测试。改动 1 个生产函数 + 1 个测试文件。风险：另外 3 个执法点仍然是软的，同类问题换个入口复发。

**方案 2（宽，结构收束）**：把「段长上限」变成**终态产物的可验证不变式**，而不是每个切分器的企图：
1. 段构造的最后一步做**不可放弃**的长度执法（含硬切兜底），`paragraphize` 不再承担切段职责；
2. 段构造返回前跑一次结构化校验（每段 ≤ 上限、正文拼接 == 原文、时间不倒挂），不满足走**显式降级 + 计数告警**（或 fail fast，取决于下面的裁定），把「静默放弃」变成可观测欠账；
3. `hard_max_chars` 的语义与实现对齐（要么真硬，要么把配置注释改成「合并目标」）；`max_segment_size` 的默认值与配置示例对齐。

我倾向方案 2，理由是：声明的上限只有在**唯一执法点 + 不可放弃**时才是上限；现状 N 个各自为政的 best-effort 切分器，合起来等于没有上限。但我担心方案 2 触发「保护机制规模接近被保护对象」——它新增的是校验与降级路径，不是减法。

## 四、请你裁的四点

1. **段长上限在本仓是硬契约还是软目标？** 请沿消费者链核实再答：`_create_segments_from_capswriter` 的 `max_len=300` 有没有真正的下游硬需求（LLM 侧 `hard_max_chars=600`、`max_segment_size=12000/3000`、各处 truncate），还是说它本来就只是质量启发式？如果是启发式，「配置注释写硬上限、实现却可放弃」这件事本身该按什么级别处理？
2. **根治边界选方案 1 还是方案 2？** 如果选 2，请指出最小可行的落点（改哪几个文件、哪个位置承担唯一执法点），以及哪些部分应该明确**不做**（防范围膨胀）。
3. **「静默放弃」的正确形态是什么？** 硬切 / 结构化降级+计数告警 / fail fast 三选一，或分层（软目标降级、硬契约 fail fast）。请给出在 personal 档（红线是数据丢失、静默出错、崩溃）下的判据。
4. **英文不分句（病根 B）该不该同一批修？** 我的判断是不该（会改变整篇英文转录的分句行为，影响面远大于段长兜底，且需要产品期望：英文到底该不该按句号分句）。请你独立判断，如果该修，给出最小形态与它会连带改变什么。

## 五、我已经否决的（不要重新提起，除非你给出我漏掉的反证）

- 直接 cherry-pick 一条已归档分支里的旧实现：归档 `refs/reclaimed/20261002/card/VideoTranscriptAPI-261001-contract`（commit `d8c22e67 修主审三条：删除危险的跨文件回滚、补硬切兜底、临时文件就地清理`）里就有「补硬切兜底」。但那一版 API 是 `(text, tokens, timestamps)`，主干现在是 `(tokens, timestamps, min_len, max_len)`，且 `Config.max_segment_length` 在主干已不存在，cherry-pick 必冲突并会把废弃的 `text=` 路径带回来。
- 引入 rapidfuzz/edlib 之类的对齐依赖：本问题与文本对齐无关（主干 `grep -E "difflib|SequenceMatcher|rapidfuzz|Levenshtein" src/` 零命中）。
- 把 3 条已归档的锚点对齐分支（`token_timeline.py` + 854~931 行测试）搬回主干：那是 issue #109 重新收束时刻意不走的路线。

## 六、输出要求

按下面四段给，每条结论标注它依据的是「你独立核实到的」还是「我提供的」：

1. **前提核实**：我上面 6 条事实里，哪几条你复核后不成立或需要修正（给命令与实测输出）。
2. **裁定**：第四节点 1~4 的答案，每条给判据。
3. **落点**：你认可的最小落点（文件 + 函数 + 每处要满足的可观察判据），以及明确不做的部分。
4. **风险与盲点**：我这个方案里最可能错的地方，以及有没有我完全没看到的同类执法点（请你自己扫一遍全仓的长度/体积/数量上限声明，不要只看我列的 4 个）。
